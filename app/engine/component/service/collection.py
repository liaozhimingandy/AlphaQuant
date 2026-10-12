#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : collection.py
# @Description : 采集服务 —— 把"数据怎么进库"这件事收在一处
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

重构说明
========

这里由原 ``DataCollectorComponent``（K 线采集）、``ExtDataComponent``
（日频截面）、``DataGuardComponent``（完整性与补全）**三个平级组件
合并而成**。

为什么要合并
------------

它们本来就是一件事的三个面：**让库里的数据变完整**。
分成三个平级组件时，引擎里出现三个都要"定时跑、都要报进度、
都要被面板单独展示"的东西，而用户的心智模型只有一个——"采集服务"。
更实际的麻烦是：想关掉采集要在三个地方关，漏一个就还在联网。

合并后**对外是一个服务、一个状态、一份快照**，
内部按三个单元分区（方法名带 ``_kline`` / ``_cross`` / ``_guard`` 后缀），
既保留了原来各自的精细语义，又让"采集"在引擎里只有一个开关。

为什么方法名要加后缀
--------------------

三个源类里有大量同名方法（``_tick`` / ``_release`` / ``snapshot`` /
``health_check`` / ``is_busy`` 各重复 2~3 次）。合并进同一个类
就必须区分，否则后面的定义会静默覆盖前面的 ——
这类覆盖不会报错，只会让"资金流采集"悄悄变成"什么都没做"。

本文件由 ``scripts/merge_services.py`` 生成，方法体逐个搬运、
未改写逻辑；可以用 ``python scripts/merge_services.py --check`` 复核。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from twisted.internet import defer, task, threads

import sys

from app.engine.component.base import Progress
from app.engine.component.kinds import Cap, RestartPolicy, ServiceComponent
from app.engine.collect.spec import (
    CollectJob,
    CollectorSpec,
    DAILY_PERIOD,
    humanize_frequency,
    is_intraday,
    load_collector_spec,
    normalize_period,
    parse_frequency,
)
from app.engine.integrity import DataIntegrityChecker, IntegrityReport, summarize
from app.engine.event import StandardEvents
from app.domain.symbol import DataFetchError, normalize_symbol
from app.common.config import settings
from app.common.logging import log_throttled, logger


