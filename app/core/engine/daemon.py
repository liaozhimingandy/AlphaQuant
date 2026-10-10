#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : daemon.py
# @Description : 后台守护：把服务真正放到后台跑，并提供 start/stop/status/restart
#               停止机制刻意用"停止标志文件"而不是信号：
#               Windows 没有 SIGTERM，靠信号停服务在 Windows 上根本不可靠
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import settings
from app.utils.jsonio import read_json, write_json_atomic
from app.utils.logger import logger

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore


# ============================================================
# PID 文件
# ============================================================
def read_pid(pid_file: Optional[str | Path] = None) -> Optional[Dict[str, Any]]:
    data = read_json(pid_file or settings.PID_FILE)
    return data if isinstance(data, dict) and data.get("pid") else None


def write_pid(payload: Dict[str, Any], pid_file: Optional[str | Path] = None) -> Path:
    return write_json_atomic(pid_file or settings.PID_FILE, payload, indent=2)


def clear_pid(pid_file: Optional[str | Path] = None) -> None:
    p = Path(pid_file or settings.PID_FILE)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug(f"清理 PID 文件失败: {exc}")


def is_alive(pid: Optional[int]) -> bool:
    """进程是否存活。优先 psutil，退化到 os.kill 探测。"""
    if not pid or pid <= 0:
        return False
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(int(pid)))
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


def process_info(pid: int) -> Dict[str, Any]:
    if psutil is None:
        return {"pid": pid}
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            mem = p.memory_info()
            return {
                "pid": pid,
                "name": p.name(),
                "cmdline": " ".join(p.cmdline()),
                "create_time": datetime.fromtimestamp(p.create_time()).isoformat(),
                "cpu_percent": p.cpu_percent(interval=0.0),
                "rss_mb": round(mem.rss / 1024 / 1024, 2),
                "num_threads": p.num_threads(),
                "status": p.status(),
            }
    except Exception as exc:
        return {"pid": pid, "error": str(exc)}


# ============================================================
# 启动
# ============================================================
def _popen_kwargs() -> Dict[str, Any]:
    """跨平台"脱离终端"参数。

    Windows: DETACHED_PROCESS 让子进程不带控制台，关掉终端也不受影响；
    POSIX:   start_new_session 让它成为新会话首进程，脱离父进程的进程组。
    """
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            flags |= int(getattr(subprocess, name, 0))
        return {"creationflags": flags}
    return {"start_new_session": True}


