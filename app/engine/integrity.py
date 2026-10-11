#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : integrity.py
# @Description : 数据完整性校验：库里缺了什么、缺的那段要不要连夜补
#
#                为什么必须有这一层（这是本文件存在的全部理由）：
#                  **库里缺一根K线，回测不会报错**。
#                  它只是悄悄少一段行情 —— 均线在那几天断开、信号漂移几根、
#                  收益差一点点。没有任何一行日志会说"你少了 2026-09-30 这一天"。
#                  这类错误不会崩溃、不会告警，只会让结论慢慢偏离，
#                  是所有数据问题里最难查、也最该被自动化拦住的一类。
#
#                所以这里做的事分三步，顺序不能反：
#                  1. **比对**：拿交易日历算出"应该有哪几天"，减去库里实际有的；
#                  2. **判型**：缺失 ≠ 一定该补。停牌、退市、长期停牌的票
#                     本来就没有那些交易日的数据，硬去补只会打爆数据源；
#                  3. **补全**：确认是缺口才连数据源，走注册表的 daily 链路
#                     （权重排序 → 每源重试 → 换源 → 全挂告警）。
#
#                两个刻意的设计约束：
#                  * **只比对日期与价格，不比对成交量**。各源的成交量单位
#                    不一致（baostock=股，akshare/腾讯=手），跨源比对成交量
#                    必然误报，见 app/data/kline_base.py 的说明；
#                  * **体检不等于修复**。auto_repair=False 时只出报告不联网，
#                    方便先看清楚问题再决定要不要补。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from app.common.config import settings
from app.domain.calendar import TradeCalendar, get_calendar
from app.domain.symbol import normalize_symbol
from app.storage.schema import ChipDistribution, FundFlow, StockDaily
from app.storage.repo.stock_repo import StockRepository, session_scope
from app.common.logging import logger, log_throttled

#: 缺失天数超过这个比例，且绝对天数也超过阈值 → 判定为"停牌/退市"，
#: 不再逐个补（补了也没有，只会白打数据源）。
SUSPEND_MISSING_RATIO = 0.6
SUSPEND_MIN_DAYS = 5

#: 检查哪些表。daily 是唯一会自动补全的，其余只出报告。
TABLE_CHECKS = ("daily", "fund_flow", "chip")


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        ts = pd.to_datetime(value)
        return ts.date()
    except Exception:
        return None


@dataclass
class SymbolReport:
    """单只标的的体检结果。"""
    symbol: str
    #: 覆盖区间（库里实际有的）
    start: Optional[date] = None
    end: Optional[date] = None
    rows: int = 0
    #: 应该有但库里没有的交易日
    missing: List[date] = field(default_factory=list)
    #: 数据本身有问题（high<low / 价格非正 / OHLC 缺值）
    anomalies: List[Dict[str, Any]] = field(default_factory=list)
    #: 判定为停牌/退市 → 不补
    suspended: bool = False
    #: 实际补回去的天数
    repaired: int = 0
    #: 补的时候用的哪个源
    source: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.anomalies and not self.error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "rows": self.rows,
            "missing": [d.isoformat() for d in self.missing],
            "missing_count": len(self.missing),
            "anomalies": self.anomalies[:20],
            "anomaly_count": len(self.anomalies),
            "suspended": self.suspended,
            "repaired": self.repaired,
            "source": self.source,
            "error": self.error,
            "ok": self.ok,
        }


@dataclass
class IntegrityReport:
    """一次体检的汇总。"""
    started_at: str = ""
    duration_ms: int = 0
    checked: int = 0
    symbols: List[SymbolReport] = field(default_factory=list)
    missing_total: int = 0
    repaired_total: int = 0
    anomaly_total: int = 0
    failed: List[str] = field(default_factory=list)
    tables: Dict[str, Any] = field(default_factory=dict)
    auto_repair: bool = True

    @property
    def ok(self) -> bool:
        return self.missing_total == 0 and self.anomaly_total == 0 and not self.failed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "checked": self.checked,
            "ok": self.ok,
            "missing_total": self.missing_total,
            "repaired_total": self.repaired_total,
            "anomaly_total": self.anomaly_total,
            "failed": self.failed[:20],
            "auto_repair": self.auto_repair,
            "tables": self.tables,
            "symbols": [s.to_dict() for s in self.symbols],
        }


