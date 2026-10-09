#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : broker.py
# @Description : 撮合层：订单生命周期管理 + 滑点/费用模拟
#               回测/模拟盘共用同一套撮合语义，实盘只需换一个 Broker 实现
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import itertools
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from app.core.market.types import Order, OrderStatus, Side
from app.core.portfolio.manager import Portfolio, TradeRecord
from app.utils.logger import logger


class IBaseBroker:
    """撮合接口。"""

    def submit(self, order: Order) -> Order:
        raise NotImplementedError

    def cancel(self, order_id: str) -> bool:
        raise NotImplementedError

    @property
    def open_orders(self) -> List[Order]:
        return []


class SimulatedBroker(IBaseBroker):
    """即时成交的模拟撮合。

    设计取舍：
      - 不做订单薄/排队，收到即成交（对本项目的"事件触发交易"场景足够）
      - **没有挂单概念**——这直接消除上一轮"僵尸止损单"那类问题：
        撤不掉的挂单是回测/实盘最常见的隐性缺陷来源
      - 滑点按成交价百分比计算，买入抬高、卖出压低
    """

    _seq = itertools.count(1)

    def __init__(
        self,
        portfolio: Portfolio,
        slippage: float = 0.0005,
        fee_rate: float = 0.0003,
    ) -> None:
        self.portfolio = portfolio
        self.slippage = float(slippage)
        self.fee_rate = float(fee_rate)
        self.orders: Dict[str, Order] = {}
        self._last_price: float = 0.0

    def set_price(self, price: float) -> None:
        """更新可成交价格。实时场景由行情驱动，回测由当前K线驱动。"""
        self._last_price = float(price)
        self.portfolio.update_price(price)

    @property
    def open_orders(self) -> List[Order]:
        # 即时成交，不存在未结订单
        return []

    def new_order_id(self) -> str:
        return f"{next(self._seq):06d}-{uuid.uuid4().hex[:6]}"

    def create_order(
        self,
        symbol: str,
        side: Side,
        size: int,
        task_id: str = "",
        reason: str = "",
        source: str = "",
    ) -> Order:
        return Order(
            order_id=self.new_order_id(),
            task_id=task_id,
            symbol=symbol,
            side=side,
            size=int(size),
            price=self._last_price,
            status=OrderStatus.PENDING,
            reason=reason,
            created_at=datetime.now(),
            meta={"source": source},
        )

    def submit(self, order: Order) -> Order:
        """提交并立即撮合。size<=0 或价格无效时直接拒绝，不产生成交。"""
        self.orders[order.order_id] = order

        if order.size <= 0:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "下单数量为 0"
            logger.warning(f"[{order.task_id}] 订单被拒: {order.reject_reason}")
            return order

        price = self._last_price or order.price
        if price <= 0:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "无有效成交价"
            logger.warning(f"[{order.task_id}] 订单被拒: {order.reject_reason}")
            return order

        fill_price = self._apply_slippage(price, order.side)
        rec: TradeRecord = self.portfolio.apply_fill(
            side=order.side,
            size=order.size,
            price=fill_price,
            dt=order.created_at,
            reason=order.reason,
            source=str(order.meta.get("source", "")),
        )

        order.status = OrderStatus.FILLED
        order.filled_size = rec.size
        order.filled_price = fill_price
        return order

    def cancel(self, order_id: str) -> bool:
        order = self.orders.get(order_id)
        if order is None or order.status != OrderStatus.PENDING:
            return False
        order.status = OrderStatus.CANCELLED
        return True

    def _apply_slippage(self, price: float, side: Side) -> float:
        if side == Side.BUY:
            return price * (1 + self.slippage)
        return price * (1 - self.slippage)


__all__ = ["IBaseBroker", "SimulatedBroker"]
