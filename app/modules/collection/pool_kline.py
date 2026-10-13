#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : pool_kline.py
# @Description : K 线定时编排 —— 盘中刷池、盘后定稿、定期查漏
#
#                三个任务是**同一个问题的三个时间切面**：
#
#                  ┌──────────┬──────────────┬────────────────┬──────────────┐
#                  │          │ 盘中刷新      │ 盘后同步        │ 缺口体检      │
#                  ├──────────┼──────────────┼────────────────┼──────────────┤
#                  │ 目标     │ 让策略看到近况 │ 当天收盘价定稿  │ 历史别漏      │
#                  │ 标的     │ 候选池（少量） │ 当天该有K线的票 │ 主表全部/池中 │
#                  │ 窗口     │ 最近几天       │ 缺的那一天      │ 回溯 N 天     │
#                  │ 联网     │ 每轮都要       │ 只补缺的        │ 有缺口才连    │
#                  └──────────┴──────────────┴────────────────┴──────────────┘
#
#                但它们仍然共享同一套"怎么采"的代码 —— 三个函数共用一个
#                ``_run_batch``。这么做的好处很具体：调限速、改去重逻辑、
#                改要不要跳过退市票，都只有一处，
#                **不存在三个定时任务的行为逐渐分叉的风险**。
#
#                三条共有的纪律：
#                  1. **先查再采**。**请求次数比 CPU 稀缺得多** ——
#                     刚采过的票没必要再采一遍；
#                  2. **跳过不该有数据的票**。未上市 / 已退市的判断统一由
#                     :func:`stock_info_repo.filter_listed` 负责，
#                     这里绝不写第二套规则；
#                  3. **一只失败不拖垮一批**。失败逐只记 ``{"symbol","error"}``
#                     汇总在 ``failed`` 里 —— 一颗老鼠屎坏一锅汤是最糟的结果。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.common.logging import logger

#: 默认参数。它们都是"可配置"的，默认值的选择理由写在各自注释里。
DEFAULT_INTRADAY_LOOKBACK = 10      # 盘中：只捞最近 10 个交易日，够算指标就行
DEFAULT_POST_LOOKBACK = 30          # 盘后：多一点，命中"刚除完权要重算"的场景
DEFAULT_LIMIT = 30                  # 单轮最多采多少只候选池标的
DEFAULT_GAP_LOOKBACK = 120          # 体检回溯天数：一个季度左右