class DataIntegrityChecker:
    """库里的数据对不对、缺的那段能不能补回来。

    :param lookback_days: 每只标的回溯多少天找缺口
    :param auto_repair: 是否自动补采（False = 只体检不联网）
    :param max_repair: 单次最多补多少只（控制请求量）
    """

    def __init__(
        self,
        calendar: Optional[TradeCalendar] = None,
        lookback_days: int = 0,
        auto_repair: bool = True,
        max_repair: int = 20,
        max_symbols: int = 50,
        checks: Optional[Sequence[str]] = None,
        db: Any = None,
    ) -> None:
        self.calendar = calendar or get_calendar()
        self.lookback_days = int(
            lookback_days or settings.DATA_GUARD_LOOKBACK_DAYS
        )
        self.auto_repair = bool(auto_repair)
        self.max_repair = max(0, int(max_repair))
        self.max_symbols = max(0, int(max_symbols))
        self.checks = tuple(checks or settings.data_guard_check_list()
                            or TABLE_CHECKS)
        self._db = db

    # ===================================================================
    # 期望交易日
    # ===================================================================
    def expected_days(self, start: date, end: date) -> List[date]:
        """[start, end] 区间内的交易日（含端点）。"""
        out: List[date] = []
        if start > end:
            return out
        d = start
        # 上限保护：lookback 被配成极端值时别把内存打满
        for _ in range(2000):
            if d > end:
                break
            if self.calendar.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def window(self, cov: Optional[Dict[str, Any]]) -> tuple[date, date]:
        """确定本次要看的区间。

        右端点取**最近交易日**而不是"今天"：今天是周日的话，
        把周日也算进"应该有数据"的日子，会凭空造出一堆假缺口。
        左端点 = max(库里最早那天, 最近交易日 - lookback)。
        """
        end = self.calendar.last_trading_day()
        start = end - timedelta(days=self.lookback_days)
        if cov and cov.get("start"):
            got = _as_date(cov["start"])
            if got and got > start:
                start = got
        return start, end

    # ===================================================================
    # 单只标的
    # ===================================================================
    def check_symbol(self, symbol: str, repair: Optional[bool] = None
                     ) -> SymbolReport:
        """体检一只标的：缺哪几天、有没有脏数据、（可选）补回去。"""
        code = normalize_symbol(symbol)
        rep = SymbolReport(symbol=code)
        try:
            with session_scope(self._db) as db:
                cov = StockRepository.get_coverage(db, code)
                if not cov:
                    rep.error = "库中无任何日线数据"
                    return rep
                rep.rows = int(cov.get("rows") or 0)
                rep.start = _as_date(cov.get("start"))
                rep.end = _as_date(cov.get("end"))

                start, end = self.window(cov)
                have = self._existing_days(db, code, start, end)
                expected = self.expected_days(start, end)
                rep.missing = [d for d in expected if d not in have]

                # 缺失太多 → 大概率是停牌/退市/新股未上市，不是"漏采"
                if expected and len(rep.missing) >= SUSPEND_MIN_DAYS and (
                    len(rep.missing) / len(expected) >= SUSPEND_MISSING_RATIO
                ):
                    rep.suspended = True

                rep.anomalies = self._find_anomalies(db, code, start, end)
        except Exception as exc:
            rep.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning(f"数据校验 {code} 失败: {rep.error}")
            return rep

        do_repair = self.auto_repair if repair is None else bool(repair)
        if do_repair and rep.missing and not rep.suspended:
            self._repair(rep)
        return rep

    @staticmethod
    def _existing_days(db, symbol: str, start: date, end: date) -> set:
        rows = (
            db.query(StockDaily.date)
            .filter(
                StockDaily.symbol == symbol,
                StockDaily.date >= start,
                StockDaily.date <= end,
            )
            .all()
        )
        return {_as_date(r[0]) for r in rows if _as_date(r[0])}

    @staticmethod
    def _find_anomalies(db, symbol: str, start: date, end: date,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """找出"数据存在但明显不对"的行。

        这些不是缺数据，而是**坏数据** —— 比缺失更危险，因为查覆盖度查不出来。
        """
        rows = (
            db.query(StockDaily)
            .filter(
                StockDaily.symbol == symbol,
                StockDaily.date >= start,
                StockDaily.date <= end,
            )
            .order_by(StockDaily.date.asc())
            .all()
        )
        bad: List[Dict[str, Any]] = []
        for r in rows:
            why = ""
            o, h, l, c = r.open, r.high, r.low, r.close
            if None in (o, h, l, c):
                why = "OHLC 有空值"
            elif h is not None and l is not None and h < l:
                why = "最高价 < 最低价"
            elif c is not None and c <= 0:
                why = "收盘价非正"
            elif h is not None and max(o or 0, c or 0) > h:
                why = "最高价 < 开/收盘价"
            elif l is not None and min(o or h, c or h) < l:
                why = "最低价 > 开/收盘价"
            if why:
                bad.append({
                    "date": _as_date(r.date).isoformat() if _as_date(r.date) else str(r.date),
                    "reason": why,
                    "open": o, "high": h, "low": l, "close": c,
                })
                if len(bad) >= limit:
                    break
        return bad

    # ===================================================================
    # 补全
    # ===================================================================
    def _repair(self, rep: SymbolReport) -> None:
        """把缺失的交易日补回来。走注册表的 daily 链路（自动换源）。"""
        days = sorted(rep.missing)
        try:
            from app.datasource.compat.kline_sources import fetch_daily

            df = fetch_daily(
                rep.symbol, start=days[0], end=days[-1], adjust="qfq"
            )
        except Exception as exc:
            rep.error = f"补全失败: {type(exc).__name__}: {str(exc)[:200]}"
            logger.warning(f"数据补全失败 {rep.symbol}: {rep.error}")
            return

        if df is None or df.empty:
            rep.error = "数据源未返回该区间的日线"
            return

        wanted = set(days)
        got = df[df["date"].isin(wanted)]
        if got.empty:
            # 源里也没有 → 大概率是停牌，别每轮都重试
            rep.error = "数据源中也没有这些交易日的数据（停牌？）"
            return

        try:
            with session_scope(self._db) as db:
                written = StockRepository.save_df(db, got, commit=False)
        except Exception as exc:
            rep.error = f"补全入库失败: {type(exc).__name__}: {str(exc)[:200]}"
            logger.warning(f"数据补全入库失败 {rep.symbol}: {rep.error}")
            return

        filled = {_as_date(d) for d in got["date"] if _as_date(d)}
        rep.missing = [d for d in rep.missing if d not in filled]
        rep.repaired = int(written or len(got))
        try:
            from app.datasource.compat.registry import REGISTRY

            rep.source = REGISTRY.used_sources().get("daily", "")
        except Exception:
            rep.source = ""
        logger.info(
            f"数据补全 | {rep.symbol} | 补回 {rep.repaired} 行"
            f"（{days[0]}~{days[-1]}，源: {rep.source or '未知'}）"
        )

    # ===================================================================
    # 表级检查（除日线外的其它表）
    # ===================================================================
    def check_tables(self) -> Dict[str, Any]:
        """各表的整体覆盖情况。

        只出报告、不自动补：这些表的"缺口"含义各不相同
        （资金流没数据可能是停牌，也可能是没同步），
        贸然补只会制造噪声。先把数量摆出来，人来判断要不要补。
        """
        out: Dict[str, Any] = {}
        try:
            from sqlalchemy import func

            with session_scope(self._db) as db:
                if "daily" in self.checks:
                    row = db.query(
                        func.count(StockDaily.id),
                        func.count(func.distinct(StockDaily.symbol)),
                        func.max(StockDaily.date),
                    ).first()
                    out["daily"] = {
                        "rows": int(row[0] or 0),
                        "symbols": int(row[1] or 0),
                        "latest": str(row[2]) if row[2] else None,
                    }
                if "fund_flow" in self.checks:
                    row = db.query(
                        func.count(FundFlow.id),
                        func.count(func.distinct(FundFlow.symbol)),
                        func.max(FundFlow.date),
                    ).first()
                    out["fund_flow"] = {
                        "rows": int(row[0] or 0),
                        "symbols": int(row[1] or 0),
                        "latest": str(row[2]) if row[2] else None,
                    }
                if "chip" in self.checks:
                    row = db.query(
                        func.count(ChipDistribution.id),
                        func.count(func.distinct(ChipDistribution.symbol)),
                        func.max(ChipDistribution.date),
                    ).first()
                    out["chip"] = {
                        "rows": int(row[0] or 0),
                        "symbols": int(row[1] or 0),
                        "latest": str(row[2]) if row[2] else None,
                    }
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return out

    # ===================================================================
    # 扫描
    # ===================================================================
    def scan(self, symbols: Optional[Sequence[str]] = None,
             on_progress: Optional[Callable[[int, int, bool], None]] = None
             ) -> IntegrityReport:
        """体检一批标的（默认库里全部标的，受 max_symbols 限制）。

        :param on_progress: 每检完一只回调 ``(已完成数, 总数, 该只是否异常)``。
            给面板的"已完成 / 全部"用 —— 体检几十只票要好几分钟，
            没有进度就只能干等，看不出是在跑还是卡住了。
        """
        t0 = time.monotonic()
        report = IntegrityReport(
            started_at=datetime.now().isoformat(timespec="seconds"),
            auto_repair=self.auto_repair,
        )

        targets = list(symbols or [])
        if not targets:
            try:
                with session_scope(self._db) as db:
                    targets = [r["symbol"] for r in StockRepository.list_symbols(db)]
            except Exception as exc:
                report.failed.append(f"列举标的失败: {exc}")
                targets = []
        if self.max_symbols:
            targets = targets[: self.max_symbols]

        total = len(targets)
        repairs = 0
        for idx, sym in enumerate(targets):
            rep = self.check_symbol(sym)
            if on_progress is not None:
                try:
                    on_progress(idx + 1, total, not rep.ok)
                except Exception:  # pragma: no cover - 进度回调不能拖垮体检
                    pass
            report.symbols.append(rep)
            report.missing_total += len(rep.missing)
            report.anomaly_total += len(rep.anomalies)
            if rep.error and rep.symbol not in report.failed:
                report.failed.append(f"{rep.symbol}: {rep.error[:120]}")
            if rep.repaired:
                report.repaired_total += rep.repaired
                repairs += 1
            # 单次体检的补采上限：宁可下一轮再补，也不要启动瞬间打出几百个请求
            if repairs >= self.max_repair and self.max_repair > 0:
                logger.info(
                    f"数据校验已达单次补采上限 {self.max_repair} 只，"
                    f"剩余标的下一轮继续"
                )
                break

        report.checked = len(report.symbols)
        report.tables = self.check_tables()
        report.duration_ms = int((time.monotonic() - t0) * 1000)
        return report


def quick_check(symbol: str, lookback_days: int = 120,
                auto_repair: bool = False) -> SymbolReport:
    """单只标的的便捷入口（CLI / 面板用）。"""
    return DataIntegrityChecker(
        lookback_days=lookback_days, auto_repair=auto_repair, max_symbols=1
    ).check_symbol(symbol)


def summarize(report: IntegrityReport) -> str:
    """一行摘要，给日志和面板用。"""
    if report.checked == 0:
        return "数据校验：库中无标的可查"
    bits = [f"检查 {report.checked} 只"]
    if report.missing_total:
        bits.append(f"缺口 {report.missing_total} 天")
    if report.repaired_total:
        bits.append(f"已补 {report.repaired_total} 行")
    if report.anomaly_total:
        bits.append(f"异常行 {report.anomaly_total}")
    if report.failed:
        bits.append(f"失败 {len(report.failed)} 只")
    if not (report.missing_total or report.anomaly_total or report.failed):
        bits.append("未发现问题")
    return "数据校验：" + " | ".join(bits)


__all__ = [
    "DataIntegrityChecker",
    "IntegrityReport",
    "SymbolReport",
    "quick_check",
    "summarize",
]
