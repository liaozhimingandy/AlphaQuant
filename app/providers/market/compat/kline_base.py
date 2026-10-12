#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : kline_base.py
# @Description : 日线（K线）数据的统一契约
#
#                注册表之前只管"截面数据"（资金流 / 人气榜 / 资金榜），
#                K线一直走 `DataSourceFactory` 那条老链路——它有自己的
#                降级顺序、自己的异常类型，和注册表的权重/熔断/换源是**两套**。
#                要加 mootdx / a-stock-data 这类新源时问题就来了：
#                新源注册进注册表，K线却根本不走注册表，等于白注册。
#
#                所以这里先把"日线长什么样"定成契约：
#                  - 列名与顺序固定（见 :data:`DAILY_COLUMNS`）；
#                  - 适配器只管把自家原始表喂给 :func:`normalize_daily`，
#                    剩下的取值、类型转换、区间裁剪、去重、排序全在这里做。
#
#                这样新增一个 K线源的成本 = 写 20 行适配器 + 一行 register。
#
#                ⚠️ 成交量单位：**各源不一致，这里不做换算**。
#                  baostock / 通达信(mootdx) 给的是「股」，akshare / 腾讯给的是「手」。
#                  换算需要知道每个源的确切口径，猜错就是 100 倍的错；
#                  所以这里原样透传，并在文档中标注。**完整性校验只比对日期与
#                  价格，不比对成交量**——那是唯一安全的做法。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from app.domain.symbol import normalize_symbol
from app.common.logging import logger

#: 日线统一列（顺序即输出顺序）
DAILY_COLUMNS: List[str] = [
    "date", "symbol", "open", "high", "low", "close", "volume", "amount",
]

#: 列名别名。各源叫法五花八门（通达信 vol / 腾讯 无成交额 / akshare 中文列），
#: 集中在一处映射，新增源只管往里加别名，不用改解析逻辑。
COLUMN_ALIASES: Dict[str, Sequence[str]] = {
    "date": ("date", "日期", "trade_date", "datetime", "dt", "时间", "day"),
    "open": ("open", "开盘", "开盘价", "open_price"),
    "high": ("high", "最高", "最高价", "high_price"),
    "low": ("low", "最低", "最低价", "low_price"),
    "close": ("close", "收盘", "收盘价", "close_price"),
    "volume": ("volume", "成交量", "vol", "chengjiaoliang"),
    "amount": ("amount", "成交额", "成交金额", "turnover", "amt"),
}

#: OHLCV 里必须转成 float 的列
_NUMERIC = ("open", "high", "low", "close", "volume", "amount")


def _as_date(value: Any) -> Optional[date]:
    """把 '2024-01-02' / '20240102' / date / datetime / Timestamp 统一成 date。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return None
    # Timestamp / numpy datetime64 走 pandas 解析
    try:
        ts = pd.to_datetime(text, errors="coerce")
        if ts is not None and not pd.isna(ts):
            return ts.date()
    except Exception:
        pass
    raw = text.replace("-", "").replace("/", "").replace(".", "")
    if len(raw) >= 8 and raw[:8].isdigit():
        try:
            return datetime.strptime(raw[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    """按别名找列名：先完全匹配（忽略大小写），再包含匹配。"""
    if df is None or df.empty:
        return None
    cols = {str(c): str(c) for c in df.columns}
    lowered = {c.lower().strip(): c for c in cols}
    for cand in candidates:
        key = str(cand).lower().strip()
        if key in lowered:
            return lowered[key]
    for cand in candidates:
        key = str(cand).lower().strip()
        for low, original in lowered.items():
            if key and key in low:
                return original
    return None


def normalize_daily(
    df: Optional[pd.DataFrame],
    symbol: str,
    start: Any = None,
    end: Any = None,
    required: Sequence[str] = ("date", "open", "high", "low", "close"),
) -> pd.DataFrame:
    """把任意源的日线原始表归一成 :data:`DAILY_COLUMNS`。

    :param start/end: 区间裁剪（含端点），支持 date / 'YYYY-MM-DD' / 'YYYYMMDD'；
        传 None 表示不限。
    :param required: 缺失就报错的列。成交额在部分源（腾讯 K线）里没有，
        所以默认**不要求** amount。
    :raises ValueError: 缺少必需列（通常是接口改版，要让人立刻看见）
    """
    empty = pd.DataFrame(columns=DAILY_COLUMNS)
    if df is None or len(df) == 0:
        return empty

    code = normalize_symbol(symbol)
    src = df.copy()

    mapping: Dict[str, str] = {}
    for target in DAILY_COLUMNS:
        if target == "symbol":
            continue
        found = _col(src, COLUMN_ALIASES.get(target, (target,)))
        if found:
            mapping[found] = target
    if mapping:
        src = src.rename(columns=mapping)

    missing = [c for c in required if c not in src.columns]
    if missing:
        raise ValueError(
            f"日线数据缺少必需列 {missing}（源列: {list(df.columns)}）——"
            f"接口可能已改版，请检查适配器"
        )

    out = pd.DataFrame(index=src.index)
    out["date"] = src["date"].map(_as_date) if "date" in src.columns else None
    out["symbol"] = code
    for col in _NUMERIC:
        if col in src.columns:
            out[col] = pd.to_numeric(src[col], errors="coerce")
        else:
            out[col] = None

    # 停牌/空行：日期或 OHLC 缺一不可，直接丢掉，
    # 否则会往库里写一行"有日期没价格"的假数据。
    out = out.dropna(subset=["date", "open", "high", "low", "close"])

    lo = _as_date(start)
    hi = _as_date(end)
    if lo is not None:
        out = out[out["date"] >= lo]
    if hi is not None:
        out = out[out["date"] <= hi]

    if out.empty:
        return empty
    out = (out.sort_values("date")
              .drop_duplicates(subset=["date"], keep="last")
              .reset_index(drop=True))
    return out[DAILY_COLUMNS]


def market_prefix(symbol: str) -> str:
    """沪深北市场前缀（腾讯/新浪/通达信接口都要带）。"""
    code = normalize_symbol(symbol)
    if code.startswith(("60", "68", "9")):
        return "sh"
    if code.startswith(("8", "43", "92")):
        return "bj"
    return "sz"


def daily_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """转成可直写入库的 dict 列表（date 用 date 对象，便于比对）。"""
    if df is None or df.empty:
        return []
    return df[DAILY_COLUMNS].to_dict(orient="records")


__all__ = [
    "COLUMN_ALIASES",
    "DAILY_COLUMNS",
    "daily_rows",
    "market_prefix",
    "normalize_daily",
]
