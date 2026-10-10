#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : sync.py
# @Description : 扩展行情数据同步：把资金流/筹码/涨停池/题材 抓回来入库
#
#                三条硬约束：
#
#                1. **单只失败不影响整批**。几百只票里总有几只接口取不到
#                   （停牌、退市、接口抽风），一颗老鼠屎坏一锅汤是最糟的结果。
#                2. **失败要说得清**。记 "symbol: 原因"，而不是一句"同步失败"，
#                   否则用户只能靠猜。
#                3. **限速**。数据源限流封 IP 是最难恢复的故障，宁可慢。
#
#                这些函数都返回 {"ok": n, "failed": [(symbol, reason), ...]}，
#                让调用方能同时看到"成了多少"和"哪些没成"。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.data import marketx as MX
from app.data.datasource import normalize_symbol
from app.repository import marketx_repository as R
from app.utils.logger import log_throttled, logger


def _result(ok: int, failed: List[Tuple[str, str]]) -> Dict[str, Any]:
    return {"ok": ok, "failed": failed, "total": ok + len(failed)}


def sync_fund_flow(symbols: Sequence[str], days: int = 60) -> Dict[str, Any]:
    ok = 0
    failed: List[Tuple[str, str]] = []
    for sym in dict.fromkeys(normalize_symbol(s) for s in symbols if s):
        try:
            df = MX.fetch_fund_flow(sym, days=days)
            R.save_fund_flow(df)
            ok += 1
        except MX.MarketXError as exc:
            failed.append((sym, str(exc)))
        except Exception as exc:  # 兜底：任何异常都不能中断整批
            failed.append((sym, f"{type(exc).__name__}: {exc}"))
    if failed:
        log_throttled("WARNING", f"资金流同步有 {len(failed)} 只失败（已跳过）",
                      key="sync-fundflow-failed")
    return _result(ok, failed)


def sync_chip(symbols: Sequence[str]) -> Dict[str, Any]:
    ok = 0
    failed: List[Tuple[str, str]] = []
    for sym in dict.fromkeys(normalize_symbol(s) for s in symbols if s):
        try:
            R.save_chip(MX.fetch_chip(sym))
            ok += 1
        except MX.MarketXError as exc:
            failed.append((sym, str(exc)))
        except Exception as exc:
            failed.append((sym, f"{type(exc).__name__}: {exc}"))
    if failed:
        log_throttled("WARNING", f"筹码同步有 {len(failed)} 只失败（已跳过）",
                      key="sync-chip-failed")
    return _result(ok, failed)


def sync_fundamentals(symbols: Sequence[str]) -> Dict[str, Any]:
    """同步基本面：估值 + 财务。

    为什么拆成两个接口再合并：
      估值（PE/PB/市值）随价格**日频**变，一天一采；
      财务（ROE/增长）是**季频**的，一份报告要用一个季度。
    两个接口的可用性与返回形状都不一样，各自失败各自降级 ——
    拿到估值没拿到财务，基本面维度照样能算（只是成长项缺失）。

    这也是它必须放在**日频采集**而不是分钟级采集的原因：
    季频数据你一天拉 100 次，99 次拿到的是同一份三季报。
    """
    ok = 0
    failed: List[Tuple[str, str]] = []
    for sym in dict.fromkeys(normalize_symbol(s) for s in symbols if s):
        valuation: Dict[str, Any] = {}
        finance: Dict[str, Any] = {}
        try:
            valuation = MX.fetch_valuation(sym) or {}
        except MX.MarketXError as exc:
            failed.append((sym, f"估值: {exc}"))
        except Exception as exc:
            failed.append((sym, f"估值: {type(exc).__name__}: {exc}"))
        try:
            finance = MX.fetch_financial_indicator(sym) or {}
        except MX.MarketXError as exc:
            failed.append((sym, f"财务: {exc}"))
        except Exception as exc:
            failed.append((sym, f"财务: {type(exc).__name__}: {exc}"))

        if not valuation and not finance:
            continue
        from datetime import date as _date

        row = dict(valuation)
        row.update({k: v for k, v in finance.items() if v is not None})
        row.setdefault("date", _date.today())
        row["updated_at"] = _now()
        try:
            R.save_fundamentals(row)
            ok += 1
        except Exception as exc:
            failed.append((sym, f"入库: {type(exc).__name__}: {exc}"))
    if failed:
        log_throttled("WARNING", f"基本面同步有 {len(failed)} 项失败（已跳过）",
                      key="sync-fundamental-failed")
    return _result(ok, failed)