def start(
    cmd: Sequence[str],
    pid_file: Optional[str | Path] = None,
    log_file: Optional[str | Path] = None,
    stop_file: Optional[str | Path] = None,
    cwd: Optional[str | Path] = None,
    extra: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """把命令放到后台运行。返回 ``{"ok", "pid", "log_file", ...}``。"""
    settings.ensure_dirs()
    pid_file = Path(pid_file or settings.PID_FILE)
    log_file = Path(log_file or settings.DAEMON_LOG)
    stop_file = Path(stop_file or settings.STOP_FILE)

    existing = read_pid(pid_file)
    if existing and is_alive(existing.get("pid")):
        return {
            "ok": False,
            "error": f"服务已在运行 (pid={existing['pid']})，请先 stop 或使用 restart",
            "pid": existing["pid"],
        }

    # 清掉上一轮残留
    if stop_file.exists():
        try:
            stop_file.unlink()
        except OSError:
            pass
    clear_pid(pid_file)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    run_env = dict(os.environ)
    run_env.setdefault("PYTHONUNBUFFERED", "1")
    if env:
        run_env.update(env)

    handle = log_file.open("a", encoding="utf-8")
    header = f"\n{'=' * 70}\n[{datetime.now().isoformat()}] 启动: {' '.join(map(str, cmd))}\n{'=' * 70}\n"
    handle.write(header)
    handle.flush()

    try:
        proc = subprocess.Popen(
            list(map(str, cmd)),
            cwd=str(cwd or settings.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=run_env,
            close_fds=True,
            **_popen_kwargs(),
        )
    except Exception as exc:
        handle.close()
        return {"ok": False, "error": f"启动失败: {exc}"}
    finally:
        try:
            handle.close()
        except Exception:
            pass

    payload: Dict[str, Any] = {
        "pid": proc.pid,
        "ppid": os.getpid(),
        "started_at": datetime.now().isoformat(),
        "argv": list(map(str, cmd)),
        "cwd": str(cwd or settings.BASE_DIR),
        "log_file": str(log_file),
        "stop_file": str(stop_file),
    }
    payload.update(extra or {})
    write_pid(payload, pid_file)

    # 给一点时间确认没有秒退（参数写错时 child 会立刻退出）
    # 注意：Windows 上 venv 的 python.exe 是个 launcher 壳，它会再拉起真正的解释器，
    # 所以 Popen 返回的 pid 未必是"持有引擎的那个进程"。子进程启动后会把自己的
    # os.getpid() 写进同一个 PID 文件，这里以子进程自报的 pid 为准。
    time.sleep(0.8)
    self_reported = read_pid(pid_file) or {}
    real_pid = int(self_reported.get("pid") or proc.pid)
    alive = is_alive(real_pid)
    if alive and real_pid != proc.pid:
        # 记录真实的引擎进程 pid，避免 status/stop 打到 launcher 壳上
        self_reported["pid"] = real_pid
        self_reported["launcher_pid"] = proc.pid
        write_pid(self_reported, pid_file)

    return {
        "ok": alive,
        "pid": real_pid,
        "launcher_pid": proc.pid,
        "log_file": str(log_file),
        "pid_file": str(pid_file),
        "error": "" if alive else f"进程启动后立即退出，请查看日志: {log_file}",
    }


# ============================================================
# 停止
# ============================================================
def stop(
    pid_file: Optional[str | Path] = None,
    stop_file: Optional[str | Path] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """优雅停止后台服务。

    顺序：写停止标志 → 等服务自己退出 → 超时则 terminate → 再超时则 kill。
    先优雅后强制，是因为"杀掉"会让最后一次快照和持仓状态丢掉。
    """
    pid_file = Path(pid_file or settings.PID_FILE)
    stop_file = Path(stop_file or settings.STOP_FILE)
    timeout = float(timeout if timeout is not None else settings.DAEMON_STOP_TIMEOUT)

    info = read_pid(pid_file)
    if not info:
        # 没有 PID 文件，但仍可能残留停止标志
        if stop_file.exists():
            try:
                stop_file.unlink()
            except OSError:
                pass
        return {"ok": True, "stopped": False, "message": "没有运行中的服务（PID 文件不存在）"}

    pid = int(info["pid"])
    if not is_alive(pid):
        clear_pid(pid_file)
        return {"ok": True, "stopped": False, "message": f"进程 {pid} 已不存在，清理 PID 文件"}

    stop_file.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        stop_file,
        {"requested_at": datetime.now().isoformat(), "reason": "daemon.stop", "pid": pid},
        indent=None,
    )
    logger.info(f"已写入停止标志，等待进程 {pid} 优雅退出（最多 {timeout:.0f}s）")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            break
        time.sleep(0.3)

    forced = ""
    if is_alive(pid) and psutil is not None:
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=5)
                forced = "terminate"
            except Exception:
                p.kill()
                forced = "kill"
        except Exception as exc:
            forced = f"强制结束失败: {exc}"

    still = is_alive(pid)
    if not still:
        clear_pid(pid_file)
        # 停止标志会被下次启动清掉；这里顺手删掉更干净
        try:
            stop_file.unlink()
        except OSError:
            pass

    return {
        "ok": not still,
        "stopped": not still,
        "pid": pid,
        "forced": forced,
        "message": ("已退出" if not still else f"进程 {pid} 仍未退出"),
    }


# ============================================================
# 状态
# ============================================================
def status(pid_file: Optional[str | Path] = None) -> Dict[str, Any]:
    pid_file = Path(pid_file or settings.PID_FILE)
    info = read_pid(pid_file)
    if not info:
        return {"running": False, "message": "未运行"}
    pid = int(info["pid"])
    if not is_alive(pid):
        return {
            "running": False,
            "stale": True,
            "pid": pid,
            "message": "PID 文件存在但进程已退出（可能是异常退出）",
            "info": info,
        }
    detail = process_info(pid)
    return {"running": True, "pid": pid, "info": info, "detail": detail, "message": "运行中"}


def restart(**kwargs: Any) -> Dict[str, Any]:
    """先停后起。``kwargs`` 与 :func:`start` 一致。"""
    stopped = stop(kwargs.get("pid_file"), kwargs.get("stop_file"))
    cmd = kwargs.pop("cmd", None)
    if not cmd:
        return {"ok": False, "error": "restart 需要提供 cmd"}
    result = start(cmd, **kwargs)
    result["stop_result"] = stopped
    return result


# ============================================================
# 服务入口命令拼装
# ============================================================
def service_cmd(
    config: str = "",
    mode: str = "",
    market_mode: str = "",
    interval: Optional[float] = None,
    start_date: str = "",
    end_date: str = "",
    data_source: str = "",
    symbols: Optional[List[str]] = None,
    host: str = "",
    port: Optional[int] = None,
    snapshot_interval: Optional[float] = None,
    namespace: str = "default",
    no_news: bool = False,
    no_monitor: bool = False,
    python: Optional[str] = None,
    no_collector: bool = False,
    collect_interval: str = "",
    collect_period: str = "",
    collect_symbols: Optional[List[str]] = None,
    collector_config: str = "",
    broker_gateway: str = "",
    broker_endpoint: str = "",
) -> List[str]:
    """拼出 ``python -m app.core.engine.service_runner ...`` 的完整命令。"""
    cmd: List[str] = [python or sys.executable, "-m", "app.core.engine.service_runner"]
    if config:
        cmd += ["--config", str(config)]
    if mode:
        cmd += ["--mode", str(mode)]
    if market_mode:
        cmd += ["--market-mode", str(market_mode)]
    if interval is not None:
        cmd += ["--interval", str(interval)]
    if start_date:
        cmd += ["--start", str(start_date)]
    if end_date:
        cmd += ["--end", str(end_date)]
    if data_source:
        cmd += ["--data-source", str(data_source)]
    for s in symbols or []:
        cmd += ["--symbol", str(s)]
    if host:
        cmd += ["--host", str(host)]
    if port is not None:
        cmd += ["--port", str(port)]
    if snapshot_interval is not None:
        cmd += ["--snapshot-interval", str(snapshot_interval)]
    if namespace:
        cmd += ["--namespace", str(namespace)]
    if no_news:
        cmd += ["--no-news"]
    if no_monitor:
        cmd += ["--no-monitor"]
    if no_collector:
        cmd += ["--no-collector"]
    if collect_interval:
        cmd += ["--collect-interval", str(collect_interval)]
    if collect_period:
        cmd += ["--collect-period", str(collect_period)]
    for s in collect_symbols or []:
        cmd += ["--collect-symbol", str(s)]
    if collector_config:
        cmd += ["--collector-config", str(collector_config)]
    if broker_gateway:
        cmd += ["--broker-gateway", str(broker_gateway)]
    if broker_endpoint:
        cmd += ["--broker-endpoint", str(broker_endpoint)]
    return cmd


__all__ = [
    "read_pid",
    "write_pid",
    "clear_pid",
    "is_alive",
    "process_info",
    "start",
    "stop",
    "restart",
    "status",
    "service_cmd",
]
