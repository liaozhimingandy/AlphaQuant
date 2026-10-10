#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : extdata.py
# @Description : 扩展行情采集组件：资金流 / 筹码 / 基本面 / 涨停池 / 题材
#
#                为什么不用行情采集组件（data_collector）顺手做掉：
#                  那是**逐根K线**的量价数据，按标的按周期定时抓；
#                  这里是**日频截面**数据，粒度、频率、失败语义都不一样
#                  （资金流一天只有一条，抓两次是浪费；涨停池是"市场级"的，
#                   一次就覆盖全市场，不该按标的循环抓）。
#                  硬塞进同一个组件只会让两边都变得难懂。
#
#                ============================================================
#                最重要的一条：**日频截面数据每天只采一次**
#                ============================================================
#                资金流/筹码/基本面这类数据，一天之内只有"盘中波动"和
#                "收盘定稿"两种状态。10 分钟采一次意味着一天里绝大多数请求
#                都是重复的 —— 除了把自己送进限流名单，什么也没得到。
#
#                所以采集改成按**市场状态**驱动：
#                  - 交易时段：按 interval 采一次（盘中资金流会变，有价值）
#                  - 收盘结算后：补采一次"定稿版"（当天只做一次）
#                  - 非交易日 / 深夜：一次都不采
#
#                全部在 worker 线程执行 —— 这些都是同步网络 IO。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from twisted.internet import defer, task, threads

from app.core.config import settings
from app.core.engine.components.ibase import IBaseComponent
from app.utils.logger import log_throttled, logger