def _now():
    from datetime import datetime

    return datetime.now()


# ===========================================================================
# 多类别截面数据（写入通用因子值表）
# ===========================================================================
#: 个股级类别 → 获取函数。新增一类数据 = 在这里加一行。
CATEGORY_FETCHERS = {
    "lhb": lambda sym: MX.fetch_dragon_tiger(sym),
    "margin": lambda sym: MX.fetch_margin(sym),
    "forecast": lambda sym: MX.fetch_forecast(sym),
    "holders": lambda sym: MX.fetch_holders(sym),
    "rating": lambda sym: MX.fetch_rating(sym),
}
#: 市场级类别（不需要标的）
MARKET_LEVEL = ("macro", "breadth")

MACRO_NAMES = ("cpi", "ppi", "pmi", "m2", "lpr")


def sync_factors(category: str, symbols: Sequence[str]) -> Dict[str, Any]:
    """同步某一类个股截面数据。

    单只失败不影响整批，失败原因写清 "symbol: 原因" ——
    这类数据接口稳定性参差不齐（龙虎榜非涨停日没有、两融 T+1 才更新），
    "拉不到"是常态，不能让整批崩掉。
    """
    fn = CATEGORY_FETCHERS.get(str(category))
    if fn is None:
        return _result(0, [(str(category),
                            f"未知类别 | 可用: {', '.join(sorted(CATEGORY_FETCHERS))}")])
    ok = 0
    failed: List[Tuple[str, str]] = []
    for sym in dict.fromkeys(normalize_symbol(s) for s in symbols if s):
        try:
            rows = fn(sym) or []
        except MX.MarketXError as exc:
            failed.append((sym, str(exc)[:120]))
            continue
        except Exception as exc:
            failed.append((sym, f"{type(exc).__name__}: {exc}"[:120]))
            continue
        if not rows:
            continue
        try:
            R.save_factor_values(rows, category=str(category))
            ok += 1
        except Exception as exc:
            failed.append((sym, f"入库: {type(exc).__name__}: {exc}"[:120]))
    if failed:
        log_throttled("WARNING",
                      f"{category} 同步有 {len(failed)} 项失败（已跳过）",
                      key=f"sync-{category}-failed")
    return _result(ok, failed)


def sync_news(symbols: Sequence[str], limit: int = 20) -> Dict[str, Any]:
    """同步个股新闻并算情感分。"""
    ok = 0
    failed: List[Tuple[str, str]] = []
    for sym in dict.fromkeys(normalize_symbol(s) for s in symbols if s):
        try:
            rows = MX.fetch_news(sym, limit=limit) or []
        except MX.MarketXError as exc:
            failed.append((sym, str(exc)[:120]))
            continue
        except Exception as exc:
            failed.append((sym, f"{type(exc).__name__}: {exc}"[:120]))
            continue
        if not rows:
            continue
        try:
            R.save_news(rows)
            ok += 1
        except Exception as exc:
            failed.append((sym, f"入库: {exc}"[:120]))
    return _result(ok, failed)


def sync_macro(names: Sequence[str] = MACRO_NAMES) -> Dict[str, Any]:
    """同步宏观指标（市场级）。"""""
    ok = 0
    failed: List[Tuple[str, str]] = []
    for name in names:
        try:
            rows = MX.fetch_macro(name) or []
        except MX.MarketXError as exc:
            failed.append((name, str(exc)[:120]))
            continue
        except Exception as exc:
            failed.append((name, f"{type(exc).__name__}: {exc}"[:120]))
            continue
        if not rows:
            continue
        try:
            R.save_macro(rows)
            ok += 1
        except Exception as exc:
            failed.append((name, f"入库: {exc}"[:120]))
    return _result(ok, failed)


def sync_breadth() -> Dict[str, Any]:
    """同步市场宽度（市场级）。"""
    try:
        rows = MX.fetch_market_breadth() or []
    except MX.MarketXError as exc:
        return _result(0, [("breadth", str(exc)[:160])])
    except Exception as exc:
        return _result(0, [("breadth", f"{type(exc).__name__}: {exc}"[:160])])
    if not rows:
        return _result(0, [("breadth", "返回空数据")])
    try:
        return _result(R.save_breadth(rows), [])
    except Exception as exc:
        return _result(0, [("breadth", f"入库: {exc}"[:160])])


