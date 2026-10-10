#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : marketx_repository.py
# @Description : 资金流 / 筹码 / 涨停池 / 题材 / 评分 的仓储
#
#                写入一律 **upsert**（按 symbol+date 唯一键）：
#                同一天重复采集必须覆盖而不是追加 —— 否则回看"某天主力净流入多少"
#                会得到一堆互相矛盾的行。
#
#                查询一律自带 `available` 语义：**查不到就返回空**，
#                上层按"该维度不可用"处理，绝不能用 0 顶替。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import (
    ChipDistribution,
    FundFlow,
    LimitUpPool,
    MacroIndicator,
    MarketBreadth,
    MarketNews,
    StockFactorValue,
    StockFundamental,
    StockScore,
    StockTheme,
    ThemeFlow,
)


def _rows_to_dicts(df: Any) -> List[Dict[str, Any]]:
    if df is None:
        return []
    if isinstance(df, pd.DataFrame):
        return [dict(r) for _, r in df.iterrows()]
    if isinstance(df, list):
        return [dict(r) for r in df]
    raise TypeError(f"不支持的数据类型: {type(df).__name__}")


def _date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def _upsert(session: Session, model: Any, rows: Sequence[Dict[str, Any]],
            key_fields: Sequence[str]) -> int:
    if not rows:
        return 0
    stmt = insert(model).values(list(rows))
    updatable = {
        c.name: getattr(stmt.excluded, c.name)
        for c in model.__table__.columns
        if c.name not in key_fields and c.name != "id"
    }
    stmt = stmt.on_conflict_do_update(index_elements=list(key_fields), set_=updatable)
    session.execute(stmt)
    return len(rows)