class CollectionService(ServiceComponent):
    """采集服务。

    **提供的能力**（见 :class:`~app.engine.component.kinds.Cap`）：
      * ``collect.kline``     —— 逐根 K 线，按标的按周期定时抓
      * ``collect.cross``     —— 日频截面：资金流 / 筹码 / 基本面 / 涨停池 / 题材
      * ``collect.integrity`` —— 体检 + 缺口自动补全

    内部三个单元
    ------------
    ============  =====================  ================================
    单元          管什么                  节拍
    ============  =====================  ================================
    ``_kline``    K 线（量价）           按任务频率，分钟级到日级
    ``_cross``    资金流/筹码/涨停池等    按市场状态：盘前/盘后各一次
    ``_guard``    完整性与补全            启动后延后首检，之后周期性
    ============  =====================  ================================

    节拍不同是它们当初被写成三个组件的**唯一正当理由**，
    所以合并后这个差异被完整保留，只是不再各自占一个引擎槽位。

    三个单元共用一套"上一轮没跑完就跳过本轮"的保护：
    网络慢的时候堆积请求只会让数据源更慢，最后被限流，
    比漏采一次严重得多。
    """

    name = "collection_service"
    provides = frozenset({Cap.COLLECT_KLINE, Cap.COLLECT_CROSS, Cap.DATA_INTEGRITY})
    restart_policy = RestartPolicy.ON_ERROR

    #: 三个单元各自的进度分区名（与合并时的方法后缀一一对应）
    _UNIT_PROGRESS: tuple = ("kline", "cross", "guard")

    def __init__(
        self,
        symbols: Optional[Sequence[str]] = None,
        # ---- K 线单元 ----
        kline_spec: Optional[CollectorSpec] = None,
        kline_symbols: Optional[Sequence[str]] = None,
        kline_interval: Any = None,
        kline_period: Any = None,
        kline_adjust: str = "",
        kline_source: str = "",
        kline_min_interval: Optional[float] = None,
        kline_config_path: Optional[str] = None,
        kline_enabled: Optional[bool] = None,
        # ---- 日频截面单元 ----
        cross_symbols: Optional[Sequence[str]] = None,
        cross_interval: Optional[float] = None,
        cross_daily_interval: Optional[float] = None,
        cross_theme_top: Optional[int] = None,
        cross_enabled: Optional[bool] = None,
        cross_with_fundamental: Optional[bool] = None,
        cross_categories: Optional[List[str]] = None,
        # ---- 完整性单元 ----
        guard_symbols: Optional[Sequence[str]] = None,
        guard_interval: Optional[float] = None,
        guard_start_delay: Optional[float] = None,
        guard_lookback_days: Optional[int] = None,
        guard_auto_repair: Optional[bool] = None,
        guard_max_symbols: Optional[int] = None,
        guard_max_repair: Optional[int] = None,
        guard_enabled: Optional[bool] = None,
        # ---- 服务级 ----
        enabled: Optional[bool] = None,
        disabled_reason: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__()
        #: 默认标的。三个单元都没单独指定时用它 —— 采集的对象
        #: 通常就是策略要交易的那些票，不该让用户填三遍。
        self.symbols: List[str] = [str(s) for s in (symbols or [])]

        # ---------------- K 线单元 ----------------
        self._pending_kline_spec = kline_spec
        self._kline_symbols = list(kline_symbols or self.symbols)
        self._kline_interval = kline_interval
        self._kline_period = kline_period
        self._kline_adjust = kline_adjust
        self._kline_source = kline_source
        self._kline_min_interval = kline_min_interval
        self._kline_config_path = kline_config_path
        self._kline_enabled = kline_enabled
        self.kline_spec: CollectorSpec = kline_spec or self._load_kline_spec({})
        self._loops_kline: Dict[float, task.LoopingCall] = {}
        self._busy_kline: Dict[float, bool] = {}
        self._job_state_kline: Dict[str, Dict[str, Any]] = {}
        self._run_counts_kline: Dict[float, int] = {}
        self.stats_kline: Dict[str, int] = {
            "runs": 0, "rows": 0, "errors": 0, "skipped": 0, "out_of_session": 0,
        }
        self._sync_kline_state()

        # ---------------- 日频截面单元 ----------------
        self._cross_enabled = (
            settings.EXT_DATA_ENABLED if cross_enabled is None else bool(cross_enabled)
        )
        self.cross_categories: List[str] = list(cross_categories) if cross_categories else [
            "lhb", "margin", "forecast", "holders", "rating", "news",
        ]
        self.cross_symbols: List[str] = list(cross_symbols or self._kline_symbols)
        self.cross_interval = float(
            cross_interval if cross_interval is not None else settings.EXT_DATA_INTERVAL
        )
        self.cross_daily_interval = float(
            cross_daily_interval if cross_daily_interval is not None
            else settings.EXT_DATA_DAILY_INTERVAL
        )
        self.cross_theme_top = int(
            cross_theme_top if cross_theme_top is not None
            else settings.EXT_DATA_THEME_TOP
        )
        self.cross_with_fundamental = (
            settings.EXT_DATA_FUNDAMENTAL if cross_with_fundamental is None
            else bool(cross_with_fundamental)
        )
        self._cross_loop: Optional[task.LoopingCall] = None
        self._cross_daily_loop: Optional[task.LoopingCall] = None
        self._busy_cross = False
        self._cross_slots: Dict[date, set] = {}
        self._cross_synced_date: Optional[date] = None
        self._cross_final_date: Optional[date] = None
        self._cross_last_daily: Optional[date] = None
        self.stats_cross: Dict[str, Any] = {
            "runs": 0, "fund_flow_ok": 0, "chip_ok": 0, "limitup_ok": 0,
            "boards_ok": 0, "themes_ok": 0, "fundamental_ok": 0,
            "failed": 0, "errors": 0, "skipped_closed": 0, "skipped_synced": 0,
            "last_run_at": None, "last_daily_at": None,
        }
        self.cross_last_report: Dict[str, Any] = {}

        # ---------------- 完整性单元 ----------------
        self._guard_enabled = (
            settings.DATA_GUARD_ENABLED if guard_enabled is None else bool(guard_enabled)
        )
        self.guard_symbols: List[str] = list(guard_symbols or self._kline_symbols)
        self.guard_interval = float(
            guard_interval if guard_interval is not None
            else settings.DATA_GUARD_INTERVAL
        )
        self.guard_start_delay = float(
            guard_start_delay if guard_start_delay is not None
            else settings.DATA_GUARD_START_DELAY
        )
        self.guard_lookback_days = int(
            guard_lookback_days if guard_lookback_days is not None
            else settings.DATA_GUARD_LOOKBACK_DAYS
        )
        self.guard_auto_repair = (
            settings.DATA_GUARD_AUTO_REPAIR if guard_auto_repair is None
            else bool(guard_auto_repair)
        )
        self.guard_max_symbols = int(
            guard_max_symbols if guard_max_symbols is not None
            else settings.DATA_GUARD_MAX_SYMBOLS
        )
        self.guard_max_repair = int(
            guard_max_repair if guard_max_repair is not None
            else settings.DATA_GUARD_MAX_REPAIR
        )
        self._guard_loop: Optional[task.LoopingCall] = None
        # 首检的延迟回调句柄。必须存下来，否则在首检延迟窗口内停止服务，
        # 回调照样会触发、照常联网做一次体检。
        self._guard_start_call: Optional[Any] = None
        self._busy_guard = False
        self._guard_report: Optional[IntegrityReport] = None
        self.stats_guard: Dict[str, int] = {
            "runs": 0, "missing": 0, "repaired": 0, "anomalies": 0, "errors": 0,
        }

        # ---------------- 服务级 ----------------
        if enabled is not None:
            self.enabled = bool(enabled)
        self.disabled_reason = str(disabled_reason or "")
        self.kline_disabled_reason = str(disabled_reason or "")
        self.guard_disabled_reason = str(disabled_reason or "")
        #: 引擎引用（日频截面单元要据此推导标的），由装配器注入
        self._engine: Optional[Any] = None
        #: 三个单元**各自**的进度对象。为什么不能共用一份，见
        #: :meth:`_caller_unit` 上方的说明。
        self._unit_p: Dict[str, Progress] = {
            unit: Progress() for unit in self._UNIT_PROGRESS
        }
        # 具名字面量：搬来的方法体里若有**直接读**进度的地方
        # （体检单元的"只在第一轮开一轮"判断），读的必须是自己那一份 ——
        # 读错成服务级那一份会导致每只标的都重开一轮进度。
        self._progress_kline = self._unit_p["kline"]
        self._progress_cross = self._unit_p["cross"]
        self._progress_guard = self._unit_p["guard"]

    # ============================================================
    # 三个单元的开关（只读视图）
    # ============================================================
    # 内部存的都是带下划线的私有标志（避免与 IBaseComponent.enabled 混淆），
    # 但面板/CLI/测试要读它们 —— 直接暴露私有字段会让"读"和"写"分不清，
    # 所以用只读属性转发。
    @property
    def kline_enabled(self) -> bool:
        """K 线采集是否启用。真正的开关在 ``kline_spec.enabled`` 里。"""
        return bool(getattr(self.kline_spec, "enabled", False))

    @property
    def cross_enabled(self) -> bool:
        return bool(self._cross_enabled)

    @property
    def guard_enabled(self) -> bool:
        return bool(self._guard_enabled)

    # ============================================================
    # 引擎协作
    # ============================================================
    def set_engine(self, engine: Any) -> None:
        """装配器注入引擎引用。仅用于"没指定标的时跟随引擎标的"。"""
        self._engine = engine

    def on_initialize(self) -> None:
        self._init_kline()
        self._init_cross()
        self._init_guard()

    @defer.inlineCallbacks
    def on_start(self) -> defer.Deferred:
        # 顺序有讲究：K 线先起来把当天数据喂进库，
        # 完整性单元后跑（它本身还延后了首检），才不会把
        # "还没来得及采"误判成"缺数据"然后急着联网补一轮。
        yield self._start_kline()
        yield self._start_cross()
        yield self._start_guard()

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        # 逆序：先停补全（它可能正在联网），再停截面，最后停 K 线
        yield self._stop_guard()
        yield self._stop_cross()
        yield self._stop_kline()

    def is_busy(self) -> bool:
        """任一单元还在干活，引擎就不该被判定为空闲。"""
        return (
            self._is_busy_kline()
            or self._is_busy_cross()
            or self._is_busy_guard()
        )

    # ============================================================
    # 进度：三个单元各一份，不共用
    # ============================================================
    # 合并前三个组件各有各的 ``Progress``。合并进同一个类后如果继续共用
    # 基类那一 ``self._progress``，K 线单元（每分钟一轮）会把日频截面单元
    # （一天两轮）的进度条刷成 "1/68"，面板上就再也看不到截面跑到哪了。
    # 而"看不到进度"恰好是进度功能存在的理由 ——
    # 看不到就以为卡死，以为卡死就重启，重启又丢一轮数据。
    #
    # 所以三个单元各发一份。难点在于搬来的方法体里写的都是
    # ``self.start_progress(...)``：逐个改就要改生成产物，
    # 而产物下一次 ``merge_services.py --apply`` 会被覆盖回去。
    #
    # 于是按**调用方方法名里的单元后缀**路由（``_kline`` / ``_cross`` /
    # ``_guard``）。这个后缀正是合并时的命名约定，由生成脚本保证，
    # 并由 ``test_progress_routing`` 守着。认不出来就退回服务级进度 ——
    # 行为与合并前一致，不会更差。
    #: 进度转发方法。在栈上跳过它们，找到真正发起进度写入的那个单元方法。
    _PROGRESS_ROUTERS = frozenset({
        "_caller_unit", "_scoped_progress",
        "start_progress", "advance_progress", "finish_progress",
    })

    def _caller_unit(self) -> str:
        """这次进度写入属于哪个单元；认不出返回 ``""``（服务级）。"""
        try:
            frame = sys._getframe(1)
        except (AttributeError, ValueError):   # pragma: no cover
            return ""
        depth = 0
        while frame is not None and depth < 10:
            is_router = (
                frame.f_globals.get("__name__") == __name__
                and frame.f_code.co_name in self._PROGRESS_ROUTERS
            )
            if not is_router:
                name = frame.f_code.co_name
                for unit in self._UNIT_PROGRESS:
                    if f"_{unit}" in name:
                        return unit
                return ""
            depth += 1
            frame = frame.f_back
        return ""

    def _scoped_progress(self) -> Progress:
        """本次写入该落到哪份进度上。"""
        return {
            "kline": self._progress_kline,
            "cross": self._progress_cross,
            "guard": self._progress_guard,
        }.get(self._caller_unit()) or self._progress

    def start_progress(self, total: int, label: str = "") -> None:
        saved, self._progress = self._progress, self._scoped_progress()
        try:
            super().start_progress(total, label)
        finally:
            self._progress = saved

    def advance_progress(self, n: int = 1, failed: bool = False) -> None:
        saved, self._progress = self._progress, self._scoped_progress()
        try:
            super().advance_progress(n, failed)
        finally:
            self._progress = saved

    def finish_progress(self) -> None:
        saved, self._progress = self._progress, self._scoped_progress()
        try:
            super().finish_progress()
        finally:
            self._progress = saved

    def progress(self) -> Dict[str, Any]:
        """面板要的是"一个组件一个进度"。合并后这里返回**最值得看的那一份**：
        正在跑的单元优先；都在歇着时给最近动过的那个；都没动过给服务级。
        """
        running = [p for p in self._unit_p.values() if p.running]
        if running:
            return running[0].to_dict()
        touched = [p for p in self._unit_p.values() if p.updated_at]
        if touched:
            return max(touched, key=lambda p: p.updated_at or 0.0).to_dict()
        return self._progress.to_dict()

    def unit_progress(self) -> Dict[str, Dict[str, Any]]:
        """三个单元的进度全貌（面板想分别展示时用）。"""
        return {unit: p.to_dict() for unit, p in self._unit_p.items()}

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base["progress_units"] = self.unit_progress()
        try:
            base["kline"] = self._snapshot_kline()
        except Exception as exc:
            base["kline_error"] = str(exc)
        try:
            base["cross"] = self._snapshot_cross()
        except Exception as exc:
            base["cross_error"] = str(exc)
        try:
            base["guard"] = self._snapshot_guard()
        except Exception as exc:
            base["guard_error"] = str(exc)
        base["symbols"] = list(self.symbols)
        base["disabled_reason"] = self.disabled_reason
        return base

    def health_check(self):
        if not self.enabled:
            return True, "disabled"
        if self.state != "RUNNING":
            return False, self.state
        # 三个单元各自判断，任一不健康就整体标红。
        # 只报"服务在跑"是不够的——K 线采集中全挂了而截面正常，
        # 面板上不该还是一片绿。
        bad: List[str] = []
        for label, fn in (
            ("K线", self._health_check_kline),
            ("截面", self._health_check_cross),
            ("完整性", self._health_check_guard),
        ):
            try:
                ok, detail = fn()
                if not ok:
                    bad.append(f"{label}: {detail}")
            except Exception as exc:
                bad.append(f"{label}: 检查异常 {exc}")
        if bad:
            return False, " | ".join(bad)
        return True, "采集服务正常"

    # ============================================================
    # 对外 API（沿用旧组件的方法名，调用方零改动）
    # ============================================================
    def set_interval(self, seconds: Any, symbol: str = "", period: str = "") -> int:
        """现场改 K 线采集频率，立刻生效。"""
        return self._set_kline_interval(seconds, symbol, period)

    def add_symbol(self, symbol: str, period: str = "", interval: Any = None,
                   **params: Any):
        return self._add_kline_symbol(symbol, period, interval, **params)

    def remove_symbol(self, symbol: str, period: str = "") -> int:
        return self._remove_kline_symbol(symbol, period)

    def reload_config(self, path: Optional[str] = None) -> Dict[str, Any]:
        return self._reload_kline_config(path)

    def collect_now(self, symbol: str = "") -> defer.Deferred:
        """立刻采一次 K 线（面板的"立即采集"按钮）。"""
        return self._collect_kline_now(symbol)

    def check_now(self, symbols: Optional[Sequence[str]] = None,
                  auto_repair: Optional[bool] = None) -> defer.Deferred:
        """立刻体检一次数据完整性（面板/CLI 的"立即体检"）。"""
        return self._check_guard_now(symbols, auto_repair)

    def collect_cross_now(self, slot: str = "post") -> defer.Deferred:
        """立刻跑一轮日频截面采集。

        带忙碌保护：面板上连点两下就会同时跑两轮全市场采集
        （十几个接口 × 数百只票），既打满线程池、又把自己往限流里推。
        定时路径（``_tick_cross``）本来就有这道保护，手动入口不能例外。
        """
        if self._busy_cross:
            logger.warning("日频截面采集仍在进行中，忽略本次手动触发")
            return defer.succeed(None)
        self._busy_cross = True
        d = threads.deferToThread(self._run_cross_once, slot)
        d.addBoth(self._release_cross)
        return d


# ============================================================
# 三个单元的方法（由脚本从原组件搬来，方法体未改写）
# ============================================================


# ------------------------------------------------------------------
# 单元：kline（原 DataCollectorComponent）
# ------------------------------------------------------------------

    def _load_kline_spec(self, extra_cfg: Dict[str, Any]) -> CollectorSpec:
        """按「构造参数 → 组件配置 → 配置文件」的优先级解析采集配置。"""
        return load_collector_spec(
            path=self._kline_config_path,
            symbols=(extra_cfg.get("symbols") or self._kline_symbols or None),
            interval=(
                self._kline_interval if self._kline_interval is not None
                else extra_cfg.get("interval")
            ),
            period=(
                self._kline_period if self._kline_period is not None
                else extra_cfg.get("period")
            ),
            adjust=self._kline_adjust,
            source=self._kline_source,
            enabled=self._kline_enabled,
            min_interval=self._kline_min_interval,
        )
    def _sync_kline_state(self) -> None:
        """为每个任务补齐状态槽，并清掉已经不存在的任务的旧状态。"""
        live = {job.key for job in self.kline_spec.jobs}
        for key in list(self._job_state_kline):
            if key not in live:
                self._job_state_kline.pop(key, None)
        for job in self.kline_spec.jobs:
            self._job_state_kline.setdefault(job.key, {
                "runs": 0, "rows": 0, "errors": 0,
                "last_run_at": None, "last_rows": 0, "last_error": None,
            })
    # ---------------- 生命周期 ----------------
    def _init_kline(self) -> None:
        cfg = self.component_config or {}
        collector_cfg = cfg.get("collector") or cfg

        if self._pending_kline_spec is None:
            # 只有真的传了组件配置（且非构造期已经覆盖过的）才重解析；
            # 构造时那次解析保证了「未 initialize 时 snapshot() 也是对的」
            self.kline_spec = self._load_kline_spec(collector_cfg)

        self._sync_kline_state()
        self._ensure_kline_tables()

        if not self.kline_spec.jobs:
            logger.warning("采集服务没有配置任何标的，组件空转（不注册定时器）")
            return

        buckets: Dict[float, int] = {}
        for job in self.kline_spec.jobs:
            buckets[job.interval] = buckets.get(job.interval, 0) + 1
        logger.info(
            f"行情采集就绪 | 任务 {len(self.kline_spec.jobs)} 个 | 频率分档: "
            + ", ".join(f"{humanize_frequency(k)}×{v}" for k, v in sorted(buckets.items()))
            + f" | 交易时段限定: {self.kline_spec.trading_hours_only}"
        )
    def _ensure_kline_tables(self) -> None:
        """补建分钟线表。

        用户可能从没跑过 init-db 就直接用采集服务；``create_all`` 是幂等的，
        这里补一次比让整条链路在第一次入库时才炸要好定位得多。
        """
        try:
            from app.storage.engine import engine
            from app.storage.schema import Base

            Base.metadata.create_all(bind=engine)
        except Exception as exc:
            logger.warning(f"补建数据表失败（忽略，后续写入仍会尝试）: {exc}")
    def _start_kline(self) -> defer.Deferred:
        if not self.kline_spec.enabled or not self.kline_spec.jobs:
            logger.info(
                "行情采集服务未启用或无任务"
                + (f" | 原因: {self.kline_disabled_reason}" if self.kline_disabled_reason else "")
                + "（组件仍已装配，可在面板手动补采）"
            )
            return defer.succeed(None)
        for interval in sorted({j.interval for j in self.kline_spec.jobs}):
            self._start_kline_bucket(interval)
        if self.kline_spec.backfill_on_start:
            # 立刻先采一轮：服务刚起来就要有数据，而不是干等第一个周期
            for interval in sorted({j.interval for j in self.kline_spec.jobs}):
                self._tick_kline(interval)
        return defer.succeed(None)
    def _stop_kline(self, graceful: bool = True) -> defer.Deferred:
        for loop in self._loops_kline.values():
            if loop.running:
                loop.stop()
        self._loops_kline.clear()
        self._busy_kline.clear()
        return defer.succeed(None)
    # ---------------- 定时器 ----------------
    def _start_kline_bucket(self, interval: float) -> None:
        # LoopingCall 的间隔就是任务频率本身，不要再套一层 max(0.5, ...)：
        # 那会让配了 sub-second 频率的部署悄悄变成 0.5s，与用户写的配置不符。
        seconds = float(interval)
        if seconds <= 0:
            logger.warning(f"采集频率 {seconds}s 非法，跳过该任务")
            return
        loop = task.LoopingCall(self._tick_kline, seconds)
        loop.start(seconds, now=False)
        self._loops_kline[seconds] = loop
        self._busy_kline[seconds] = False
        logger.info(f"采集定时器启动 | 每 {humanize_frequency(seconds)}")
    def _stop_kline_bucket(self, interval: float) -> None:
        loop = self._loops_kline.pop(interval, None)
        if loop is not None and loop.running:
            loop.stop()
        self._busy_kline.pop(interval, None)
    def _reschedule_kline(self) -> None:
        """按当前 jobs 重算频率分档，增删必要的定时器。

        **组件没在运行时只登记意图、不起定时器**。定时器一动就向 reactor
        注册调度；若在 INITIALIZED 阶段起了定时器，on_start 又会按桶再起一轮，
        同一个频率出现两个定时器 —— 采集频率翻倍，最后被数据源限流。
        """
        wanted = {j.interval for j in self.kline_spec.jobs if j.enabled and self.kline_spec.enabled}
        for interval in list(self._loops_kline):
            if interval not in wanted:
                self._stop_kline_bucket(interval)
        if not self.is_running:
            return
        for interval in sorted(wanted):
            if interval not in self._loops_kline:
                self._start_kline_bucket(interval)
    def _tick_kline(self, interval: float) -> None:
        if self._busy_kline.get(interval):
            self.stats_kline["skipped"] += 1
            logger.warning(
                f"采集({humanize_frequency(interval)}) 上一轮尚未结束，跳过本轮"
            )
            return
        if not self.kline_spec.enabled:
            return

        jobs = [j for j in self.kline_spec.jobs if j.interval == interval and j.enabled]
        if not jobs:
            return

        self._busy_kline[interval] = True
        d = threads.deferToThread(self._run_kline_bucket, jobs)
        d.addErrback(self._on_kline_bucket_error, interval)
        d.addBoth(self._release_kline, interval)
    def _on_kline_bucket_error(self, failure_obj, interval: float):
        """整批采集炸了（线程池里的漏网之鱼）。单个任务的失败在 _run_bucket 里已记过。"""
        logger.error(
            f"采集批次({humanize_frequency(interval)}) 异常: "
            f"{failure_obj.getErrorMessage() if hasattr(failure_obj, 'getErrorMessage') else failure_obj}"
        )
        return None
    def _release_kline(self, result: Any, interval: float) -> Any:
        self._busy_kline[interval] = False
        return result
    # ---------------- 采集执行（工作线程）----------------
    def _run_kline_bucket(self, jobs: List[CollectJob]) -> None:
        if not jobs:
            return
        key = jobs[0].interval
        n = self._run_counts_kline.get(key, 0) + 1
        self._run_counts_kline[key] = n

        # 先筛出本轮真正要跑的任务：被"非交易时段"跳过的不算任务，
        # 否则进度条会显示 3/10 然后卡住不动（那 7 个压根没打算跑）。
        todo: List[CollectJob] = []
        for job in jobs:
            if not self.kline_spec.should_run(job):
                self.stats_kline["out_of_session"] += 1
                continue
            todo.append(job)
        if not todo:
            return

        self.start_progress(len(todo),
                            f"{humanize_frequency(key)} · {len(todo)} 个采集任务")
        try:
            for job in todo:
                self.stats_kline["runs"] += 1
                try:
                    rows = self._collect_one_kline(job)
                except DataFetchError as exc:
                    self._record_kline_failure(job, exc)
                    self.advance_progress(1, failed=True)
                except Exception as exc:
                    logger.error(f"采集 {job.key} 异常: {exc}", exc_info=True)
                    self._record_kline_failure(job, exc)
                    self.advance_progress(1, failed=True)
                else:
                    self._record_kline_success(job, rows)
                    self.advance_progress(1)
                    if job.keep_days > 0 and n % _RETENTION_EVERY_N_RUNS == 0:
                        self._evict_kline(job)
        finally:
            self.finish_progress()
    def _collect_one_kline(self, job: CollectJob) -> int:
        from app.storage.service import MarketDataService

        source = job.source or None
        if is_intraday(job.period):
            return int(
                MarketDataService.collect_minute(
                    job.symbol,
                    period=job.period,
                    adjust=job.adjust,
                    source=source,
                )
            )
        info = MarketDataService.collect_incremental(
            job.symbol,
            lookback_days=job.lookback_days,
            adjust=job.adjust,
            source=source,
        )
        return int(info.get("rows") or 0)
    def _evict_kline(self, job: CollectJob) -> None:
        """清理过期分钟线。失败不影响主流程——磁盘撑爆是个慢问题。"""
        try:
            from app.storage.repo.minute_repo import MinuteRepository
            from app.storage.repo.stock_repo import session_scope

            cutoff = datetime.now() - timedelta(days=int(job.keep_days))
            with session_scope(None) as session:
                removed = MinuteRepository.delete_before(
                    session, cutoff, period=job.period, symbol=job.symbol, commit=False
                )
            if removed:
                logger.info(f"分钟线回收 | {job.key} 清理 {removed} 条（早于 {cutoff:%F}）")
        except Exception as exc:
            logger.warning(f"分钟线回收失败（忽略）: {exc}")
    def _record_kline_success(self, job: CollectJob, rows: int) -> None:
        st = self._job_state_kline.setdefault(job.key, {})
        st["runs"] = int(st.get("runs", 0)) + 1
        st["rows"] = int(st.get("rows", 0)) + rows
        st["last_rows"] = rows
        st["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        st["last_error"] = None
        self.stats_kline["rows"] += rows
        logger.info(f"采集完成 | {job.key} | {rows} 条")
        if self.event_bus is not None:
            try:
                self.event_bus.publish(
                    StandardEvents.DATA_COLLECTED,
                    symbol=job.symbol,
                    period=job.period,
                    rows=rows,
                )
            except Exception as exc:  # 事件回调故障不该影响采集
                logger.debug(f"DATA_COLLECTED 发布失败: {exc}")
    def _record_kline_failure(self, job: CollectJob, exc: Exception) -> None:
        st = self._job_state_kline.setdefault(job.key, {})
        st["runs"] = int(st.get("runs", 0)) + 1
        st["errors"] = int(st.get("errors", 0)) + 1
        st["last_error"] = str(exc)
        st["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        self.stats_kline["errors"] += 1
        logger.warning(f"采集失败 | {job.key} | {exc}")
    # ---------------- 运行时控制 ----------------
    def _set_kline_interval(self, seconds: Any, symbol: str = "", period: str = "") -> int:
        """现场改采集频率，立刻生效——不用重启服务。"""
        target = parse_interval(seconds, self.kline_spec.min_interval)
        want_period = normalize_period(period) if period else ""
        n = 0
        for job in self.kline_spec.jobs:
            if symbol and job.symbol != normalize_symbol(symbol):
                continue
            if want_period and job.period != want_period:
                continue
            job.interval = target
            n += 1
        if n:
            self._reschedule_kline()
            logger.info(f"采集频率已更新为 {humanize_frequency(target)} | 影响 {n} 个任务")
        return n
    def _add_kline_symbol(
        self,
        symbol: str,
        period: str = "",
        interval: Any = None,
        **params: Any,
    ) -> Optional[CollectJob]:
        """运行时新增采集标的（面板新增任务/手动补全数据时用）。"""
        sym = normalize_symbol(symbol)
        per = period or DAILY_PERIOD
        for job in self.kline_spec.jobs:
            if job.symbol == sym and job.period == per:
                if interval is not None:
                    self._set_kline_interval(interval, sym, per)
                return job
        job = CollectJob(
            symbol=sym,
            period=per,
            interval=parse_interval(interval, self.kline_spec.min_interval),
            adjust=params.get("adjust") or self.kline_spec.adjust,
            source=params.get("source") or self.kline_spec.source,
            lookback_days=int(params.get("lookback_days") or self.kline_spec.lookback_days),
            keep_days=int(params.get("keep_days") or self.kline_spec.keep_days),
        )
        self.kline_spec.jobs.append(job)
        self._job_state_kline[job.key] = {
            "runs": 0, "rows": 0, "errors": 0,
            "last_run_at": None, "last_rows": 0, "last_error": None,
        }
        self._reschedule_kline()
        logger.info(
            f"采集新增标的 | {job.key} | 频率 {humanize_frequency(job.interval)}"
        )
        return job
    def _remove_kline_symbol(self, symbol: str, period: str = "") -> int:
        sym = normalize_symbol(symbol)
        keep, removed = [], 0
        for job in self.kline_spec.jobs:
            if job.symbol == sym and (not period or job.period == period):
                removed += 1
                self._job_state_kline.pop(job.key, None)
            else:
                keep.append(job)
        self.kline_spec.jobs = keep
        if removed:
            self._reschedule_kline()
        return removed
    def _reload_kline_config(self, path: Optional[str] = None) -> Dict[str, Any]:
        """重新读一遍采集配置文件，热更新任务列表（保留已跑出来的统计）。"""
        self.kline_spec = load_collector_spec(
            path=path or self._kline_config_path,
            symbols=self._kline_symbols or None,
            interval=self._kline_interval,
            period=self._kline_period,
        )
        for job in self.kline_spec.jobs:
            self._job_state_kline.setdefault(job.key, {
                "runs": 0, "rows": 0, "errors": 0,
                "last_run_at": None, "last_rows": 0, "last_error": None,
            })
        self._reschedule_kline()
        logger.info(f"采集配置已热加载 | 任务 {len(self.kline_spec.jobs)} 个")
        return {"jobs": len(self.kline_spec.jobs)}
    def _collect_kline_now(self, symbol: str = "") -> defer.Deferred:
        """立刻采一次（不等定时器）。返回 Deferred，供面板/CLI await。"""
        jobs = [
            j for j in self.kline_spec.jobs
            if j.enabled and (not symbol or j.symbol == normalize_symbol(symbol))
        ]
        if not jobs:
            return defer.succeed({"rows": 0, "detail": "没有匹配的采集任务"})

        d = threads.deferToThread(self._run_kline_forced, jobs)
        return d
    def _run_kline_forced(self, jobs: List[CollectJob]) -> Dict[str, Any]:
        """强制执行一次：忽略交易时段（用户点了"立即采集"就是要立刻看到结果）。"""
        rows, errors = 0, []
        for job in jobs:
            self.stats_kline["runs"] += 1
            try:
                n = self._collect_one_kline(job)
            except Exception as exc:
                self.stats_kline["errors"] += 1
                self._record_kline_failure(job, exc)
                errors.append(f"{job.key}: {exc}")
            else:
                self._record_kline_success(job, n)
                rows += n
        return {"rows": rows, "tasks": len(jobs), "errors": errors}
    # ---------------- 引擎协作 ----------------
    def _is_busy_kline(self) -> bool:
        """还有定时器在跑 → 引擎不该被判定为空闲。采集服务本身就是"有事要做"。"""
        return any(loop.running for loop in self._loops_kline.values())
    def _snapshot_kline(self) -> Dict[str, Any]:
        return {
            "enabled": self.kline_spec.enabled,
            "assembled": True,
            "disabled_reason": self.kline_disabled_reason,
            "running": bool(self._loops_kline),
            "jobs": [j.to_dict() for j in self.kline_spec.jobs],
            "state": {k: dict(v) for k, v in self._job_state_kline.items()},
            "stats": dict(self.stats_kline),
            "buckets": sorted(self._loops_kline.keys()),
            "trading_hours_only": self.kline_spec.trading_hours_only,
            "min_interval": self.kline_spec.min_interval,
            "config": self.kline_spec.config_path,
            "pending": {str(k): v for k, v in self._busy_kline.items() if v},
        }
    def _health_check_kline(self):
        if not self.kline_spec.enabled:
            return True, "disabled"
        failing = sum(
            1 for st in self._job_state_kline.values() if st.get("last_error")
        )
        if failing and failing == len(self.kline_spec.jobs):
            return False, f"全部 {failing} 个采集任务最近都失败了"
        return True, f"{len(self.kline_spec.jobs)} 个采集任务"


# ------------------------------------------------------------------
# 单元：cross（原 ExtDataComponent）
# ------------------------------------------------------------------

    # ============================================================
    # 生命周期
    # ============================================================
    def _init_cross(self) -> None:
        cfg = self.component_config or {}
        if cfg.get("symbols"):
            self.cross_symbols = [str(s) for s in cfg["symbols"]]
        if cfg.get("interval") is not None:
            self.cross_interval = float(cfg["interval"])
        if cfg.get("daily_interval") is not None:
            self.cross_daily_interval = float(cfg["daily_interval"])
        if cfg.get("theme_top") is not None:
            self.cross_theme_top = int(cfg["theme_top"])
        if cfg.get("enabled") is not None:
            self._cross_enabled = bool(cfg["enabled"])
        if cfg.get("with_fundamental") is not None:
            self.cross_with_fundamental = bool(cfg["with_fundamental"])
        if cfg.get("categories") is not None:
            self.cross_categories = [str(c) for c in cfg["categories"]]

        # 没指定标的时，从引擎的市场组件拿 —— 采集服务和交易标的保持一致
        if not self.cross_symbols:
            self.cross_symbols = self._cross_market_symbols()

        if not self._cross_enabled:
            logger.info("扩展行情采集已关闭（EXT_DATA_ENABLED=false）")
            return
        if not self.cross_symbols:
            logger.warning(
                "扩展行情采集：没有标的，只做市场级数据（涨停池/题材）。"
                "要采集个股资金流请在 config/collector.json 或本组件配置里给 symbols"
            )
        state = self._cross_calendar().to_dict()
        logger.info(
            f"扩展行情采集就绪 | 标的 {len(self.cross_symbols)} 个 | "
            f"资金流/筹码 {self.cross_interval}s | 市场级 {self.cross_daily_interval}s | "
            f"基本面 {'开' if self.cross_with_fundamental else '关'} | "
            f"当前 {state['state_name']}"
        )
    def _cross_calendar(self):
        from app.domain.calendar import get_calendar

        return get_calendar()
    def _cross_market_symbols(self) -> List[str]:
        engine = getattr(self, "_engine", None)
        if engine is None:
            return []
        for name in ("market_service", "strategy"):
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
    def _start_cross(self) -> defer.Deferred:
        if not self._cross_enabled:
            return defer.succeed(None)
        self._cross_loop = task.LoopingCall(self._tick_cross)
        self._cross_loop.start(max(30.0, self.cross_interval), now=False)
        self._cross_daily_loop = task.LoopingCall(self._tick_cross_daily)
        self._cross_daily_loop.start(max(300.0, self.cross_daily_interval), now=False)
        # 启动时不无脑采一次：先看市场状态，闭市就什么都不做
        self._startup_check_cross()
        return defer.succeed(None)
    def _startup_check_cross(self) -> None:
        """启动时的补采判断。

        盘后启动的服务最容易漏掉当天数据 ——
        定时器下一次触发可能是几小时后，而当天的收盘数据现在就该拿到。
        """
        if not self._cross_enabled:
            return
        # 启动补采也走同一套时间窗口判断 ——
        # 凌晨三点启动的服务不该去请求"今天的"数据（那时还没有）
        slot = self._cross_slot()
        if slot is None:
            return
        d = threads.deferToThread(self._run_cross_once, slot)
        d.addErrback(lambda f: logger.warning(f"扩展数据启动补采失败: "
                                              f"{f.getErrorMessage()}"))
        return None
    @defer.inlineCallbacks
    def _stop_cross(self, graceful: bool = True):
        for loop in (self._cross_loop, self._cross_daily_loop):
            if loop is not None and loop.running:
                loop.stop()
        self._cross_loop = None
        self._cross_daily_loop = None
        logger.info(f"扩展行情采集已停止 | {self.stats_cross}")
        yield defer.succeed(None)
    def _is_busy_cross(self) -> bool:
        return self._busy_cross
    # ============================================================
    # 采集
    # ============================================================
    def _tick_cross(self):
        if self._busy_cross:
            return defer.succeed(None)
        slot = self._cross_slot()
        if slot is None:
            return defer.succeed(None)
        self._busy_cross = True
        d = threads.deferToThread(self._run_cross_once, slot)
        d.addBoth(self._release_cross)
        return d
    def _release_cross(self, result: Any) -> Any:
        self._busy_cross = False
        if isinstance(result, Exception):
            self.stats_cross["errors"] += 1
            log_throttled("ERROR", f"扩展行情采集异常: {result}",
                          key="extdata-error")
            return None
        return result
    def _cross_slot(self) -> Optional[str]:
        """此刻该采哪一个时间窗口的数据。

        ``pre``  盘前（开盘前 30 分钟起）：补齐昨日漏采、预热当日标的
        ``post`` 盘后定稿（闭市 30 分钟后）：当日真正落地的数据
        ``None`` 此刻不该采集

        每个窗口**每天只做一次**。日频截面数据一天一条，
        在同一个窗口里反复拉就是在浪费请求额度。
        """
        if not self._cross_enabled:
            return None
        slot = self._cross_schedule().slot_of()
        if slot is None:
            self.stats_cross["skipped_closed"] += 1
            return None
        today = date.today()
        done = self._cross_slots.get(today) or set()
        if slot in done:
            self.stats_cross["skipped_synced"] += 1
            return None
        return slot
    def _cross_schedule(self):
        from app.engine.collect.schedule import get_schedule

        return get_schedule()
    def _cross_should_fetch(self) -> bool:
        """供测试与面板判断是否会发请求。"""
        return self._cross_slot() is not None
    def _cross_plan_items(self, slot: str) -> List[str]:
        """本轮要跑哪些采集项 —— 进度条的"全部"就是它的长度。

        单独抽出来是因为**计划必须和真正执行的分支完全一致**：
        少算一项进度条到不了 100%，多算一项永远停在 99%，
        两种都会让人以为组件卡住了。
        """
        final = slot == "post"
        items: List[str] = []
        if self.cross_symbols:
            items += ["fund_flow", "chip"]
            if self.cross_with_fundamental and (final or self._cross_final_date is None):
                items.append("fundamental")
            if final:
                items += list(self.cross_categories)
        if final:
            items += ["macro", "breadth"]
        return items
    def _run_cross_once(self, slot: str = "post") -> Dict[str, Any]:
        """按标的采集资金流、筹码、基本面。**单只失败不影响其它**。

        :param slot: ``pre`` 盘前 / ``post`` 盘后定稿 / ``intraday`` 盘中
        """
        plan = self._cross_plan_items(slot)
        self.start_progress(
            len(plan),
            f"{len(self.cross_symbols)} 只 · {len(plan)} 项"
            + ("（盘后定稿）" if slot == "post" else ""),
        )
        try:
            return self._run_cross_once_body(slot)
        finally:
            # 无论成功还是中途抛异常都要收尾，否则面板永远显示"进行中"
            self.finish_progress()
    def _run_cross_once_body(self, slot: str) -> Dict[str, Any]:
        from app.alpha.pool import (
            sync_category,
            sync_chip,
            sync_fund_flow,
            sync_fundamentals,
        )

        final = slot == "post"
        report: Dict[str, Any] = {"slot": slot}
        today = date.today()

        if self.cross_symbols:
            r1 = sync_fund_flow(self.cross_symbols)
            self.advance_progress(1, failed=bool(r1.get("failed")))
            r2 = sync_chip(self.cross_symbols)
            self.advance_progress(1, failed=bool(r2.get("failed")))
            self.stats_cross["fund_flow_ok"] += r1.get("ok", 0)
            self.stats_cross["chip_ok"] += r2.get("ok", 0)
            self.stats_cross["failed"] += (len(r1.get("failed") or [])
                                     + len(r2.get("failed") or []))
            report["fund_flow"] = r1
            report["chip"] = r2

            # 基本面（估值+财务）一天一次就够，只在收盘后采 ——
            # 盘中的估值随价格跳动，拉到的是未定稿的数，存进去反而污染序列
            if self.cross_with_fundamental and (final or self._cross_final_date is None):
                r3 = sync_fundamentals(self.cross_symbols)
                self.advance_progress(1, failed=bool(r3.get("failed")))
                self.stats_cross["fundamental_ok"] += r3.get("ok", 0)
                self.stats_cross["failed"] += len(r3.get("failed") or [])
                report["fundamental"] = r3

            # 其它类别（龙虎榜/两融/业绩预告/股东户数/评级/舆情）
            # 更新慢或只在特定时点有，所以**只在盘后**采一次
            if final:
                for cat in self.cross_categories:
                    try:
                        r = sync_category(cat, self.cross_symbols)
                    except Exception as exc:
                        # 一个类别挂了不该让整轮中断（龙虎榜没数据不等于资金流没采到）
                        self.stats_cross["failed"] += 1
                        self.advance_progress(1, failed=True)
                        report[cat] = {"ok": 0, "error": str(exc)[:120]}
                        continue
                    self.advance_progress(1, failed=bool(r.get("failed")))
                    self.stats_cross["failed"] += len(r.get("failed") or [])
                    report[cat] = {"ok": r.get("ok", 0)}

        # 市场级数据（宏观/宽度）一天一次，也只在盘后
        if final:
            for cat in ("macro", "breadth"):
                try:
                    r = sync_category(cat)
                except Exception as exc:
                    self.stats_cross["failed"] += 1
                    self.advance_progress(1, failed=True)
                    report[cat] = {"ok": 0, "error": str(exc)[:120]}
                    continue
                self.advance_progress(1)
                report[cat] = {"ok": r.get("ok", 0)}

        self.stats_cross["runs"] += 1
        self.stats_cross["last_run_at"] = datetime.now().isoformat()
        self._cross_slots.setdefault(today, set()).add(slot)
        self._cross_synced_date = today
        if final:
            self._cross_final_date = today

        # 采完新数据后，特征缓存必须失效 ——
        # 否则策略会一直拿着几天前的资金流在打分。
        try:
            from app.feature.features import get_provider

            get_provider().invalidate()
        except Exception:
            pass
        self.cross_last_report = report
        return report
    def _tick_cross_daily(self):
        """市场级数据：涨停池 + 题材板块 + 题材成分映射。

        同样受"一天一次"约束 —— 涨停池收盘后就定稿了，
        盘后反复拉纯属浪费请求额度。
        """
        if self._busy_cross or not self._cross_enabled:
            return defer.succeed(None)
        cal = self._cross_calendar()
        if not cal.is_trading_day():
            self.stats_cross["skipped_closed"] += 1
            return defer.succeed(None)
        if self._cross_last_daily == date.today():
            self.stats_cross["skipped_synced"] += 1
            return defer.succeed(None)
        if not cal.is_open():
            self.stats_cross["skipped_closed"] += 1
            return defer.succeed(None)
        self._busy_cross = True
        d = threads.deferToThread(self._run_cross_daily)
        d.addBoth(self._release_cross)
        return d
    def _run_cross_daily(self) -> Dict[str, Any]:
        from app.alpha.pool import sync_boards, sync_concept_members, sync_limitup

        report: Dict[str, Any] = {}
        self.start_progress(3, "市场级 · 涨停池/板块/题材")
        try:
            return self._run_cross_daily_body()
        finally:
            self.finish_progress()
    def _run_cross_daily_body(self) -> Dict[str, Any]:
        from app.alpha.pool import sync_boards, sync_concept_members, sync_limitup

        report: Dict[str, Any] = {}
        try:
            r = sync_limitup()
            self.advance_progress(1, failed=not r.get("ok"))
            self.stats_cross["limitup_ok"] += r.get("ok", 0)
            report["limitup"] = r
        except Exception as exc:
            self.stats_cross["errors"] += 1
            self.advance_progress(1, failed=True)
            log_throttled("WARNING", f"涨停池同步失败: {exc}", key="extdata-zt")
        try:
            r1 = sync_boards("concept")
            r2 = sync_boards("industry")
            self.advance_progress(1, failed=not (r1.get("ok") or r2.get("ok")))
            self.stats_cross["boards_ok"] += r1.get("ok", 0) + r2.get("ok", 0)
            report["boards"] = {"concept": r1, "industry": r2}
        except Exception as exc:
            self.stats_cross["errors"] += 1
            self.advance_progress(1, failed=True)
            log_throttled("WARNING", f"板块同步失败: {exc}", key="extdata-board")
        # 题材成分映射最慢也最没必要频繁做：归属变化很慢，一天一次足够
        try:
            r = sync_concept_members(self.cross_theme_top)
            self.advance_progress(1, failed=not r.get("ok"))
            self.stats_cross["themes_ok"] += r.get("ok", 0)
            report["themes"] = r
        except Exception as exc:
            self.stats_cross["errors"] += 1
            self.advance_progress(1, failed=True)
            log_throttled("WARNING", f"题材成分同步失败: {exc}", key="extdata-theme")
        self._cross_last_daily = date.today()
        self.stats_cross["last_daily_at"] = datetime.now().isoformat()
        self.cross_last_report.update(report)
        return report
    # ============================================================
    # 可观测
    # ============================================================
    def _snapshot_cross(self) -> Dict[str, Any]:
        info = self._cross_calendar().to_dict()
        sch = self._cross_schedule().to_dict()
        return {
            "schedule": sch,
            "slots_done": {str(k): sorted(v) for k, v in self._cross_slots.items()},
            "next_slot": self._cross_slot(),
            "enabled": self._cross_enabled,
            "symbols": len(self.cross_symbols),
            "interval": self.cross_interval,
            "daily_interval": self.cross_daily_interval,
            "theme_top": self.cross_theme_top,
            "with_fundamental": self.cross_with_fundamental,
            "market": info,
            "synced_date": self._cross_synced_date.isoformat() if self._cross_synced_date else None,
            "final_date": self._cross_final_date.isoformat() if self._cross_final_date else None,
            "stats": dict(self.stats_cross),
            "last_report": self.cross_last_report,
        }
    def _health_check_cross(self):
        if not self._cross_enabled:
            return True, "已关闭"
        if self.stats_cross["errors"] and self.stats_cross["runs"] and \
                self.stats_cross["errors"] > self.stats_cross["runs"]:
            return False, f"采集失败次数偏多（{self.stats_cross['errors']} 次）"
        info = self._cross_calendar().to_dict()
        if not info["is_trading_day"]:
            return True, f"非交易日，不采集（下次开盘 {info['next_open']}）"
        if not self.cross_symbols:
            return True, "无个股标的，只采集市场级数据"
        return True, f"已采集 {self.stats_cross['fund_flow_ok']} 条资金流"


# ------------------------------------------------------------------
# 单元：guard（原 DataGuardComponent）
# ------------------------------------------------------------------

    # ---------------- 生命周期 ----------------
    def _init_guard(self) -> None:
        cfg = self.component_config or {}
        guard_cfg = cfg.get("data_guard") or cfg
        if not self.guard_symbols:
            self.guard_symbols = list(guard_cfg.get("symbols") or [])
        if guard_cfg.get("interval") is not None and self._guard_loop is None:
            try:
                self.guard_interval = float(guard_cfg["interval"])
            except (TypeError, ValueError):
                pass
        if not self._guard_enabled:
            logger.info(
                "数据校验服务已停用"
                + (f" | 原因: {self.guard_disabled_reason}" if self.guard_disabled_reason else "")
            )
            return
        logger.info(
            f"数据校验服务就绪 | 启动后 {self.guard_start_delay:.0f}s 首检 | "
            f"之后每 {self.guard_interval:.0f}s 一次 | 回溯 {self.guard_lookback_days} 天 | "
            f"自动补全: {'开' if self.guard_auto_repair else '关（只体检）'}"
        )
    def _start_guard(self) -> defer.Deferred:
        if not self._guard_enabled:
            return defer.succeed(None)
        if self.guard_interval <= 0:
            logger.warning("数据校验周期非法（<=0），只跑启动那一次")
        else:
            self._guard_loop = task.LoopingCall(self._tick_guard)
            self._guard_loop.start(self.guard_interval, now=False)
            logger.info(f"数据校验定时器启动 | 每 {self.guard_interval:.0f}s")

        # 首次体检：延后 + 线程池。理由见文件头第 1 点。
        if self.guard_start_delay <= 0:
            self._tick_guard()
        else:
            from twisted.internet import reactor

            self._guard_start_call = reactor.callLater(
                self.guard_start_delay, self._tick_guard
            )
            logger.debug(f"数据校验首检已安排在 {self.guard_start_delay:.0f}s 后")
        return defer.succeed(None)

    def _stop_guard(self, graceful: bool = True) -> defer.Deferred:
        call = getattr(self, "_guard_start_call", None)
        if call is not None:
            try:
                if call.active():
                    call.cancel()
            except Exception:
                pass
        self._guard_start_call = None
        if self._guard_loop is not None and self._guard_loop.running:
            self._guard_loop.stop()
        self._guard_loop = None
        # 停止时若体检正在跑，一并复位忙标志，避免留下永久 "busy"
        self._busy_guard = False
        return defer.succeed(None)
    # ---------------- 执行 ----------------
    def _on_guard_step(self, done: int, total: int, bad: bool) -> None:
        """体检每检完一只回调一次，驱动面板上的"已完成 / 全部"。

        第一次回调时才开一轮：总数只有扫描器开始跑之后才知道
        （没指定标的时要从库里列举），没法提前 start。
        """
        if self._progress_guard.round == 0 or not self._progress_guard.running:
            self.start_progress(total, f"数据体检 {total} 只")
        self.advance_progress(1, failed=bad)
        if done >= total:
            self.finish_progress()
    def _tick_guard(self) -> None:
        if self._busy_guard:
            logger.warning("数据校验上一轮尚未结束，跳过本轮")
            return
        if not self._guard_enabled:
            return
        self._busy_guard = True
        d = threads.deferToThread(self._run_guard)
        d.addErrback(self._on_guard_error)
        d.addBoth(self._release_guard)
    def _on_guard_error(self, failure_obj) -> None:
        self.stats_guard["errors"] += 1
        msg = (failure_obj.getErrorMessage()
               if hasattr(failure_obj, "getErrorMessage") else str(failure_obj))
        logger.error(f"数据校验批次异常: {msg}")
    def _release_guard(self, result: Any) -> Any:
        self._busy_guard = False
        return result
    def _run_guard(self) -> IntegrityReport:
        checker = DataIntegrityChecker(
            lookback_days=self.guard_lookback_days,
            auto_repair=self.guard_auto_repair,
            max_symbols=self.guard_max_symbols,
            max_repair=self.guard_max_repair,
        )
        report = checker.scan(self.guard_symbols or None, on_progress=self._on_guard_step)
        if not report.checked:
            # 一个标的都没有：进度条会一直停在上一轮，这里明确置空
            self.start_progress(0, "无可体检标的")
            self.finish_progress()
        self._guard_report = report
        self.stats_guard["runs"] += 1
        self.stats_guard["missing"] += report.missing_total
        self.stats_guard["repaired"] += report.repaired_total
        self.stats_guard["anomalies"] += report.anomaly_total
        if report.failed:
            self.stats_guard["errors"] += len(report.failed)

        line = summarize(report)
        if report.ok:
            logger.info(line)
        else:
            # 缺数据不会让程序崩，但**必须**留下痕迹 ——
            # 否则"策略最近怎么不准了"永远查不到根因。
            logger.warning(line)
            for item in report.failed[:5]:
                logger.warning(f"  校验未通过: {item}")
        return report
    # ---------------- 手动触发 ----------------
    def _check_guard_now(self, symbols: Optional[Sequence[str]] = None,
                  auto_repair: Optional[bool] = None) -> defer.Deferred:
        """立刻体检一次（不等定时器）。返回 Deferred[dict]。"""
        want = list(symbols or self.guard_symbols or [])
        repair = self.guard_auto_repair if auto_repair is None else bool(auto_repair)

        def _job() -> Dict[str, Any]:
            checker = DataIntegrityChecker(
                lookback_days=self.guard_lookback_days,
                auto_repair=repair,
                max_symbols=self.guard_max_symbols,
                max_repair=self.guard_max_repair,
            )
            report = checker.scan(want or None, on_progress=self._on_guard_step)
            if not report.checked:
                self.start_progress(0, "无可体检标的")
                self.finish_progress()
            self._guard_report = report
            self.stats_guard["runs"] += 1
            self.stats_guard["missing"] += report.missing_total
            self.stats_guard["repaired"] += report.repaired_total
            self.stats_guard["anomalies"] += report.anomaly_total
            return {"summary": summarize(report), **report.to_dict()}

        return threads.deferToThread(_job)
    # ---------------- 引擎协作 ----------------
    def _is_busy_guard(self) -> bool:
        return self._busy_guard
    def _health_check_guard(self):
        if not self._guard_enabled:
            return True, "disabled"
        if self._guard_report is None:
            return True, "尚未体检"
        if self._guard_report.anomaly_total:
            return False, f"发现 {self._guard_report.anomaly_total} 条异常行情"
        if self._guard_report.missing_total:
            return False, f"仍有 {self._guard_report.missing_total} 天日线缺口未补上"
        return True, "数据完整"
    def _snapshot_guard(self) -> Dict[str, Any]:
        report = self._guard_report.to_dict() if self._guard_report else None
        return {
            "enabled": self._guard_enabled,
            "assembled": True,
            "disabled_reason": self.guard_disabled_reason,
            "running": self._guard_loop is not None and bool(self._guard_loop.running),
            "interval": self.guard_interval,
            "start_delay": self.guard_start_delay,
            "lookback_days": self.guard_lookback_days,
            "auto_repair": self.guard_auto_repair,
            "max_symbols": self.guard_max_symbols,
            "max_repair": self.guard_max_repair,
            "symbols": list(self.guard_symbols),
            "busy": self._busy_guard,
            "stats": dict(self.stats_guard),
            "report": report,
            "last_check_at": (
                self._guard_report.started_at if self._guard_report else None
            ),
        }


# ===== 来自 collector.py 的模块级代码 =====
#: 每跑这么多次清理一次分钟线。每天删一次表比每分钟删一次划算得多
_RETENTION_EVERY_N_RUNS = 240

def parse_interval(value: Any, min_interval: float = 15.0) -> float:
    """把各种写法转成秒并夹紧到下限。

    缺省（None）直接取下限而不是硬编码 300s：下限才是"这套部署允许多快"，
    拿一个写死的默认值当兜底会在配了 min_interval=1s 的环境里出人意料。
    """
    floor = float(min_interval)
    return max(parse_frequency(value, floor), floor)


__all__ = ["CollectionService", "parse_interval"]
