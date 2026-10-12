#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : logger.py
# @Description : 全局日志：分级落盘 + 重复节流 + 环境变量控制
#
#                日志策略（省磁盘是第一原则）：
#                  INFO（默认）—— 只记「重要信息」：生命周期、成交、风控否决、
#                                 采集结果、异常。这些是事后排查事故必须有的。
#                  DEBUG        —— 调试模式：逐根K线决策、指标缓存命中等全部记录。
#                                 **只在排查问题时开**，长跑会把磁盘写满。
#
#                另两条防膨胀措施：
#                  1. 文件级可以比控制台更严（控制台 DEBUG，文件只留 INFO）
#                  2. 同一位置的重复日志按时间窗节流，并在窗口结束时补一条
#                     "被压缩了多少次" —— 信息不丢，只是不重复
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from app.common.config import settings

# 防止重复 add（模块被多次导入 / 子进程 reload 时会产生重复日志）
_CONFIGURED = False

#: 每个 sink 的当前配置。热切换级别时要照原样重建（含 rotation/retention），
#: 否则改一次级别就把轮转策略丢了。
_SINKS: Dict[str, Dict[str, Any]] = {}
_SINK_IDS: Dict[str, int] = {}
_SINK_LOCK = threading.RLock()

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<magenta>{file}:{line}</magenta> | "
    "<yellow>{function}</yellow> | "
    "<cyan>{message}</cyan>"
)

_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {file}:{line} | {function} | {message}"

#: 合法级别，用于校验环境变量（loguru 的 level 参数要求大写）
_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")


def level_of(name: str, fallback: str = "INFO") -> str:
    n = str(name or "").strip().upper()
    return n if n in _LEVELS else fallback


def is_debug() -> bool:
    """调试模式：日志级别放到 DEBUG/TRACE。

    以**当前生效**的控制台级别判断（热切过级别就要按新的算），
    否则面板把级别调到 DEBUG 之后，日志节流还在按启动时判断继续压日志。
    """
    return current_levels().get("console", "INFO") in ("TRACE", "DEBUG")


# ===========================================================================
# 运行时级别覆盖
# ===========================================================================
# 级别原本只能靠环境变量在启动时定，排查线上问题时就得重启服务 ——
# 而重启恰恰会把你要排查的那个状态冲掉。这里让它可以在运行中改，
# 并写进 config/logging.json，重启后仍然生效（面板改一次 = 永久改）。
def logging_config_path() -> Path:
    return Path(settings.CONFIG_DIR) / "logging.json"


def load_level_config() -> Dict[str, Any]:
    """读持久化的级别覆盖。文件不存在/写错一律返回空 —— 配置问题不能让日志起不来。"""
    path = logging_config_path()
    try:
        if path.exists():
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return {}


