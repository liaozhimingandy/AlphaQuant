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
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List

from utils.logger import logger


# --------------------- 枚举定义 ---------------------
class OrderStatus(Enum):

    """订单状态

    | 状态        | 含义    | 什么时候触发              |
        | --------- | ----- | ------------------- |
        | Created   | 已创建   | 你刚调用 `buy()/sell()` |
        | Submitted | 已提交   | 提交给 broker          |
        | Accepted  | 已接受   | broker 接受订单         |
        | Partial   | 部分成交  | 只成交了一部分             |
        | Completed | 完全成交  | 全部成交完成              |
        | Canceled  | 已取消   | 主动 cancel           |
        | Expired   | 已过期   | 订单过期                |
        | Margin    | 保证金不足 | 资金不够                |
        | Rejected  | 被拒绝   | broker 拒单           |
    """
    CREATED = field(default="created", doc="已创建")
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    MARGIN_INSUFFICIENT = "margin_insufficient"

class OrderSide(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"

# --------------------- 核心数据结构 ---------------------
@dataclass
class Bar:
    """K线数据（全框架通用）"""
    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class Order:
    """订单"""
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()), doc="订单唯一标识")
    side: OrderSide = field(default=OrderSide.BUY, doc="")
    price: float = field(default=0.0, doc="价格")
    size: float= field(default=0, doc="仓位")
    status: OrderStatus = field(default=OrderStatus.CREATED, doc="状态")
    commission: float = field(default=0.0, doc="交易费率")

@dataclass
class Position:
    """持仓"""
    symbol: str
    size: float = 0.0
    avg_price: float = 0.0

@dataclass
class Account:
    """账户"""
    cash: float = 100000.0
    frozen_cash: float = 0.0
    total_assets: float = 100000.0

@dataclass
class TradeSignal:
    """交易信号"""
    signal: int  # 1=买入, -1=卖出, 0=持仓
    reason: str = ""


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

        self.factors: List[IFactor] = []  # 注册的因子

    @abc.abstractmethod
    def on_init(self) -> None:
        """策略初始化"""
        pass

    @abc.abstractmethod
    def on_bar(self, bar: Bar) -> None:
        """K线推送（核心逻辑）"""
        pass

    @abc.abstractmethod
    def on_stop(self) -> None:
        """策略停止"""
        pass

    @abc.abstractmethod
    def generate_signal(self, bar: Bar) -> TradeSignal:
        """生成交易信号"""
        pass

    @abc.abstractmethod
    def on_order_update(self, order: Order) -> None:
        """
        【引擎自动调用】订单状态变更时触发
        触发时机：订单 成交/取消/拒绝 后
        """
        pass

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

    # ===================== 【标准交易API】子类直接调用 =====================
    def buy(self, price: float, size: float) -> Optional[Order]:
        """标准买入接口"""
        if size <= 0 or self.account.cash < price * size:
            return None
        self.account.frozen_cash = price * size
        logger.info(f"买入 | {self.symbol} 价格={price} 数量={size}")
        return Order(order_id="buy_001", side=OrderSide.BUY, price=price, size=size, status=OrderStatus.CREATED)

    def sell(self, price: float, size: float) -> Optional[Order]:
        """标准卖出接口"""
        if size <= 0 or self.position.size < size:
            return None
        logger.info(f"卖出 | {self.symbol} 价格={price} 数量={size}")
        return Order(order_id="sell_001", side=OrderSide.SELL, price=price, size=size, status=OrderStatus.CREATED)

    def close_position(self) -> Optional[Order]:
        """清仓"""
        return self.sell(price=0, size=self.position.size)

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