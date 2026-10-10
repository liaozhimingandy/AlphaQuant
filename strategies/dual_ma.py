#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例：双均线策略（回测 + 实盘两条链路的写法）。

这个文件演示了两件事：
  1. 怎么给**回测链路**（backtrader）写一个策略
  2. 怎么给**实盘链路**（事件引擎）写一个策略

两者放在同一个文件里，是刻意的：同一个想法应该在两条链路上用同一套参数，
而不是"回测里用 5/20，实盘里手滑写成 5/30"。

改完本文件后：
    python main.py strategies --reload      # 本地生效
    python main.py ctl strategies           # 运行中的服务热重载
"""

from __future__ import annotations

from typing import Any, Optional

import backtrader as bt

from app.core.factor.context import FactorContext
from app.core.market.types import Side, Signal, SignalSource
from app.core.strategy.base import IBaseStrategy


# ===========================================================================
# 一、回测链路（backtrader）
# ===========================================================================
class DualMaStrategy(bt.Strategy):
    """双均线 + 趋势过滤。

    与框架自带的 ``ma_cross`` 的区别：这里刻意**不挂止损单**。
    backtrader 在"同一根K线内挂单又撤单"时会留下僵尸单，
    需要额外的清理逻辑（见 app/strategy/ma_cross.py）。
    示例保持最简，把风险控制交给 stop_loss_pct 的手工判断。
    """

    STRATEGY_NAME = "dual_ma"

    params = dict(
        fast=5,
        slow=20,
        trend=60,
        stop_loss_pct=0.05,
        lot_size=100,
        cash_buffer=0.95,
        printlog=False,
    )

    def __init__(self) -> None:
        self.ma_fast = bt.indicators.SMA(self.data.close, period=self.p.fast)
        self.ma_slow = bt.indicators.SMA(self.data.close, period=self.p.slow)
        self.ma_trend = bt.indicators.SMA(self.data.close, period=self.p.trend)
        self.cross = bt.indicators.CrossOver(self.ma_fast, self.ma_slow)
        self.order = None
        self.buy_price: Optional[float] = None

    # ------------------------------------------------------------------
    def _trend_up(self) -> bool:
        if len(self) < self.p.trend:
            return False
        return self.ma_trend[-1] < self.ma_trend[0] < self.data.close[0]

    def _size(self, price: float) -> int:
        cash = float(self.broker.getcash()) * float(self.p.cash_buffer)
        lot = max(1, int(self.p.lot_size))
        return max(0, int(cash / price / lot)) * lot

    def log(self, msg: str) -> None:
        if self.p.printlog:
            print(f"{self.data.datetime.date(0)} | {msg}")

    def notify_order(self, order) -> None:
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status == order.Completed:
            if order.isbuy():
                self.buy_price = float(order.executed.price)
                self.log(f"买入 {abs(order.executed.size):.0f} 股 @ {order.executed.price:.3f}")
            else:
                self.buy_price = None
                self.log(f"卖出 {abs(order.executed.size):.0f} 股 @ {order.executed.price:.3f}")
        self.order = None

    def next(self) -> None:
        if self.order is not None:
            return
        price = float(self.data.close[0])

        if self.position:
            if self.buy_price and self.p.stop_loss_pct:
                pnl = (price - self.buy_price) / self.buy_price
                if pnl <= -float(self.p.stop_loss_pct):
                    self.log(f"止损 | 浮亏 {pnl * 100:.2f}%")
                    self.order = self.close()
                    return
            if self.cross < 0:
                self.log("死叉 → 平仓")
                self.order = self.close()
            return

        if len(self) < self.p.slow or not self._trend_up():
            return
        if self.cross > 0:
            size = self._size(price)
            if size > 0:
                self.log(f"金叉 → 买入 {size} 股")
                self.order = self.buy(size=size)


# ===========================================================================
# 二、实盘链路（事件引擎）
# ===========================================================================
class DualMaComboStrategy(IBaseStrategy):
    """与上面同一套逻辑的实盘版。

    两者必须用**同样的判据**：都用「上一根 vs 当前根」的均线关系判交叉，
    而不是"当前 fast>slow 就算金叉"（那会在趋势中反复触发）。
    """

    STRATEGY_NAME = "dual_ma_combo"
    name = "dual_ma_combo"
    description = "双均线交叉（可回测可实盘）"

    def __init__(self, fast: int = 5, slow: int = 20, trend: int = 60,
                 strength: float = 1.0, **params: Any) -> None:
        super().__init__(fast=fast, slow=slow, trend=trend, strength=strength, **params)
        self.fast = int(fast)
        self.slow = int(slow)
        self.trend = int(trend)
        self.strength = float(strength)

    # ------------------------------------------------------------------
    def _cross(self, ctx: FactorContext) -> int:
        """返回 1 = 金叉 / -1 = 死叉 / 0 = 无。

        判据：上一根 fast<=slow 且当前 fast>slow → 金叉。反之死叉。
        """
        now_f = ctx.ind("sma", period=self.fast)
        now_s = ctx.ind("sma", period=self.slow)
        prev = ctx.previous(1)
        if prev is None:
            return 0
        pre_f = prev.ind("sma", period=self.fast)
        pre_s = prev.ind("sma", period=self.slow)
        if None in (now_f, now_s, pre_f, pre_s):
            return 0
        if pre_f <= pre_s and now_f > now_s:
            return 1
        if pre_f >= pre_s and now_f < now_s:
            return -1
        return 0

    def _trend_up(self, ctx: FactorContext) -> bool:
        now_t = ctx.ind("sma", period=self.trend)
        prev = ctx.previous(1)
        pre_t = prev.ind("sma", period=self.trend) if prev else None
        close = ctx.close
        if None in (now_t, pre_t) or close != close:  # close != close 用于过滤 NaN
            return False
        return pre_t < now_t < close

    def decide(
        self,
        ctx: FactorContext,
        state: Any,
        source: SignalSource = SignalSource.BAR,
        event: Any = None,
    ) -> Optional[Signal]:
        # 数据不足（均线预热期）→ 什么都不做。这也是"没有信号"的正常情形。
        if ctx.bar_count < self.trend + 1:
            return None

        cross = self._cross(ctx)

        if not state.has_position:
            if cross > 0 and self._trend_up(ctx):
                return self.make_signal(
                    state, Side.BUY, source,
                    reason=f"双均线金叉({self.fast}/{self.slow})+趋势向上",
                    strength=self.strength,
                )
            return None

        if cross < 0:
            return self.make_signal(
                state, Side.SELL, source,
                reason=f"双均线死叉({self.fast}/{self.slow})",
                strength=1.0,
            )
        return None
