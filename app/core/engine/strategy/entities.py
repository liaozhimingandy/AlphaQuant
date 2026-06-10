#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
=================================================
    @Project: AlphaQuant
    @File： entities.py
    @Author：liaozhimingandy
    @Email: liaozhimingandy@gmail.com
    @Date：2026/6/3 20:39
    @Desc: 
=================================================
"""
import uuid
from dataclasses import dataclass, field
from enum import Enum


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


class OrderStatus(Enum):
    """
    订单状态

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
    MARGIN_INSUFFICIENT = "margin"


class OrderSide(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"


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

class FactorSignal(Enum):
    """因子标准化输出信号
    LONG: 看多/买入
    SHORT: 看空/卖出
    NEUTRAL: 观望/无信号
    """
    LONG = 1
    SHORT = -1
    NEUTRAL = 0