#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : manager.py
# @Description : 组合/账户管理：一个任务一份，互不影响
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from app.core.market.types import Account, Position, Side
from app.core.portfolio.sizer import IBaseSizer, build_sizer
from app.utils.logger import logger


@dataclass
class TradeRecord:
    """一笔成交的完整记录，用于统计与复盘。"""
    task_id: str = ""
    symbol: str = ""
    side: str = ""
    size: int = 0
    price: float = 0.0
    fee: float = 0.0
    realized_pnl: float = 0.0
    reason: str = ""
    source: str = ""
    dt: Optional[datetime] = None

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "price": round(self.price, 4),
            "fee": round(self.fee, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "reason": self.reason,
            "source": self.source,
            "dt": self.dt.isoformat() if self.dt else None,
        }


class Portfolio:
    """单个任务的账本。

    关键约束：**不允许净做空**（与上一轮修复的口径一致）。
    卖出数量超过持仓时会被自动截断到持仓量——宁可少卖，也不能凭空出现负仓位。
    """

    def __init__(
        self,
        task_id: str,
        symbol: str,
        initial_cash: float = 100_000.0,
        fee_rate: float = 0.0003,
        sizer: Optional[IBaseSizer] = None,
    ) -> None:
        self.task_id = task_id
        self.symbol = symbol
        self.account = Account(cash=float(initial_cash), initial_cash=float(initial_cash))
        self.position = Position(symbol=symbol)
        self.fee_rate = float(fee_rate)
        self.sizer: IBaseSizer = sizer or build_sizer("percent")

        self.trades: List[TradeRecord] = []
        self.equity_curve: List[tuple] = []   # [(dt, equity)]
        self.peak_equity: float = float(initial_cash)
        self.realized_pnl: float = 0.0

    # ---------------- 市值与权益 ----------------
    def update_price(self, price: float) -> None:
        self.position.last_price = price

    @property
    def market_value(self) -> float:
        return self.position.market_value

    @property
    def equity(self) -> float:
        return self.account.cash + self.market_value

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    def mark(self, dt: Optional[datetime] = None) -> float:
        """记录一次权益快照，返回当前权益。"""
        eq = self.equity
        self.peak_equity = max(self.peak_equity, eq)
        self.equity_curve.append((dt or datetime.now(), eq))
        return eq

    # ---------------- 下单计算 ----------------
    def plan_size(self, side: Side, price: float, strength: float = 1.0, scale: float = 1.0) -> int:
        """计算目标股数（不含资金校验，资金校验在风控里做）。"""
        size = self.sizer.size(
            side=side,
            price=price,
            account=self.account,
            strength=strength,
            position_size=self.position.size,
            scale=scale,
        )
        if side == Side.SELL:
            # 硬约束：卖出量不允许超过持仓，杜绝净做空
            size = max(0, min(int(size), self.position.size))
        return max(0, int(size))

    # ---------------- 成交 ----------------
    def apply_fill(
        self,
        side: Side,
        size: int,
        price: float,
        dt: Optional[datetime] = None,
        reason: str = "",
        source: str = "",
    ) -> TradeRecord:
        """撮合成功后记账。返回成交记录。

        调用前必须确保 size 已经过 sizer/风控校验。
        """
        if size <= 0 or price <= 0:
            return TradeRecord(task_id=self.task_id, symbol=self.symbol, reason="空单忽略")

        if side == Side.SELL:
            size = min(size, self.position.size)  # 二次兜底

        fee = price * size * self.fee_rate
        realized = self.position.apply_fill(side, size, price)

        if side == Side.BUY:
            self.account.cash -= price * size + fee
        else:
            self.account.cash += price * size - fee

        self.realized_pnl += realized
        rec = TradeRecord(
            task_id=self.task_id,
            symbol=self.symbol,
            side=side.value,
            size=size,
            price=price,
            fee=fee,
            realized_pnl=realized,
            reason=reason,
            source=source,
            dt=dt,
        )
        self.trades.append(rec)
        logger.info(
            f"[{self.task_id}] 成交 {side.value} {size}股 @{price:.3f} "
            f"| 实现盈亏 {realized:+.2f} | 现金 {self.account.cash:.2f} "
            f"| 持仓 {self.position.size}"
        )
        return rec

    # ---------------- 快照 ----------------
    def snapshot(self) -> Dict:
        return {
            "task_id": self.task_id,
            "symbol": self.symbol,
            "cash": round(self.account.cash, 2),
            "equity": round(self.equity, 2),
            "market_value": round(self.market_value, 2),
            "position_size": self.position.size,
            "avg_price": round(self.position.avg_price, 4),
            "last_price": round(self.position.last_price, 4),
            "realized_pnl": round(self.realized_pnl, 2),
            "total_pnl": round(self.equity - self.account.initial_cash, 2),
            "drawdown": round(self.drawdown, 4),
            "trade_count": len(self.trades),
        }


__all__ = ["Portfolio", "TradeRecord"]
