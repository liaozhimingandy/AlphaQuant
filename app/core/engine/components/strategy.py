#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : strategy.py
# @Description : 策略管理组件：引擎里承载 TaskRuntime 的那个组件
#               订阅行情与新闻两个通道，统一驱动所有任务，订单回流到事件总线
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, List, Optional

from twisted.internet import defer

from app.core.engine.components.ibase import IBaseComponent
from app.core.engine.event import StandardEvents
from app.core.market.types import Order, OrderStatus
from app.core.task.runtime import TaskRuntime
from app.core.task.spec import parse_task_specs
from app.utils.logger import logger


class StrategyManagerComponent(IBaseComponent):
    """多任务调度中枢。

    它是引擎里唯一持有 :class:`TaskRuntime` 的组件，职责边界很清晰：
      - **输入**：BAR_RECEIVED（行情通道）、NEWS_ANALYZED（事件通道）
      - **输出**：ORDER_FILLED / SIGNAL_REJECTED / POSITION_UPDATED

    这样"一个引擎跑 N 个任务"对引擎本身是透明的——引擎只知道有这个组件，
    不需要理解什么是任务、什么是策略。
    """

    name = "strategy_manager"

    #: 已发布订单事件的去重集合上限
    _PUBLISHED_MAX = 8192

    def __init__(self, tasks: Optional[List[Any]] = None, **kwargs: Any) -> None:
        super().__init__()
        self.runtime = TaskRuntime()
        self._task_configs: List[Any] = list(tasks or [])
        self.runtime.set_order_sink(self._on_order)
        #: (order_id, status, filled_size) 去重集合，保证订单事件幂等
        self._published: set = set()

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        raw = cfg.get("tasks") or self._task_configs
        self.load_tasks(raw)

        self.subscribe_event(StandardEvents.BAR_RECEIVED, self._on_bar)
        self.subscribe_event(StandardEvents.NEWS_ANALYZED, self._on_news)
        # 实盘：任务建好后要挂到网关上，让成交回报能路由回来。
        # 用事件解耦，策略中枢不需要知道实盘网关存不存在。
        self.subscribe_event(StandardEvents.TASK_ADDED, self._on_task_added)
        logger.info(f"策略中枢已订阅行情/新闻通道 | 任务数: {len(self.runtime)}")

    def _on_task_added(self, task: Any = None, task_id: str = "", **kwargs: Any) -> None:
        if task is not None:
            self._attach_gateway(task)

    def _attach_gateway(self, task: Any) -> None:
        """把新任务挂到实盘网关（网关不存在时这一步自然什么都不做）。"""
        engine = getattr(self, "_engine", None)
        if engine is None:
            return
        gw = engine.get_component("live_gateway")
        if gw is None:
            return
        try:
            gw.attach(task)
        except Exception as exc:
            logger.error(f"任务 {getattr(task, 'task_id', '?')} 挂载实盘网关失败: {exc}")

    def set_engine(self, engine: Any) -> None:
        self._engine = engine

    def load_tasks(self, raw: Any) -> List[str]:
        """从配置装载任务。返回任务ID列表。"""
        specs = parse_task_specs(raw)
        ids: List[str] = []
        for spec in specs:
            try:
                task = self.runtime.add_from_spec(spec)
                ids.append(task.task_id)
            except Exception as exc:
                logger.error(f"任务装载失败 {spec.symbol}: {exc}", exc_info=True)
        return ids

    def on_start(self) -> defer.Deferred:
        # 先挂网关再启动任务：装载阶段（initialize）进来的任务不会触发
        # TASK_ADDED 事件，只能在这里补挂。漏挂的后果是成交回报到达时
        # "无法归属"被整批丢弃 —— 下单成功但账本永远不动。
        for task in list(self.runtime):
            self._attach_gateway(task)
        self.runtime.start_all()
        if len(self.runtime) == 0:
            logger.warning("策略中枢没有任何任务，引擎将空转")
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        self.runtime.stop_all()
        return defer.succeed(None)

    # ---------------- 通道一：行情 ----------------
    def _on_bar(self, bar=None, **kwargs) -> None:
        if bar is None:
            return
        # 只驱动，不在这里发订单事件 —— TaskRuntime 内部对每个产出的订单
        # 都会走一次 order_sink（见 _emit），返回值是同一批对象。
        # 以前两处都发，于是每个订单被发布两次：
        # 落库后成交记录直接翻倍，胜率/成交笔数全部算错。
        self.runtime.on_bar(bar)

    # ---------------- 通道二：新闻 ----------------
    def _on_news(self, news=None, analysis=None, event=None, **kwargs) -> None:
        if news is None:
            return
        self.runtime.on_news(news, analysis)

    # ---------------- 订单回流 ----------------
    def _on_order(self, order: Order) -> None:
        self._publish_order(order)

    def _publish_order(self, order: Order) -> None:
        """订单**每一个状态**都要发事件。

        以前只在 FILLED / REJECTED 时发，于是"挂单中"和"已撤单/部分成交"
        这两类状态在审计上完全不可见 —— 而实盘最容易出问题的恰好是它们
        （撤不掉的单、只成交了一半的单）。落库组件靠这些事件重建订单生命周期。

        幂等：同一个订单的同一个 (状态, 累计成交量) 只发一次。
        订单状态是单向推进的，所以这个组合天然唯一；
        重发只会让下游把同一笔成交记两遍（胜率直接失真）。
        """
        key = (order.order_id, order.status, int(order.filled_size or 0))
        if key in self._published:
            return
        self._published.add(key)
        if len(self._published) > self._PUBLISHED_MAX:
            # 有界集合：长跑服务不能因为记"发过什么"而无限吃内存
            for old in list(self._published)[: self._PUBLISHED_MAX // 4]:
                self._published.discard(old)

        status = order.status
        if status == OrderStatus.PENDING:
            self.event_bus.publish(
                StandardEvents.ORDER_CREATED, order=order, task_id=order.task_id
            )
        elif status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            self.event_bus.publish(
                StandardEvents.ORDER_FILLED, order=order, task_id=order.task_id
            )
            self.event_bus.publish(
                StandardEvents.POSITION_UPDATED, order=order, task_id=order.task_id
            )
        elif status == OrderStatus.CANCELLED:
            self.event_bus.publish(
                StandardEvents.ORDER_CANCELLED, order=order, task_id=order.task_id
            )
        elif status == OrderStatus.REJECTED:
            self.event_bus.publish(
                StandardEvents.SIGNAL_REJECTED,
                order=order,
                reason=order.reject_reason,
                task_id=order.task_id,
            )

    # ---------------- 可观测 ----------------
    def snapshot(self) -> Dict[str, Any]:
        return self.runtime.snapshot()

    def summary(self) -> str:
        return self.runtime.summary()

    def __len__(self) -> int:
        return len(self.runtime)


__all__ = ["StrategyManagerComponent"]
