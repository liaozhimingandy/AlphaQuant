#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 16:46
# @FileName    : engine.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from abc import ABC, abstractmethod
import time
from typing import Dict, Any, Optional, List, Generator
import signal

from twisted.internet import reactor, defer
from twisted.internet.defer import Deferred

from app.core.engine.component import IBaseComponent, TaskSchedulerComponent
from app.core.engine.components import TimerComponent
from app.core.engine.event import EventBus, StandardEvents
from app.core.engine.settings import EngineContext, EngineStatus, RunMode
from app.core.engine.utils import async_sleep
from app.utils.logger import logger


class IQuantEngine(ABC):
    """
    量化引擎核心抽象接口，定义所有引擎的标准能力
    你的架构顶层Engine的唯一对外协议
    """

    # ------------------------------
    # 工厂方法（唯一推荐的初始化方式）
    # ------------------------------
    @classmethod
    @abstractmethod
    def create(cls, config: Dict[str, Any]) -> "IQuantEngine":
        """
        工厂方法：从配置创建引擎实例
        :param config: 全局配置字典
        :return: 引擎实例
        """
        raise NotImplementedError

    # ------------------------------
    # 生命周期核心接口
    # ------------------------------
    @abstractmethod
    def initialize(self) -> None:
        """引擎初始化时执行"""
        raise NotImplementedError

    @abstractmethod
    def start(self) -> EngineContext:
        """
        引擎统一启动入口，自动根据配置切换运行模式
        :return: 最终运行上下文
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self, graceful: bool = True) -> None:
        """
        停止引擎
        :param graceful: 是否优雅停止（处理完当前任务再退出）
        """
        raise NotImplementedError

    # ------------------------------
    # 组件管理接口
    # ------------------------------
    @abstractmethod
    def register_component(self, component: IBaseComponent) -> None:
        """
        注册业务组件到引擎
        支持注册你架构中的所有模块：
        MarketCenter、TaskScheduler、StrategyManager、RiskManager、Sizer、Portfolio、Execution、BrokerAdapter
        """
        raise NotImplementedError

    @abstractmethod
    def get_component(self, component_name: str) -> Optional[IBaseComponent]:
        """根据名称获取组件实例"""
        pass

    @abstractmethod
    def enable_component(self, component_name: str) -> None:
        """动态启用组件，无需重启引擎"""
        raise NotImplementedError

    @abstractmethod
    def disable_component(self, component_name: str) -> None:
        """动态禁用组件，无需重启引擎"""
        raise NotImplementedError

    # ------------------------------
    # 状态与管理接口
    # ------------------------------
    @abstractmethod
    def get_status(self) -> EngineStatus:
        """获取引擎当前运行状态"""
        raise NotImplementedError

    @abstractmethod
    def get_context(self) -> EngineContext:
        """获取引擎全局上下文"""
        raise NotImplementedError

    @abstractmethod
    def get_event_bus(self) -> EventBus:
        """获取全局事件总线"""
        raise NotImplementedError


class BaseQuantEngine(IQuantEngine):
    """
    标准量化引擎实现，对应你架构的顶层Engine
    100%匹配你的架构，基于Twisted异步，和Scrapy内核同构
    """

    def __init__(self, config: Dict[str, Any]):
        # 基础配置与状态
        self.config = config
        self.run_mode = RunMode(config.get("RUN_MODE", "BACKTEST"))
        self._status = EngineStatus.INITIALIZING
        self._stop_requested = False
        self._graceful_stop = True

        # 全局上下文与核心组件
        self.context = EngineContext(run_mode=self.run_mode, config=config)
        self.event_bus = EventBus()

        # 任务调度器
        # self.scheduler = TaskScheduler(self.context, self.event_bus)

        # 组件容器：管理你架构中所有注册的模块
        self._components: Dict[str, IBaseComponent] = {}
        self._component_order: List[str] = []  # 按注册顺序初始化/启动

        # 日志与信号
        self._register_system_signals()

    @classmethod
    def create(cls, config: Dict[str, Any]) -> IQuantEngine:
        return cls(config)

    # ------------------------------
    # 生命周期核心实现
    # ------------------------------
    def initialize(self) -> None:
        logger.info("=" * 60)
        logger.info("=== 量化引擎开始初始化 ===")
        self._status = EngineStatus.INITIALIZING
        self.context.engine_status = self._status

        # 按注册顺序初始化所有组件，异常隔离，一个组件失败不影响其他
        for component_name in self._component_order:
            component = self._components[component_name]
            if not component.enabled:
                logger.warning(f'component "{component_name}" is disabled')
                continue
            try:
                logger.info(f"初始化组件: {component_name}")
                component.initialize(self.context, self.event_bus)
            except Exception as e:
                logger.error(f"组件 {component_name} 初始化失败: {str(e)}", exc_info=True)
                self.event_bus.publish(StandardEvents.COMPONENT_ERROR, component=component_name, error=e)

        # 启动任务调度器
        # self.scheduler.start()
        # self.event_bus.subscribe(event_name=StandardEvents.TASK_SUBMIT, receiver=self._on_task_submit)

        logger.info("=== 量化引擎初始化完成 ===")
        logger.info("=" * 60)

    def start(self) -> EngineContext:
        logger.info(f"=== 引擎启动 | 运行模式: {self.run_mode.value} | 运行ID: {self.context.run_id} ===")
        self._status = EngineStatus.RUNNING
        self.context.engine_status = self._status
        self._stop_requested = False

        # 1. 初始化所有组件
        self.initialize()

        # 2. 启动所有组件
        self._start_all_components()

        # 3. 发布引擎启动事件
        self.event_bus.publish(StandardEvents.ENGINE_STARTED, context=self.context)
        self._status = EngineStatus.RUNNING
        # 4. 启动Twisted Reactor主循环（永久运行，除非调用stop）
        reactor.run()

        # 5. Reactor停止后返回最终结果
        self._status = EngineStatus.STOPPED
        self.context.engine_status = self._status
        self.event_bus.publish(StandardEvents.ENGINE_STOPPED, context=self.context)
        logger.info("=== 量化引擎已正常停止 ===")
        return self.context

    def stop(self, graceful: bool = True) -> None:
        logger.debug(f"目前引擎状态:{self._status}")
        if self._status not in [EngineStatus.RUNNING, EngineStatus.PAUSED]:
            return
        self._graceful_stop = graceful
        self._stop_requested = True
        self._status = EngineStatus.STOPPING
        self.context.engine_status = self._status
        logger.warning("收到引擎停止请求，正在执行优雅退出...")

        # 异步执行优雅退出流程
        reactor.callLater(0, self._graceful_shutdown)

    # ------------------------------
    # 组件管理实现
    # ------------------------------
    def register_component(self, component: IBaseComponent) -> None:
        """
        注册你架构中的所有模块，按注册顺序调度
        推荐注册顺序（和你的架构完全一致）：
        1. MarketCenter 行情中心
        2. TaskScheduler 定时调度器
        3. StrategyManager 策略管理器
        4. RiskManager 风控管理器
        5. Sizer 仓位计算器
        6. Portfolio 持仓账户
        7. Execution 执行层
        8. BrokerAdapter 券商适配器
        """
        if not component.name:
            raise ValueError("组件必须定义name属性")
        if component.name in self._components:
            raise ValueError(f"组件 {component.name} 已注册")

        self._components[component.name] = component
        self._component_order.append(component.name)
        logger.info(f"注册组件: {component.name} | 状态: {'启用' if component.enabled else '禁用'}")

    def get_component(self, component_name: str) -> Optional[IBaseComponent]:
        return self._components.get(component_name)

    def enable_component(self, component_name: str) -> None:
        component = self._get_component_or_throw(component_name)
        component.enabled = True
        logger.info(f"动态启用组件: {component_name}")

    def disable_component(self, component_name: str) -> None:
        component = self._get_component_or_throw(component_name)
        component.enabled = False
        logger.info(f"动态禁用组件: {component_name}")

    # ------------------------------
    # 状态与管理接口
    # ------------------------------
    def get_status(self) -> EngineStatus:
        return self._status

    def get_context(self) -> EngineContext:
        return self.context

    def get_event_bus(self) -> EventBus:
        return self.event_bus

    # ------------------------------
    # 内部核心逻辑
    # ------------------------------
    def _start_all_components(self) -> None:
        """按注册顺序启动所有组件，异常隔离"""
        for component_name in self._component_order:
            component = self._components[component_name]
            if not component.enabled:
                continue
            try:
                logger.info(f"启动组件: {component_name}")
                component.start()
            except Exception as e:
                logger.error(f"组件 {component_name} 启动失败: {str(e)}", exc_info=True)
                self.event_bus.publish(StandardEvents.COMPONENT_ERROR, component=component_name, error=e)

    @defer.inlineCallbacks
    def _graceful_shutdown(self) -> Generator[Deferred[Any], Any, None]:
        """优雅退出流程，对标Scrapy"""
        logger.info("开始执行优雅退出流程...")
        self.event_bus.publish(StandardEvents.ENGINE_STOPPED, context=self.context)

        # 全局停止超时：最多60秒，超时强制退出
        shutdown_timeout = 60
        start_time = time.time()
        # 1. 等待当前任务完成（优雅模式）
        if self._graceful_stop:
            logger.info("等待当前任务处理完成...")
            scheduler = self.get_component("task_scheduler")
            max_task_wait = 30
            waited = 0
            while not scheduler.is_idle and waited < max_task_wait:
                if time.time() - start_time > shutdown_timeout:
                    logger.warning("⚠️ 全局停止超时，终止任务等待")
                    break
                logger.debug(f"等待中 | 活跃任务: {scheduler.active_tasks} | 待执行: {scheduler._task_queue.qsize()} | 已等待: {waited:.1f}s")
                yield async_sleep(0.1)
                waited += 0.1

        # 2. 逆序停止所有组件（先停下游交易层，再停上游行情层）
        for component_name in reversed(self._component_order):
            component = self._components[component_name]
            try:
                logger.info(f"停止组件: {component_name}")
                d = defer.maybeDeferred(component.stop, self._graceful_stop)
                d.addTimeout(5, reactor)
                yield d
            except Exception as e:
                logger.error(f"组件 {component_name} 停止异常: {str(e)}", exc_info=True)

        # 3. 持久化状态
        self._persist_state()

        # 4. 停止Twisted Reactor
        if reactor.running:
            reactor.stop()
        self._status = EngineStatus.STOPPED

        yield defer.succeed(None)

    def _get_component_or_throw(self, component_name: str) -> IBaseComponent:
        component = self._components.get(component_name)
        if not component:
            raise ValueError(f"未找到组件: {component_name}")
        return component

    def _register_system_signals(self) -> None:
        """捕获系统终止信号，触发退出"""

        def handle_stop(signum, frame):
            signal_name = signal.Signals(signum).name
            logger.warning(f"收到系统终止信号: {signal_name}, {frame}")
            self.stop(graceful=True)

        for sig in [signal.SIGINT, signal.SIGTERM]:
            signal.signal(sig, handle_stop)


    def _persist_state(self) -> None:
        """持久化引擎状态，可扩展"""
        logger.info("引擎状态已持久化")


if __name__ == "__main__":
    # 1. 全局配置
    config = {
        "RUN_MODE": "BACKTEST",  # 改BACKTEST就是回测自动停止
        "SYMBOL": "000001.SZ",
        "LOG_LEVEL": "INFO",
    }

    # 2. 创建引擎实例
    engine = BaseQuantEngine.create(config)

    engine.register_component(TaskSchedulerComponent())  # 1. 调度器（必须）
    # engine.register_component(MarketCenter())  # 2. 行情中心（必须）
    # engine.register_component(StrategyManager())  # 3. 策略管理器（必须）
    # engine.register_component(RiskManager())        # 你的风控管理器
    # engine.register_component(Sizer())              # 你的仓位计算器
    # engine.register_component(Portfolio())          # 你的持仓账户
    # engine.register_component(BrokerAdapter())  # 4. 券商/撮合适配器（必须）

    # 测试一个自定义组件
    engine.register_component(TimerComponent())


    # 4. 订阅事件示例（所有模块通过事件通信，无直接调用）
    def on_signal_generated(*args, **kwargs):
        logger.debug(f"收到策略信号: {kwargs}")

    engine.get_event_bus().subscribe(StandardEvents.ENGINE_STARTED, on_signal_generated)

    # 5. 一键启动引擎
    context = engine.start()

    # 6. 运行完成后查看结果
    logger.debug(f"运行完成 | 模式: {context.run_mode.value}")