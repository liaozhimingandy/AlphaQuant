#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/27 16:34
# @FileName    : strategy.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc
from typing import List

import backtrader as bt

from app.strategy.factor import BaseRule, BaseFactor
from app.utils import logger


# -------------------------- 3. 核心策略模板：所有通用逻辑固化，新策略自动继承 --------------------------
class BaseComposableStrategy(bt.Strategy, abc.ABC):
    """
    可组合策略的核心模板，所有通用逻辑100%固化
    新策略只需要定义：因子列表、买入规则、卖出规则，其他全部自动继承
    """
    params = dict(
        stop_loss_pct=0.05,
        max_position_ratio=0.7,
        cash_buffer=0.05,
        lot_size=100,
        commission_rate=0.0003,
        printlog=True,
    )

    def __init__(self):
        # 子类自动继承的通用状态
        self.order = None
        self.stop_order = None
        self.buy_price = None
        self.buy_total_cost = 0.0
        self.hold_size = 0
        self.frozen_cash = 0.0

        # 子类只需要实现这三个属性，就完成了一个新策略
        self.entry_factors: List[BaseFactor] = self.define_entry_factors()
        self.exit_factors: List[BaseFactor] = self.define_exit_factors()
        self.entry_rule: BaseRule = self.define_entry_rule()
        self.exit_rule: BaseRule = self.define_exit_rule()

    @abc.abstractmethod
    def define_entry_factors(self) -> List[BaseFactor]:
        """子类实现：定义开仓用的所有因子"""
        pass

    @abc.abstractmethod
    def define_exit_factors(self) -> List[BaseFactor]:
        """子类实现：定义平仓用的所有因子"""
        pass

    @abc.abstractmethod
    def define_entry_rule(self) -> BaseRule:
        """子类实现：定义开仓规则（AllRule/AnyRule）"""
        pass

    @abc.abstractmethod
    def define_exit_rule(self) -> BaseRule:
        """子类实现：定义平仓规则（AllRule/AnyRule）"""
        pass

    # -------------------------- 以下所有逻辑100%通用，子类永远不用写 --------------------------
    @property
    def available_cash(self):
        return self.broker.getcash() - self.frozen_cash

    def log(self, txt):
        if not self.p.printlog:
            return
        dt = self.data.datetime.date(0)
        logger.info(f"{dt} | {txt}")

    def _calc_buy_size(self, price: float) -> int:
        total_equity = self.broker.getvalue()
        max_use_cash = total_equity * self.p.max_position_ratio * (1 - self.p.cash_buffer)
        one_lot_cost = price * self.p.lot_size * (1 + self.p.commission_rate)
        lots = int(max_use_cash / one_lot_cost)
        return max(0, lots) * self.p.lot_size

    def _cancel_stop_order(self):
        if self.stop_order is not None:
            self.cancel(self.stop_order)
            self.stop_order = None

    def _close_position(self, reason: str):
        self._cancel_stop_order()
        current_price = self.data.close[0]
        pnl_log = ""
        if self.hold_size > 0 and self.buy_total_cost > 0:
            estimate_sell_total = current_price * self.hold_size * (1 - self.p.commission_rate)
            estimate_pnl = estimate_sell_total - self.buy_total_cost
            estimate_pnl_pct = estimate_pnl / self.buy_total_cost * 100
            pnl_sign = "+" if estimate_pnl >= 0 else ""
            pnl_log = f" | 预估净盈亏（含手续费）：{pnl_sign}{estimate_pnl:.2f}元 | {pnl_sign}{estimate_pnl_pct:.1f}%"
        self.log(f"🔔 {reason} | 全部平仓{pnl_log}")
        self.order = self.sell(size=self.position.size, exectype=bt.Order.Close)

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None

        if order.status == order.Completed:
            executed_size = abs(order.executed.size)
            executed_price = order.executed.price
            if order.isbuy():
                self.buy_price = executed_price
                self.hold_size = executed_size
                self.buy_total_cost = executed_price * executed_size + order.executed.comm
                self.frozen_cash = 0.0
                self.log(
                    f"✅ 买入成交 | 价格={executed_price:.2f} | 数量={executed_size:.0f}份 | "
                    f"手续费={order.executed.comm:.2f} | 持仓总成本={self.buy_total_cost:.2f}元 | "
                    f"仓位={self.position.size*executed_price/self.broker.getvalue()*100:.1f}%"
                )
                if self.p.stop_loss_pct > 0:
                    stop_price = (self.buy_total_cost / executed_size) * (1 - self.p.stop_loss_pct)
                    self.stop_order = self.sell(size=self.position.size, exectype=bt.Order.Stop, price=stop_price)
                    self.log(f"🛡️ 止损单已挂出 | 止损价={stop_price:.2f}")
            else:
                pnl_log = ""
                if self.hold_size > 0 and self.buy_total_cost > 0:
                    actual_sell_total = order.executed.value - order.executed.comm
                    actual_pnl = actual_sell_total - self.buy_total_cost
                    actual_pnl_pct = actual_pnl / self.buy_total_cost * 100
                    pnl_sign = "+" if actual_pnl >= 0 else ""
                    pnl_log = f" | 实际净盈亏（含双边手续费）：{pnl_sign}{actual_pnl:.2f}元 | {pnl_sign}{actual_pnl_pct:.1f}%"
                sell_type = "🛡️ 止损自动平仓成交" if order == self.stop_order else "❌ 主动平仓成交"
                self.frozen_cash = 0.0
                self.buy_price = None
                self.buy_total_cost = 0.0
                self.hold_size = 0
                self.log(f"{sell_type} | 价格={executed_price:.2f} | 数量={executed_size:.0f}份 | 手续费={order.executed.comm:.2f}{pnl_log}")
        elif order.status in [order.Canceled, order.Rejected, order.Margin]:
            self.frozen_cash = 0.0
            if order == self.stop_order:
                self.stop_order = None
            self.log(f"⚠️ 订单异常 | 状态={order.getstatusname()} | 冻结资金已退回")

    def next(self):
        current_price = self.data.close[0]
        position_size = self.position.size
        position_value = position_size * current_price
        total_equity = self.broker.getvalue()
        position_ratio = (position_value / total_equity * 100) if total_equity else 0.0

        hold_pnl_log = ""
        if self.hold_size > 0 and self.buy_total_cost > 0:
            current_sell_total = current_price * self.hold_size * (1 - self.p.commission_rate)
            hold_pnl = current_sell_total - self.buy_total_cost
            hold_pnl_pct = hold_pnl / self.buy_total_cost * 100
            pnl_sign = "+" if hold_pnl >= 0 else ""
            hold_pnl_log = f" | 持仓浮盈亏：{pnl_sign}{hold_pnl:.2f}元 | {pnl_sign}{hold_pnl_pct:.1f}%"
        self.log(
            f"每日监控 | 价格={current_price:.2f} | 持仓={position_size:.0f}份 | "
            f"总资产={total_equity:.2f} | 仓位={position_ratio:.1f}% | 可用资金={self.available_cash:.2f}{hold_pnl_log}"
        )

        if self.order is not None:
            return

        # 平仓逻辑：自动执行子类定义的平仓规则
        if self.position:
            if self.exit_rule.is_satisfied():
                self._close_position("平仓规则触发")
                return
            if self.buy_total_cost > 0 and self.p.stop_loss_pct > 0:
                current_sell_total = current_price * self.hold_size * (1 - self.p.commission_rate)
                pnl_pct = (current_sell_total - self.buy_total_cost) / self.buy_total_cost
                if pnl_pct <= -self.p.stop_loss_pct:
                    self._close_position(f"手工止损触发 | 浮亏={pnl_pct * 100:.1f}%")
            return

        # 开仓逻辑：自动执行子类定义的开仓规则
        if self.entry_rule.is_satisfied():
            buy_size = self._calc_buy_size(current_price)
            if buy_size <= 0:
                self.log("买入信号出现，但可用资金不足，跳过")
                return
            need_cash = buy_size * current_price * (1 + self.p.commission_rate)
            self.frozen_cash = need_cash
            self.log(
                f"🔔 买入信号触发 | 价格={current_price:.2f} | 买入={buy_size}份 | "
                f"预计仓位={buy_size*current_price/self.broker.getvalue()*100:.1f}%"
            )
            self.order = self.buy(size=buy_size, exectype=bt.Order.Close)

def main():
    pass

if __name__ == '__main__':
    main()
