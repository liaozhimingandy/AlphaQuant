#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:47
# @FileName    : base.py
# @Description : 调度层，只负责调度
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc
from typing import Optional, Dict, Any, List

from app.core.engine.strategy.entities import Account, Position, Order, Bar, TradeSignal, OrderSide, OrderStatus
from app.core.engine.strategy.factor import IFactor, FactorSignal
from app.utils.logger import logger

class IBaseStrategy(abc.ABC):
    # 策略基础信息
    name: str = "base_strategy"
    version: str = "1.0.0"

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        self.symbol = symbol
        self.config = config or {}
        self.account = Account()
        self.position = Position(symbol=symbol)
        self.active_order: Optional[Order] = None
        self.stop_order: Optional[Order] = None
        self.buy_price: float = 0.0
        self.close_prices: List[float] = [] # 存放历史价格

        self.factors: List[IFactor] = []  # 注册的因子


    def on_init(self) -> None:
        """策略初始化"""
        pass


    def on_bar(self, bar: Bar) -> None:
        """一次K线,执行一次"""
        # 保存计算过的数据
        logger.debug(
            f"每日监控 | 日期:{bar.timestamp} | 价格={bar.close:.2f} | 持仓={self.position.size:.0f}股 | "
            f"持仓市值={self.position.size*bar.close:.2f} | 总资产={self.account.cash:.2f} | "
            f"可用资金={self.account.cash:.2f} | 仓位=0.0% | "
            f"冻结资金={self.account.frozen_cash:.2f}"
        )
        self.close_prices.append(bar.close)
        signal = self.generate_signal(bar)
        if signal.signal == 1:
            self.active_order = self.buy(bar)
        elif signal.signal == -1:
            self.active_order = self.close_position(bar)
        if self.active_order:
            self.on_order_update(self.active_order)


    def on_stop(self) -> None:
        """策略停止"""
        pass

    def generate_signal(self, bar: Bar) -> TradeSignal:
        """生成交易信号"""
        signal = self._aggregate_factor_signals(bar)
        if signal == FactorSignal.LONG:
            return TradeSignal(1, "+")
        elif signal == FactorSignal.SHORT:
            return TradeSignal(-1, "-")
        return TradeSignal(0, "空")


    def on_order_update(self, order: Order) -> None:
        """
        【引擎自动调用】订单状态变更时触发
        触发时机：订单 成交/取消/拒绝 后
        """
        if order.status == OrderStatus.CREATED and order.side == OrderSide.BUY:
            logger.info(f'✅ 买入成交 | 价格={order.price:.2f} | 数量={order.size:.0f}股')
            self.buy_price = order.price
            self.account.cash -= order.price * order.size
            self.position.size += order.size
            self.account.frozen_cash = self.account.cash
        elif order.status == OrderStatus.CREATED and order.side == OrderSide.SELL:
            logger.info(f'❌ 卖出成交 | 价格={order.price:.2f} | 数量={order.size:.0f}股')
            self.buy_price = 0.0
            self.account.cash += order.price * order.size
            self.position.size -= order.size
            self.account.frozen_cash = 0
        self.active_order = None


    def add_factor(self, factor: IFactor) -> None:
        """
        自由添加因子！支持无限组合
        例：add_factor(MaFactor()) + add_factor(MacdFactor())
        """
        factor.on_init()
        self.factors.append(factor)
        logger.info(f"✅ 因子已加载: {factor.name} | 策略: {self.name}")

    def _aggregate_factor_signals(self, bar: Bar) -> FactorSignal:
        """
        聚合所有因子的信号（可自定义规则：全部满足/任一满足/加权）
        默认规则：所有因子 同时看多=买入，同时看空=卖出
        你可以随意改聚合逻辑！
        """
        if not self.factors:
            return FactorSignal.NEUTRAL

        # 获取所有因子信号
        signals = [factor.on_bar(bar) for factor in self.factors]

        # 聚合规则1：全部看多 → 买入
        if all(s == FactorSignal.LONG for s in signals):
            return FactorSignal.LONG
        # 聚合规则2：全部看空 → 卖出
        if all(s == FactorSignal.SHORT for s in signals):
            return FactorSignal.SHORT

        return FactorSignal.NEUTRAL

    def buy(self, bar: Bar) -> Optional[Order]:
        """标准买入接口"""
        size = self._calc_buy_size(bar.close)
        if self.account.cash < bar.close * size:
            return None
        self.account.frozen_cash = bar.close * size
        # logger.info(f"买入 - {self.symbol} - 价格={price} - 数量={size}")
        return Order(order_id="buy_001", side=OrderSide.BUY, price=bar.close, size=size, status=OrderStatus.CREATED)

    def sell(self, bar: Bar) -> Optional[Order]:
        """标准卖出接口"""
        if self.position.size <= 0 :
            return None
        # logger.info(f"卖出 | {self.symbol} 价格={price} 数量={size}")
        return Order(order_id="sell_001", side=OrderSide.SELL, price=bar.close, size=self.position.size, status=OrderStatus.CREATED)

    def close_position(self, bar: Bar) -> Optional[Order]:
        """清仓"""
        return self.sell(bar)

    def cancel_order(self, order: Order) -> None:
        """取消订单"""
        if order:
            order.status = OrderStatus.CANCELED
            self.account.frozen_cash = 0.0

    # ===================== 【耗时任务调度】可插拔 =====================
    def submit_task(self, func, *args, **kwargs):
        """提交耗时任务到调度器（报表/数据/模型训练）"""
        scheduler = self.config.get("scheduler")
        if scheduler:
            scheduler.submit(func, *args, **kwargs)

    def _calc_buy_size(self, close) -> float | int:
        pass