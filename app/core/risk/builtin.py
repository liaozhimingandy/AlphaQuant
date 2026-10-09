#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : builtin.py
# @Description : 内置风控规则
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Optional

from app.core.factor.context import FactorContext
from app.core.market.types import Account, Side, Signal, SignalSource
from app.core.risk.base import IBaseRiskRule, RiskVerdict
from app.core.risk.registry import register_risk
from app.core.strategy.state import DecisionState


@register_risk
class MaxDrawdownRisk(IBaseRiskRule):
    """最大回撤风控：账户回撤超过阈值时禁止开新仓。

    这是最重要的一道闸门——上一轮修掉的"僵尸止损单凭空做空"如果再发生，
    这道闸门能保证它至少不会继续加仓。
    """
    name = "max_drawdown"
    description = "账户回撤超阈值时禁止开仓"

    def __init__(self, max_drawdown: float = 0.20, **kw) -> None:
        super().__init__(max_drawdown=max_drawdown, **kw)
        self.max_drawdown = float(max_drawdown)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if signal.side != Side.BUY:
            return self._pass("非买入信号")
        dd = state.current_drawdown
        if dd >= self.max_drawdown:
            return self._block(
                f"账户回撤 {dd:.2%} 已达上限 {self.max_drawdown:.2%}，禁止开仓"
            )
        return self._pass(f"回撤 {dd:.2%}")


@register_risk
class PositionLimitRisk(IBaseRiskRule):
    """单标的仓位上限：持仓市值 / 总资产 不得超过阈值。"""
    name = "position_limit"
    description = "单标的持仓占比上限"

    def __init__(self, max_position_pct: float = 0.30, **kw) -> None:
        super().__init__(max_position_pct=max_position_pct, **kw)
        self.max_position_pct = float(max_position_pct)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if signal.side != Side.BUY:
            return self._pass("非买入信号")
        equity = state.equity or (account.cash if account else 0.0)
        if equity <= 0:
            return self._pass("无权益数据")
        ratio = state.position_value / equity
        if ratio >= self.max_position_pct:
            return self._block(
                f"持仓占比 {ratio:.2%} 已达上限 {self.max_position_pct:.2%}"
            )
        return self._pass(f"持仓占比 {ratio:.2%}")


@register_risk
class CashReserveRisk(IBaseRiskRule):
    """现金校验：买入金额不得超过可用资金。"""
    name = "cash_reserve"
    description = "可用资金不足以买入指定数量时拒绝"

    def __init__(self, lot_size: int = 100, fee_rate: float = 0.0003, **kw) -> None:
        super().__init__(lot_size=lot_size, fee_rate=fee_rate, **kw)
        self.lot_size = int(lot_size)
        self.fee_rate = float(fee_rate)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if signal.side != Side.BUY:
            return self._pass("非买入信号")
        price = state.last_price
        if price <= 0:
            return self._block("无有效价格，无法校验资金")
        size = int(signal.meta.get("planned_size") or self.lot_size)
        cost = price * size * (1 + self.fee_rate)
        cash = account.available if account else state.cash
        if cost > cash:
            return self._block(f"资金不足: 需 {cost:.2f}，可用 {cash:.2f}")
        return self._pass(f"需 {cost:.2f}")


@register_risk
class CooldownRisk(IBaseRiskRule):
    """交易冷却：距上次成交不足 N 根K线不再交易，防止震荡行情反复打脸。"""
    name = "cooldown"
    description = "距上次成交的最小间隔（按K线数）"

    def __init__(self, min_bars: int = 1, **kw) -> None:
        super().__init__(min_bars=min_bars, **kw)
        self.min_bars = int(min_bars)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if state.bars_since_last_trade < self.min_bars:
            return self._block(
                f"冷却期内（距上次成交 {state.bars_since_last_trade} 根K线 < {self.min_bars}）"
            )
        return self._pass()


@register_risk
class NewsConfidenceRisk(IBaseRiskRule):
    """新闻置信度风控：低置信度的新闻信号不放行。

    这是接大模型时最实用的一道闸：模型"拿不准"的时候宁可不动。
    """
    name = "news_confidence"
    description = "新闻/LLM 信号的最低置信度要求"

    def __init__(self, min_confidence: float = 0.5, **kw) -> None:
        super().__init__(min_confidence=min_confidence, **kw)
        self.min_confidence = float(min_confidence)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if signal.source not in (SignalSource.NEWS, SignalSource.LLM):
            return self._pass("非新闻信号")
        conf = signal.meta.get("news_confidence")
        if conf is None and ctx is not None:
            conf = ctx.feature("news_confidence")
        if conf is None:
            return self._block("新闻信号缺少置信度，按拒绝处理")
        if float(conf) < self.min_confidence:
            return self._block(
                f"新闻置信度 {float(conf):.2f} 低于阈值 {self.min_confidence:.2f}"
            )
        return self._pass(f"置信度 {float(conf):.2f}")


@register_risk
class MaxHoldBarsRisk(IBaseRiskRule):
    """最长持股周期：超过 N 根K线还没出场则强制允许卖出（防止出场规则失效）。"""
    name = "max_hold_bars"
    description = "最长持股K线数，超时强制允许卖出"

    def __init__(self, max_bars: int = 60, **kw) -> None:
        super().__init__(max_bars=max_bars, **kw)
        self.max_bars = int(max_bars)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        # 持股超时后禁止继续加仓（强制平仓动作由 Task 的超时检查触发，不在风控里下单）
        if signal.side == Side.BUY and state.has_position:
            if state.holding_bars >= self.max_bars:
                return self._block(
                    f"持股 {state.holding_bars} 根K线已达上限 {self.max_bars}，禁止加仓"
                )
        return self._pass()


@register_risk
class DailyLossRisk(IBaseRiskRule):
    """单日亏损上限：当日已实现亏损超过阈值则停止开仓。"""
    name = "daily_loss"
    description = "单日亏损占比上限"

    def __init__(self, max_daily_loss_pct: float = 0.03, **kw) -> None:
        super().__init__(max_daily_loss_pct=max_daily_loss_pct, **kw)
        self.max_daily_loss_pct = float(max_daily_loss_pct)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if signal.side != Side.BUY:
            return self._pass("非买入信号")
        loss_today = float(state.meta.get("daily_realized_pnl", 0.0))
        if loss_today >= 0:
            return self._pass()
        equity = state.equity or 1.0
        pct = abs(loss_today) / equity
        if pct >= self.max_daily_loss_pct:
            return self._block(
                f"当日亏损 {pct:.2%} 已达上限 {self.max_daily_loss_pct:.2%}"
            )
        return self._pass(f"当日亏损 {pct:.2%}")
