#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : kline.py
# @Description : Canonical Model —— K 线（标准数据模型）
#
#                为什么必须有它：
#                  baostock 返回中文列、akshare 返回中文列但字段名不同、
#                  通达信返回小写英文列且**没有成交额**、腾讯没有成交额且量纲是"手"。
#                  如果上层直接吃这些 DataFrame，那么"换一个数据源"就等于
#                  "把所有因子重写一遍"。
#
#                标准模型解决的就是这件事：**外部格式 → Kline，只发生在适配器里一次**。
#                上层（因子/策略/回测）永远只看到 Kline，不知道数据是谁给的。
#
#                和 app/data/kline_base.py 的分工：
#                  * ``kline_base`` 是**列式契约**（DAILY_COLUMNS + normalize_daily），
#                    面向"整表搬运"，速度快，适配器和入库都用它；
#                  * 这里是**行式契约**，带完整血缘（source/fetched_at/raw_id），
#                    面向"这一根 K 线是谁给的、什么时候给的"。
#                  两者列完全一致（有测试守着），可无损互转。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from app.providers.market.model.enums import AdjustType, Frequency, Market

#: 标准 K 线列（顺序即输出顺序）。与 app.providers.market.compat.kline_base.DAILY_COLUMNS 保持一致。
KLINE_COLUMNS: List[str] = [
    "date", "symbol", "open", "high", "low", "close", "volume", "amount",
]

#: 血缘列：不参与计算，只在排查"这个价格哪来的"时有用
LINEAGE_COLUMNS: List[str] = ["source", "provider", "fetched_at", "raw_id"]


def _f(value: Any) -> Optional[float]:
    """尽力转 float，转不了给 None（缺失值不该变成 0 混进计算）。"""
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
class Kline:
    """一根标准 K 线。

    ``source/provider/fetched_at/raw_id`` 是**血缘**：发现
    "2025-08-01 茅台收盘价和历史数据库对不上" 时，靠它们一路追回
    "哪次请求、哪个源、原始响应存哪"。
    """

    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    amount: Optional[float] = None

    frequency: Frequency = Frequency.DAY
    adjust: AdjustType = AdjustType.QFQ
    market: Market = Market.CN

    # ---- 血缘 ----
    source: str = ""            # 数据来源（具体接口/路由名，如 akshare_daily）
    provider: str = ""          # 数据源身份（akshare / baostock / mootdx …）
    fetched_at: float = field(default_factory=time.time)
    raw_id: str = ""            # Raw 层存档 id，可回放原始响应

    def __post_init__(self) -> None:
        if not isinstance(self.date, date):
            raise TypeError(f"Kline.date 必须是 date，收到 {type(self.date).__name__}")
        self.open = float(self.open)
        self.high = float(self.high)
        self.low = float(self.low)
        self.close = float(self.close)
        self.volume = _f(self.volume)
        self.amount = _f(self.amount)
        if not isinstance(self.frequency, Frequency):
            self.frequency = Frequency.parse(self.frequency)
        if not isinstance(self.adjust, AdjustType):
            self.adjust = AdjustType.parse(self.adjust)

    # ---------------- 转换 ----------------
    def to_row(self, with_lineage: bool = False) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "date": self.date, "symbol": str(self.symbol),
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume, "amount": self.amount,
        }
        if with_lineage:
            row.update({
                "frequency": self.frequency.value,
                "adjust": self.adjust.value,
                "source": self.source, "provider": self.provider,
                "fetched_at": self.fetched_at, "raw_id": self.raw_id,
            })
        return row

    @classmethod
    def from_row(cls, row: Dict[str, Any], **meta: Any) -> "Kline":
        """从任意 dict 构造（列名走 KLINE_COLUMNS 口径）。"""
        data = dict(row or {})
        data.update({k: v for k, v in (meta or {}).items() if v is not None})
        return cls(
            symbol=str(data.get("symbol") or ""),
            date=data.get("date"),
            open=_f(data.get("open")) or 0.0,
            high=_f(data.get("high")) or 0.0,
            low=_f(data.get("low")) or 0.0,
            close=_f(data.get("close")) or 0.0,
            volume=_f(data.get("volume")),
            amount=_f(data.get("amount")),
            frequency=Frequency.parse(data.get("frequency", Frequency.DAY)),
            adjust=AdjustType.parse(data.get("adjust", AdjustType.QFQ)),
            source=str(data.get("source") or ""),
            provider=str(data.get("provider") or ""),
            fetched_at=data.get("fetched_at") or time.time(),
            raw_id=str(data.get("raw_id") or ""),
        )

    @classmethod
    def from_frame(cls, df: Optional[pd.DataFrame], **meta: Any) -> List["Kline"]:
        """列式 → 行式标准模型。缺必需列直接报错（接口改版要立刻能看见）。"""
        if df is None or len(df) == 0:
            return []
        missing = [c for c in KLINE_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"K线缺少标准列 {missing}（现有: {list(df.columns)}）")
        return [cls.from_row(r, **meta) for r in df[KLINE_COLUMNS].to_dict("records")]

    @staticmethod
    def to_frame(rows: Iterable["Kline"],
                 with_lineage: bool = False) -> pd.DataFrame:
        """行式 → 列式。with_lineage=True 时多带 4 列血缘。"""
        records = [r.to_row(with_lineage=with_lineage) for r in rows]
        cols = KLINE_COLUMNS + (LINEAGE_COLUMNS if with_lineage else [])
        return pd.DataFrame(records, columns=cols)


