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
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Generator
import signal

from twisted.internet import reactor, defer
from twisted.internet.defer import Deferred

from app.common.config import settings
from app.engine import component
from app.engine.component import (
    IBaseComponent,
    SchedulerComponent,
    StrategyComponent,
    WorkerComponent,
)
from app.engine.component.kinds import ComponentKind, KIND_LAYER, ServiceComponent
from app.engine.component.registry import (
    LEGACY_ALIASES,
    ServiceRegistry,
    aliases_for,
)
from app.engine.control import ControlCenter

from app.engine.event import EventBus, StandardEvents
from app.engine.settings import EngineContext, EngineStatus, RunMode
from app.engine.utils import async_sleep
from app.common.jsonio import json_safe
from app.common.logging import logger


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

    @classmethod
    def async_sleep(cls, seconds: float) -> defer.Deferred:
        """
        全版本兼容的Twisted异步sleep，替代高版本才有的defer.sleep
        :param seconds: 等待时间（秒），支持小数如0.1、0.01
        :return: Deferred对象，等待完成后自动触发callback
        """
        d = defer.Deferred()
        reactor.callLater(delay=seconds, callable=d.callback,)
        return d


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

        # 运行时控制中心：面板 / CLI 通过它增删任务、启停组件、优雅停止
        self.control = ControlCenter(self)

        # 任务调度器
        # self.scheduler = TaskScheduler(self.context, self.event_bus)

        # 组件容器：管理你架构中所有注册的模块
        self._components: Dict[str, IBaseComponent] = {}
        self._component_order: List[str] = []  # 按注册顺序初始化/启动

        # 服务注册表：只登记**服务**，负责能力查询、依赖解析与注入。
        #
        # 为什么组件容器之外还要一个注册表：容器回答"它在不在"，
        # 注册表回答"谁能提供这个能力、它依赖谁、该按什么顺序起"。
        # 以前这些问题全靠注册顺序和魔法字符串回答 ——
        # 于是"采集服务先于行情服务启动"这种约束只存在于注释里。
        self.services = ServiceRegistry()

        #: 旧组件名 → 新组件名。服务那份别名由注册表管（连同能力一起），
        #: 这里补的是**非服务**组件（strategy / scheduler）——
        #: 它们不进注册表，但旧代码同样在用旧名字找它们。
        self._name_aliases: Dict[str, str] = dict(LEGACY_ALIASES)

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

        # 按依赖拓扑顺序初始化所有组件，异常隔离，一个组件失败不影响其他
        self._component_order = self.ordered_component_names()
        for component_name in self._component_order:
            component = self._components[component_name]
            if not component.enabled:
                logger.warning(f'component "{component_name}" is disabled')
                continue
            try:
                logger.info(f"初始化组件: {component_name}")

                # 依赖注入：把该组件声明的服务先塞进去，再初始化。
                # 顺序不能反 —— 组件的 on_initialize 里就要用这些服务
                # （策略组件在 on_initialize 里就把风控闸门交给任务运行时）。
                injector = getattr(component, "bind_services", None)
                if callable(injector):
                    injector(self.services.build_injections(component))

                # 组件配置从全局配置里按组件名取，这样一个组件的行为
                # 也能通过配置驱动，不必改代码
                components_cfg = self.config.get("components") or {}
                cfg = (
                    components_cfg.get(component_name)
                    or self.config.get(component_name)
                    or {}
                )
                component.initialize(self.context, self.event_bus, cfg)
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

        # 3.1 回测模式：装一个"空闲自动停止"看门狗。
        #      否则 reactor.run() 会永久阻塞，回测永远不会返回——这是旧实现最大的可用性问题。
        if self.run_mode == RunMode.BACKTEST and self.config.get(
            "BACKTEST_AUTO_STOP", settings.BACKTEST_AUTO_STOP
        ):
            self._start_idle_watchdog()

        # 4. 启动Twisted Reactor主循环（永久运行，除非调用stop）
        reactor.run()

        # 5. Reactor停止后返回最终结果
        self._status = EngineStatus.STOPPED
        self.context.engine_status = self._status
        self.context.end_time = datetime.now()
        self.event_bus.publish(StandardEvents.ENGINE_STOPPED, context=self.context)
        logger.info("=== 量化引擎已正常停止 ===")
        return self.context

    def _start_idle_watchdog(self) -> None:
        """回测模式空闲看门狗：调度器持续空闲 N 秒后自动停止引擎。

        这样"跑完就退出"成为默认行为；SIMULATE/LIVE 模式不会安装它。
        """
        timeout = float(
            self.config.get("BACKTEST_IDLE_TIMEOUT", settings.BACKTEST_IDLE_TIMEOUT)
        )
        idle_since: Optional[float] = None

        def _watch() -> None:
            nonlocal idle_since
            if self._status in (EngineStatus.STOPPING, EngineStatus.STOPPED):
                return
            if self._stop_requested:
                return

            if self._is_idle():
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since >= timeout:
                    logger.info(
                        f"回测模式：调度器已空闲 {timeout:.1f}s，触发自动停止"
                    )
                    self.stop(graceful=True)
                    return
            else:
                idle_since = None

            reactor.callLater(0.5, _watch)

        logger.info(f"回测模式已启用空闲自动停止 | 空闲阈值: {timeout:.1f}s")
        reactor.callLater(0.5, _watch)

    def _component_is_busy(self) -> bool:
        """是否有业务组件仍在工作。

        只问调度组件是不够的：回放行情、轮询新闻这类组件不往调度器里
        放任务，但显然不应该被判定为空闲。所以这里让组件自己声明忙闲。
        """
        for comp in self._components.values():
            fn = getattr(comp, "is_busy", None)
            if not callable(fn):
                continue
            try:
                if bool(fn()):
                    return True
            except Exception as exc:
                logger.debug(f"组件 {getattr(comp, 'name', comp)} 忙闲检测失败: {exc}")
        return False

    def _is_idle(self) -> bool:
        """调度器是否空闲。调度器组件缺失时视为空闲。"""
        if self._component_is_busy():
            return False
        scheduler = self.get_component("scheduler")
        if scheduler is None:
            return True
        is_idle = getattr(scheduler, "is_idle", None)
        if is_idle is None:
            return True
        try:
            return bool(is_idle)
        except Exception as exc:
            logger.debug(f"调度器空闲检测失败，按空闲处理: {exc}")
            return True

    def stop(self, graceful: bool = True) -> None:
        logger.debug(f"目前引擎状态:{self._status}")
        if self._status in [EngineStatus.STOPPING, EngineStatus.STOPPED]:
            return
        self._graceful_stop = graceful
        self._stop_requested = True
        self._status = EngineStatus.STOPPING
        self.context.engine_status = self._status
        logger.warning("收到引擎停止请求，正在执行优雅退出...")

        if not reactor.running:
            # Reactor 尚未启动（例如启动阶段就失败），同步退出避免 callLater 丢失
            defer.maybeDeferred(self._graceful_shutdown)
            return

        # 异步执行优雅退出流程
        reactor.callLater(0, self._graceful_shutdown)

    # ------------------------------
    # 组件管理实现
    # ------------------------------
    def register_component(self, component: IBaseComponent) -> None:
        """
        注册一个组件。

        **注册顺序不再是启停顺序**。以前靠"先注册先启动"这种隐式约定
        表达依赖（"采集要在行情之前注册"），问题是：
          - 顺序要求写在 builder 的注释里，改装配代码时极易破坏；
          - 组件之间真正的依赖关系完全没被表达出来。

        现在启停顺序由 :class:`ServiceRegistry` 按三样东西算出来：
          1. 服务声明的 ``depends_on``（硬依赖，拓扑排序保证）；
          2. 组件种类层级（调度 → 服务 → 策略 → 工作单元 → 其它）；
          3. 注册顺序（同层内保持稳定，日志不至于乱跳）。

        服务会额外登记进注册表，并自动带上旧组件名作为别名，
        于是 ``get_component("market_center")`` 这类旧写法仍能工作
        （别名表见 :data:`~app.engine.component.registry.LEGACY_ALIASES`）。
        """
        if not component.name:
            raise ValueError("组件必须定义name属性")
        if component.name in self._components:
            raise ValueError(f"组件 {component.name} 已注册")

        self._components[component.name] = component
        self._component_order.append(component.name)

        if isinstance(component, ServiceComponent):
            self.services.register(component, alias=aliases_for(component.name))

        kind = getattr(component, "kind", ComponentKind.PLAIN)
        logger.info(
            f"注册组件: {component.name} | 种类: {kind.label} "
            f"| 状态: {'启用' if component.enabled else '禁用'}"
        )

    def get_component(self, component_name: str) -> Optional[IBaseComponent]:
        """按名字取组件。

        三级查找：组件容器 → 服务注册表（含旧名别名）→ None。
        这样旧代码里的 ``get_component("live_gateway")`` 依然能拿到
        交易服务，而新代码一律用新服务名 ``trading_service``。
        """
        if component_name in self._components:
            return self._components[component_name]
        # 旧名 → 新名（非服务组件靠这一层，服务组件注册表里也有别名）
        real = self._name_aliases.get(component_name, component_name)
        if real in self._components:
            return self._components[real]
        return self.services.get(component_name)

    # ------------------------------
    # 服务访问（新代码请用这一组，别再用字符串）
    # ------------------------------
    def get_service(self, service_name: str) -> Optional[ServiceComponent]:
        """按服务名取服务。找不到返回 None。"""
        return self.services.get(service_name)

    def require_service(self, service_name: str) -> ServiceComponent:
        """按服务名取服务，**取不到就抛**。

        用于"这个服务必须在场"的场合。返回 None 会让调用方写出
        ``if svc:`` 的降级分支，而在实盘里那条分支通常意味着
        "单没发出去但没人知道"。
        """
        return self.services.require(service_name)

    def service_by_capability(self, capability: str) -> Optional[ServiceComponent]:
        """按**能力**取服务。

        这是比按名字取更抗改名的写法：服务改了名，调用方一行都不用动。
        """
        return self.services.first_by_capability(capability)

    def ordered_component_names(self) -> List[str]:
        """安全的启动顺序（停止时反序）。见 :meth:`register_component`。"""
        try:
            svc_order = self.services.resolve_dependencies()
        except Exception as exc:
            # 依赖成环是**装配错误**，但它不该让引擎连日志都打不出来。
            # 退化成注册顺序，把问题打进日志等人修。
            logger.error(f"服务依赖解析失败，退化为注册顺序: {exc}")
            svc_order = [n for n in self._component_order
                         if isinstance(self._components[n], ServiceComponent)]

        # 服务内部已经有了拓扑顺序，把它"折叠"进全局的层级排序里：
        # 先按种类分层（调度 → 服务 → 策略 → 工作单元 → 其它），
        # 服务那一层内部再按拓扑结果排。
        #
        # 不能简单写成 [服务们] + [其它们] —— 那样调度组件会被排到服务之后，
        # 而它必须**最先启动**：它是 TASK_SUBMIT 的订阅者，
        # 晚于服务启动就会丢掉服务启动阶段提交的任务。
        svc_pos = {n: i for i, n in enumerate(svc_order)}

        def layer(n: str) -> int:
            kind = getattr(self._components[n], "kind", ComponentKind.PLAIN)
            return KIND_LAYER.get(kind, 99)

        def sort_key(n: str) -> tuple:
            if n in svc_pos:
                return (layer(n), 0, svc_pos[n])
            return (layer(n), 1, self._component_order.index(n))

        return sorted(self._component_order, key=sort_key)

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
    # 快照（面板 / 落盘 / 复盘共用）
    # ------------------------------
    @property
    def uptime_seconds(self) -> float:
        end = self.context.end_time or datetime.now()
        return max(0.0, (end - self.context.start_time).total_seconds())

    def component_names(self) -> List[str]:
        """组件名单，顺序即**启停顺序**（停止时逆序）。

        这里直接走 :meth:`ordered_component_names` 而不是返回注册顺序。
        理由：注册顺序是装配器书写的偶然结果，今天改一行注册代码
        面板上的组件顺序就变了，而看面板的人会以为"顺序有意义"。
        依赖算出来的顺序才是有意义的那一个（调度 → 服务 → 策略 →
        工作单元 → 其它），所以对外只暴露它。
        """
        return self.ordered_component_names()

    def snapshot(self) -> Dict[str, Any]:
        """聚合引擎 + 全部组件的瞬时状态，供监控面板与快照落盘使用。

        设计约束：**任何组件快照失败都不能让整体快照失败**。
        监控链路本身崩掉是最糟的失败模式——你恰恰在出问题时看不到问题。
        """
        components: Dict[str, Any] = {}
        for name in self._component_order:
            comp = self._components[name]
            kind = getattr(comp, "kind", ComponentKind.PLAIN)
            entry: Dict[str, Any] = {
                "name": name,
                "kind": kind.value,
                "kind_label": kind.label,
                "enabled": bool(comp.enabled),
                "state": comp.state,
            }
            try:
                healthy, detail = comp.health_check()
                entry["healthy"] = bool(healthy)
                entry["health_detail"] = detail
            except Exception as exc:  # pragma: no cover - 防御
                entry["healthy"] = None
                entry["health_detail"] = f"health_check 异常: {exc}"

            snap_fn = getattr(comp, "snapshot", None)
            if callable(snap_fn):
                try:
                    entry["snapshot"] = json_safe(snap_fn())
                except Exception as exc:
                    entry["snapshot_error"] = str(exc)
            # 任务进度（"已完成 / 全部"）：没有跑过任务的组件 total=0，面板据此不显示进度条
            prog_fn = getattr(comp, "progress", None)
            if callable(prog_fn):
                try:
                    entry["progress"] = prog_fn()
                except Exception as exc:  # pragma: no cover - 防御
                    entry["progress"] = {"total": 0, "error": str(exc)}
            components[name] = entry

        try:
            idle = bool(self._is_idle())
        except Exception:
            idle = None

        return {
            "engine": {
                "run_id": self.context.run_id,
                "mode": self.run_mode.value,
                "status": self._status.value,
                "started_at": self.context.start_time.isoformat(),
                "ended_at": (
                    self.context.end_time.isoformat() if self.context.end_time else None
                ),
                "uptime_sec": round(self.uptime_seconds, 1),
                "stop_requested": self._stop_requested,
                "idle": idle,
                "component_order": list(self._component_order),
            },
            "components": components,
            # 服务全景：能力、依赖、缺失情况。面板按这个画依赖图，
            # 也可以直接拿 missing_dependencies 当"装配自检报告"看。
            "services": self.services.snapshot(),
        }

    # ------------------------------
    # 内部核心逻辑
    # ------------------------------
    def _start_all_components(self) -> None:
        """按依赖拓扑顺序启动所有组件，异常隔离"""
        for component_name in self.ordered_component_names():
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
        """优雅退出流程，对标Scrapy。"""
        logger.info("开始执行优雅退出流程...")

        # 1. 等待当前任务完成（优雅模式）
        #    注意：旧实现里 self.async_sleep(0.1) 没有 yield，
        #    结果是 while 循环瞬间跑满 300 次并阻塞 reactor——这里修正为真等待。
        if self._graceful_stop:
            logger.info("等待当前任务处理完成...")
            max_task_wait = float(
                self.config.get("SHUTDOWN_TASK_WAIT", 30)
            )
            deadline = time.time() + max_task_wait
            waited = 0.0
            while not self._is_idle() and time.time() < deadline:
                logger.debug(f"等待剩余任务完成 | 已等待: {waited:.1f}s")
                yield self.async_sleep(0.1)
                waited += 0.1
            if not self._is_idle():
                logger.warning(f"⚠️ 任务等待超时({max_task_wait:.0f}s)，强制进入停止流程")

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
        self._status = EngineStatus.STOPPED
        self.context.engine_status = self._status
        if reactor.running:
            reactor.stop()

        logger.info("优雅退出流程执行完毕")

    def _get_component_or_throw(self, component_name: str) -> IBaseComponent:
        component = self._components.get(component_name)
        if not component:
            raise ValueError(f"未找到组件: {component_name}")
        return component

    def _register_system_signals(self) -> None:
        """捕获系统终止信号，触发优雅退出。

        仅在**主线程**注册：Python 规定 signal.signal 只能在主线程调用，
        在子线程里注册会抛 ValueError（这也是单元测试里容易踩的坑）。
        """
        if threading.current_thread() is not threading.main_thread():
            logger.debug("非主线程，跳过系统信号注册")
            return

        def handle_stop(signum, frame):
            signal_name = signal.Signals(signum).name
            logger.warning(f"收到系统终止信号: {signal_name}")
            self.stop(graceful=True)

        for sig in [signal.SIGINT, signal.SIGTERM]:
            try:
                signal.signal(sig, handle_stop)
            except ValueError as exc:
                logger.debug(f"注册信号 {sig} 失败(可忽略): {exc}")


    def _persist_state(self) -> None:
        """持久化引擎状态。

        监控组件在场时让它写最后一份快照——否则"停止前最后一次权益"
        这条最有价值的数据就丢了。
        """
        monitor = self.get_component("monitor")
        flush = getattr(monitor, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception as exc:
                logger.error(f"最终快照落盘失败: {exc}", exc_info=True)

        # 运行时新增的任务，停止时固化，下次可用同一 run_id 重放
        try:
            self.control.save_runtime_tasks()
        except Exception as exc:
            logger.debug(f"运行时任务固化失败（可忽略）: {exc}")

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

    # 调度器：负责"什么时候跑"，先注册也行后注册也行 ——
    # 启停顺序由种类层级决定，不再依赖这里的书写顺序。
    engine.register_component(SchedulerComponent())
    engine.register_component(WorkerComponent(capacity=2))

    # 服务层：提供能力，被策略依赖
    # engine.register_component(MarketService(symbols=[...]))
    # engine.register_component(CollectionService(symbols=[...]))
    # engine.register_component(TradingService())
    # engine.register_component(RiskService(rules=[...]))

    # 策略层：消费服务，产出订单
    # engine.register_component(StrategyComponent(tasks=[...]))


    # 4. 订阅事件示例（所有模块通过事件通信，无直接调用）
    def on_signal_generated(*args, **kwargs):
        logger.debug(f"收到策略信号: {kwargs}")


    engine.get_event_bus().subscribe(StandardEvents.ENGINE_STARTED, on_signal_generated)

    # 5. 一键启动引擎
    context = engine.start()

    # 6. 运行完成后查看结果
    logger.debug(f"运行完成 | 模式: {context.run_mode.value}")