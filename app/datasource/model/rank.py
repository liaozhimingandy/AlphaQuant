#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : rank.py
# @Description : Canonical Model —— 榜单（人气榜 / 资金榜）
#
#                人气榜（东财人气、雪球关注）和资金榜（东财资金流排名）
#                字段并不一致：一个只有名次，一个还带主力净额/净占比。
#                硬塞进同一个 dataclass 会多出一堆永远为 None 的字段，
#                所以这里只固定**所有榜单都有的**四件事，
#                各家多出来的字段（净额、成交额、换手…）放 ``extras``，
#                由调用方按需取 —— 既不丢信息，也不造假列。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from app.datasource.model.enums import Market

#: 榜单标准列（所有榜单至少要有这四列）
RANK_COLUMNS: List[str] = ["symbol", "name", "rank", "pct_change"]


class RankKind(str, Enum):
    """榜单种类。值是项目内部口径，与注册表/路由的 capability 一一对应。"""

    HOT = "hot"      # 人气榜
    FLOW = "flow"    # 资金榜

    @classmethod
    def parse(cls, value) -> "RankKind":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        if text in ("flow", "fund", "资金", "资金榜", "fund_flow_rank", "flow_rank"):
            return cls.FLOW
        return cls.HOT


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
class RankRow:
    """榜单里的一行。"""

    symbol: str
    rank: int = 0
    name: str = ""
    pct_change: Optional[float] = None
    kind: RankKind = RankKind.HOT
    trade_date: Optional[date] = None
    market: Market = Market.CN
    #: 各家多出来的字段（主力净额/净占比/成交额…），按需取
    extras: Dict[str, Any] = field(default_factory=dict)

    # ---- 血缘 ----
    source: str = ""
    provider: str = ""
    fetched_at: float = field(default_factory=time.time)
    raw_id: str = ""

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "")
        try:
            self.rank = int(self.rank or 0)
        except (TypeError, ValueError):
            self.rank = 0
        self.pct_change = _f(self.pct_change)
        if not isinstance(self.kind, RankKind):
            self.kind = RankKind.parse(self.kind)

    # ---------------- 转换 ----------------
    def to_row(self, with_extras: bool = True,
               with_lineage: bool = False) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "symbol": self.symbol, "name": self.name,
            "rank": self.rank, "pct_change": self.pct_change,
        }
        if with_extras:
            row.update(self.extras or {})
        if with_lineage:
            row.update({
                "kind": self.kind.value, "trade_date": self.trade_date,
                "source": self.source, "provider": self.provider,
                "fetched_at": self.fetched_at, "raw_id": self.raw_id,
            })
        return row

    @classmethod
    def from_row(cls, row: Dict[str, Any], **meta: Any) -> "RankRow":
        data = dict(row or {})
        data.update({k: v for k, v in (meta or {}).items() if v is not None})
        known = {"symbol", "rank", "name", "pct_change", "kind", "trade_date",
                 "source", "provider", "fetched_at", "raw_id", "market"}
        extras = {k: v for k, v in data.items() if k not in known}
        return cls(
            symbol=str(data.get("symbol") or ""),
            rank=data.get("rank") or 0,
            name=str(data.get("name") or ""),
            pct_change=_f(data.get("pct_change")),
            kind=RankKind.parse(data.get("kind", RankKind.HOT)),
            trade_date=data.get("trade_date"),
            market=data.get("market") or Market.CN,
            extras=extras,
            source=str(data.get("source") or ""),
            provider=str(data.get("provider") or ""),
            fetched_at=data.get("fetched_at") or time.time(),
            raw_id=str(data.get("raw_id") or ""),
        )

    @classmethod
    def from_frame(cls, df: Optional[pd.DataFrame], **meta: Any) -> List["RankRow"]:
        """列式 → 行式标准模型。

        ``rank`` 缺失时按**返回顺序**补名次：资金榜接口只给主力净额、
        不给名次，而它本来就是按净额降序返回的，顺序即名次。
        """
        if df is None or len(df) == 0:
            return []
        missing = [c for c in ("symbol",) if c not in df.columns]
        if missing:
            raise ValueError(f"榜单缺少标准列 {missing}（现有: {list(df.columns)}）")
        if "rank" not in df.columns:
            df = df.assign(rank=range(1, len(df) + 1))
        return [cls.from_row(r, **meta) for r in df.to_dict("records")]

    @staticmethod
    def to_frame(rows: Sequence["RankRow"], with_lineage: bool = False
                 ) -> pd.DataFrame:
        records = [r.to_row(with_lineage=with_lineage) for r in rows]
        cols = list(RANK_COLUMNS)
        for r in rows:                       # 把各家 extras 的列名也排上
            for key in (r.extras or {}):
                if key not in cols:
                    cols.append(key)
        if with_lineage:
            cols += ["kind", "trade_date", "source", "provider", "fetched_at",
                     "raw_id"]
        return pd.DataFrame(records, columns=cols)


@dataclass(slots=True)
class RankRequest:
    """一次榜单请求。"""

    kind: RankKind = RankKind.HOT
    top: Optional[int] = None
    trade_date: Any = None
    #: 资金榜专用：即时 / 3日 / 5日 / 10日
    rank_kind: str = "即时"
    market: Market = Market.CN
    #: 指定只走某个源（排障 / 单测用）
    source: str = ""

    def to_params(self) -> Dict[str, Any]:
        """转成适配器 kwargs。

        注意资金榜适配器的参数叫 ``rank_kind`` 而不是 ``kind``：
        路由的 ``fetch(capability, ...)`` 里 kind 已经被"榜单种类"占用，
        撞名会让调用直接 TypeError。
        """
        params: Dict[str, Any] = {"top": self.top}
        if self.kind is RankKind.FLOW:
            params["rank_kind"] = self.rank_kind or "即时"
        else:
            params["trade_date"] = self.trade_date
        return params


__all__ = ["RANK_COLUMNS", "RankKind", "RankRequest", "RankRow"]
