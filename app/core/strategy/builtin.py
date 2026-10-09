#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : builtin.py
# @Description : 内置策略
#               ComboStrategy 是核心：入场/出场/事件入场/事件出场 全部由规则拼装，
#               因此"策略自由组合"不需要写新类，只需要一张配置表
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.factor.context import FactorContext
from app.core.factor.registry import create_factor
from app.core.market.types import Side, Signal, SignalSource
from app.core.rule.base import IBaseRule
from app.core.strategy.base import IBaseStrategy
from app.core.strategy.state import DecisionState
from app.utils.logger import logger


class ComboStrategy(IBaseStrategy):
    """组合式策略。

    - ``entry``       行情通道入场规则（空仓时判定）
    - ``exit``        行情通道出场规则（持仓时判定）
    - ``event_entry`` 事件通道入场规则；为 None 时复用 entry
    - ``event_exit``  事件通道出场规则；为 None 时复用 exit

    ``event_requires_trend=True`` 表示事件入场必须**同时**满足行情入场规则，
    用于"利好来了但趋势仍是空头就不买"这类保守配置。
    """

    name = "combo"
    description = "规则组合式策略，入场/出场/事件通道均可配置"

    def __init__(
        self,
        entry: Optional[IBaseRule] = None,
        exit: Optional[IBaseRule] = None,
        event_entry: Optional[IBaseRule] = None,
        event_exit: Optional[IBaseRule] = None,
        event_requires_trend: bool = False,
        strength: float = 1.0,
        use_news_confidence_as_strength: bool = False,
        **params: Any,
    ) -> None:
        super().__init__(**params)
        self.entry = entry
        self.exit = exit
        self.event_entry = event_entry
        self.event_exit = event_exit
        self.event_requires_trend = bool(event_requires_trend)
        self.strength = float(strength)
        self.use_news_confidence_as_strength = bool(use_news_confidence_as_strength)

    # ---------------- 决策 ----------------
    def decide(
        self,
        ctx: FactorContext,
        state: DecisionState,
        source: SignalSource = SignalSource.BAR,
        event: Any = None,
    ) -> Optional[Signal]:
        is_event = source in (SignalSource.NEWS, SignalSource.LLM)

        if not state.has_position:
            rule = (self.event_entry or self.entry) if is_event else self.entry
            if rule is None or not rule.is_satisfied(ctx):
                return None
            if is_event and self.event_requires_trend and self.entry is not None:
                if not self.entry.is_satisfied(ctx):
                    logger.debug(
                        f"[{state.task_id}] 事件入场被趋势条件否决（event_requires_trend=True）"
                    )
                    return None
            strength = self._strength(ctx, source)
            return self.make_signal(
                state,
                Side.BUY,
                source,
                reason=self._reason("入场", source, rule),
                strength=strength,
                meta=self._meta(ctx, source, event),
            )

        # 持仓中：只看出场
        rule = (self.event_exit or self.exit) if is_event else self.exit
        if rule is None or not rule.is_satisfied(ctx):
            return None
        return self.make_signal(
            state,
            Side.SELL,
            source,
            reason=self._reason("出场", source, rule),
            strength=1.0,  # 出场不看强度，必须走完
            meta=self._meta(ctx, source, event),
        )

    # ---------------- 辅助 ----------------
    def _strength(self, ctx: FactorContext, source: SignalSource) -> float:
        if self.use_news_confidence_as_strength and source in (
            SignalSource.NEWS,
            SignalSource.LLM,
        ):
            conf = ctx.feature("news_confidence")
            if conf is not None:
                return max(0.0, min(1.0, float(conf)))
            return 0.0  # 拿不到置信度就不该按下单强度处理
        return self.strength

    @staticmethod
    def _reason(prefix: str, source: SignalSource, rule: IBaseRule) -> str:
        name = getattr(rule, "name", "") or rule.__class__.__name__
        return f"{prefix}@{source.value}:{name}"

    @staticmethod
    def _meta(
        ctx: FactorContext, source: SignalSource, event: Any
    ) -> Dict[str, Any]:
        meta: Dict[str, Any] = {"channel": source.value}
        if event is not None:
            meta["event"] = (
                event.to_dict() if hasattr(event, "to_dict") else str(event)
            )
        for k in ("news_sentiment", "news_confidence", "news_count"):
            if ctx.has_feature(k):
                meta[k] = ctx.feature(k)
        return meta


class MaCrossStrategy(ComboStrategy):
    """经典双均线：快线上穿慢线买入，下穿卖出（行情通道）"""
    name = "ma_cross"
    description = "双均线金叉买入、死叉卖出"

    def __init__(self, fast: int = 5, slow: int = 20, **params: Any) -> None:
        from app.core.rule.builtin import CrossDownRule, CrossUpRule

        super().__init__(
            entry=CrossUpRule(
                "ma", "ma", left_params={"period": fast}, right_params={"period": slow}
            ),
            exit=CrossDownRule(
                "ma", "ma", left_params={"period": fast}, right_params={"period": slow}
            ),
            **params,
        )
        self.fast, self.slow = int(fast), int(slow)


class NewsDrivenStrategy(ComboStrategy):
    """新闻驱动：行情通道用趋势过滤，事件通道用新闻影响力直接触发。

    这是"实时采集新闻 → 有事件就触发交易"的默认实现。
    """
    name = "news_driven"
    description = "新闻事件驱动交易，可叠加行情趋势过滤"

    def __init__(
        self,
        bullish_threshold: float = 0.3,
        bearish_threshold: float = -0.3,
        min_confidence: float = 0.5,
        trend_fast: int = 5,
        trend_slow: int = 20,
        event_requires_trend: bool = True,
        **params: Any,
    ) -> None:
        from app.core.rule.builtin import CrossDownRule, CrossUpRule, ThresholdRule
        from app.core.rule.combinator import AllRule

        super().__init__(
            entry=CrossUpRule(
                "ma",
                "ma",
                left_params={"period": trend_fast},
                right_params={"period": trend_slow},
            ),
            exit=CrossDownRule(
                "ma",
                "ma",
                left_params={"period": trend_fast},
                right_params={"period": trend_slow},
            ),
            event_entry=AllRule([
                ThresholdRule("news_impact", "gt", bullish_threshold),
                ThresholdRule("news_confidence", "gte", min_confidence),
            ]),
            event_exit=AllRule([
                ThresholdRule("news_impact", "lt", bearish_threshold),
                ThresholdRule("news_confidence", "gte", min_confidence),
            ]),
            event_requires_trend=event_requires_trend,
            use_news_confidence_as_strength=True,
            **params,
        )
