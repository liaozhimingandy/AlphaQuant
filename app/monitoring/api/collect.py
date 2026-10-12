#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集编排视图：立即补采/计划查看与保存/交易日历。"""
from __future__ import annotations


from datetime import date, datetime
from typing import Any, Deque, Dict, List, Optional

from app.common.logging import logger


class CollectViewsMixin:
    # ============================================================
    # 数据采集（标的即时采集 + 时间窗口设置）
    # ============================================================
    def collect_now(self, symbols: Any = None,
                    categories: Any = None) -> Dict[str, Any]:
        """立即采集一批标的的**全类别**数据（工作线程执行）。

        用户选了一只新票，最想的是"马上看看它的因子长什么样"，
        而不是等下一个采集窗口。所以这里把所有类别都跑一遍。
        """
        try:
            from app.modules.universe.pool import sync_category

            syms = self._factor_symbols(symbols)
            if not syms:
                return {"ok": False, "error": "没有指定标的"}
            cats = [str(c) for c in (categories or [])] or [
                "lhb", "margin", "forecast", "holders", "rating", "news",
            ]
            report: Dict[str, Any] = {"ok": True, "symbols": syms, "detail": {}}

            # 先采量价类（有行情才能算技术/表达式因子）
            try:
                from app.data.service import MarketDataService

                rows = 0
                for s in syms:
                    info = MarketDataService.collect_incremental(
                        s, lookback_days=365, adjust="qfq")
                    rows += int(info.get("rows") or 0)
                report["detail"]["daily"] = {"ok": len(syms), "rows": rows}
            except Exception as exc:
                report["detail"]["daily"] = {"ok": 0, "error": str(exc)[:120]}

            # 再采日频截面
            from app.modules.universe.pool import sync_chip, sync_fund_flow, sync_fundamentals

            for label, fn in (("fundflow", lambda: sync_fund_flow(syms)),
                              ("chip", lambda: sync_chip(syms)),
                              ("fundamental", lambda: sync_fundamentals(syms))):
                try:
                    r = fn()
                    report["detail"][label] = {
                        "ok": r.get("ok", 0),
                        "failed": (r.get("failed") or [])[:5],
                    }
                except Exception as exc:
                    report["detail"][label] = {"ok": 0, "error": str(exc)[:120]}

            for cat in cats:
                try:
                    r = sync_category(cat, syms)
                    report["detail"][cat] = {
                        "ok": r.get("ok", 0),
                        "failed": (r.get("failed") or [])[:5],
                    }
                except Exception as exc:
                    report["detail"][cat] = {"ok": 0, "error": str(exc)[:120]}

            # 市场级
            for cat in ("macro", "breadth"):
                try:
                    r = sync_category(cat)
                    report["detail"][cat] = {"ok": r.get("ok", 0)}
                except Exception as exc:
                    report["detail"][cat] = {"ok": 0, "error": str(exc)[:120]}

            # 采完必须失效特征缓存，否则界面上看到的还是旧值
            try:
                from app.modules.factor.features import get_provider

                get_provider().invalidate()
            except Exception:
                pass
            return report
        except Exception as exc:
            logger.error(f"即时采集失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def schedule_view(self) -> Dict[str, Any]:
        """采集时间窗口 + 当前处在哪个窗口。"""
        try:
            from app.modules.collection.schedule import get_schedule
            from app.domain.calendar import get_calendar

            sch = get_schedule()
            slot = sch.slot_of()
            return {
                "schedule": sch.to_dict(),
                "current_slot": slot,
                "slot_names": {"pre": "盘前", "intraday": "盘中",
                               "post": "盘后定稿"},
                "market": get_calendar().to_dict(),
            }
        except Exception as exc:
            logger.debug(f"读取采集时间窗口失败: {exc}")
            return {}

    def schedule_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.modules.collection.schedule import (
                CollectionSchedule,
                get_schedule,
                set_schedule,
            )

            base = get_schedule()
            fields = {f for f in CollectionSchedule.__dataclass_fields__}
            raw = {k: v for k, v in body.items() if k in fields}
            if not raw:
                return {"ok": False, "message": "没有可识别的字段"}
            merged = CollectionSchedule.from_dict({**base.to_dict(), **raw})
            set_schedule(merged, persist=True)
            # 审计：改采集时间会影响"什么时候有数据"，属于值得留痕的操作
            audit = getattr(self, "audit", None)
            if audit is not None:
                audit.append({
                    "at": datetime.now().isoformat(), "action": "save_schedule",
                    "detail": {k: getattr(merged, k) for k in raw},
                })
            return {"ok": True, "message": "已保存", "schedule": merged.to_dict()}
        except Exception as exc:
            logger.error(f"保存采集时间窗口失败: {exc}", exc_info=True)
            return {"ok": False, "message": str(exc)}

    def calendar_view(self) -> Dict[str, Any]:
        """交易日历与市场状态。

        面板要能回答"为什么它不采数据" —— 闭市是**正常行为**，
        但如果不显示出来，用户看到"采集 0 次"只会以为坏了。
        """
        try:
            from app.domain.calendar import STATE_NAMES, get_calendar

            cal = get_calendar()
            info = cal.to_dict()
            info["state_names"] = dict(STATE_NAMES)
            return info
        except Exception as exc:
            logger.debug(f"交易日历读取失败: {exc}")
            return {}
