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
from enum import Enum
from typing import Optional, Callable

from twisted.internet import defer

from app.core.engine.event import EventBus
from app.core.engine.settings import EngineContext
from app.utils.logger import logger

# ======================
# 1. 组件状态枚举
# ======================
class ComponentState(Enum):
    UNINITIALIZED = "UNINITIALIZED"  # 未初始化
    INITIALIZED = "INITIALIZED"      # 已初始化
    STARTING = "STARTING"            # 启动中
    RUNNING = "RUNNING"              # 运行中
    STOPPING = "STOPPING"            # 停止中
    STOPPED = "STOPPED"              # 已停止
    ERROR = "ERROR"                  # 异常状态


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

        self.component_config: dict = {}  # 组件独立配置

        # 状态管理
        self._component_status = ComponentState.UNINITIALIZED

        self._subscribed_events: list[tuple[str, Callable]] = []

    def initialize(self, context: EngineContext, event_bus: EventBus, component_config: dict = None) -> None:

        """
        组件初始化，整个生命周期仅执行1次
        用途：加载配置、初始化连接、订阅事件
        """
        # 1. 状态防护：仅未初始化可执行
        if self._component_status != ComponentState.UNINITIALIZED:
            logger.warning(f"组件 {self.name} 重复初始化，已跳过")
            return

        # 2. 强制校验：子类必须定义name
        if not self.name or not isinstance(self.name, str):
            raise ValueError(f"组件 {self.__class__.__name__} 必须定义类属性 name")

        self.context = context
        self.event_bus = event_bus
        self.component_config = component_config or {}

        # 初始化
        try:
            # 4. 子类初始化钩子
            self.on_initialize()
            self._component_status = ComponentState.INITIALIZED
            logger.info(f"✅组件 {self.name} 初始化完成")
        except Exception as e:
            self._component_status = ComponentState.ERROR
            logger.error(f"❌组件 {self.name} 初始化失败", exc_info=True)
            raise

    @defer.inlineCallbacks
    def start(self) -> defer.Deferred:
        """组件启动时执行"""
        # 状态防护
        if not self.enabled:
            logger.info(f"⏹️组件 {self.name} 已禁用，跳过启动")
            return
        if self._component_status not in [ComponentState.INITIALIZED, ComponentState.STOPPED]:
            logger.warning(f"组件 {self.name} 状态异常，无法启动：{self._component_status.value}")
            return

        self._component_status = ComponentState.STARTING
        logger.info(f"🚀 启动组件 {self.name}")

        try:
            # 子类启动钩子（异步）
            yield self.on_start()
            self._component_status = ComponentState.RUNNING
            logger.info(f"✅组件 {self.name} 运行中")
        except Exception as e:
            self._component_status = ComponentState.ERROR
            logger.error(f"❌组件 {self.name} 启动失败", exc_info=True)
            raise

    @defer.inlineCallbacks
    def stop(self, graceful=True) -> defer.Deferred:
        """组件停止时执行"""
        # 状态防护
        if self._component_status in [ComponentState.STOPPING, ComponentState.STOPPED]:
            return

        self._state = ComponentState.STOPPING
        logger.info(f"🛑停止组件 {self.name} | 优雅模式: {graceful}")

        try:
            # 1. 子类优雅停止钩子
            yield self.on_stop(graceful)
            # 2. 自动清理所有事件订阅（核心：杜绝僵尸回调）
            self._clear_subscriptions()
            self._component_status = ComponentState.STOPPED
            logger.info(f"✅组件 {self.name} 已停止")
        except Exception as e:
            self._component_status = ComponentState.ERROR
            logger.error(f"❌组件 {self.name} 停止异常", exc_info=True)
            raise

    @abstractmethod
    def on_initialize(self) -> None:
        """子类初始化钩子（可选实现）"""
        pass

    @abstractmethod
    def on_start(self) -> defer.Deferred:
        """子类启动钩子（可选实现，支持异步）"""
        return defer.succeed(None)

    @abstractmethod
    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        """子类停止钩子（可选实现，支持异步）"""
        return defer.succeed(None)

    def health_check(self) -> tuple[bool, str]:
        """健康检查，引擎定时调用，返回True表示健康"""
        if not self.enabled:
            return True, "disabled"
        return self._component_status in [ComponentState.RUNNING], self._component_status.value

    def subscribe_event(self, event_name: str, handler: Callable) -> None:
        """统一事件订阅，自动注册+自动清理"""
        if not self.event_bus:
            raise RuntimeError(f"组件 {self.name} 未初始化，无法订阅事件")

        self.event_bus.subscribe(event_name, handler)
        self._subscribed_events.append((event_name, handler))
        logger.debug(f"📩 组件 {self.name} 订阅事件: {event_name}")

    def _clear_subscriptions(self) -> None:
        """组件停止时自动清理所有订阅的事件"""
        if not self.event_bus:
            return

        for event_name, handler in self._subscribed_events:
            try:
                self.event_bus.unsubscribe(event_name, handler)
            except Exception as e:
                logger.debug(f"取消订阅事件失败 {event_name}: {str(e)}")

        self._subscribed_events.clear()

    @property
    def state(self) -> str:
        """获取组件状态（只读）"""
        return self._component_status.value

    @property
    def is_running(self) -> bool:
        """组件是否运行中"""
        return self._component_status == ComponentState.RUNNING

