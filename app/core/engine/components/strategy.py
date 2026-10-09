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

    def __init__(self, tasks: Optional[List[Any]] = None, **kwargs: Any) -> None:
        super().__init__()
        self.runtime = TaskRuntime()
        self._task_configs: List[Any] = list(tasks or [])
        self.runtime.set_order_sink(self._on_order)

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        raw = cfg.get("tasks") or self._task_configs
        self.load_tasks(raw)

        self.subscribe_event(StandardEvents.BAR_RECEIVED, self._on_bar)
        self.subscribe_event(StandardEvents.NEWS_ANALYZED, self._on_news)
        logger.info(f"策略中枢已订阅行情/新闻通道 | 任务数: {len(self.runtime)}")

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
        orders = self.runtime.on_bar(bar)
        for order in orders:
            self._publish_order(order)

    # ---------------- 通道二：新闻 ----------------
    def _on_news(self, news=None, analysis=None, event=None, **kwargs) -> None:
        if news is None:
            return
        orders = self.runtime.on_news(news, analysis)
        for order in orders:
            self._publish_order(order)

    # ---------------- 订单回流 ----------------
    def _on_order(self, order: Order) -> None:
        self._publish_order(order)

    def _publish_order(self, order: Order) -> None:
        if order.status == OrderStatus.FILLED:
            self.event_bus.publish(
                StandardEvents.ORDER_FILLED, order=order, task_id=order.task_id
            )
            self.event_bus.publish(
                StandardEvents.POSITION_UPDATED, order=order, task_id=order.task_id
            )
        elif order.status == OrderStatus.REJECTED:
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
