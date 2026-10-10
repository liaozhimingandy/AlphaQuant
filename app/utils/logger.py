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

import sys
import threading
import time
from typing import Any, Dict

from loguru import logger

from app.core.config import settings

# 防止重复 add（模块被多次导入 / 子进程 reload 时会产生重复日志）
_CONFIGURED = False

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
    """调试模式：日志级别放到 DEBUG/TRACE。"""
    return level_of(settings.LOG_LEVEL) in ("TRACE", "DEBUG")


def setup_logging(level: str | None = None, force: bool = False) -> None:
    """初始化日志。

    :param level: 覆盖日志级别，None 表示使用 settings.LOG_LEVEL
    :param force: 强制重新初始化（一般不需要）
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings.ensure_dirs()
    console_level = level_of(level or settings.LOG_LEVEL)
    # 文件默认与级别一致；配了 LOG_FILE_LEVEL 就以它为准（可以比控制台更严）
    file_level = level_of(settings.LOG_FILE_LEVEL) if settings.LOG_FILE_LEVEL else console_level

    logger.remove()
    logger.add(
        sys.stderr,
        level=console_level,
        colorize=True,
        format=_CONSOLE_FORMAT,
        backtrace=True,
        diagnose=False,
    )
    logger.add(
        (settings.LOG_DIR / "alphaquant_{time:YYYY-MM-DD}.log").as_posix(),
        rotation=settings.LOG_ROTATION,
        retention=settings.LOG_RETENTION,
        level=file_level,
        encoding="utf-8",
        enqueue=True,
        format=_FILE_FORMAT,
        backtrace=True,
        diagnose=False,
    )
    _CONFIGURED = True
    logger.debug(
        f"日志已初始化 | 控制台={console_level} | 文件={file_level} | "
        f"目录={settings.LOG_DIR} | 节流窗口={settings.LOG_THROTTLE_WINDOW}s"
    )


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