# =============================================================================
# 标的从哪里来
# =============================================================================
def resolve_symbols(source: str = "", limit: int = 0,
                    symbols: Optional[Sequence[str]] = None,
                    day: Any = None) -> Tuple[List[str], Dict[str, Any]]:
    """定出这一轮要采哪些标的。

    优先级：**显式 symbols > 候选池 > 保底字段**。

    为什么显式优先：定时任务和手动触发可能共用这段编排，
    手动指定了标的却被配置里的池子盖掉，是最难解释的那类 bug
    （"我明明只点了这一只票"）。

    :return: ``(symbols, info)``。``info`` 里写清楚这条清单是怎么来的，
             供面板与日志回答"为什么采的是这批票"。
    """
    explicit = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if explicit:
        return list(dict.fromkeys(explicit)), {"source": "explicit",
                                              "note": "调用方直接指定了标的"}
    src = str(source or "")
    info: Dict[str, Any] = {"source": src or "flow"}
    try:
        from app.modules.universe.pool import StockPool

        pool = StockPool()
        if src:
            pool.config.source = src
        syms, extra = pool.candidates(source=src or None, day=day)
        info.update(extra or {})
    except Exception as exc:
        # 候选池拉不到 **就返回空**，绝不退化成"全市场"：
        # 一次几十秒的操作变成几十分钟，还会在数据源那里留下一个
        # "你们系统疯了"的印象。失败要说出来，由上层决定要不要重投。
        logger.warning(f"取候选池失败: {exc}")
        return [], {"source": src, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    syms = [str(s) for s in syms if s]
    if limit and limit > 0:
        info["truncated"] = max(0, len(syms) - int(limit))
        syms = syms[: int(limit)]
    info["count"] = len(syms)
    return syms, info


def _filter_symbols(symbols: Sequence[str], day: Any = None
                    ) -> Tuple[List[str], Dict[str, List[str]]]:
    """按主表剔除不该采的标的（未上市/已退市），其余放行。"""
    try:
        from app.data.repo import stock_info_repo as P

        return P.filter_listed(list(symbols), day=day)
    except Exception as exc:  # pragma: no cover - 主表不可用不该阻断采集
        logger.debug(f"标的主表过滤失败（本轮不过滤）: {exc}")
        return list(symbols), {"unknown": [], "unlisted": [], "delisted": []}


# =============================================================================
# 会话判断
# =============================================================================
def session_guard(require_trading: bool = True,
                  allow_outside_hours: bool = False) -> Optional[str]:
    """该不该此刻联网。不该就返回**理由**（None = 放行）。

    这不是多余的谨慎：一次"休市日的盘后同步"会挨个请求数据源，
    拿回一堆"今天没有数据"，然后在次日凌晨之前再也没人管它。

    :param require_trading: False = 完全不检查（调用方明确说"什么时候都能跑"）。
                            注意它与 ``allow_outside_hours`` **不是同一个开关**：
                            前者是"要不要检查"，后者是"检查时是否把盘前/盘后
                            也算作可以干活"。两个开关叠在一起出现过一次
                            "明明传了 False 却还是被拦" —— 因为当时的写法是
                            ``if not require_trading and allow_outside_hours``，
                            少传一个参数就永远走检查分支。
    :param allow_outside_hours: True = 盘前/盘后也算可干活（但休市日仍然不算）。
    """
    if not require_trading:
        return None
    try:
        from app.domain.calendar import get_calendar

        cal = get_calendar()
        state = cal.state()
    except Exception as exc:  # pragma: no cover
        logger.debug(f"交易日历不可用（放行）: {exc}")
        return None
    if state == "holiday":
        return "非交易日"
    if state == "closed":
        return "已闭市"
    if not allow_outside_hours and state in ("pre_open", "post_close"):
        return f"非交易时段（{state}）"
    return None


# =============================================================================
# 单只标的的采集
# =============================================================================
def _collect_one(symbol: str, lookback_days: int, adjust: str = "qfq",
                 source: str = "") -> Dict[str, Any]:
    """采一只票的日线。失败**抛出**，由调用方记进 failed —— 不在这里吞。"""
    from app.data.service import MarketDataService

    info = MarketDataService.collect_incremental(
        symbol, lookback_days=int(lookback_days), adjust=adjust,
        source=(source or None),
    )
    return {
        "symbol": str(info.get("symbol") or symbol),
        "rows": int(info.get("rows") or 0),
        "new_rows": int(info.get("new_rows") or 0),
        "start": info.get("start") or "",
        "end": info.get("end") or "",
    }


def _run_batch(symbols: Sequence[str], lookback_days: int, adjust: str,
               source: str, min_interval: float,
               on_progress: Optional[Callable[[int, int], None]] = None
               ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], int]:
    """逐只采集一批。**串行 + 最小间隔**，这是刻意的。

    为什么不并发：这些请求的另一端是**免费的公开数据源**，并发 8 条请求
    在它们看来就是一次扫描。代价是被限流几十分钟到几小时 ——
    而这类限制的恢复时间远长于串行多花的那几秒。
    """
    done: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    rows_total = 0
    total = len(symbols)
    for idx, sym in enumerate(symbols):
        if min_interval > 0 and idx:
            time.sleep(min_interval)
        try:
            info = _collect_one(sym, lookback_days, adjust=adjust, source=source)
            done.append(info)
            rows_total += int(info.get("rows") or 0)
        except Exception as exc:
            failed.append({"symbol": str(sym),
                           "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            logger.warning(f"K线采集失败 {sym}: {exc}")
        finally:
            if on_progress is not None:
                try:
                    on_progress(idx + 1, total)
                except Exception:  # pragma: no cover
                    pass
    return done, failed, rows_total


def _base_report(mode: str, **kw: Any) -> Dict[str, Any]:
    rep = {
        "mode": mode,
        "ok": False,
        "requested": 0,
        "collected": 0,
        "rows": 0,
        "new_rows": 0,
        "failed": [],
        "skipped": {"unlisted": [], "delisted": [], "unknown": [], "up_to_date": []},
        "symbols": [],
        "source_info": {},
        "skipped_reason": "",
        "duration_ms": 0,
    }
    rep.update(kw)
    return rep


# =============================================================================
# 1. 盘中：定时刷新候选池 K 线
# =============================================================================
def refresh_intraday(source: str = "", limit: int = 0,
                     symbols: Optional[Sequence[str]] = None,
                     lookback_days: int = 0, adjust: str = "qfq",
                     min_interval: float = 0.3, require_trading: bool = True,
                     on_progress: Optional[Callable[[int, int], None]] = None,
                     bus: Any = None) -> Dict[str, Any]:
    """盘中：把候选池里那批票的**近况**捞回来。

    盘中数据的特点：它**只会越来越准**，不会"漏"。所以这里不查"有没有"，
    每轮都重采一遍最近几天 —— 当天那根 K 线在收盘前每几分钟就变一次，
    只差一次更新，策略看到的就是上一分钟的旧价。

    .. note::
       这正是内存里那句"Bar 已收 vs 未收"要分开的原因：这里写回的是
       **可能还会被同一天后续数据覆盖**的中间态，所以依赖
       ``market.kline.updated``（已定稿）的因子计算不要订阅它。
    """
    t0 = time.monotonic()
    rep = _base_report("intraday")

    why = session_guard(require_trading=require_trading)
    if why:
        rep["skipped_reason"] = why
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        logger.info(f"K线盘中刷新跳过：{why}")
        return rep

    lookback = int(lookback_days or DEFAULT_INTRADAY_LOOKBACK)
    n = int(limit or DEFAULT_LIMIT)
    syms, info = resolve_symbols(source=source, limit=n, symbols=symbols)
    rep["source_info"] = info
    if info.get("error"):
        rep["skipped_reason"] = f"候选池不可用: {info['error']}"
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return rep
    if not syms:
        rep["skipped_reason"] = "候选池为空"
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return rep

    keep, buckets = _filter_symbols(syms)
    rep["requested"] = len(syms)
    rep["skipped"].update({k: v for k, v in buckets.items() if k in rep["skipped"]})
    logger.info(
        f"K线盘中刷新 | 候选 {len(syms)} 只 → 实采 {len(keep)} 只"
        f"（未上市 {len(buckets.get('unlisted', []))} · "
        f"退市 {len(buckets.get('delisted', []))} · "
        f"上市日期未知 {len(buckets.get('unknown', []))}）"
    )
    if not keep:
        rep["skipped_reason"] = "过滤后无标的可采"
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return rep

    done, failed, rows = _run_batch(
        keep, lookback, adjust, "", min_interval, on_progress=on_progress)
    rep["collected"] = len(done)
    rep["rows"] = rows
    rep["new_rows"] = sum(int(d.get("new_rows") or 0) for d in done)
    rep["failed"] = failed
    rep["symbols"] = [d["symbol"] for d in done]
    rep["ok"] = len(done) > 0
    rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
    if rep["ok"]:
        _publish_kline_updated(bus, "intraday", rep)
    return rep


# =============================================================================
# 2. 盘后：同步当天 K 线
# =============================================================================
def sync_post_close(source: str = "", limit: int = 0,
                    symbols: Optional[Sequence[str]] = None,
                    lookback_days: int = 0, adjust: str = "qfq",
                    min_interval: float = 0.3, day: Any = None,
                    on_progress: Optional[Callable[[int, int], None]] = None,
                    bus: Any = None) -> Dict[str, Any]:
    """盘后：把"今天（最近交易日）"这一根 K 线补齐。

    与盘中的关键区别：**先查再采**。查询 library-wide 一次 group_by
    就能知道谁已经有了当天数据 —— 已经有的一律不动。

    盘后不设 require_trading：**盘后同步恰恰是在闭市之后跑的**。
    如果照抄盘中那道"必须在交易时段"的检查，这个任务永远跑不起来。
    这类"两个相似场景 & 一个相反的默认值"是最容易抄错的地方，
    所以默认值与其理由一起写在这里。
    """
    t0 = time.monotonic()
    rep = _base_report("post_close")

    try:
        from app.domain.calendar import get_calendar

        target = day or get_calendar().last_trading_day()
    except Exception:
        target = date.today()
    target = _as_date(target) or date.today()
    rep["target_day"] = target.isoformat()

    syms, info = resolve_symbols(source=source, limit=int(limit or 0),
                                 symbols=symbols, day=target)
    rep["source_info"] = info
    if info.get("error"):
        rep["skipped_reason"] = f"候选池不可用: {info['error']}"
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return rep
    if not syms:
        # 没指定候选池来源时，用**库里全部标的**做盘后补齐：
        # 盘后是兜底性质，漏一只就欠一天的数据。
        try:
            from app.data.repo import stock_info_repo as P

            syms = P.all_symbols()
        except Exception as exc:  # pragma: no cover
            logger.warning(f"列举标的失败: {exc}")
        info["note"] = "未指定来源 → 盘后按主表全量兜底"
        rep["source_info"] = info
    if not syms:
        rep["skipped_reason"] = "无标的可同步"
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return rep

    keep, buckets = _filter_symbols(syms, day=target)
    rep["requested"] = len(syms)
    rep["skipped"].update({k: v for k, v in buckets.items() if k in rep["skipped"]})

    # 谁已经有当天的数据了 —— 一次 group_by 拿全，不要逐只查
    have: Dict[str, str] = {}
    if keep:
        try:
            from app.data.repo.kline_repo import latest_dates

            have = latest_dates("daily", keep)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"查询最新日期失败（全部重采）: {exc}")
    todo = [s for s in keep if str(have.get(s) or "") < target.isoformat()]
    up_to_date = [s for s in keep if s not in todo]
    rep["skipped"]["up_to_date"] = up_to_date
    logger.info(
        f"K线盘后同步 {target} | 应采 {len(keep)} 只 → 待补 {len(todo)} 只"
        f"（已是最新 {len(up_to_date)} 只）"
    )
    if not todo:
        rep["skipped_reason"] = f"全部标的已有 {target} 的数据"
        rep["ok"] = True
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return rep

    lookback = int(lookback_days or DEFAULT_POST_LOOKBACK)
    done, failed, rows = _run_batch(
        todo, lookback, adjust, "", min_interval, on_progress=on_progress)
    rep["collected"] = len(done)
    rep["rows"] = rows
    rep["new_rows"] = sum(int(d.get("new_rows") or 0) for d in done)
    rep["failed"] = failed
    rep["symbols"] = [d["symbol"] for d in done]
    rep["ok"] = len(done) > 0
    rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
    if rep["ok"]:
        _publish_kline_updated(bus, "post_close", rep)
    return rep


# =============================================================================
# 3. 定期查漏
# =============================================================================
def scan_gaps(source: str = "", limit: int = 0,
              symbols: Optional[Sequence[str]] = None,
              lookback_days: int = 0, auto_repair: bool = True,
              max_symbols: int = 50, max_repair: int = 20,
              use_pool: bool = True, bus: Any = None) -> Dict[str, Any]:
    """定期体检：哪些票在应有的交易日里没有数据，能补的当场补。

    复用 :class:`app.modules.collection.integrity.DataIntegrityChecker`，
    本文件只负责"查哪些票"和"把结论翻译成面板能读的形状"。

    :param auto_repair: 是否联网补。``False`` = 只出报告，适合先看一眼。
    :param use_pool:    True = 体检候选池；False = 体检库里全部标的。
    """
    t0 = time.monotonic()
    rep = _base_report("gap_scan")

    syms: List[str] = []
    info: Dict[str, Any] = {}
    if symbols:
        syms, info = list(symbols), {"source": "explicit"}
    elif use_pool:
        syms, info = resolve_symbols(source=source, limit=int(limit or 0))
        if not syms:
            # 候选池取不到 → 退回库里全部标的。与前两个任务不同，
            # 体检可以容忍"范围大些、慢些"，因为它本就是低频任务。
            try:
                from app.data.repo import stock_info_repo as P

                syms = P.all_symbols()
                info = {"source": "all", "note": "候选池为空 → 退回主表全量"}
            except Exception:  # pragma: no cover
                syms = []
    else:
        try:
            from app.data.repo import stock_info_repo as P

            syms = P.all_symbols()
            info = {"source": "all"}
        except Exception as exc:  # pragma: no cover
            logger.warning(f"列举标的失败: {exc}")

    rep["source_info"] = info
    keep, buckets = _filter_symbols(syms)
    rep["requested"] = len(syms)
    rep["skipped"].update({k: v for k, v in buckets.items() if k in rep["skipped"]})
    if not keep:
        rep["skipped_reason"] = "无标的可体检"
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        rep["ok"] = True
        return rep

    try:
        from app.modules.collection.integrity import DataIntegrityChecker, summarize

        checker = DataIntegrityChecker(
            lookback_days=int(lookback_days or DEFAULT_GAP_LOOKBACK),
            auto_repair=bool(auto_repair),
            max_symbols=int(max_symbols or 0),
            max_repair=int(max_repair or 0),
        )
        report = checker.scan(keep)
    except Exception as exc:
        rep["failed"] = [{"symbol": "__scan__", "error": str(exc)[:200]}]
        rep["duration_ms"] = int((time.monotonic() - t0) * 1000)
        logger.error(f"K线缺口体检失败: {exc}")
        return rep

    try:
        rep["integrity"] = report.to_dict()
        rep["summary_text"] = summarize(report)
    except Exception:  # pragma: no cover
        rep["integrity"] = {}
    rep["checked"] = int(report.checked or 0)
    rep["missing_total"] = int(report.missing_total or 0)
    rep["repaired"] = int(report.repaired_total or 0)
    rep["anomaly_total"] = int(report.anomaly_total or 0)
    rep["collected"] = int(report.repaired_total or 0)
    rep["rows"] = int(report.repaired_total or 0)
    rep["ok"] = True
    rep["duration_ms"] = int((time.monotonic() - t0) * 1000)

    if rep["missing_total"]:
        _publish_gap(bus, report.missing_total, int(report.checked or 0))
    logger.info(
        f"K线缺口体检 | 体检 {rep['checked']} 只 | 缺口 {rep['missing_total']} 天 | "
        f"已补 {rep['repaired']} 行 | 异常行 {rep['anomaly_total']}"
    )
    return rep


# =============================================================================
# 事件
# =============================================================================
def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _publish(bus: Any, event_name: str, **payload: Any) -> None:
    """发一个事件。

    :param bus: 事件总线。由**任务处理器**注入（它手上有 ctx，
                也只有它知道这次执行处在哪个引擎里）。
                拿不到就不发 —— 这三个函数也会被 CLI 直接调用，
                那时没有总线；而事件只是"通知别人"，**数据已经入库了**，
                不该反过来让通知失败拖垮采集。
    """
    if bus is None:
        return
    try:
        bus.publish(event_name, source="pool_kline", **payload)
    except Exception as exc:  # pragma: no cover
        logger.debug(f"发布事件失败 {event_name}: {exc}")


def _publish_kline_updated(bus: Any, mode: str, rep: Dict[str, Any]) -> None:
    try:
        from app.runtime.events.types import EventType
    except Exception:  # pragma: no cover
        return
    _publish(
        bus,
        EventType.MARKET_KLINE_UPDATED,
        mode=mode,
        symbols=list(rep.get("symbols") or [])[:50],
        rows=int(rep.get("rows") or 0),
        new_rows=int(rep.get("new_rows") or 0),
        failed=len(rep.get("failed") or []),
    )


def _publish_gap(bus: Any, missing: int, checked: int) -> None:
    try:
        from app.runtime.events.types import EventType
    except Exception:  # pragma: no cover
        return
    _publish(
        bus,
        EventType.MARKET_KLINE_GAP_DETECTED,
        missing_days=int(missing),
        checked=int(checked),
    )


__all__ = [
    "resolve_symbols",
    "refresh_intraday",
    "sync_post_close",
    "scan_gaps",
    "session_guard",
]
