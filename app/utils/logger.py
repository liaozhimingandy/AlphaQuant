#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : logger.py
# @Description : 全局日志：绝对路径 + 环境变量控制级别 + 可重复初始化
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import sys

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


def setup_logging(level: str | None = None, force: bool = False) -> None:
    """初始化日志。

    :param level: 日志级别，None 表示使用 settings.LOG_LEVEL
    :param force: 强制重新初始化（一般不需要）
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings.ensure_dirs()
    level = (level or settings.LOG_LEVEL).upper()

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format=_CONSOLE_FORMAT,
        backtrace=True,
        diagnose=False,
    )
    logger.add(
        (settings.LOG_DIR / "alphaquant_{time:YYYY-MM-DD}.log").as_posix(),
        rotation="00:00",
        retention=settings.LOG_RETENTION,
        level=level,
        encoding="utf-8",
        enqueue=True,
        format=_FILE_FORMAT,
        backtrace=True,
        diagnose=False,
    )
    _CONFIGURED = True


setup_logging()


if __name__ == "__main__":
    logger.info("logger 自检完成，日志目录: {}", settings.LOG_DIR)