def _clean(model: Any, row: Dict[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in fields:
        v = row.get(f)
        if f == "date":
            v = _date(v)
        out[f] = v
    return out


# ===========================================================================
# 写入
# ===========================================================================
_FUND_FLOW_FIELDS = ("symbol", "date", "close", "pct_change", "turnover", "amount",
                     "main_net", "main_net_pct", "super_large_net", "large_net",
                     "medium_net", "small_net", "updated_at")
_CHIP_FIELDS = ("symbol", "date", "avg_cost", "profit_ratio", "cost_90_low",
                "cost_90_high", "cost_70_low", "cost_70_high",
                "concentration_90", "concentration_70", "peak_price", "updated_at")
_LIMITUP_FIELDS = ("date", "symbol", "name", "pct_change", "close", "amount",
                   "seal_amount", "first_seal_at", "last_seal_at", "broken_times",
                   "limit_stat", "continuous", "industry", "updated_at")
_THEME_FIELDS = ("date", "board", "kind", "pct_change", "turnover", "main_net",
                 "up_count", "down_count", "leading_stock", "rank", "updated_at")
_STOCK_THEME_FIELDS = ("symbol", "board", "kind", "source", "date", "updated_at")
_FUNDAMENTAL_FIELDS = (
    "symbol", "date", "report_date", "pe", "pb", "ps", "dividend_yield",
    "market_cap", "float_market_cap", "revenue_yoy", "profit_yoy", "roe",
    "gross_margin", "net_margin", "debt_ratio", "bps", "updated_at",
)


def save_fund_flow(df: Any) -> int:
    rows = [_clean(FundFlow, r, _FUND_FLOW_FIELDS) for r in _rows_to_dicts(df)]
    rows = [r for r in rows if r.get("symbol") and r.get("date")]
    with SessionLocal() as s:
        n = _upsert(s, FundFlow, rows, ["symbol", "date"])
        s.commit()
    return n


def save_chip(df: Any) -> int:
    rows = [_clean(ChipDistribution, r, _CHIP_FIELDS) for r in _rows_to_dicts(df)]
    rows = [r for r in rows if r.get("symbol") and r.get("date")]
    with SessionLocal() as s:
        n = _upsert(s, ChipDistribution, rows, ["symbol", "date"])
        s.commit()
    return n


def save_limitup(df: Any) -> int:
    rows = [_clean(LimitUpPool, r, _LIMITUP_FIELDS) for r in _rows_to_dicts(df)]
    rows = [r for r in rows if r.get("symbol") and r.get("date")]
    with SessionLocal() as s:
        n = _upsert(s, LimitUpPool, rows, ["date", "symbol"])
        s.commit()
    return n


def save_themes(df: Any) -> int:
    rows = [_clean(ThemeFlow, r, _THEME_FIELDS) for r in _rows_to_dicts(df)]
    rows = [r for r in rows if r.get("board") and r.get("date")]
    with SessionLocal() as s:
        n = _upsert(s, ThemeFlow, rows, ["date", "kind", "board"])
        s.commit()
    return n


def save_stock_themes(pairs: Any) -> int:
    """写入个股↔题材映射。传 DataFrame 或 list[dict]。"""
    rows = []
    for r in _rows_to_dicts(pairs):
        row = _clean(StockTheme, r, _STOCK_THEME_FIELDS)
        if row.get("symbol") and row.get("board"):
            row.setdefault("kind", "concept")
            row.setdefault("source", "unknown")
            rows.append(row)
    if not rows:
        return 0
    with SessionLocal() as s:
        n = _upsert(s, StockTheme, rows, ["symbol", "board"])
        s.commit()
    return n


def save_fundamentals(rows: Any) -> int:
    """写入基本面。传 DataFrame / list[dict] / 单个 dict 都行。

    ``report_date`` 也要能是日期 —— 季频数据用它标记归属报告期，
    不记清楚就会把三季报当成"今天才知道"，回看时是未来函数。
    """
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for r in _rows_to_dicts(rows):
        row = _clean(StockFundamental, r, _FUNDAMENTAL_FIELDS)
        row["report_date"] = _date(row.get("report_date"))
        if row.get("symbol") and row.get("date"):
            out.append(row)
    if not out:
        return 0
    # 同一天只留最新一条：估值与财务是两个接口拼出来的，可能各来一次
    merged: Dict[tuple, Dict[str, Any]] = {}
    for r in out:
        k = (r["symbol"], r["date"])
        prev = merged.get(k)
        if prev is None:
            merged[k] = r
            continue
        merged[k] = {**prev, **{k2: v for k2, v in r.items() if v is not None}}
    with SessionLocal() as s:
        n = _upsert(s, StockFundamental, list(merged.values()), ["symbol", "date"])
        s.commit()
    return n


def save_scores(results: Sequence[Any], score_date: Optional[Any] = None,
                source: str = "") -> int:
    """保存综合评分结果。

    接受 :class:`app.core.score.engine.ScoreResult` 或等价 dict。
    """
    rows: List[Dict[str, Any]] = []
    for r in results:
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        day = _date(d.get("date") or score_date) or date.today()
        rows.append({
            "date": day,
            "symbol": d.get("symbol"),
            "total_score": d.get("total"),
            "rank": d.get("rank"),
            "grade": d.get("grade") or "",
            "dims_json": _dump(d.get("dims")),
            "reasons_json": _dump(d.get("reasons")),
            "available_json": _dump(d.get("available")),
            "source": d.get("source") or source or "",
            "created_at": datetime.now(),
        })
    rows = [r for r in rows if r.get("symbol")]
    if not rows:
        return 0
    with SessionLocal() as s:
        n = _upsert(s, StockScore, rows, ["date", "symbol"])
        s.commit()
    return n


# ===========================================================================
# 通用因子值（新增数据类别不用改这里）
# ===========================================================================
def save_factor_values(rows: Any, category: str = "") -> int:
    """写通用因子值。

    接受 dict / list[dict] / DataFrame。每行需要 ``symbol``、``date``、
    ``name``、``value``；``category`` 可逐行给，也可统一由参数给。

    设计上刻意**不校验指标名**：新增一类数据不该需要来这里登记白名单，
    否则"通用表"就退化成了另一套硬编码。
    """
    if isinstance(rows, dict):
        rows = [rows]
    out: List[Dict[str, Any]] = []
    for r in _rows_to_dicts(rows):
        day = _date(r.get("date"))
        sym = r.get("symbol")
        name = r.get("name") or r.get("indicator")
        if not (day and sym and name):
            continue
        out.append({
            "symbol": str(sym), "date": day,
            "category": str(r.get("category") or category or "misc"),
            "name": str(name),
            "value": _num_or_none(r.get("value")),
            "extra_json": _dump(r.get("extra")) if r.get("extra") else None,
            "updated_at": datetime.now(),
        })
    if not out:
        return 0
    # 同一 (symbol, date, category, name) 只留最后一条：
    # 一次采集里同一指标可能来自多个接口片段
    merged: Dict[tuple, Dict[str, Any]] = {}
    for r in out:
        merged[(r["symbol"], r["date"], r["category"], r["name"])] = r
    with SessionLocal() as s:
        n = _upsert(s, StockFactorValue, list(merged.values()),
                    ["symbol", "date", "category", "name"])
        s.commit()
    return n


def _num_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def factor_values(symbol: str, day: Any = None, category: str = ""
                  ) -> Dict[str, Optional[float]]:
    """读某只票某天（不晚于）的某类指标，返回 {name: value}。

    取"不晚于 day 的最近一条"而不是"恰好等于" ——
    股东户数、机构持仓这类数据更新很慢，按天精确匹配会几乎永远查不到。
    """
    d = _date(day) or date.today()
    with SessionLocal() as s:
        q = select(StockFactorValue).where(
            StockFactorValue.symbol == symbol,
            StockFactorValue.date <= d,
        )
        if category:
            q = q.where(StockFactorValue.category == category)
        rows = s.execute(q.order_by(StockFactorValue.date.desc())).scalars().all()
    out: Dict[str, Optional[float]] = {}
    for r in rows:
        out.setdefault(r.name, r.value)      # 同一指标保留最近一条
    return out


def factor_categories(limit: int = 200) -> List[Dict[str, Any]]:
    """列出库里有哪些类别 / 指标（界面用来展示"现在有什么数据"）。"""
    from sqlalchemy import func as _func

    with SessionLocal() as s:
        rows = s.execute(
            select(StockFactorValue.category, StockFactorValue.name,
                   _func.count(StockFactorValue.id))
            .group_by(StockFactorValue.category, StockFactorValue.name)
            .order_by(StockFactorValue.category, StockFactorValue.name)
            .limit(int(limit))
        ).all()
    return [{"category": r[0], "name": r[1], "rows": int(r[2])} for r in rows]


# ===========================================================================
# 舆情
# ===========================================================================
def save_news(rows: Any) -> int:
    """写新闻/公告/研报。**按 (symbol, date, kind, title) 去重**。"""
    if isinstance(rows, dict):
        rows = [rows]
    out: List[Dict[str, Any]] = []
    for r in _rows_to_dicts(rows):
        day = _date(r.get("date"))
        title = str(r.get("title") or "").strip()
        if not (day and title):
            continue
        out.append({
            "symbol": str(r.get("symbol") or ""),
            "date": day,
            "title": title[:500],
            "kind": str(r.get("kind") or "news"),
            "source": str(r.get("source") or ""),
            "sentiment": _num_or_none(r.get("sentiment")),
            "heat": _num_or_none(r.get("heat")),
            "url": str(r.get("url") or "")[:500] or None,
            "created_at": datetime.now(),
        })
    if not out:
        return 0
    merged: Dict[tuple, Dict[str, Any]] = {}
    for r in out:
        merged[(r["symbol"], r["date"], r["kind"], r["title"])] = r
    with SessionLocal() as s:
        n = _upsert(s, MarketNews, list(merged.values()),
                    ["symbol", "date", "kind", "title"])
        s.commit()
    return n


def news_stats(symbol: str, day: Any = None, days: int = 10) -> Dict[str, Any]:
    """最近 N 天的舆情统计：条数、平均情感、正面占比。"""
    d = _date(day) or date.today()
    lo = d - timedelta(days=int(days))
    with SessionLocal() as s:
        rows = s.execute(
            select(MarketNews).where(
                MarketNews.symbol == str(symbol),
                MarketNews.date >= lo, MarketNews.date <= d,
            )
        ).scalars().all()
    sents = [r.sentiment for r in rows if r.sentiment is not None]
    return {
        "count": len(rows),
        "with_sentiment": len(sents),
        "avg_sentiment": (sum(sents) / len(sents)) if sents else None,
        "positive_ratio": ((sum(1 for x in sents if x > 0) / len(sents))
                           if sents else None),
        "total_heat": sum(r.heat or 0.0 for r in rows),
    }


# ===========================================================================
# 宏观 / 市场宽度
# ===========================================================================
def save_macro(rows: Any) -> int:
    if isinstance(rows, dict):
        rows = [rows]
    out: List[Dict[str, Any]] = []
    for r in _rows_to_dicts(rows):
        d = _date(r.get("date"))
        name = r.get("name") or r.get("indicator")
        if not (d and name):
            continue
        out.append({
            "date": d, "name": str(name),
            "value": _num_or_none(r.get("value")),
            "yoy": _num_or_none(r.get("yoy")),
            "period": str(r.get("period") or ""),
            "source": str(r.get("source") or ""),
            "updated_at": datetime.now(),
        })
    if not out:
        return 0
    merged: Dict[tuple, Dict[str, Any]] = {}
    for r in out:
        merged[(r["date"], r["name"])] = r
    with SessionLocal() as s:
        n = _upsert(s, MacroIndicator, list(merged.values()), ["date", "name"])
        s.commit()
    return n


def latest_macro(day: Any = None, limit: int = 50) -> Dict[str, Optional[float]]:
    """最近一期各宏观指标的值。"""
    d = _date(day) or date.today()
    with SessionLocal() as s:
        rows = s.execute(
            select(MacroIndicator).where(MacroIndicator.date <= d)
            .order_by(MacroIndicator.date.desc()).limit(int(limit))
        ).scalars().all()
    out: Dict[str, Optional[float]] = {}
    for r in rows:
        out.setdefault(r.name, r.value if r.value is not None else r.yoy)
    return out


def save_breadth(rows: Any) -> int:
    if isinstance(rows, dict):
        rows = [rows]
    out: List[Dict[str, Any]] = []
    for r in _rows_to_dicts(rows):
        d = _date(r.get("date"))
        if not d:
            continue
        out.append({
            "date": d,
            "up_count": int(r.get("up_count") or 0),
            "down_count": int(r.get("down_count") or 0),
            "limit_up_count": int(r.get("limit_up_count") or 0),
            "limit_down_count": int(r.get("limit_down_count") or 0),
            "total_amount": _num_or_none(r.get("total_amount")),
            "sentiment": _num_or_none(r.get("sentiment")),
            "updated_at": datetime.now(),
        })
    if not out:
        return 0
    merged: Dict[Any, Dict[str, Any]] = {}
    for r in out:
        merged[r["date"]] = r
    with SessionLocal() as s:
        n = _upsert(s, MarketBreadth, list(merged.values()), ["date"])
        s.commit()
    return n


def latest_breadth(day: Any = None) -> Optional[Dict[str, Any]]:
    d = _date(day) or date.today()
    with SessionLocal() as s:
        r = s.execute(
            select(MarketBreadth).where(MarketBreadth.date <= d)
            .order_by(MarketBreadth.date.desc()).limit(1)
        ).scalars().first()
    if r is None:
        return None
    return {
        "date": str(r.date), "up_count": r.up_count, "down_count": r.down_count,
        "limit_up_count": r.limit_up_count, "limit_down_count": r.limit_down_count,
        "total_amount": r.total_amount, "sentiment": r.sentiment,
    }


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


# ===========================================================================
# 查询
# ===========================================================================
def latest_fund_flow(symbol: str, n: int = 10) -> List[Dict[str, Any]]:
    """最近 N 天资金流（按日期升序，便于算加速度）。查不到返回空列表。"""
    with SessionLocal() as s:
        rows = s.execute(
            select(FundFlow).where(FundFlow.symbol == symbol)
            .order_by(FundFlow.date.desc()).limit(int(n))
        ).scalars().all()
    out = [_flow_dict(r) for r in rows]
    return list(reversed(out))


def _flow_dict(r: FundFlow) -> Dict[str, Any]:
    return {
        "symbol": r.symbol, "date": str(r.date), "close": r.close,
        "pct_change": r.pct_change, "turnover": r.turnover, "amount": r.amount,
        "main_net": r.main_net, "main_net_pct": r.main_net_pct,
        "super_large_net": r.super_large_net, "large_net": r.large_net,
        "medium_net": r.medium_net, "small_net": r.small_net,
    }


def latest_chip(symbol: str, n: int = 1) -> List[Dict[str, Any]]:
    with SessionLocal() as s:
        rows = s.execute(
            select(ChipDistribution).where(ChipDistribution.symbol == symbol)
            .order_by(ChipDistribution.date.desc()).limit(int(n))
        ).scalars().all()
    out = [{
        "symbol": r.symbol, "date": str(r.date), "avg_cost": r.avg_cost,
        "profit_ratio": r.profit_ratio, "cost_90_low": r.cost_90_low,
        "cost_90_high": r.cost_90_high, "cost_70_low": r.cost_70_low,
        "cost_70_high": r.cost_70_high, "concentration_90": r.concentration_90,
        "concentration_70": r.concentration_70, "peak_price": r.peak_price,
    } for r in rows]
    return list(reversed(out))


def limitup_pool(day: Optional[Any] = None, limit: int = 200) -> List[Dict[str, Any]]:
    d = _date(day) or date.today()
    with SessionLocal() as s:
        rows = s.execute(
            select(LimitUpPool).where(LimitUpPool.date == d)
            .order_by(LimitUpPool.continuous.desc()).limit(int(limit))
        ).scalars().all()
    return [{
        "date": str(r.date), "symbol": r.symbol, "name": r.name,
        "pct_change": r.pct_change, "close": r.close, "amount": r.amount,
        "seal_amount": r.seal_amount, "first_seal_at": r.first_seal_at,
        "last_seal_at": r.last_seal_at, "broken_times": r.broken_times,
        "limit_stat": r.limit_stat, "continuous": r.continuous,
        "industry": r.industry,
    } for r in rows]


def themes(day: Optional[Any] = None, kind: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    d = _date(day) or date.today()
    with SessionLocal() as s:
        q = select(ThemeFlow).where(ThemeFlow.date == d)
        if kind:
            q = q.where(ThemeFlow.kind == kind)
        q = q.order_by(ThemeFlow.rank.asc()).limit(int(limit))
        rows = s.execute(q).scalars().all()
    return [{
        "date": str(r.date), "board": r.board, "kind": r.kind,
        "pct_change": r.pct_change, "turnover": r.turnover, "main_net": r.main_net,
        "up_count": r.up_count, "down_count": r.down_count,
        "leading_stock": r.leading_stock, "rank": r.rank,
    } for r in rows]


def stock_themes(symbol: str) -> List[Dict[str, Any]]:
    with SessionLocal() as s:
        rows = s.execute(
            select(StockTheme).where(StockTheme.symbol == symbol)
        ).scalars().all()
    return [{
        "symbol": r.symbol, "board": r.board, "kind": r.kind,
        "source": r.source, "date": str(r.date) if r.date else None,
    } for r in rows]


def latest_fundamental(symbol: str, day: Any = None) -> Optional[Dict[str, Any]]:
    """最近一条基本面（不晚于 day）。查不到返回 None。"""
    d = _date(day) or date.today()
    with SessionLocal() as s:
        row = s.execute(
            select(StockFundamental)
            .where(StockFundamental.symbol == symbol,
                   StockFundamental.date <= d)
            .order_by(StockFundamental.date.desc())
            .limit(1)
        ).scalars().first()
    if row is None:
        # 没有日期上界时兜一次：数据可能比"今天"新（补录历史）
        with SessionLocal() as s:
            row = s.execute(
                select(StockFundamental)
                .where(StockFundamental.symbol == symbol)
                .order_by(StockFundamental.date.desc())
                .limit(1)
            ).scalars().first()
    return _fundamental_dict(row) if row else None


def _fundamental_dict(r: StockFundamental) -> Dict[str, Any]:
    return {
        "symbol": r.symbol,
        "date": str(r.date) if r.date else None,
        "report_date": str(r.report_date) if r.report_date else None,
        "pe": r.pe, "pb": r.pb, "ps": r.ps,
        "dividend_yield": r.dividend_yield,
        "market_cap": r.market_cap, "float_market_cap": r.float_market_cap,
        "revenue_yoy": r.revenue_yoy, "profit_yoy": r.profit_yoy,
        "roe": r.roe, "gross_margin": r.gross_margin,
        "net_margin": r.net_margin, "debt_ratio": r.debt_ratio, "bps": r.bps,
    }


def scores(day: Optional[Any] = None, limit: int = 50,
           min_score: Optional[float] = None,
           symbol: str = "") -> List[Dict[str, Any]]:
    d = _date(day) or date.today()
    with SessionLocal() as s:
        q = select(StockScore).where(StockScore.date == d)
        if symbol:
            q = q.where(StockScore.symbol == symbol)
        if min_score is not None:
            q = q.where(StockScore.total_score >= float(min_score))
        q = q.order_by(StockScore.total_score.desc()).limit(int(limit))
        rows = s.execute(q).scalars().all()
    return [_score_dict(r) for r in rows]


def score_of(symbol: str, day: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    got = scores(day=day, symbol=symbol, limit=1)
    return got[0] if got else None


def _score_dict(r: StockScore) -> Dict[str, Any]:
    # "total" 是给消费端（CLI / 面板）的别名，字段本体叫 total_score。
    # 两个名字并存不是冗余：库列名要能一眼看出是"总分"（表里还有各维度分），
    # 而调用方（打分引擎 ScoreResult.to_dict 用的就是 total）不该为此改名 ——
    # 以前只给了 total_score，结果面板和 `pool top` 全部读到空，总分显示成 0。
    return {
        "id": r.id, "date": str(r.date), "symbol": r.symbol,
        "total_score": r.total_score, "total": r.total_score,
        "rank": r.rank, "grade": r.grade,
        "dims": _load(r.dims_json), "reasons": _load(r.reasons_json),
        "available": _load(r.available_json), "source": r.source,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _load(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def score_dates(limit: int = 30) -> List[str]:
    """有哪些日期做过评分（面板下拉用）。"""
    with SessionLocal() as s:
        rows = s.execute(
            select(StockScore.date).distinct().order_by(StockScore.date.desc())
            .limit(int(limit))
        ).scalars().all()
    return [str(d) for d in rows]


__all__ = [
    "latest_chip",
    "latest_fund_flow",
    "limitup_pool",
    "save_chip",
    "save_fund_flow",
    "save_limitup",
    "save_scores",
    "save_stock_themes",
    "save_themes",
    "score_dates",
    "score_of",
    "scores",
    "stock_themes",
    "themes",
]