def sync_category(category: str, symbols: Sequence[str] = ()) -> Dict[str, Any]:
    """按类别同步的统一入口（个股级 + 市场级都走这里）。"""
    cat = str(category or "").strip().lower()
    if cat == "news":
        return sync_news(symbols)
    if cat == "macro":
        return sync_macro()
    if cat == "breadth":
        return sync_breadth()
    return sync_factors(cat, symbols)


def sync_limitup(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """同步涨停池，并按"所属行业"顺手建立题材归属。

    涨停池自带所属行业字段，这是**零额外请求**拿到题材线索的途径 ——
    不用为了知道它属于什么题材再去拉一遍概念成分。
    """
    try:
        df = MX.fetch_limitup_pool(trade_date)
    except MX.MarketXError as exc:
        log_throttled("WARNING", f"涨停池同步失败: {exc}", key="sync-zt-failed")
        return _result(0, [("__pool__", str(exc))])
    except Exception as exc:
        return _result(0, [("__pool__", f"{type(exc).__name__}: {exc}")])

    n = R.save_limitup(df)
    pairs = [
        {"symbol": r.get("symbol"), "board": r.get("industry"),
         "kind": "industry", "source": "limitup", "date": r.get("date")}
        for r in df.to_dict("records")
        if r.get("symbol") and r.get("industry")
    ]
    R.save_stock_themes(pairs)
    return _result(n, [])


def sync_boards(kind: str = "concept") -> Dict[str, Any]:
    """同步板块表现，并叠加板块资金流（有则更好，没有也不算失败）。"""
    try:
        df = MX.fetch_boards(kind)
    except MX.MarketXError as exc:
        return _result(0, [(kind, str(exc))])
    except Exception as exc:
        return _result(0, [(kind, f"{type(exc).__name__}: {exc}")])

    try:
        flow = MX.fetch_board_fund_flow(kind)
        by_board = {r.get("board"): r for r in flow.to_dict("records")}
    except Exception as exc:
        logger.debug(f"板块资金流获取失败（不影响板块涨幅）: {exc}")
        by_board = {}

    rows = []
    for r in df.to_dict("records"):
        info = by_board.get(r.get("board")) or {}
        merged = dict(r)
        if info.get("main_net") is not None:
            merged["main_net"] = info.get("main_net")
        if merged.get("pct_change") is None and info.get("pct_change") is not None:
            merged["pct_change"] = info.get("pct_change")
        rows.append(merged)
    return _result(R.save_themes(rows), [])


def sync_concept_members(top: int = 20) -> Dict[str, Any]:
    """把最热的 N 个概念板块的成分股建成"个股↔题材"映射。

    只做最热的若干个：全市场概念板块有几百个，
    每个都拉一遍成分股既慢又必然触发限流。
    而且冷门题材对选股本来也没什么价值。
    """
    try:
        boards = MX.fetch_boards("concept")
    except MX.MarketXError as exc:
        return _result(0, [("concept", str(exc))])
    except Exception as exc:
        return _result(0, [("concept", f"{type(exc).__name__}: {exc}")])

    names = [str(r.get("board")) for r in boards.to_dict("records")][: int(top)]
    ok = 0
    failed: List[Tuple[str, str]] = []
    for board in names:
        try:
            cons = MX.fetch_concept_cons(board)
        except MX.MarketXError as exc:
            failed.append((board, str(exc)))
            continue
        except Exception as exc:
            failed.append((board, f"{type(exc).__name__}: {exc}"))
            continue
        pairs = [{"symbol": c.get("symbol"), "board": board,
                  "kind": "concept", "source": "concept_cons"}
                 for c in cons.to_dict("records") if c.get("symbol")]
        R.save_stock_themes(pairs)
        ok += 1
    return _result(ok, failed)


__all__ = [
    "CATEGORY_FETCHERS",
    "MACRO_NAMES",
    "MARKET_LEVEL",
    "sync_board_members",
    "sync_boards",
    "sync_breadth",
    "sync_category",
    "sync_chip",
    "sync_concept_members",
    "sync_factors",
    "sync_fund_flow",
    "sync_fundamentals",
    "sync_limitup",
    "sync_macro",
    "sync_news",
]


def sync_board_members(*args: Any, **kwargs: Any) -> Dict[str, Any]:  # noqa: D401
    """兼容别名（旧脚本可能这么叫）。"""
    return sync_concept_members(*args, **kwargs)
