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

import pandas as pd

from app.datasource.service import facade as MX
from app.domain.symbol import normalize_symbol
from app.storage.repo import kline_repo as R
from app.common.logging import log_throttled, logger


def _result(ok: int, failed: List[Tuple[str, str]],
            skipped: int = 0, reason: str = "") -> Dict[str, Any]:
    out = {"ok": ok, "failed": failed, "total": ok + len(failed)}
    if skipped:
        # 库里已有当日数据 → 没联网。界面上要能区分"采到了 0 条"和"根本没采"
        out["skipped"] = skipped
        out["reason"] = reason
    return out


def _flush(what: str, frames: List[Any], save_one: Any,
           failed: List[Tuple[str, str]]) -> int:
    """攒批落库：**一次事务**而不是每票一次。

    每个 ``save_*`` 都自己开 Session 并立刻 commit（`kline_repo.py` 里
    13 个 save 全是这个模式）。60 只票 × 9 个类别就是约 540 次
    BEGIN/COMMIT —— WAL 下每次 commit 一次 fsync，还要反复取还连接池。
    日频截面表都按 ``(symbol, date)`` upsert，把多只票的 df 拼起来
    一次写入是等价的。

    失败退回逐条：批量写失败最常见的成因是其中某一行数据有问题，
    退回逐条至少保住"一只坏票不拖垮整批"这个原有语义，
    不会比改造前更差。
    """
    if not frames:
        return 0
    try:
        df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
        save_one(df)
        return len(frames)
    except Exception as exc:
        logger.warning(f"{what} 批量落库失败，退回逐条写入: {exc}")
        n = 0
        for one in frames:
            try:
                save_one(one)
                n += 1
            except Exception as exc2:
                sym = "?"
                try:
                    sym = str(one["symbol"].iloc[0])
                except Exception:
                    pass
                failed.append((sym, f"落库失败: {exc2}"))
        return n


def _target_day(day: Any = None):
    """本次同步的"目标数据日"。

    显式传了就尊重调用方；没传则取**最近交易日** ——
    周末刷新时"今天"根本没有行情数据，拿今天做去重基准会永远判成"缺失"。
    """
    from datetime import date as _date

    if day:
        parsed = _to_date_safe(day)
        if parsed:
            return parsed
    try:
        from app.domain.calendar import get_calendar
        return get_calendar().last_trading_day()
    except Exception as exc:  # 日历不可用就退回今天，最坏情况只是多采一次
        logger.debug(f"交易日历不可用，去重基准退回今天: {exc}")
        return _date.today()


def sync_fund_flow(symbols: Sequence[str], days: int = 60,
                   force: bool = False, day: Any = None) -> Dict[str, Any]:
    """同步个股资金流。

    :param force: True = 忽略"库里已有"判断，强制重新联网。
    :param day: 指定数据日（去重基准）；不传取最近交易日。

    库里该股已有目标日的资金流就**跳过不重复请求** —— 同一交易日的数据
    拉第二次拿到的还是同一份，白白消耗请求额度还容易被限流。
    """
    ok = 0
    skipped = 0
    failed: List[Tuple[str, str]] = []
    syms = [normalize_symbol(s) for s in dict.fromkeys(symbols) if s]
    target = _target_day(day)
    have: Dict[str, str] = {} if force else R.latest_dates("fund_flow", syms)
    frames: List[Any] = []
    for sym in syms:
        if not force and have.get(sym) and str(have[sym]) >= str(target):
            skipped += 1
            continue
        try:
            df = MX.fetch_fund_flow(sym, days=days)
        except MX.MarketXError as exc:
            failed.append((sym, str(exc)))
            continue
        except Exception as exc:  # 兜底：任何异常都不能中断整批
            failed.append((sym, f"{type(exc).__name__}: {exc}"))
            continue
        if df is None or getattr(df, "empty", True):
            failed.append((sym, "返回空数据"))
            continue
        frames.append(df)
    ok += _flush("fund_flow", frames, R.save_fund_flow, failed)
    if skipped:
        logger.info(f"资金流同步跳过 {skipped} 只（库内已有 {target} 的数据，"
                    f"force=True 可强制重采）")
    if failed:
        sample = "; ".join(f"{s}→{r[:55]}" for s, r in failed[:5])
        more = f" 等{len(failed)}只" if len(failed) > 5 else ""
        log_throttled("WARNING",
                      f"资金流同步有 {len(failed)} 只失败（已跳过）。样例: {sample}{more}",
                      key="sync-fundflow-failed")
    return _result(ok, failed, skipped, f"库内已有 {target} 数据")