def _persist_levels(console: str, file: str, throttle_window: Any) -> None:
    path = logging_config_path()
    payload = {
        "console": console,
        "file": file,
        "throttle_window": throttle_window,
        # 留个说明：这个文件是面板写的，手改也生效，但下次启动仍会被它覆盖
        "_note": "运行时日志配置（面板『运行时 → 日志等级』写入，启动时自动应用）",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # 写不了配置不该影响日志本身
        logger.warning(f"持久化日志级别失败（本次仍生效，重启后回退）: {exc}")


def setup_logging(level: str | None = None, force: bool = False) -> None:
    """初始化日志。

    :param level: 覆盖日志级别，None 表示使用 settings.LOG_LEVEL
    :param force: 强制重新初始化（一般不需要）

    优先级：显式参数 > ``config/logging.json``（面板改过的）> 环境变量。
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings.ensure_dirs()
    saved = load_level_config()

    console_level = level_of(
        level or saved.get("console") or settings.LOG_LEVEL
    )
    # 文件默认与级别一致；配了 LOG_FILE_LEVEL 就以它为准（可以比控制台更严）
    file_level = level_of(
        saved.get("file")
        or settings.LOG_FILE_LEVEL
        or console_level
    )
    if saved.get("throttle_window") is not None:
        try:
            settings.LOG_THROTTLE_WINDOW = float(saved["throttle_window"])
        except (TypeError, ValueError):
            pass

    with _SINK_LOCK:
        logger.remove()
        _SINK_IDS.clear()
        _SINKS.clear()
        _install("console", sys.stderr, level=console_level, **{
            "colorize": True,
            "format": _CONSOLE_FORMAT,
            "backtrace": True,
            "diagnose": False,
        })
        _install("file", (settings.LOG_DIR / "alphaquant_{time:YYYY-MM-DD}.log").as_posix(),
                 level=file_level, **{
                     "rotation": settings.LOG_ROTATION,
                     "retention": settings.LOG_RETENTION,
                     "encoding": "utf-8",
                     "enqueue": True,
                     "format": _FILE_FORMAT,
                     "backtrace": True,
                     "diagnose": False,
                 })
    _CONFIGURED = True
    logger.debug(
        f"日志已初始化 | 控制台={console_level} | 文件={file_level} | "
        f"目录={settings.LOG_DIR} | 节流窗口={settings.LOG_THROTTLE_WINDOW}s"
    )


def _install(name: str, sink: Any, *, level: str, **kwargs: Any) -> None:
    """装一个 sink 并记住它的完整配置（热切换级别时要照原样重建）。"""
    cfg = {"sink": sink, "level": level}
    cfg.update(kwargs)
    _SINKS[name] = cfg
    _SINK_IDS[name] = logger.add(sink, level=level, **kwargs)


def current_levels() -> Dict[str, Any]:
    """当前生效的日志配置（面板据此显示"我现在是什么档位"）。"""
    with _SINK_LOCK:
        console = _SINKS.get("console", {}).get("level") or level_of(settings.LOG_LEVEL)
        file = _SINKS.get("file", {}).get("level") or console
    return {
        "console": console,
        "file": file,
        "throttle_window": settings.LOG_THROTTLE_WINDOW,
        "retention": settings.LOG_RETENTION,
        "rotation": settings.LOG_ROTATION,
        "dir": str(settings.LOG_DIR),
        "levels": list(_LEVELS),
        "persisted": bool(load_level_config()),
        "config_path": str(logging_config_path()),
        "debug_mode": str(console).upper() in ("TRACE", "DEBUG"),
        "throttled": throttle_stats(),
    }


def set_level(
    console: Optional[str] = None,
    file: Optional[str] = None,
    throttle_window: Optional[float] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """运行中改日志级别（**不用重启服务**）。

    loguru 没有公开的"改已有 handler 级别"接口，所以做法是
    ``remove`` 掉这个 sink 再按原配置 ``add`` 回来 —— 配置里带着
    rotation/retention/format，重建不会打断轮转，只是换了个级别。

    :param console: 控制台级别，None 表示不动
    :param file: 文件级别，None 表示不动（可以是 "" 表示"跟随控制台"）
    :param throttle_window: 重复日志节流窗口（秒），None 表示不动
    :param persist: 是否写进 config/logging.json（重启后继续生效）
    :return: 改动后的完整配置，见 :func:`current_levels`
    """
    if not _CONFIGURED:
        setup_logging()

    # "" 是一级语义："文件级别跟随控制台"，不是"不改"。
    # 必须在循环前折算，否则 level_of("") 会当成非法值原样退回。
    if file == "":
        file = level_of(console) if console else _SINKS.get("console", {}).get("level")

    with _SINK_LOCK:
        for name, wanted in (("console", console), ("file", file)):
            cfg = _SINKS.get(name)
            if cfg is None or wanted is None:
                continue
            new_level = level_of(wanted, fallback=str(cfg.get("level") or "INFO"))
            if new_level == cfg.get("level"):
                continue
            old_id = _SINK_IDS.get(name)
            if old_id is not None:
                try:
                    logger.remove(old_id)
                except ValueError:
                    pass            # 已被移除过，忽略
            cfg = dict(cfg)
            cfg["level"] = new_level
            _SINKS[name] = cfg
            _SINK_IDS[name] = logger.add(
                cfg["sink"],
                level=new_level,
                **{k: v for k, v in cfg.items() if k not in ("sink", "level")},
            )

    if throttle_window is not None:
        try:
            settings.LOG_THROTTLE_WINDOW = max(0.0, float(throttle_window))
        except (TypeError, ValueError):
            pass

    result = current_levels()
    if persist:
        _persist_levels(result["console"], result["file"], result["throttle_window"])
    logger.info(
        f"日志级别已切换 | 控制台={result['console']} | 文件={result['file']} "
        f"| 节流窗口={result['throttle_window']}s"
    )
    return result


# ===========================================================================
# 重复日志节流
# ===========================================================================
# 长跑服务里最常见的问题是"同一句话刷满整个文件"：
# 某根K线风控连续否决、某个标的反复取数失败……这些信息第一次有价值，
# 第一百次只是噪音。
_THROTTLE_LOCK = threading.Lock()
_THROTTLE_STATE: Dict[str, Dict[str, Any]] = {}


def log_throttled(
    level: str,
    message: str,
    *,
    key: str = "",
    window: float = 0.0,
    **fmt: Any,
) -> bool:
    """按时间窗节流地记一条日志。

    :param message:
    :param level:
    :param key: 节流键，默认用消息本身
    :param window: 窗口秒数，0 表示取 settings.LOG_THROTTLE_WINDOW
    :return: True=真的写了；False=被节流吞掉
    """
    win = float(window or settings.LOG_THROTTLE_WINDOW or 0.0)
    lv = level_of(level)
    if win <= 0 or is_debug():
        # 调试模式下不节流——排查问题时就是要看全部
        logger.log(lv, message, **fmt)
        return True

    now = time.monotonic()
    bucket = key or message
    with _THROTTLE_LOCK:
        st = _THROTTLE_STATE.get(bucket)
        if st is None:
            _THROTTLE_STATE[bucket] = {"at": now, "skipped": 0}
            emit, skipped = True, 0
        elif now - st["at"] >= win:
            skipped = int(st["skipped"])
            st["at"] = now
            st["skipped"] = 0
            emit = True
        else:
            st["skipped"] = int(st["skipped"]) + 1
            emit, skipped = False, 0

    if not emit:
        return False

    if skipped:
        message = f"{message}  （同类消息在上一窗口内被压缩 {skipped} 次）"
    logger.log(lv, message, **fmt)
    return True


def throttle_stats() -> Dict[str, int]:
    """当前被节流的条目数与压缩量，供面板/自检确认节流没把重要信息吃掉。"""
    with _THROTTLE_LOCK:
        return {
            "buckets": len(_THROTTLE_STATE),
            "pending": sum(int(v.get("skipped", 0)) for v in _THROTTLE_STATE.values()),
        }


def reset_throttle() -> None:
    """仅供测试使用。"""
    with _THROTTLE_LOCK:
        _THROTTLE_STATE.clear()


setup_logging()


if __name__ == "__main__":
    logger.info("logger 自检完成，日志目录: {}", settings.LOG_DIR)
    logger.debug("这条只在 DEBUG 级别下出现")
