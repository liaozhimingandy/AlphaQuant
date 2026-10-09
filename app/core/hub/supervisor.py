#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : supervisor.py
# @Description : 多服务编排：按 services.json 拉起/监控/重启 N 个独立引擎进程
#               进程隔离是刻意的——一个策略服务写崩了，不该带走行情服务和别的策略
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.engine import daemon
from app.core.hub.spec import HubSpec, ServiceSpec
from app.utils.jsonio import read_json, write_json_atomic
from app.utils.logger import logger


class ServiceSupervisor:
    """多进程服务编排器。

    协作模型：
      - 每个服务是**独立进程**（独立崩溃域、独立面板端口、独立快照目录）
      - 服务之间通过文件总线（bar/news 主题）交换数据 —— 行情服务推、策略服务收
      - 本类只负责编排与看护，不参与业务；业务在各自进程里
    """

    def __init__(self, hub: HubSpec, python: Optional[str] = None, autostart: bool = False) -> None:
        self.hub = hub
        self.python = python or sys.executable
        self.autostart = bool(autostart)
        self._restarts: Dict[str, int] = {}
        self.started_at = datetime.now().isoformat()
        self.state_file = self.hub.state_root / "hub.json"
        self.commands_dir = self.hub.state_root / "_hub_commands"

    # ============================================================
    # 路径
    # ============================================================
    def service_dir(self, service_id: str) -> Path:
        return self.hub.state_root / service_id

    def paths(self, service_id: str) -> Dict[str, Path]:
        d = self.service_dir(service_id)
        return {
            "dir": d,
            "pid_file": d / "service.pid",
            "stop_file": d / "service.stop",
            "log_file": d / "service.log",
            "state_file": d / "state.json",
            "snapshots": d / "snapshots",
        }

    def _cmd(self, spec: ServiceSpec) -> List[str]:
        return [
            self.python,
            "-m",
            "app.core.hub.worker",
            "--service",
            spec.id,
            "--services-config",
            self.hub.source,
        ]

    # ============================================================
    # 生命周期
    # ============================================================
    def start_service(self, service_id: str) -> Dict[str, Any]:
        spec = self.hub.get(service_id)
        if spec is None:
            return {"ok": False, "error": f"未知服务: {service_id}"}
        p = self.paths(service_id)
        result = daemon.start(
            self._cmd(spec),
            pid_file=p["pid_file"],
            log_file=p["log_file"],
            stop_file=p["stop_file"],
            cwd=settings.BASE_DIR,
            extra={"service_id": spec.id, "role": spec.role, "port": spec.port},
            env=spec.env or None,
        )
        if result.get("ok"):
            logger.info(f"服务 {service_id} 已启动 | pid={result.get('pid')}")
        else:
            logger.error(f"服务 {service_id} 启动失败: {result.get('error')}")
        result["service_id"] = service_id
        return result

    def stop_service(self, service_id: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        spec = self.hub.get(service_id)
        if spec is None:
            return {"ok": False, "error": f"未知服务: {service_id}"}
        p = self.paths(service_id)
        result = daemon.stop(p["pid_file"], p["stop_file"], timeout=timeout)
        result["service_id"] = service_id
        logger.info(f"服务 {service_id} 停止结果: {result.get('message')}")
        return result

    def restart_service(self, service_id: str) -> Dict[str, Any]:
        self.restart_only_stop(service_id)
        self._restarts[service_id] = self._restarts.get(service_id, 0) + 1
        return self.start_service(service_id)

    def restart_only_stop(self, service_id: str) -> Dict[str, Any]:
        p = self.paths(service_id)
        return daemon.stop(p["pid_file"], p["stop_file"])

    def start_all(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for spec in self.hub.enabled():
            out[spec.id] = self.start_service(spec.id)
        return out

    def stop_all(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for spec in self.hub.services:
            out[spec.id] = self.stop_service(spec.id)
        return out

    # ============================================================
    # 看护
    # ============================================================
    def _read_state(self, service_id: str) -> Optional[Dict[str, Any]]:
        data = read_json(self.paths(service_id)["state_file"])
        return data if isinstance(data, dict) else None

    def _heartbeat_age(self, state: Optional[Dict[str, Any]]) -> Optional[float]:
        if not state:
            return None
        raw = state.get("heartbeat_at")
        if not raw:
            return None
        try:
            return max(0.0, (datetime.now() - datetime.fromisoformat(raw)).total_seconds())
        except Exception:
            return None

    def service_status(self, service_id: str) -> Dict[str, Any]:
        spec = self.hub.get(service_id)
        p = self.paths(service_id)
        pid_info = daemon.read_pid(p["pid_file"]) or {}
        pid = pid_info.get("pid")
        alive = daemon.is_alive(pid)
        state = self._read_state(service_id)
        age = self._heartbeat_age(state)
        timeout = self.hub.heartbeat_timeout or settings.SERVICE_HEARTBEAT_TIMEOUT

        healthy = bool(alive and age is not None and age <= timeout)
        if not spec:
            health = "unknown"
        elif not alive:
            health = "stopped"
        elif not healthy:
            health = "stale"
        else:
            health = "healthy"

        return {
            "service_id": service_id,
            "role": spec.role if spec else "",
            "enabled": bool(spec.enabled) if spec else False,
            "pid": pid,
            "alive": bool(alive),
            "health": health,
            "heartbeat_age": round(age, 1) if age is not None else None,
            "heartbeat_timeout": timeout,
            "restarts": self._restarts.get(service_id, 0),
            "panel_url": (state or {}).get("panel_url") or (
                f"http://{spec.host or settings.MONITOR_HOST}:{spec.port}/"
                if spec and spec.monitor and spec.port
                else ""
            ),
            "state": state,
            "spec": spec.to_dict() if spec else None,
            "log_file": str(p["log_file"]),
        }

    def _exited_cleanly(self, service_id: str) -> bool:
        """判断上次退出是"正常结束"还是"崩溃"。

        只靠 PID 存活与否无法区分：回测服务跑完会自动退出。
        判断依据是 final state 里有没有 ``stopped_at``（正常退出才会写）。
        """
        state = self._read_state(service_id)
        return bool(state and state.get("stopped_at"))

    def poll(self) -> bool:
        """看护循环：刷新 hub 状态 + 按需重启失联服务。"""
        for spec in self.hub.enabled():
            try:
                st = self.service_status(spec.id)
            except Exception as exc:
                logger.debug(f"服务 {spec.id} 状态检查异常: {exc}")
                continue
            if st["alive"] or not spec.autorestart:
                continue
            if self._exited_cleanly(spec.id):
                # 正常结束（例如回测回放完毕），不要重启
                continue
            used = self._restarts.get(spec.id, 0)
            if used >= spec.max_restarts:
                logger.warning(f"服务 {spec.id} 重启次数已达上限({spec.max_restarts})，不再自动重启")
                continue
            logger.warning(f"服务 {spec.id} 已失联，自动重启（第 {used + 1} 次）")
            self._restarts[spec.id] = used + 1
            try:
                self.start_service(spec.id)
            except Exception as exc:
                logger.error(f"服务 {spec.id} 自动重启失败: {exc}")
        self.write_state()
        return True

    def write_state(self) -> Path:
        payload = {
            "hub": self.hub.to_dict(),
            "started_at": self.started_at,
            "updated_at": datetime.now().isoformat(),
            "restarts": dict(self._restarts),
            "services": [self.service_status(s.id) for s in self.hub.services],
        }
        return write_json_atomic(self.state_file, payload, indent=None)

    def status(self, with_state: bool = True) -> Dict[str, Any]:
        items = []
        for spec in self.hub.services:
            st = self.service_status(spec.id)
            if not with_state:
                st.pop("state", None)
            items.append(st)
        return {
            "hub": self.hub.to_dict(),
            "restarts": dict(self._restarts),
            "services": items,
        }

    # ============================================================
    # 远程指令（写给 worker 的命令文件）
    # ============================================================
    def send_command(self, service_id: str, command: Dict[str, Any]) -> Dict[str, Any]:
        """把指令写进服务的 commands 目录，由该服务在自己的 reactor 线程执行。

        为什么不直接改内存：服务是独立进程，跨进程"直接调用"不存在。
        命令文件是最简单可靠的方式，而且天然留下审计痕迹。
        """
        spec = self.hub.get(service_id)
        if spec is None:
            return {"ok": False, "error": f"未知服务: {service_id}"}
        st = self.service_status(service_id)
        if not st["alive"]:
            return {"ok": False, "error": f"服务 {service_id} 未运行"}

        cmd_dir = self.service_dir(service_id) / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}.json"
        path = write_json_atomic(cmd_dir / name, command, indent=2)
        logger.info(f"指令已投递给 {service_id}: {command} → {path.name}")
        return {"ok": True, "service_id": service_id, "command_file": str(path), "command": command}

    def list_commands(self, service_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        from app.utils.jsonio import read_jsonl

        done = self.service_dir(service_id) / "commands" / "done"
        if not done.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for p in sorted(done.glob("*.json"))[-limit:]:
            data = read_json(p)
            if isinstance(data, dict):
                rows.append(data)
        return list(reversed(rows))

    def clear_stop_flags(self) -> None:
        for spec in self.hub.services:
            p = self.paths(spec.id)
            try:
                p["stop_file"].unlink()
            except OSError:
                pass


__all__ = ["ServiceSupervisor"]