def sync_chip(symbols: Sequence[str], force: bool = False,
              day: Any = None) -> Dict[str, Any]:
    """同步筹码分布。去重逻辑见 ``sync_fund_flow``。"""
    ok = 0
    skipped = 0
    tripped = 0
    failed: List[Tuple[str, str]] = []
    syms = [normalize_symbol(s) for s in dict.fromkeys(symbols) if s]
    target = _target_day(day)
    have: Dict[str, str] = {} if force else R.latest_dates("chip", syms)
    frames: List[Any] = []
    for sym in syms:
        if not force and have.get(sym) and str(have[sym]) >= str(target):
            skipped += 1
            continue
        try:
            df = MX.fetch_chip(sym)
        except MX.MarketXError as exc:
            if "熔断" in str(exc):
                # 源已断开：这不是这只票的问题，记"跳过"而不是"失败"，
                # 否则一次源故障会被报成 60 只票各自失败，把真问题淹掉
                tripped += 1
                continue
            failed.append((sym, str(exc)))
            continue
        except Exception as exc:
            if "熔断" in str(exc):
                tripped += 1
                continue
            failed.append((sym, f"{type(exc).__name__}: {exc}"))
            continue
        if df is None or getattr(df, "empty", True):
            failed.append((sym, "返回空数据"))
            continue
        frames.append(df)
    ok += _flush("chip", frames, R.save_chip, failed)
    if skipped:
        logger.info(f"筹码同步跳过 {skipped} 只（库内已有 {target} 的数据）")
    if tripped:
        logger.info(f"筹码同步另有 {tripped} 只因源熔断跳过（不重复重试）")
    skipped += tripped
    if failed:
        log_throttled("WARNING", f"筹码同步有 {len(failed)} 只失败（已跳过）",
                      key="sync-chip-failed")
    return _result(ok, failed, skipped, f"库内已有 {target} 数据")


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


def sync_limitup(trade_date: Optional[str] = None,
                 force: bool = False) -> Dict[str, Any]:
    """同步涨停池，并按"所属行业"顺手建立题材归属。

    涨停池自带所属行业字段，这是**零额外请求**拿到题材线索的途径 ——
    不用为了知道它属于什么题材再去拉一遍概念成分。

    库里已有该交易日数据就跳过（一个交易日一份，重复拉拿到的还是同一份）。
    """
    from datetime import date as _date

    d = _to_date_safe(trade_date) if trade_date else _target_day()
    if not force:
        existing = R.pool_count("limitup", d)
        if existing:
            logger.info(f"涨停池已存在 {existing} 条（{d}），跳过重复采集"
                        f"（force=True 可强制重采）")
            return _result(0, [], existing, f"库内已有 {d} 涨停池 {existing} 条")
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


def sync_hot_rank(trade_date: Optional[str] = None,
                  force: bool = False) -> Dict[str, Any]:
    """同步人气榜快照。

    人气榜是实时榜单，之前每次选候选池都现场联网拉、且**不落库**，
    代理一抽风整页 0 条、下次还得再拉。这里落库后，刷新先读库、
    库空再联网补拉（见 ``StockPool.candidates``），没网也能看上一期的。

    库里已有该交易日数据就跳过 —— 同一天的榜单拉第二次没有意义。
    """
    from datetime import date as _date

    d = _to_date_safe(trade_date) if trade_date else _target_day()
    if not force:
        existing = R.pool_count("hot", d)
        if existing:
            logger.info(f"人气榜已存在 {existing} 条（{d}），跳过重复采集"
                        f"（force=True 可强制重采）")
            return _result(0, [], existing, f"库内已有 {d} 人气榜 {existing} 条")

    try:
        df = MX.fetch_hot_rank()
    except MX.MarketXError as exc:
        # 原因里已带"哪个源、试了几次、为什么"，直接透传，不要只写"同步失败"
        log_throttled("WARNING", f"人气榜同步失败: {exc}", key="sync-hot-failed")
        return _result(0, [("__pool__", str(exc))])
    except Exception as exc:
        return _result(0, [("__pool__", f"{type(exc).__name__}: {exc}")])

    # 数据到底是谁给的？外网源全挂时会落到本地兜底（local_hot），
    # 这时候榜单是历史数据或日线合成的 —— 必须说清楚，
    # 否则界面上"人气榜 200 只"看着正常，实际是旧数据。
    from app.datasource.compat.registry import REGISTRY

    used = ""
    for ev in reversed(REGISTRY.recent_events(3)):
        if ev.get("kind") == "hot_rank" and ev.get("ok"):
            used = str(ev.get("source") or "")
            break
    if used.startswith("local"):
        log_throttled("WARNING",
                      f"人气榜由本地兜底源 {used} 提供（外网源均不可用），"
                      f"内容可能是历史榜单或按成交额合成的近似排名",
                      key="sync-hot-local")

    d = _to_date_safe(trade_date) if trade_date else _target_day()
    recs = []
    for r in df.to_dict("records"):
        rec = dict(r)
        rec["date"] = d
        recs.append(rec)
    n = R.save_hot_rank(pd.DataFrame(recs)) if recs else 0
    # 顺手把名称灌进对照表，评分列表才有名字
    R.save_stock_info([{"symbol": r.get("symbol"), "name": r.get("name", "")}
                       for r in recs if r.get("symbol")])
    res = _result(n, [])
    res["source"] = used
    if used.startswith("local"):
        res["note"] = "本地兜底（外网人气榜源均不可用）"
    return res


