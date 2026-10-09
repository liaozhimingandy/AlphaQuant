#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : types.py
# @Description : 领域模型：所有层之间的"共同语言"
#               行情/事件/信号/订单/成交/持仓 全部在这里定义，避免各层各自定义一套
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


# ============================================================
# 枚举
# ============================================================
class Side(str, Enum):
    """买卖方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # 已创建，未提交
    SUBMITTED = "SUBMITTED"  # 已提交券商
    FILLED = "FILLED"        # 已成交
    PARTIAL = "PARTIAL"      # 部分成交
    CANCELLED = "CANCELLED"  # 已撤单
    REJECTED = "REJECTED"    # 被拒绝（风控/资金不足）


class TaskState(str, Enum):
    """任务状态机"""
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class RunMode(str, Enum):
    BACKTEST = "BACKTEST"    # 回测：数据放完就停
    SIMULATE = "SIMULATE"    # 模拟盘：实时行情 + 模拟撮合
    LIVE = "LIVE"            # 实盘：真实券商


class SignalSource(str, Enum):
    """信号来源，用于区分是行情触发还是新闻事件触发"""
    BAR = "bar"
    TICK = "tick"
    NEWS = "news"
    LLM = "llm"
    TIMER = "timer"
    MANUAL = "manual"


# ============================================================
# 行情
# ============================================================
@dataclass
class Bar:
    """一根K线。实时/回测共用同一结构，策略层无需区分数据来源。"""
    symbol: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["dt"] = self.dt.isoformat()
        return d


@dataclass
class Tick:
    symbol: str
    dt: datetime
    price: float
    volume: float = 0.0


# ============================================================
# 新闻（事件通道的核心载体）
# ============================================================
@dataclass
class NewsItem:
    """一条新闻。source 决定来源，symbols 决定它会影响哪些任务。"""
    title: str
    content: str = ""
    url: str = ""
    source: str = "unknown"
    published_at: Optional[datetime] = None
    symbols: list[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """去重指纹：标题+来源。URL 可能带随机参数，不可靠。"""
        import hashlib

        base = f"{self.source}|{self.title.strip()}"
        return hashlib.md5(base.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["published_at"] = (
            self.published_at.isoformat() if self.published_at else None
        )
        return d


@dataclass
class NewsAnalysis:
    """大模型/规则对一条新闻的分析结论。

    score 约定：[-1, 1]，正=利好，负=利空。
    confidence 约定：[0, 1]，低于阈值不触发交易。
    """
    news_fingerprint: str
    symbols: list[str]
    score: float = 0.0
    confidence: float = 0.0
    direction: str = "neutral"   # bullish / bearish / neutral
    reason: str = ""
    analyzer: str = "unknown"
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# 信号与订单
# ============================================================
@dataclass
class Signal:
    """策略产出的交易意图。注意：Signal 不是订单，还要过风控和仓位计算。"""
    symbol: str
    side: Side
    source: SignalSource = SignalSource.BAR
    reason: str = ""
    strength: float = 1.0          # [0,1]，供仓位计算使用
    task_id: str = ""
    ts: Optional[datetime] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["source"] = self.source.value
        d["ts"] = self.ts.isoformat() if self.ts else None
        return d


@dataclass
class Order:
    order_id: str
    task_id: str
    symbol: str
    side: Side
    size: int
    price: float = 0.0             # 0 = 市价
    status: OrderStatus = OrderStatus.PENDING
    reason: str = ""
    created_at: Optional[datetime] = None
    filled_size: int = 0
    filled_price: float = 0.0
    reject_reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        return d


@dataclass
class Position:
    """单个标的的持仓。"""
    symbol: str
    size: int = 0
    avg_price: float = 0.0
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.size * self.last_price

    @property
    def pnl(self) -> float:
        """浮动盈亏（不含费用）"""
        if self.size == 0:
            return 0.0
        return (self.last_price - self.avg_price) * self.size

    def apply_fill(self, side: Side, size: int, price: float) -> float:
        """成交后更新持仓，返回本次实现的盈亏（平仓部分）。"""
        if size <= 0:
            return 0.0
        realized = 0.0
        if side == Side.BUY:
            total = self.size + size
            self.avg_price = (
                (self.avg_price * self.size + price * size) / total if total else 0.0
            )
            self.size = total
        else:
            # 卖出：先平掉已有仓位。这里不允许净做空（与上一轮修复的口径一致）
            closing = min(size, self.size)
            realized = (price - self.avg_price) * closing
            self.size -= closing
            if self.size == 0:
                self.avg_price = 0.0
        self.last_price = price
        return realized


@dataclass
class Account:
    """账户快照。"""
    cash: float = 0.0
    initial_cash: float = 0.0
    frozen: float = 0.0

    @property
    def available(self) -> float:
        return self.cash - self.frozen

    @property
    def total_pnl(self) -> float:
        return self.cash - self.initial_cash