class ExtDataComponent(IBaseComponent):
    """扩展行情数据采集。"""

    name = "ext_data"

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        interval: Optional[float] = None,
        daily_interval: Optional[float] = None,
        theme_top: Optional[int] = None,
        enabled: Optional[bool] = None,
        with_fundamental: Optional[bool] = None,
        categories: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.enabled = settings.EXT_DATA_ENABLED if enabled is None else bool(enabled)
        #: 要采集的**个股级**类别（龙虎榜/两融/业绩预告/股东户数/评级/舆情）
        self.categories: List[str] = list(categories) if categories else [
            "lhb", "margin", "forecast", "holders", "rating", "news",
        ]
        self.symbols: List[str] = list(symbols or [])
        self.interval = float(interval if interval is not None
                              else settings.EXT_DATA_INTERVAL)
        self.daily_interval = float(
            daily_interval if daily_interval is not None
            else settings.EXT_DATA_DAILY_INTERVAL
        )
        self.theme_top = int(theme_top if theme_top is not None
                             else settings.EXT_DATA_THEME_TOP)
        self.with_fundamental = (
            settings.EXT_DATA_FUNDAMENTAL if with_fundamental is None
            else bool(with_fundamental)
        )

        self._loop: Optional[task.LoopingCall] = None
        self._daily_loop: Optional[task.LoopingCall] = None
        self._busy = False

        #: {日期: 已完成的时间窗口集合} —— 这是"每个窗口一天只采一次"的依据
        self._slots: Dict[date, set] = {}
        #: 兼容旧字段（面板/测试会读）
        self._synced_date: Optional[date] = None
        self._final_date: Optional[date] = None
        self._last_daily: Optional[date] = None

        self.stats: Dict[str, Any] = {
            "runs": 0, "fund_flow_ok": 0, "chip_ok": 0, "limitup_ok": 0,
            "boards_ok": 0, "themes_ok": 0, "fundamental_ok": 0,
            "failed": 0, "errors": 0, "skipped_closed": 0, "skipped_synced": 0,
            "last_run_at": None, "last_daily_at": None,
        }
        self.last_report: Dict[str, Any] = {}

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        if cfg.get("symbols"):
            self.symbols = [str(s) for s in cfg["symbols"]]
        if cfg.get("interval") is not None:
            self.interval = float(cfg["interval"])
        if cfg.get("daily_interval") is not None:
            self.daily_interval = float(cfg["daily_interval"])
        if cfg.get("theme_top") is not None:
            self.theme_top = int(cfg["theme_top"])
        if cfg.get("enabled") is not None:
            self.enabled = bool(cfg["enabled"])
        if cfg.get("with_fundamental") is not None:
            self.with_fundamental = bool(cfg["with_fundamental"])
        if cfg.get("categories") is not None:
            self.categories = [str(c) for c in cfg["categories"]]

        # 没指定标的时，从引擎的市场组件拿 —— 采集服务和交易标的保持一致
        if not self.symbols:
            self.symbols = self._market_symbols()

        if not self.enabled:
            logger.info("扩展行情采集已关闭（EXT_DATA_ENABLED=false）")
            return
        if not self.symbols:
            logger.warning(
                "扩展行情采集：没有标的，只做市场级数据（涨停池/题材）。"
                "要采集个股资金流请在 config/collector.json 或本组件配置里给 symbols"
            )
        state = self._calendar().to_dict()
        logger.info(
            f"扩展行情采集就绪 | 标的 {len(self.symbols)} 个 | "
            f"资金流/筹码 {self.interval}s | 市场级 {self.daily_interval}s | "
            f"基本面 {'开' if self.with_fundamental else '关'} | "
            f"当前 {state['state_name']}"
        )

    def _calendar(self):
        from app.core.market.calendar import get_calendar

        return get_calendar()

    def _market_symbols(self) -> List[str]:
        engine = getattr(self, "_engine", None)
        if engine is None:
            return []
        for name in ("market_center", "strategy_manager"):
            comp = None
            try:
                comp = engine.get_component(name)
            except Exception:
                continue
            syms = getattr(comp, "symbols", None)
            if syms:
                return [str(s) for s in syms]
            rt = getattr(comp, "runtime", None)
            if rt is not None:
                try:
                    return [str(t.symbol) for t in rt]
                except Exception:
                    pass
        return []

    def set_engine(self, engine: Any) -> None:
        self._engine = engine

    def on_start(self) -> defer.Deferred:
        if not self.enabled:
            return defer.succeed(None)
        self._loop = task.LoopingCall(self._tick)
        self._loop.start(max(30.0, self.interval), now=False)
        self._daily_loop = task.LoopingCall(self._tick_daily)
        self._daily_loop.start(max(300.0, self.daily_interval), now=False)
        # 启动时不无脑采一次：先看市场状态，闭市就什么都不做
        self._startup_check()
        return defer.succeed(None)

    def _startup_check(self) -> None:
        """启动时的补采判断。

        盘后启动的服务最容易漏掉当天数据 ——
        定时器下一次触发可能是几小时后，而当天的收盘数据现在就该拿到。
        """
        if not self.enabled:
            return
        # 启动补采也走同一套时间窗口判断 ——
        # 凌晨三点启动的服务不该去请求"今天的"数据（那时还没有）
        slot = self._slot()
        if slot is None:
            return
        d = threads.deferToThread(self._run_once, slot)
        d.addErrback(lambda f: logger.warning(f"扩展数据启动补采失败: "
                                              f"{f.getErrorMessage()}"))
        return None

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True):
        for loop in (self._loop, self._daily_loop):
            if loop is not None and loop.running:
                loop.stop()
        self._loop = None
        self._daily_loop = None
        logger.info(f"扩展行情采集已停止 | {self.stats}")
        yield defer.succeed(None)

    def is_busy(self) -> bool:
        return self._busy

    # ============================================================
    # 采集
    # ============================================================
    def _tick(self):
        if self._busy:
            return defer.succeed(None)
        slot = self._slot()
        if slot is None:
            return defer.succeed(None)
        self._busy = True
        d = threads.deferToThread(self._run_once, slot)
        d.addBoth(self._release)
        return d

    def _release(self, result: Any) -> Any:
        self._busy = False
        if isinstance(result, Exception):
            self.stats["errors"] += 1
            log_throttled("ERROR", f"扩展行情采集异常: {result}",
                          key="extdata-error")
            return None
        return result

    def _slot(self) -> Optional[str]:
        """此刻该采哪一个时间窗口的数据。

        ``pre``  盘前（开盘前 30 分钟起）：补齐昨日漏采、预热当日标的
        ``post`` 盘后定稿（闭市 30 分钟后）：当日真正落地的数据
        ``None`` 此刻不该采集

        每个窗口**每天只做一次**。日频截面数据一天一条，
        在同一个窗口里反复拉就是在浪费请求额度。
        """
        if not self.enabled:
            return None
        slot = self._schedule().slot_of()
        if slot is None:
            self.stats["skipped_closed"] += 1
            return None
        today = date.today()
        done = self._slots.get(today) or set()
        if slot in done:
            self.stats["skipped_synced"] += 1
            return None
        return slot

    def _schedule(self):
        from app.core.collect.schedule import get_schedule

        return get_schedule()

    def _should_fetch(self) -> bool:
        """供测试与面板判断是否会发请求。"""
        return self._slot() is not None

    def _run_once(self, slot: str = "post") -> Dict[str, Any]:
        """按标的采集资金流、筹码、基本面。**单只失败不影响其它**。

        :param slot: ``pre`` 盘前 / ``post`` 盘后定稿 / ``intraday`` 盘中
        """
        from app.core.pool import (
            sync_category,
            sync_chip,
            sync_fund_flow,
            sync_fundamentals,
        )

        final = slot == "post"
        report: Dict[str, Any] = {"slot": slot}
        today = date.today()

        if self.symbols:
            r1 = sync_fund_flow(self.symbols)
            r2 = sync_chip(self.symbols)
            self.stats["fund_flow_ok"] += r1.get("ok", 0)
            self.stats["chip_ok"] += r2.get("ok", 0)
            self.stats["failed"] += (len(r1.get("failed") or [])
                                     + len(r2.get("failed") or []))
            report["fund_flow"] = r1
            report["chip"] = r2

            # 基本面（估值+财务）一天一次就够，只在收盘后采 ——
            # 盘中的估值随价格跳动，拉到的是未定稿的数，存进去反而污染序列
            if self.with_fundamental and (final or self._final_date is None):
                r3 = sync_fundamentals(self.symbols)
                self.stats["fundamental_ok"] += r3.get("ok", 0)
                self.stats["failed"] += len(r3.get("failed") or [])
                report["fundamental"] = r3

            # 其它类别（龙虎榜/两融/业绩预告/股东户数/评级/舆情）
            # 更新慢或只在特定时点有，所以**只在盘后**采一次
            if final:
                for cat in self.categories:
                    r = sync_category(cat, self.symbols)
                    self.stats["failed"] += len(r.get("failed") or [])
                    report[cat] = {"ok": r.get("ok", 0)}

        # 市场级数据（宏观/宽度）一天一次，也只在盘后
        if final:
            for cat in ("macro", "breadth"):
                r = sync_category(cat)
                report[cat] = {"ok": r.get("ok", 0)}

        self.stats["runs"] += 1
        self.stats["last_run_at"] = datetime.now().isoformat()
        self._slots.setdefault(today, set()).add(slot)
        self._synced_date = today
        if final:
            self._final_date = today

        # 采完新数据后，特征缓存必须失效 ——
        # 否则策略会一直拿着几天前的资金流在打分。
        try:
            from app.core.data.features import get_provider

            get_provider().invalidate()
        except Exception:
            pass
        self.last_report = report
        return report

    def _tick_daily(self):
        """市场级数据：涨停池 + 题材板块 + 题材成分映射。

        同样受"一天一次"约束 —— 涨停池收盘后就定稿了，
        盘后反复拉纯属浪费请求额度。
        """
        if self._busy or not self.enabled:
            return defer.succeed(None)
        cal = self._calendar()
        if not cal.is_trading_day():
            self.stats["skipped_closed"] += 1
            return defer.succeed(None)
        if self._last_daily == date.today():
            self.stats["skipped_synced"] += 1
            return defer.succeed(None)
        if not cal.is_open():
            self.stats["skipped_closed"] += 1
            return defer.succeed(None)
        self._busy = True
        d = threads.deferToThread(self._run_daily)
        d.addBoth(self._release)
        return d

    def _run_daily(self) -> Dict[str, Any]:
        from app.core.pool import sync_boards, sync_concept_members, sync_limitup

        report: Dict[str, Any] = {}
        try:
            r = sync_limitup()
            self.stats["limitup_ok"] += r.get("ok", 0)
            report["limitup"] = r
        except Exception as exc:
            self.stats["errors"] += 1
            log_throttled("WARNING", f"涨停池同步失败: {exc}", key="extdata-zt")
        try:
            r1 = sync_boards("concept")
            r2 = sync_boards("industry")
            self.stats["boards_ok"] += r1.get("ok", 0) + r2.get("ok", 0)
            report["boards"] = {"concept": r1, "industry": r2}
        except Exception as exc:
            self.stats["errors"] += 1
            log_throttled("WARNING", f"板块同步失败: {exc}", key="extdata-board")
        # 题材成分映射最慢也最没必要频繁做：归属变化很慢，一天一次足够
        try:
            r = sync_concept_members(self.theme_top)
            self.stats["themes_ok"] += r.get("ok", 0)
            report["themes"] = r
        except Exception as exc:
            self.stats["errors"] += 1
            log_throttled("WARNING", f"题材成分同步失败: {exc}", key="extdata-theme")
        self._last_daily = date.today()
        self.stats["last_daily_at"] = datetime.now().isoformat()
        self.last_report.update(report)
        return report

    # ============================================================
    # 可观测
    # ============================================================
    def snapshot(self) -> Dict[str, Any]:
        info = self._calendar().to_dict()
        sch = self._schedule().to_dict()
        return {
            "schedule": sch,
            "slots_done": {str(k): sorted(v) for k, v in self._slots.items()},
            "next_slot": self._slot(),
            "enabled": self.enabled,
            "symbols": len(self.symbols),
            "interval": self.interval,
            "daily_interval": self.daily_interval,
            "theme_top": self.theme_top,
            "with_fundamental": self.with_fundamental,
            "market": info,
            "synced_date": self._synced_date.isoformat() if self._synced_date else None,
            "final_date": self._final_date.isoformat() if self._final_date else None,
            "stats": dict(self.stats),
            "last_report": self.last_report,
        }

    def health_check(self):
        if not self.enabled:
            return True, "已关闭"
        if self.stats["errors"] and self.stats["runs"] and \
                self.stats["errors"] > self.stats["runs"]:
            return False, f"采集失败次数偏多（{self.stats['errors']} 次）"
        info = self._calendar().to_dict()
        if not info["is_trading_day"]:
            return True, f"非交易日，不采集（下次开盘 {info['next_open']}）"
        if not self.symbols:
            return True, "无个股标的，只采集市场级数据"
        return True, f"已采集 {self.stats['fund_flow_ok']} 条资金流"


__all__ = ["ExtDataComponent"]
