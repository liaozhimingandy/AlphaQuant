#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : score_strategy.py
# @Description : 综合评分策略（事件引擎链路）
#
#                与单因子策略的本质区别：**它不自己判断，而是消费打分引擎的结论**。
#                好处是"判断逻辑"集中在打分引擎里，一份权重配置同时服务于
#                选股（股票池）和择时（本策略），两边口径必然一致。
#
#                参数：
#                  entry_score  综合分 ≥ 此值 → 买入（默认 70）
#                  exit_score   综合分 ≤ 此值 → 卖出（默认 55）
#                  hold_score   持仓期间低于此值也减仓？——刻意**不做**。
#                               频繁进出会把手续费吃光，评分小幅波动不该触发交易。
#
#                关键设计：**评分缺失（None）时不动手**。
#                拿不到评分可能是因为数据没同步，此时"不交易"是唯一安全的选择 ——
#                凭空假设一个分数去交易，比不交易危险得多。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Optional

from app.core.factor.context import FactorContext
from app.core.market.types import Side, Signal, SignalSource
from app.core.strategy.base import IBaseStrategy
from app.core.strategy.registry import register_strategy


@register_strategy
class ScoreStrategy(IBaseStrategy):
    """按多维度综合分决策。"""

    name = "score"
    description = "多维度综合评分策略（题材+资金链+筹码+技术+涨停）"

    def __init__(
        self,
        entry_score: float = 70.0,
        exit_score: float = 55.0,
        min_score_gap: float = 0.0,
        **params: Any,
    ) -> None:
        super().__init__(entry_score=entry_score, exit_score=exit_score, **params)
        self.entry_score = float(entry_score)
        self.exit_score = float(exit_score)
        self.min_score_gap = float(min_score_gap)
        if self.exit_score > self.entry_score:
            # 出口高于入口会造成"刚买就卖"的抖动，直接拒绝这种配置
            raise ValueError(
                f"exit_score({self.exit_score}) 不能大于 entry_score({self.entry_score})"
            )

    def _score(self, ctx: FactorContext) -> Optional[float]:
        v = ctx.feature("composite_score")
        if v is None:
            return None
        try:
            s = float(v)
        except (TypeError, ValueError):
            return None
        return None if s != s else s

    def decide(
        self,
        ctx: FactorContext,
        state: Any,
        source: SignalSource = SignalSource.BAR,
        event: Any = None,
    ) -> Optional[Signal]:
        score = self._score(ctx)
        if score is None:
            return None

        if not state.has_position:
            if score >= self.entry_score:
                # 强度按超出阈值的程度给，用于下游 sizer 决定仓位
                strength = min(1.0, 0.5 + (score - self.entry_score) / 60.0)
                return self.make_signal(
                    state, Side.BUY, source,
                    reason=f"综合评分 {score:.1f} ≥ {self.entry_score}",
                    strength=strength,
                    meta={"score": score},
                )
            return None

        if score <= self.exit_score:
            return self.make_signal(
                state, Side.SELL, source,
                reason=f"综合评分 {score:.1f} ≤ {self.exit_score}",
                strength=1.0,
                meta={"score": score},
            )
        return None


__all__ = ["ScoreStrategy"]
