#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 16:51
# @FileName    : component.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from abc import ABC, abstractmethod
from typing import Optional

from app.core.engine.event import EventBus
from app.core.engine.settings import EngineContext
from app.utils.logger import logger


# ------------------------------
# 3. 所有组件的统一基类
# ------------------------------
class BaseComponent(ABC):
    """
    所有业务组件的统一基类，你的架构中所有模块都必须实现这个接口
    包括：MarketCenter、StrategyManager、RiskManager、Portfolio、Execution、BrokerAdapter等
    Engine统一管理所有组件的生命周期，无差别调度
    """
    # 组件唯一名称，全局唯一
    name: str = None

    def __init__(self):
        self.logger = logger
        self.enabled: bool = True
        # 由Engine自动注入，所有组件共享
        self.context: Optional[EngineContext] = None
        self.event_bus: Optional[EventBus] = None

    @abstractmethod
    def initialize(self, context: EngineContext, event_bus: EventBus) -> None:
        """
        组件初始化，整个生命周期仅执行1次
        用途：加载配置、初始化连接、订阅事件
        """
        self.context = context
        self.event_bus = event_bus

    @abstractmethod
    def start(self) -> None:
        """组件启动，引擎启动时执行1次"""
        pass

    @abstractmethod
    def stop(self, graceful: bool = True) -> None:
        """组件停止，引擎停止时执行1次"""
        self.logger.info(f"组件 {self.name} 已停止")

    def health_check(self) -> bool:
        """健康检查，引擎定时调用，返回True表示健康"""
        return True