def sync_flow_rank(trade_date: Optional[str] = None,
                   force: bool = False) -> Dict[str, Any]:
    """同步资金流排名快照（按主力净流入）。见 ``sync_hot_rank``。"""
    from datetime import date as _date

    d = _to_date_safe(trade_date) if trade_date else _target_day()
    if not force:
        existing = R.pool_count("flow", d)
        if existing:
            logger.info(f"资金榜已存在 {existing} 条（{d}），跳过重复采集")
            return _result(0, [], existing, f"库内已有 {d} 资金榜 {existing} 条")

    try:
        df = MX.fetch_fund_flow_rank()
    except MX.MarketXError as exc:
        log_throttled("WARNING", f"资金榜同步失败: {exc}", key="sync-flow-failed")
        return _result(0, [("__pool__", str(exc))])
    except Exception as exc:
        return _result(0, [("__pool__", f"{type(exc).__name__}: {exc}")])

    d = _to_date_safe(trade_date) if trade_date else _target_day()
    recs = []
    for r in df.to_dict("records"):
        rec = dict(r)
        rec["date"] = d
        recs.append(rec)
    n = R.save_flow_rank(pd.DataFrame(recs)) if recs else 0
    R.save_stock_info([{"symbol": r.get("symbol"), "name": r.get("name", "")}
                       for r in recs if r.get("symbol")])
    return _result(n, [])


def sync_stock_info() -> Dict[str, Any]:
    """一次性把全市场代码↔名称灌入对照表。

    评分结果只存代码，没有名称"600042 是哪只票"根本看不出来。
    ``stock_zh_a_spot_em`` 一次返回全部 A 股的代码+名称，
    够覆盖绝大多数情况；查不到名称的代码（脏数据/退市/幻想代码）会显示"未知"。
    """
    try:
        df = MX.fetch_spot()
    except MX.MarketXError as exc:
        log_throttled("WARNING", f"全市场名称同步失败: {exc}", key="sync-info-failed")
        return _result(0, [("__info__", str(exc))])
    except Exception as exc:
        return _result(0, [("__info__", f"{type(exc).__name__}: {exc}")])
    rows = [{"symbol": r.get("symbol"), "name": r.get("name", "")}
            for r in df.to_dict("records") if r.get("symbol")]
    return _result(R.save_stock_info(rows), [])


def _to_date_safe(value: Any) -> Any:
    """把 YYYYMMDD / YYYY-MM-DD 尽量解析成 date，失败回退今天。"""
    from datetime import date as _date

    if value is None:
        return _date.today()
    try:
        return pd.to_datetime(str(value)).date()
    except Exception:
        return _date.today()


def sync_boards(kind: str = "concept", force: bool = False,
                day: Any = None) -> Dict[str, Any]:
    """同步板块表现，并叠加板块资金流（有则更好，没有也不算失败）。

    板块行情是市场级日频数据，库里已有当日数据就跳过。
    """
    if not force:
        d = _target_day(day)
        try:
            existing = len(R.themes(d, kind=kind, limit=1))
        except Exception:
            existing = 0
        if existing:
            logger.info(f"板块({kind})已存在当日数据（{d}），跳过重复采集")
            return _result(0, [], existing, f"库内已有 {d} 的 {kind} 板块数据")
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
    "sync_flow_rank",
    "sync_fund_flow",
    "sync_fundamentals",
    "sync_hot_rank",
    "sync_limitup",
    "sync_macro",
    "sync_news",
    "sync_stock_info",
]


def sync_board_members(*args: Any, **kwargs: Any) -> Dict[str, Any]:  # noqa: D401
    """兼容别名（旧脚本可能这么叫）。"""
    return sync_concept_members(*args, **kwargs)
