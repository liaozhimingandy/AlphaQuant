#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : fundflow.py
# @Description : Canonical Model —— 个股资金流
#
#                东财给的是"主力/超大/大/中/小"五档净额，Tushare 只有净额总量。
#                标准模型的取舍是：**五档都要有列，但允许为 None** ——
#                宁可让列空着（因子自己判断能不能用），也不要拿总量去填"主力净额"
#                这种看起来一样、口径其实不同的坑。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from app.providers.market.model.enums import Market

#: 资金流标准列
FUND_FLOW_COLUMNS: List[str] = [
    "symbol", "date", "close", "pct_change",
    "main_net", "main_net_pct",
    "super_large_net", "large_net", "medium_net", "small_net",
    "turnover", "amount",
]

#: 五档净额（做"结构"分析时会整体取用）
FLOW_TIERS: List[str] = ["super_large_net", "large_net", "medium_net", "small_net"]


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class FundFlowRow:
    """一只票一天的资金流。"""

    symbol: str
    date: date
    main_net: Optional[float] = None
    main_net_pct: Optional[float] = None
    close: Optional[float] = None
    pct_change: Optional[float] = None
    super_large_net: Optional[float] = None
    large_net: Optional[float] = None
    medium_net: Optional[float] = None
    small_net: Optional[float] = None
    turnover: Optional[float] = None
    amount: Optional[float] = None
    market: Market = Market.CN

    # ---- 血缘 ----
    source: str = ""
    provider: str = ""
    fetched_at: float = field(default_factory=time.time)
    raw_id: str = ""

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "")
        for name in ("main_net", "main_net_pct", "close", "pct_change",
                     *FLOW_TIERS, "turnover", "amount"):
            setattr(self, name, _f(getattr(self, name)))

    @property
    def tiers_available(self) -> bool:
        """五档是否齐全 —— 只有 Tushare 兜底源会缺，因子据此降级。"""
        return all(getattr(self, t) is not None for t in FLOW_TIERS)

    def to_row(self, with_lineage: bool = False) -> Dict[str, Any]:
        row: Dict[str, Any] = {c: getattr(self, c)
                               for c in FUND_FLOW_COLUMNS if c != "symbol"}
        row["symbol"] = self.symbol
        row = {c: row[c] for c in FUND_FLOW_COLUMNS}
        if with_lineage:
            row.update({"source": self.source, "provider": self.provider,
                        "fetched_at": self.fetched_at, "raw_id": self.raw_id})
        return row

    @classmethod
    def from_row(cls, row: Dict[str, Any], **meta: Any) -> "FundFlowRow":
        data = dict(row or {})
        data.update({k: v for k, v in (meta or {}).items() if v is not None})
        known = set(FUND_FLOW_COLUMNS) | {"market", "source", "provider",
                                          "fetched_at", "raw_id"}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_frame(cls, df: Optional[pd.DataFrame], **meta: Any) -> List["FundFlowRow"]:
        if df is None or len(df) == 0:
            return []
        missing = [c for c in ("symbol", "date") if c not in df.columns]
        if missing:
            raise ValueError(f"资金流缺少标准列 {missing}（现有: {list(df.columns)}）")
        return [cls.from_row(r, **meta) for r in df.to_dict("records")]

    @staticmethod
    def to_frame(rows: Sequence["FundFlowRow"],
                 with_lineage: bool = False) -> pd.DataFrame:
        cols = list(FUND_FLOW_COLUMNS)
        if with_lineage:
            cols += ["source", "provider", "fetched_at", "raw_id"]
        return pd.DataFrame([r.to_row(with_lineage) for r in rows], columns=cols)


@dataclass(slots=True)
class FundFlowRequest:
    """一次资金流请求。"""

    symbol: str
    days: Optional[int] = None
    market: Market = Market.CN
    #: 指定只走某个源
    source: str = ""

    def to_params(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "days": self.days}


__all__ = ["FLOW_TIERS", "FUND_FLOW_COLUMNS", "FundFlowRequest", "FundFlowRow"]
