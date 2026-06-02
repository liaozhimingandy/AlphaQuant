#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/6/2 17:40
# @FileName    : ibase.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from abc import ABC, abstractmethod
from typing import Optional, Callable

from app.core.engine.event import EventBus
from app.core.engine.settings import EngineContext
from app.utils.logger import logger


class IBaseComponent(ABC):
    """
    所有业务组件的统一基类，你的架构中所有模块都必须实现这个接口
    包括：MarketCenter、StrategyManager、RiskManager、Portfolio、Execution、BrokerAdapter等
    Engine统一管理所有组件的生命周期，无差别调度
    """
    # 组件唯一名称，全局唯一
    name: str = None

    def __init__(self):
        self.enabled: bool = True
        # 由Engine自动注入，所有组件共享
        self.context: Optional[EngineContext] = None
        self.event_bus: Optional[EventBus] = None

        self._component_status = "UNINITIALIZED"
        self._subscribed_events: list[tuple[str, Callable]] = []

    def initialize(self, context: EngineContext, event_bus: EventBus) -> None:
        """
        组件初始化，整个生命周期仅执行1次
        用途：加载配置、初始化连接、订阅事件
        """
        self.context = context
        self.event_bus = event_bus

        # 初始化
        self.on_initialize()

        self._component_status = "INITIALIZED"

    @abstractmethod
    def on_initialize(self) -> None:
        """子类自定义初始化钩子，仅实现业务逻辑，不用管依赖注入"""
        # 用统一的事件订阅方法，自动管理生命周期
        raise NotImplementedError

    def start(self) -> None:
        """组件启动时执行"""
        self.on_start()
        self._component_status = "RUNNING"

    @abstractmethod
    def on_start(self) -> None:
        """组件启动时执行"""
        raise NotImplementedError

    def stop(self, graceful=True) -> None:
        """组件停止时执行"""
        logger.info(f"组件 {self.name} 已停止")
        # 自动清理所有订阅的事件，避免僵尸回调
        self._clear_subscriptions()
        self.on_stop(graceful=graceful)
        self._component_status = "STOPPED"

    @abstractmethod
    def on_stop(self, graceful: bool = True) -> None:
        """组件停止时执行"""
        raise NotImplementedError

    def health_check(self) -> bool:
        """健康检查，引擎定时调用，返回True表示健康"""
        return True

    def subscribe_event(self, event_name: str, handler: Callable) -> None:
        """组件统一事件订阅方法，自动管理生命周期"""
        self.event_bus.subscribe(event_name, handler)
        self._subscribed_events.append((event_name, handler))

    def _clear_subscriptions(self) -> None:
        """组件停止时自动清理所有订阅的事件"""
        for event_name, h in self._subscribed_events:
            self.event_bus.unsubscribe(event_name=event_name, receiver=h)
        self._subscribed_events.clear()