@dataclass(slots=True)
class KlineRequest:
    """一次 K 线请求。

    路由不只按"能力"选源，还要按**请求内容**选源：
      要 2020 年的日线 → 某些源不给这么长的历史；
      要前复权 → 通达信直接声明做不到（NOT_SUPPORTED，不是失败）。
    所以请求本身必须是结构化对象，而不是一串位置参数。
    """

    symbol: str
    start: Any = None
    end: Any = None
    frequency: Frequency = Frequency.DAY
    adjust: AdjustType = AdjustType.QFQ
    market: Market = Market.CN
    #: 指定只走某个源（排障 / 单测用）。留空 = 交给路由决策。
    source: str = ""

    def to_params(self) -> Dict[str, Any]:
        """转成适配器能直接吃的 kwargs。"""
        return {
            "symbol": self.symbol,
            "start": self.start,
            "end": self.end,
            "adjust": (self.adjust.value
                       if isinstance(self.adjust, AdjustType) else self.adjust),
            "frequency": (self.frequency.value
                          if isinstance(self.frequency, Frequency) else self.frequency),
        }


def kline_frame(rows: Sequence[Kline]) -> pd.DataFrame:
    """``Kline.to_frame`` 的函数式写法（只给标准列，给入库/因子用）。"""
    return Kline.to_frame(rows, with_lineage=False)


def with_lineage(df: pd.DataFrame, *, source: str = "", provider: str = "",
                 fetched_at: Optional[float] = None,
                 raw_id: str = "") -> pd.DataFrame:
    """给标准 K 线表贴上血缘列（不改原有数据的任何一行）。

    为什么是"贴"而不是"重建"：入库路径要的是 DAILY_COLUMNS 那 8 列，
    血缘只在排查问题时才需要，常驻在表里会让所有 groupby/resample 都变重。
    """
    if df is None or len(df) == 0:
        return df
    out = df.copy()
    out["source"] = source
    out["provider"] = provider
    out["fetched_at"] = fetched_at if fetched_at is not None else time.time()
    out["raw_id"] = raw_id
    return out


__all__ = [
    "KLINE_COLUMNS",
    "Kline",
    "KlineRequest",
    "LINEAGE_COLUMNS",
    "kline_frame",
    "with_lineage",
]
