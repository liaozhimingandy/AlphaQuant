#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : service.py
# @Description : 引擎服务封装（Twisted Service 规范）
#               CLI / PID 守护逻辑已统一到 app/cli.py，本文件只负责服务生命周期
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, Generator, Optional

from twisted.application import service
from twisted.internet import defer, reactor

from app.core.config import settings
from app.core.engine.engine import BaseQuantEngine
from app.core.engine.settings import EngineContext
from app.utils.logger import logger


class EngineService(service.Service):
    """把量化引擎包装成标准 Twisted Service，便于 twistd / Docker 部署。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.engine: Optional[BaseQuantEngine] = None
        self._is_stopping: bool = False
        self.CONFIG: Dict[str, Any] = config or {
            "RUN_MODE": "BACKTEST",
            "SYMBOL": "000001",
            "LOG_LEVEL": settings.LOG_LEVEL,
        }

    @defer.inlineCallbacks
    def startService(self) -> Generator[EngineContext, Any, None]:
        service.Service.startService(self)
        logger.info("=" * 60)
        logger.info("启动 AlphaQuantEngine 服务")
        logger.info("=" * 60)
        try:
            self.engine = BaseQuantEngine(self.CONFIG)
            yield self.engine.start()
            logger.info("服务运行结束")
        except Exception as exc:
            logger.critical(f"服务启动失败: {exc}", exc_info=True)
            if reactor.running:
                reactor.stop()
            raise

    @defer.inlineCallbacks
    def stopService(self) -> Generator[None, Any, None]:
        if self._is_stopping or not self.engine:
            return
        self._is_stopping = True
        logger.info("开始停止服务")
        try:
            d = defer.maybeDeferred(self.engine.stop, True)
            d.addTimeout(15, reactor)
            yield d
            logger.info("服务已停止")
        except defer.TimeoutError:
            logger.error("停止服务超时，强制结束")
        except Exception as exc:
            logger.error(f"停止服务异常: {exc}", exc_info=True)
        finally:
            service.Service.stopService(self)
