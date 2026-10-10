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


# ===========================================================================
# A 股实盘特有的前置风控
# ===========================================================================
# 这几条在回测里无所谓（回测的撮合永远"成交得了"），但**一上实盘就致命**：
# 涨停买不进、跌停卖不出、当天买的当天不能卖。
# 不前置拦掉的话，会产生大量必然失败的下单，把报单额度吃掉、把审计日志淹掉，
# 而且账本会以为自己建了仓。
# ===========================================================================


@register_risk
class TPlusOneRisk(IBaseRiskRule):
    """T+1：当日买入的股票当日不可卖出。

    回测里最容易漏掉的一条 A 股规则。漏掉它的后果很具体：
    策略在盘中做出"买入→当天反手卖出"的动作，回测显示赚钱，
    实盘全部被柜台拒单，然后策略以为仓位已经平了，后续动作全部错位。

    依赖 ``state.meta`` 里的两个字段（由 QuantTask 维护）：
      - ``bought_today``  当日买入的数量
      - ``is_same_day``   当前 bar 是否与上一根 bar 同一天
    """
    name = "t_plus_one"
    description = "A股 T+1：当日买入不可当日卖出"

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if signal.side != Side.SELL:
            return self._pass("非卖出信号")
        # 回测/模拟盘按日线推进时，一根bar就是一个交易日，
        # 不存在"同日"，此时这条规则自然不触发（而不是错误地拦掉所有卖出）
        if not bool(state.meta.get("is_same_day", False)):
            return self._pass("非同一交易日")
        bought = int(state.meta.get("bought_today", 0) or 0)
        if bought <= 0:
            return self._pass()
        sellable = max(0, int(state.position_size) - bought)
        if sellable <= 0:
            return self._block(
                f"T+1：当日买入 {bought} 股全部不可卖（持仓 {state.position_size}）"
            )
        return self._pass(f"T+1：可卖 {sellable} 股（当日买入 {bought} 股冻结）")


@register_risk
class PriceLimitRisk(IBaseRiskRule):
    """涨跌停价格笼子。

    依据当前价与信号方向判断：
      - 要买但已涨停 → 买不进（排队基本排不到）
      - 要卖但已跌停 → 卖不出
    阈值可配，默认 A 股主板 10%（创业板/科创板可用 params 覆盖成 20%）。
    """
    name = "price_limit"
    description = "涨跌停限制：涨停不追买、跌停不追杀"

    def __init__(self, limit_pct: float = 0.10, tolerance: float = 0.005, **kw) -> None:
        super().__init__(limit_pct=limit_pct, tolerance=tolerance, **kw)
        self.limit_pct = float(limit_pct)
        self.tolerance = float(tolerance)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        price = float(state.last_price or 0.0)
        prev_close = float(state.meta.get("prev_close", 0.0) or 0.0)
        if price <= 0 or prev_close <= 0:
            return self._pass("无昨收价，跳过涨跌停判定")
        move = (price - prev_close) / prev_close
        threshold = self.limit_pct - self.tolerance

        if signal.side == Side.BUY and move >= threshold:
            return self._block(
                f"已涨停（{move:+.2%}），买入无法成交"
            )
        if signal.side == Side.SELL and move <= -threshold:
            return self._block(
                f"已跌停（{move:+.2%}），卖出无法成交"
            )
        return self._pass(f"距涨跌停 {move:+.2%}")


@register_risk
class OrderValueLimitRisk(IBaseRiskRule):
    """单笔委托金额上限。防止"手滑把 1000 股打成 100000 股"。

    注意这一层的**局限**：风控跑在仓位计算**之前**，此时还不知道 sizer
    最终会算出多少股。所以这里只能按"信号想投入多少"来估：
    ``可用资金 × strength``。它是粗粒度的早期预警，不是精确闸门。

    精确的那道闸在 :class:`~app.core.execution.live_broker.LiveBroker` ——
    那里拿到的是最终下单量，能算出准确金额并按上限拒单。
    两层配合：这里早拦（省一次报单），那里终拦（保证不会超）。
    """

    name = "order_value_limit"
    description = "单笔委托金额上限（按信号强度估算）"

    def __init__(self, max_value: float = 100_000.0, **kw) -> None:
        super().__init__(max_value=max_value, **kw)
        self.max_value = float(max_value)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if self.max_value <= 0:
            return self._pass()
        if signal.side != Side.BUY:
            # 卖出受"可卖数量"约束，金额上限对它没有意义
            return self._pass("非买入信号")

        strength = max(0.0, min(1.0, float(signal.strength or 0.0)))
        if strength <= 0:
            return self._pass("信号强度为 0")
        available = (
            float(account.available) if account is not None else float(state.cash)
        )
        intent = available * strength
        if intent > self.max_value:
            return self._block(
                f"信号意图投入约 {intent:.0f}（可用 {available:.0f} × 强度 {strength:.2f}）"
                f"超过单笔上限 {self.max_value:.0f}"
            )
        return self._pass(f"意图投入 {intent:.0f} / 上限 {self.max_value:.0f}")


@register_risk
class DailyTradeLimitRisk(IBaseRiskRule):
    """单日成交笔数上限。

    防的是"策略逻辑写错导致盘中刷单"——这类 bug 在回测里只体现为
    手续费偏高，在实盘上会直接触发柜台的风控甚至被限制交易。
    """

    name = "daily_trade_limit"
    description = "单日成交笔数上限"

    def __init__(self, max_trades: int = 20, **kw) -> None:
        super().__init__(max_trades=max_trades, **kw)
        self.max_trades = int(max_trades)

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if self.max_trades <= 0:
            return self._pass()
        n = int(state.meta.get("trades_today", 0) or 0)
        if n >= self.max_trades:
            return self._block(f"当日已成交 {n} 笔，达上限 {self.max_trades} 笔")
        return self._pass(f"当日已成交 {n}/{self.max_trades} 笔")


@register_risk
class SellablePositionRisk(IBaseRiskRule):
    """卖出量不得超过可卖数量（持仓 - 当日买入 - 已挂未成）。"""

    name = "sellable_position"
    description = "卖出量不得超过可卖数量"

    def check(self, signal, state, ctx=None, account=None) -> RiskVerdict:
        if signal.side != Side.SELL:
            return self._pass("非卖出信号")
        held = int(state.position_size or 0)
        if held <= 0:
            return self._block("无持仓可卖")
        frozen = int(state.meta.get("bought_today", 0) or 0) + int(
            state.meta.get("pending_sell", 0) or 0
        )
        sellable = held - frozen
        if sellable <= 0:
            return self._block(
                f"无可卖数量（持仓 {held}，冻结 {frozen}）"
            )
        return self._pass(f"可卖 {sellable}/{held} 股")
