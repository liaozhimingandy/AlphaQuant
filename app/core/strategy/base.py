#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : base.py
# @Description : 策略层基类：只做"调度与决策"，不做计算（计算在因子层）
#               策略有两个输入通道：行情（on_bar）与事件（on_event，如新闻/LLM）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Dict, Optional

from app.core.factor.context import FactorContext
from app.core.market.types import Side, Signal, SignalSource
from app.core.strategy.state import DecisionState


class IBaseStrategy(abc.ABC):
    """策略基类。

    双通道设计：
      - ``on_bar``   —— 行情驱动（K线闭合时触发）
      - ``on_event`` —— 事件驱动（新闻/大模型/定时器，随时可能来）

    两条通道都返回 ``Signal | None``，下游统一走 风控 → 仓位 → 撮合。
    这样"新闻触发交易"不需要在行情通道里打补丁，两条链路完全对称。
    """

    name: str = ""
    description: str = ""

    def __init__(self, **params: Any) -> None:
        self.params: Dict[str, Any] = params

    # =========================
    # 生命周期
    # =========================
    def on_init(self, task_id: str = "", symbol: str = "") -> None:
        """策略实例化后调用一次"""
        pass

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def on_order(self, order: Any) -> None:
        """订单状态回调"""
        pass

    # =========================
    # 决策入口
    # =========================
    def on_bar(
        self, ctx: FactorContext, state: DecisionState
    ) -> Optional[Signal]:
        """行情通道。默认委托给通用决策逻辑。"""
        return self.decide(ctx, state, source=SignalSource.BAR)

    def on_event(
        self, ctx: FactorContext, state: DecisionState, event: Any = None
    ) -> Optional[Signal]:
        """事件通道（新闻/LLM/定时）。默认复用行情通道的逻辑。"""
        return self.decide(
            ctx, state, source=SignalSource.NEWS, event=event
        )

    # =========================
    # 子类实现
    # =========================
    @abc.abstractmethod
    def decide(
        self,
        ctx: FactorContext,
        state: DecisionState,
        source: SignalSource = SignalSource.BAR,
        event: Any = None,
    ) -> Optional[Signal]:
        raise NotImplementedError

    # =========================
    # 工具方法
    # =========================
    @staticmethod
    def make_signal(
        state: DecisionState,
        side: Side,
        source: SignalSource,
        reason: str = "",
        strength: float = 1.0,
        meta: Optional[Dict[str, Any]] = None,
        ts: Optional[datetime] = None,
    ) -> Signal:
        return Signal(
            symbol=state.symbol,
            side=side,
            source=source,
            reason=reason,
            strength=max(0.0, min(1.0, strength)),
            task_id=state.task_id,
            ts=ts,
            meta=meta or {},
        )
