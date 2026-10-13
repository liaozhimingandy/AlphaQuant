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
# Failure 必须显式引入：errback 送下来的**不是** Exception 实例，
# 只判 isinstance(x, Exception) 会让线程池里的异常被静默吞掉。
from twisted.python import failure as _failure

import sys

from app.engine.component import Progress
from app.engine.capability import Cap, RestartPolicy, ServiceComponent
from app.modules.collection.spec import (
    CollectJob,
    CollectorSpec,
    DAILY_PERIOD,
    humanize_frequency,
    is_intraday,
    load_collector_spec,
    normalize_period,
    parse_frequency,
)
from app.modules.collection.integrity import DataIntegrityChecker, IntegrityReport, summarize
from app.modules.collection.watchlist import (
    WatchlistConfig,
    collect_due_watchlist,
    get_watchlist_config,
    seed_from_config,
    set_watchlist_config,
    watchlist_view,
)
from app.runtime.event_bus import StandardEvents
from app.domain.symbol import DataFetchError, normalize_symbol
from app.common.config import settings
from app.common.errors import StorageError
from app.common.logging import log_throttled, logger


class CollectionService(ServiceComponent):
    """采集服务。

    **提供的能力**（见 :class:`~app.engine.capability.Cap`）：
      * ``collect.kline``     —— 逐根 K 线，按标的按周期定时抓
      * ``collect.cross``     —— 日频截面：资金流 / 筹码 / 基本面 / 涨停池 / 题材
      * ``collect.integrity`` —— 体检 + 缺口自动补全
      * ``collect.watchlist`` —— 关注标的：每只票按**各自的**频率自动补数据

    内部四个单元
    ------------
    =================  =========================  ===========================
    单元               管什么                      节拍
    =================  =========================  ===========================
    ``_kline``         K 线（量价）               按任务频率，分钟级到日级
    ``_cross``         资金流/筹码/涨停池等        按市场状态：盘前/盘后各一次
    ``_guard``         完整性与补全                启动后延后首检，之后周期性
    ``_watchlist``     关注标的（自选股）          每只票各自到期，到期才采
    =================  =========================  ===========================

    节拍不同是它们当初被写成多个组件的**唯一正当理由**，
    所以合并后这个差异被完整保留，只是不再各自占一个引擎槽位。

    ``_watchlist`` 的节拍为什么要"每只票各自到期"而不是整批到点一起采：
    见 :class:`app.data.schema.Watchlist` 的类文档 —— 整批共用一个频率会逼
    用户在"陪着一起被限流"和"真正要盯的那只永远是旧数据"之间二选一。

    四个单元共用一套"上一轮没跑完就跳过本轮"的保护：
    网络慢的时候堆积请求只会让数据源更慢，最后被限流，
    比漏采一次严重得多。
    """

    name = "collection_service"
    provides = frozenset({
        Cap.COLLECT_KLINE, Cap.COLLECT_CROSS, Cap.DATA_INTEGRITY,
        Cap.WATCHLIST_COLLECT,
    })
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
        cross_with_flow: Optional[bool] = None,
        cross_with_chip: Optional[bool] = None,
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
        # ---- 关注标的单元 ----
        watchlist_enabled: Optional[bool] = None,
        watchlist_interval: Any = None,
        watchlist_sweep_interval: Optional[float] = None,
        watchlist_max_per_round: Optional[int] = None,
        watchlist_categories: Optional[List[str]] = None,
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
        # 同理：显式 ``[]`` = "本单元不采个股"，不能退回默认标的集合
        self._kline_symbols = list(
            kline_symbols if kline_symbols is not None else self.symbols
        )
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
        # 判空用 ``is not None`` 而不是真值判断：``[]`` 是有意义的配置
        # ——"我一个都不要"（服务分工时很常见），不能退化成"全都采"。
        self.cross_categories: List[str] = (
            list(cross_categories) if cross_categories is not None
            else ["lhb", "margin", "forecast", "holders", "rating", "news"]
        )
        self.cross_symbols: List[str] = list(
            cross_symbols if cross_symbols is not None else self._kline_symbols
        )
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
        # 资金流/筹码默认开（与合并前的行为一致）。
        # 关掉它们的唯一理由是**服务分工**：一个专门采基本面的服务
        # 再去拉一遍全市场资金流，等于替行情服务重复烧掉一份请求额度。
        self.cross_with_flow = True if cross_with_flow is None else bool(cross_with_flow)
        self.cross_with_chip = True if cross_with_chip is None else bool(cross_with_chip)
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
        self.guard_symbols: List[str] = list(
            guard_symbols if guard_symbols is not None else self._kline_symbols
        )
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

        # ---------------- 关注标的单元 ----------------
        #: 配置来源：config/collector.json 的 watchlist 段 > 环境变量 > 内置默认。
        #: 构造参数（builder 传进来的component_config 之类）优先级最高，
        #: 因为那是"这次运行明确指定的"，不该被文件里的默认值盖掉。
        self._watchlist_cfg: WatchlistConfig = get_watchlist_config()
        if watchlist_enabled is not None:
            self._watchlist_cfg.enabled = bool(watchlist_enabled)
        if watchlist_interval is not None:
            self._watchlist_cfg.interval = str(watchlist_interval)
        if watchlist_sweep_interval is not None:
            self._watchlist_cfg.sweep_interval = float(watchlist_sweep_interval)
        if watchlist_max_per_round is not None:
            self._watchlist_cfg.max_per_round = int(watchlist_max_per_round)
        # categories 用 ``is not None``：``[]`` 是"一个都不要"的显式声明，
        # 不能用 ``or`` 兜底 —— 那会把它反向变成"全都采"。
        if watchlist_categories is not None:
            self._watchlist_cfg.categories = [
                str(c).strip() for c in watchlist_categories if str(c).strip()
            ]
        self._watchlist_loop: Optional[task.LoopingCall] = None
        self._busy_watchlist = False
        self.stats_watchlist: Dict[str, Any] = {
            "runs": 0, "collected": 0, "failed": 0, "errors": 0,
            "skipped_no_due": 0, "last_run_at": None, "last_symbols": [],
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
        self._init_watchlist()

    @defer.inlineCallbacks
    def on_start(self) -> defer.Deferred:
        # 顺序有讲究：K 线先起来把当天数据喂进库，
        # 完整性单元后跑（它本身还延后了首检），才不会把
        # "还没来得及采"误判成"缺数据"然后急着联网补一轮。
        yield self._start_kline()
        yield self._start_cross()
        yield self._start_guard()
        # 关注标的放最后：它自己去库里读清单，不依赖另外三个单元，
        # 但它是唯一会"因为用户在面板上点了一下就改写自身状态"的单元，
        # 让它晚一步起来，避免和启动期的其它补采抢同一批网络连接。
        yield self._start_watchlist()

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        # 逆序：关注标的先停（它最后起），再停补全，再停截面，最后停 K 线
        yield self._stop_watchlist()
        yield self._stop_guard()
        yield self._stop_cross()
        yield self._stop_kline()

    def is_busy(self) -> bool:
        """任一单元还在干活，引擎就不该被判定为空闲。"""
        return (
            self._is_busy_kline()
            or self._is_busy_cross()
            or self._is_busy_guard()
            or self._is_busy_watchlist()
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
        try:
            base["watchlist"] = self._snapshot_watchlist()
        except Exception as exc:
            base["watchlist_error"] = str(exc)
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
            ("关注标的", self._health_check_watchlist),
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

    #: 定向采集支持的类别。写死一张表而不是"传什么跑什么"，
    #: 是为了让未知类别当场报错 —— 拼错一个字母然后拿到
    #: "采集成功 0 条"的假成功，是排查成本最高的一种失败。
    MANUAL_CATEGORIES = (
        "fund_flow", "chip", "fundamental",
        "lhb", "margin", "forecast", "holders", "rating", "news",
    )
    #: 类别 → 统计字段。新增类别时别忘在这里登记，否则面板永远显示 0。
    _CROSS_STAT_KEY = {
        "fund_flow": "fund_flow_ok",
        "chip": "chip_ok",
        "fundamental": "fundamental_ok",
    }

    def collect_categories(self, symbols: Optional[Sequence[str]] = None,
                           categories: Optional[Sequence[str]] = None
                           ) -> defer.Deferred:
        """按**指定标的 + 指定类别**采一轮（人工触发，不看时间窗口）。

        与 :meth:`collect_cross_now` 的分工：后者按服务的既定配置跑完整一轮，
        这里听调用方的 —— "把 600519 的基本面现在采一遍"就是这条路径，
        不必等到盘后窗口，也不必为此改 services.json。
        """
        syms = [str(s).strip() for s in (symbols or []) if str(s).strip()]
        cats = [str(c).strip() for c in (categories or []) if str(c).strip()] or ["fundamental"]
        unknown = [c for c in cats if c not in self.MANUAL_CATEGORIES]
        if unknown:
            return defer.fail(ValueError(
                f"未知采集类别: {unknown} | 可选: {list(self.MANUAL_CATEGORIES)}"
            ))
        if not syms:
            syms = list(self.cross_symbols)
        if not syms:
            return defer.fail(ValueError(
                "没有指定标的，且本服务也没配截面标的（collector.symbols）"
            ))
        if self._busy_cross:
            logger.warning("日频截面采集仍在进行中，忽略本次定向采集")
            return defer.succeed(None)
        self._busy_cross = True
        d = threads.deferToThread(self._run_manual_cross, syms, cats)
        d.addBoth(self._release_cross)
        return d

    def _run_manual_cross(self, symbols: List[str],
                          categories: List[str]) -> Dict[str, Any]:
        """定向采集的执行体（在工作线程里跑）。

        注意方法名里的 ``_cross``：进度路由靠它把这一轮归到截面单元的
        进度条上，而不是刷掉 K 线单元那份（见 :meth:`_caller_unit`）。
        """
        from app.modules.universe.pool import (
            sync_category,
            sync_chip,
            sync_fund_flow,
            sync_fundamentals,
        )

        self.start_progress(
            len(categories),
            f"{len(symbols)} 只 · {len(categories)} 项（定向采集）",
        )
        report: Dict[str, Any] = {"symbols": list(symbols), "detail": {}}
        try:
            for cat in categories:
                try:
                    if cat == "fund_flow":
                        r = sync_fund_flow(symbols)
                    elif cat == "chip":
                        r = sync_chip(symbols)
                    elif cat == "fundamental":
                        r = sync_fundamentals(symbols)
                    else:
                        r = sync_category(cat, symbols)
                except StorageError:
                    # 存储后端故障（磁盘满/库锁/权限）必须上浮：
                    # 这一轮定向采集已无意义，且应让调度器把任务判失败、
                    # 进错误队列、可人工重放，而不是每条都静默记"error"。
                    raise
                except Exception as exc:
                    # 一个类别挂了不该让整轮中断：想看基本面，
                    # 龙虎榜没数据不影响结论。
                    self.stats_cross["failed"] += 1
                    self.advance_progress(1, failed=True)
                    report["detail"][cat] = {"ok": 0, "error": str(exc)[:120]}
                    continue
                failed = r.get("failed") or []
                self.advance_progress(1, failed=bool(failed))
                key = self._CROSS_STAT_KEY.get(cat)
                if key:
                    self.stats_cross[key] += r.get("ok", 0)
                self.stats_cross["failed"] += len(failed)
                report["detail"][cat] = {
                    "ok": r.get("ok", 0),
                    "failed": list(failed)[:5],
                }
            self.stats_cross["runs"] += 1
            self.stats_cross["last_run_at"] = datetime.now().isoformat()

            # 与定时路径同理：采完必须让特征缓存失效，
            # 否则面板上看到的还是上一轮的老数。
            try:
                from app.modules.factor.features import get_provider

                get_provider().invalidate()
            except Exception:
                pass
            self.cross_last_report = report
            return report
        finally:
            self.finish_progress()


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
            from app.data.engine import engine
            from app.data.schema import Base

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
        from app.data.service import MarketDataService

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
            from app.data.repo.minute_repo import MinuteRepository
            from app.data.repo.stock_repo import session_scope

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
        if cfg.get("with_flow") is not None:
            self.cross_with_flow = bool(cfg["with_flow"])
        if cfg.get("with_chip") is not None:
            self.cross_with_chip = bool(cfg["with_chip"])
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
            f"资金流{'开' if self.cross_with_flow else '关'}"
            f"/筹码{'开' if self.cross_with_chip else '关'} {self.cross_interval}s | "
            f"市场级 {self.cross_daily_interval}s | "
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
        from app.modules.collection.schedule import get_schedule

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
            # 三项各自可关：服务分工时（例如"只采基本面"）不该再替别的
            # 服务拉一遍资金流/筹码——数据源的配额是整台机器共享的。
            if self.cross_with_flow:
                items.append("fund_flow")
            if self.cross_with_chip:
                items.append("chip")
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
        from app.modules.universe.pool import (
            sync_category,
            sync_chip,
            sync_fund_flow,
            sync_fundamentals,
        )

        final = slot == "post"
        report: Dict[str, Any] = {"slot": slot}
        today = date.today()

        if self.cross_symbols:
            if self.cross_with_flow:
                r1 = sync_fund_flow(self.cross_symbols)
                self.advance_progress(1, failed=bool(r1.get("failed")))
                self.stats_cross["fund_flow_ok"] += r1.get("ok", 0)
                self.stats_cross["failed"] += len(r1.get("failed") or [])
                report["fund_flow"] = r1
            if self.cross_with_chip:
                r2 = sync_chip(self.cross_symbols)
                self.advance_progress(1, failed=bool(r2.get("failed")))
                self.stats_cross["chip_ok"] += r2.get("ok", 0)
                self.stats_cross["failed"] += len(r2.get("failed") or [])
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
            from app.modules.factor.features import get_provider

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
        from app.modules.universe.pool import sync_boards, sync_concept_members, sync_limitup

        report: Dict[str, Any] = {}
        self.start_progress(3, "市场级 · 涨停池/板块/题材")
        try:
            return self._run_cross_daily_body()
        finally:
            self.finish_progress()
    def _run_cross_daily_body(self) -> Dict[str, Any]:
        from app.modules.universe.pool import sync_boards, sync_concept_members, sync_limitup

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
            "with_flow": self.cross_with_flow,
            "with_chip": self.cross_with_chip,
            "categories": list(self.cross_categories),
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

    # ============================================================
    # 单元：watchlist（关注标的 / 自选股）
    # ============================================================
    # 这个单元和其它三个最不一样的地方：**它的输入是用户在运行时改的**。
    # K 线/截面/完整性的标的与时间窗来自配置文件，重启不变；
    # 关注清单来自用户在面板上的增删，随时变。
    # 所以它不能把任何状态缓存在内存里当权威 ——
    # 每一轮都重新去库里读一次"现在还有谁、谁到期了"。
    # 多一次几十行的查询远比"用户在面板点了取消关注，它还在采"便宜。

    # ---------------- 生命周期 ----------------
    def _init_watchlist(self) -> None:
        cfg = self.component_config or {}
        wl = cfg.get("watchlist")
        if isinstance(wl, dict):
            if wl.get("enabled") is not None:
                self._watchlist_cfg.enabled = bool(wl["enabled"])
            if wl.get("interval") is not None:
                self._watchlist_cfg.interval = str(wl["interval"])
            if wl.get("sweep_interval") is not None:
                self._watchlist_cfg.sweep_interval = float(wl["sweep_interval"])
            if wl.get("max_per_round") is not None:
                self._watchlist_cfg.max_per_round = int(wl["max_per_round"])
            # categories 依旧 ``is not None``：[] = "一个都不要"
            if wl.get("categories") is not None:
                self._watchlist_cfg.categories = [
                    str(c).strip() for c in (wl["categories"] or [])
                    if str(c).strip()
                ]

        self._ensure_watchlist_schema()
        if not self._watchlist_cfg.enabled:
            logger.info("关注标的自动采集已关闭（watchlist.enabled=false）")
            return

        seed_from_config(self._watchlist_cfg)
        try:
            from app.data.repo.watchlist_repo import (
                count_watchlist, due_watchlist,
            )

            total = count_watchlist(enabled_only=False)
            due = len(due_watchlist(limit=self._watchlist_cfg.max_per_round or 0))
        except Exception as exc:
            logger.warning(f"读取关注清单失败（本轮不采）: {exc}")
            return
        logger.info(
            f"关注标的就绪 | 共 {total} 只 | 其中 {due} 只待采 | "
            f"默认频率 {self._watchlist_cfg.interval} | "
            f"检查周期 {self._watchlist_cfg.sweep_seconds:.0f}s | "
            f"单轮上限 {self._watchlist_cfg.max_per_round}"
        )

    def _ensure_watchlist_schema(self) -> None:
        """确保 watchlist 表存在。幂等，只在库里没有这张表时才建。

        为什么这里也要建表（而不是只靠 init-db）：关注清单是**运行时产生**的
        —— 用户今天在面板上点"关注"，从来没人要求他先跑一遍数据库初始化。
        ``create_all`` 只补缺失表、不动已有表，重复调用是安全的。
        """
        try:
            from app.data.engine import engine as db_engine
            from app.data.schema import Base

            Base.metadata.create_all(bind=db_engine)
        except Exception as exc:
            logger.warning(f"关注标的表初始化失败（后续读写可能报错）: {exc}")

    def _start_watchlist(self) -> defer.Deferred:
        if not self._watchlist_cfg.enabled:
            return defer.succeed(None)
        sweep = self._watchlist_cfg.sweep_seconds
        self._watchlist_loop = task.LoopingCall(self._tick_watchlist)
        # now=False：启动那一刻**不马上采一轮**。
        # 冷启动时 kline/cross 的两轮补采正在打数据源，
        # 这时候再插一队全类别请求，是最容易把自己送进限流的时刻。
        self._watchlist_loop.start(sweep, now=False)
        logger.info(
            f"关注标的定时器启动 | 每 {sweep:.0f}s 检查一次谁到期 | "
            f"默认采集频率 {self._watchlist_cfg.interval}"
        )
        return defer.succeed(None)

    def _stop_watchlist(self, graceful: bool = True) -> defer.Deferred:
        if self._watchlist_loop is not None and self._watchlist_loop.running:
            self._watchlist_loop.stop()
        self._watchlist_loop = None
        # 停的时刻若正采着，一并复位忙标志，免得留下永久 busy
        self._busy_watchlist = False
        return defer.succeed(None)

    def _is_busy_watchlist(self) -> bool:
        return self._busy_watchlist

    # ---------------- 执行 ----------------
    def _tick_watchlist(self):
        """看一眼有没有标的到点了；没有就什么都不做。

        先用一次**轻量查询**确认有到期标的，再决定是否上线程池。
        绝大多数轮次（默认 1h 频率 / 60s 检查 = 59/60）走到第一句就返回了，
        省掉每次都搬运一次线程上下文的开销。
        """
        if self._busy_watchlist:
            return defer.succeed(None)
        try:
            from app.data.repo.watchlist_repo import due_watchlist

            due = due_watchlist(limit=1)
        except Exception as exc:
            logger.debug(f"关注标的到期查询失败: {exc}")
            return defer.succeed(None)
        if not due:
            self.stats_watchlist["skipped_no_due"] += 1
            return defer.succeed(None)

        self._busy_watchlist = True
        d = threads.deferToThread(self._run_watchlist_once)
        d.addBoth(self._release_watchlist)
        return d

    def _release_watchlist(self, result: Any) -> Any:
        self._busy_watchlist = False
        # errback 送下来的是 ``Failure``，不是 ``Exception`` ——
        # 只判 Exception 会让"线程池里炸了"这回事被静默吞掉，
        # 面板上看到的就是"关注标的再也没采过，但统计里没有失败"。
        if isinstance(result, Exception) or isinstance(result, _failure.Failure):
            self.stats_watchlist["errors"] += 1
            getter = getattr(result, "getErrorMessage", None)
            msg = getter() if callable(getter) else str(result)
            log_throttled("ERROR", f"关注标的自动采集异常: {msg}",
                          key="watchlist-error")
            return None
        return result

    def _run_watchlist_once(self) -> Dict[str, Any]:
        """真跑一轮到期标的（在线程池里执行）。"""
        summary = collect_due_watchlist(config=self._watchlist_cfg)
        st = self.stats_watchlist
        st["runs"] += 1
        st["collected"] += len(summary.get("ok_symbols") or [])
        st["failed"] += len(summary.get("failed_symbols") or [])
        st["last_run_at"] = datetime.now().isoformat()
        st["last_symbols"] = list(summary.get("symbols") or [])
        syms = summary.get("symbols") or []
        if syms:
            ok_n = len(summary.get("ok_symbols") or [])
            fail_n = len(summary.get("failed_symbols") or [])
            level = "INFO" if not fail_n else "WARNING"
            log_throttled(
                level,
                f"关注标的采集完成 | 到期 {len(syms)} 只 | 成功 {ok_n} | "
                f"失败 {fail_n}"
                + (f" | {summary.get('error') or ''}" if fail_n else ""),
                key="watchlist-run",
            )
        return summary

    def collect_watchlist_now(self, symbols: Optional[Sequence[str]] = None,
                              ignore_due: bool = True) -> Dict[str, Any]:
        """**不管到没到点**，立刻采一轮（面板「立即采集」按钮 / API 用）。

        :param symbols: 限定只采这几只；为空 = 采所有启用的关注标的
        :param ignore_due: True = 连没到期的也一起采。
            用户点了"立即采集"就是想要**现在**的数据，
            这时候再去跟他讲"你上次是 20 分钟前采的、还没到期"只会让人恼火。
        """
        if not ignore_due:
            return collect_due_watchlist(config=self._watchlist_cfg)

        from app.data.repo.watchlist_repo import list_watchlist

        want = {str(s).strip() for s in (symbols or []) if str(s).strip()}
        picked = [
            i["symbol"] for i in list_watchlist(enabled_only=True)
            if not want or i["symbol"] in want
        ]
        if not picked:
            # 两种"没得采"要分开说：**明确点名却找不到**是失败（用户点错了），
            # 而列表本来就是空的不算错误 —— 它只是"没有要做的活"。
            return {
                "ok": False if want else True,
                "due": 0, "symbols": [], "ok_symbols": [],
                "failed_symbols": [], "groups": [],
                "error": (f"指定的标的都不在关注列表里: {', '.join(sorted(want))}"
                          if want else "关注列表为空或都已停用"),
            }
        # 把"当前时间"推到足够远，于是每一只都被判成到期 ——
        # 语义上正是"用户要求现在就采，别管上次什么时候采过"。
        far = datetime.now() + timedelta(days=36500)
        return collect_due_watchlist(
            now=far, config=self._watchlist_cfg,
            limit=len(picked), symbols=picked,
        )

    def watchlist_status(self) -> Dict[str, Any]:
        """关注标的单元的状态快照（面板 / API 用）。

        单独开一个公开入口而不是让面板去读 ``snapshot()["watchlist"]``：
        后者会把体检报告等重型字段一并算出来，而面板只想每隔几秒
        问一句"还有几只没采到"。
        """
        return self._snapshot_watchlist()

    # ---------------- 可观测 ----------------
    def _watchlist_due(self, limit: int = 0) -> List[str]:
        try:
            from app.data.repo.watchlist_repo import due_watchlist

            return [d["symbol"] for d in due_watchlist(limit=limit or 0)]
        except Exception:
            return []

    def _snapshot_watchlist(self) -> Dict[str, Any]:
        cfg = self._watchlist_cfg
        due = self._watchlist_due(limit=cfg.max_per_round or 0)
        total = enabled_n = 0
        try:
            from app.data.repo.watchlist_repo import count_watchlist

            total = count_watchlist(enabled_only=False)
            enabled_n = count_watchlist(enabled_only=True)
        except Exception:
            pass
        return {
            "enabled": bool(cfg.enabled),
            "config": cfg.to_dict(),
            "busy": self._busy_watchlist,
            "running": bool(getattr(self._watchlist_loop, "running", False)),
            "total": total,
            "enabled_count": enabled_n,
            "due_count": len(due),
            "due_symbols": due,
            "stats": dict(self.stats_watchlist),
        }

    def _health_check_watchlist(self):
        cfg = self._watchlist_cfg
        if not cfg.enabled:
            return True, "disabled"
        st = self.stats_watchlist
        # 只看**连续失败且一只都没成过**：偶发几只票失败（退市、接口抽风）
        # 不该让整个采集服务标红 —— 那是 Day-to-day 的正常噪声。
        if int(st.get("errors") or 0) >= 5 and not int(st.get("collected") or 0):
            return False, f"关注标的连续失败 {st['errors']} 次且无成功记录"
        try:
            from app.data.repo.watchlist_repo import count_watchlist

            n = count_watchlist(enabled_only=True)
        except Exception:
            n = 0
        return True, f"关注标的 {n} 只在采"


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


def run_full_collection(symbols: Optional[Sequence[str]] = None,
                        categories: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """全类别立即采集（指定标的）：量价 K 线 + 日频截面 + 舆情/宏观。

    抽成模块级函数，让「任务处理器 ``handle_collect_now``」与「API 混合类
    ``CollectViewsMixin.collect_now``」共用同一份逻辑，避免两份实现漂移。

    约定：**数据问题**（某只票退市、接口抽风）就地记进 ``report["detail"]``，
    不中断整轮；但**存储后端故障**（:class:`StorageError`）必须上浮 ——
    由调用方（任务处理器）判失败、进错误队列、可重放。
    """
    syms = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    cats = [str(c).strip() for c in (categories or [])] or [
        "lhb", "margin", "forecast", "holders", "rating", "news",
    ]
    report: Dict[str, Any] = {"ok": True, "symbols": syms, "detail": {}}

    # 先采量价类（有行情才能算技术/表达式因子）
    try:
        from app.data.service import MarketDataService
        from app.domain.symbol import normalize_symbol

        rows = 0
        for s in syms:
            info = MarketDataService.collect_incremental(
                normalize_symbol(s), lookback_days=365, adjust="qfq")
            rows += int(info.get("rows") or 0)
        report["detail"]["daily"] = {"ok": len(syms), "rows": rows}
    except StorageError:
        raise
    except Exception as exc:
        report["detail"]["daily"] = {"ok": 0, "error": str(exc)[:120]}

    # 再采日频截面
    from app.modules.universe.pool import (
        sync_category, sync_chip, sync_fund_flow, sync_fundamentals,
    )
    for label, fn in (("fundflow", lambda: sync_fund_flow(syms)),
                      ("chip", lambda: sync_chip(syms)),
                      ("fundamental", lambda: sync_fundamentals(syms))):
        try:
            r = fn()
            report["detail"][label] = {
                "ok": r.get("ok", 0),
                "failed": (r.get("failed") or [])[:5],
            }
        except StorageError:
            raise
        except Exception as exc:
            report["detail"][label] = {"ok": 0, "error": str(exc)[:120]}

    for cat in cats:
        try:
            r = sync_category(cat, syms)
            report["detail"][cat] = {
                "ok": r.get("ok", 0),
                "failed": (r.get("failed") or [])[:5],
            }
        except StorageError:
            raise
        except Exception as exc:
            report["detail"][cat] = {"ok": 0, "error": str(exc)[:120]}

    # 市场级
    for cat in ("macro", "breadth"):
        try:
            r = sync_category(cat)
            report["detail"][cat] = {"ok": r.get("ok", 0)}
        except StorageError:
            raise
        except Exception as exc:
            report["detail"][cat] = {"ok": 0, "error": str(exc)[:120]}

    # 采完必须失效特征缓存，否则界面上看到的还是旧值
    try:
        from app.modules.factor.features import get_provider
        get_provider().invalidate()
    except Exception:
        pass
    return report


__all__ = ["CollectionService", "parse_interval", "run_full_collection"]
