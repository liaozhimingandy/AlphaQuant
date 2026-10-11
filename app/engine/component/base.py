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
import time
from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Callable

from twisted.internet import defer

from app.engine.event import EventBus
from app.engine.settings import EngineContext
from app.common.logging import logger

# ======================
# 1. 组件快照进度
# ======================
@dataclass
class Progress:
    """组件当前这批任务的进度：``已完成 / 全部``。

    为什么组件要自己报进度：面板上"组件 = 一个小卡片 + 一个状态灯"
    只回答了"活着/挂了"，回答不了"在干嘛、干到哪了"。
    采集类组件一轮要跑几十只票，跑了 5 分钟还是 5 秒钟，
    没有进度就只能靠猜 —— 而猜错的典型后果是"以为卡死了，又重启一次"。

    语义约定（面板与快照都按这个解释）：
      * ``total``  = 本轮任务总数；``done`` = 成功数；``failed`` = 失败数；
      * ``done + failed`` 可能小于 ``total``（还在跑），也可能因重试略大；
      * ``running`` 表示这一轮还没结束。
    """
    label: str = ""          # 这一轮在做什么，例如 "资金流/筹码 68 只"
    total: int = 0
    done: int = 0
    failed: int = 0
    round: int = 0           # 第几轮（每 start 一次 +1）
    running: bool = False
    started_at: Optional[float] = None
    updated_at: Optional[float] = None

    @property
    def finished(self) -> int:
        return self.done + self.failed

    @property
    def percent(self) -> float:
        """完成百分比。total=0 时：跑着算 0%，跑完算 100%。"""
        if self.total <= 0:
            return 0.0 if self.running else 100.0
        return round(min(100.0, self.finished / self.total * 100.0), 1)

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return round((self.updated_at or time.time()) - self.started_at, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "finished": self.finished,
            "round": self.round,
            "running": self.running,
            "percent": self.percent,
            "elapsed": self.elapsed,
        }


# ======================
# 2. 组件状态枚举
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

        # 本轮任务进度（子类用 start_progress / advance_progress 维护）
        self._progress = Progress()

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

        # 3. 生命周期方法遮蔽检测
        #    构造参数若与 start/stop/initialize 同名，会把方法覆盖成标量，
        #    报错信息（"'str' object is not callable"）极难定位，这里提前拦掉
        for method in ("start", "stop", "initialize", "health_check"):
            attr = getattr(self, method, None)
            if not callable(attr):
                raise TypeError(
                    f"组件 {self.__class__.__name__} 的 {method}() 被实例属性遮蔽"
                    f"（当前值={attr!r}），请重命名构造参数"
                )

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

        self._component_status = ComponentState.STOPPING
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

    def on_initialize(self) -> None:
        """子类初始化钩子（**可选实现**，默认什么都不做）。

        为什么不是 ``@abstractmethod``：这三个钩子在绝大多数组件里都是
        "可选"的（有的组件只需要 on_start），标成抽象方法会逼着每个子类
        写三个空方法——纯噪音。而真正漏写的风险由
        :meth:`initialize` 里的调用保证：忘了实现就只是不做任何事，
        不会崩，也不会静默吞掉错误。
        """
        return None

    def on_start(self) -> defer.Deferred:
        """子类启动钩子（**可选实现**，默认立即成功，支持异步）。"""
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        """子类停止钩子（**可选实现**，默认立即成功，支持异步）。"""
        return defer.succeed(None)

    def health_check(self) -> tuple[bool, str]:
        """健康检查，引擎定时调用，返回True表示健康"""
        if not self.enabled:
            return True, "disabled"
        return self._component_status in [ComponentState.RUNNING], self._component_status.value

    # ======================
    # 任务进度（面板上的"已完成 / 全部"）
    # ======================
    def start_progress(self, total: int, label: str = "") -> None:
        """开新一轮任务。周期性组件每轮开始时调一次，进度随之归零。"""
        self._progress.round += 1
        self._progress.total = max(0, int(total or 0))
        self._progress.done = 0
        self._progress.failed = 0
        self._progress.label = str(label or "")
        self._progress.running = True
        self._progress.started_at = time.time()
        self._progress.updated_at = self._progress.started_at

    def advance_progress(self, n: int = 1, failed: bool = False) -> None:
        """推进进度。``failed=True`` 计到失败数（面板上标红）。"""
        n = int(n or 0)
        if n <= 0:
            return
        if failed:
            self._progress.failed += n
        else:
            self._progress.done += n
        self._progress.updated_at = time.time()

    def finish_progress(self) -> None:
        """本轮结束（成功收尾或异常收尾都该调，否则面板永远显示"进行中"）。"""
        self._progress.running = False
        self._progress.updated_at = time.time()

    def progress(self) -> Dict[str, Any]:
        """给快照/面板用的进度字典。没有任务过的组件 total=0，面板不显示进度条。"""
        return self._progress.to_dict()

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

