#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : merge_services.py
# @Description : 把多个平级组件**真正合并**成一个服务类。
#
#                为什么要写脚本而不是手工抄：三个采集组件合计约 1300 行，
#                手工搬运必然抄错，而且错了不会立刻报——它会在某个深夜
#                "采集少跑了一档频率"时才暴露。
#
#                脚本做的事：
#                  1. AST 定位源类，按源码切片取出每个方法（含装饰器）；
#                  2. 按重命名表把 ``self.<属性>`` / 方法名 / 内部互调改名，
#                     解决三个类合并后的命名冲突；
#                  3. 拼进新类，方法体**逐字节原样保留**；
#                  4. 校验：每个源方法都被落地、没有残留旧名、产物可编译。
#
#                不做事：不改写任何方法体内部的逻辑。改的只有名字。
#                复合生命周期（on_initialize/on_start/on_stop/snapshot/...）
#                由本脚本里的模板字符串手写——那是真正的语义决策，
#                不适合机器推导。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "app" / "engine" / "component"
SVC = COMP / "service"


# =============================================================================
# 工具
# =============================================================================
def class_node(tree: ast.AST, name: str) -> ast.ClassDef:
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == name:
            return n
    raise SystemExit(f"❌ 源类 {name} 未找到")


def seg_of(lines: List[str], node: ast.AST) -> str:
    """取节点源码（含装饰器与紧邻其上的注释）。"""
    starts = [node.lineno]
    for d in getattr(node, "decorator_list", []) or []:
        starts.append(d.lineno)
    start = min(starts)
    # 往上吃掉连续的注释行与空行（保留方法上方的说明块）
    i = start - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        start = i + 1
        i -= 1
    return "\n".join(lines[start - 1:node.end_lineno])


def apply_renames(src: str, attr_map: Dict[str, str],
                  method_map: Dict[str, str]) -> str:
    """把 ``self.X`` 与方法定义改名。只改名字，不改逻辑。

    正则里的 ``\\b`` 关键：``self.stats`` 不会误伤已经改好的
    ``self.stats_kline``（``_`` 是词字符，边界不匹配），
    所以同一份文本反复处理是幂等的。
    """
    names = dict(attr_map)
    names.update(method_map)
    if names:
        alt = "|".join(sorted((re.escape(k) for k in names), key=len, reverse=True))
        pat = re.compile(r"(?<![\w.])self\.(" + alt + r")\b")
        src = pat.sub(lambda m: "self." + names[m.group(1)], src)
    for old, new in method_map.items():
        src = re.sub(rf"^(\s*)(async )?def {re.escape(old)}\b",
                     lambda m: f"{m.group(1)}{m.group(2) or ''}def {new}",
                     src, flags=re.M)
    return src


def module_preamble(lines: List[str], tree: ast.AST,
                    skip_classes: Tuple[str, ...]) -> str:
    """取模块级的**常量与辅助函数**（如 ``parse_interval``）。

    import 与模块 docstring 一律跳过：新模块的 import 由模板 header
    统一声明，重复导入既噪音又可能顺序错乱（比如 ``from __future__``
    必须出现在最前面）。
    """
    kinds = (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef)
    keep: List[Tuple[int, int]] = []       # (start, end) 1-based，含装饰器
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name in skip_classes:
            continue
        if not isinstance(n, kinds):
            continue
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in (
                n.targets if isinstance(n, ast.Assign) else [n.target]
            )
        ):
            continue  # 旧模块的 __all__ 不能带过来
        starts = [n.lineno]
        for d in getattr(n, "decorator_list", []) or []:
            starts.append(d.lineno)
        start = min(starts)
        i = start - 2
        while i >= 0 and lines[i].strip().startswith("#"):
            start = i + 1
            i -= 1
        keep.append((start, n.end_lineno))
    if not keep:
        return ""
    return "\n\n".join("\n".join(lines[s - 1:e]) for s, e in keep)


# =============================================================================
# 合并配置
# =============================================================================
#: 任何单元都要丢掉的方法。
#: ``__init__`` 尤其重要：合并后构造函数必须由模板统一提供，
#: 若放任三个源类的 ``__init__`` 一起搬进来，最后一个会**静默覆盖**
#: 前面所有的（包括模板里那个）—— 表现为服务实例缺属性，
#: 而报错点离根因十万八千里。
ALWAYS_DROP: Tuple[str, ...] = ("__init__",)


class Unit:
    def __init__(self, file: str, cls: str, key: str,
                 attrs: Dict[str, str], methods: Dict[str, str],
                 drop: Tuple[str, ...] = (),
                 literals: Tuple[Tuple[str, str], ...] = ()):
        self.file, self.cls, self.key = file, cls, key
        self.attrs, self.methods = attrs, methods
        self.drop = tuple(ALWAYS_DROP) + tuple(drop)
        self.literals = literals


def build_unit(u: Unit) -> Tuple[str, List[str], Dict[str, str]]:
    """返回一个单元的全部方法源码 + 模块级前导代码。"""
    path = COMP / u.file
    if not path.exists():
        # 源组件在合并完成后已被删除 —— 这是**预期状态**，不是故障。
        # 想复核映射关系请用 git 把旧文件取回来：
        #   git show <合并前的提交>:app/engine/component/collector.py
        raise SystemExit(
            f"❌ 源组件 {u.file} 已不在 {COMP}（合并后已删除）。\n"
            f"   本脚本是一次性迁移工具：合并完成后服务文件由人工继续维护，\n"
            f"   不要再 --apply 覆盖回去（那会丢掉合并之后的改动）。\n"
            f"   需要复核时用 git show 取回旧文件即可。"
        )
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    cls = class_node(tree, u.cls)

    body: List[str] = []
    emitted: Dict[str, str] = {}
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if m.name in u.drop:
            continue
        seg = seg_of(lines, m)
        seg = apply_renames(seg, u.attrs, u.methods)
        for old, new in u.literals:
            seg = seg.replace(old, new)
        new_name = u.methods.get(m.name, m.name)
        emitted[m.name] = new_name
        body.append(seg)

    pre = module_preamble(lines, tree, (u.cls,))
    return pre, body, emitted


# =============================================================================
# 目标一：CollectionService
# =============================================================================
COLLECTION_UNITS = [
    Unit(
        "collector.py", "DataCollectorComponent", "kline",
        attrs={
            "stats": "stats_kline",
            "_busy": "_busy_kline",
            "_loops": "_loops_kline",
            "_job_state": "_job_state_kline",
            "_run_counts": "_run_counts_kline",
            "spec": "kline_spec",
            "_pending_spec": "_pending_kline_spec",
            "_symbols": "_kline_symbols",
            "_interval": "_kline_interval",
            "_period": "_kline_period",
            "_adjust": "_kline_adjust",
            "_source": "_kline_source",
            "_min_interval": "_kline_min_interval",
            "_config_path": "_kline_config_path",
            "_enabled": "_kline_enabled",
            "disabled_reason": "kline_disabled_reason",
        },
        methods={
            "on_initialize": "_init_kline",
            "on_start": "_start_kline",
            "on_stop": "_stop_kline",
            "_load_spec": "_load_kline_spec",
            "_sync_state": "_sync_kline_state",
            "_ensure_tables": "_ensure_kline_tables",
            "_start_bucket": "_start_kline_bucket",
            "_stop_bucket": "_stop_kline_bucket",
            "_reschedule": "_reschedule_kline",
            "_tick": "_tick_kline",
            "_on_bucket_error": "_on_kline_bucket_error",
            "_release": "_release_kline",
            "_run_bucket": "_run_kline_bucket",
            "_collect_one": "_collect_one_kline",
            "_evict": "_evict_kline",
            "_record_success": "_record_kline_success",
            "_record_failure": "_record_kline_failure",
            "is_busy": "_is_busy_kline",
            "snapshot": "_snapshot_kline",
            "health_check": "_health_check_kline",
            "set_interval": "_set_kline_interval",
            "add_symbol": "_add_kline_symbol",
            "remove_symbol": "_remove_kline_symbol",
            "reload_config": "_reload_kline_config",
            "collect_now": "_collect_kline_now",
            "_run_forced": "_run_kline_forced",
        },
    ),
    Unit(
        "extdata.py", "ExtDataComponent", "cross",
        attrs={
            "categories": "cross_categories",
            "symbols": "cross_symbols",
            "interval": "cross_interval",
            "daily_interval": "cross_daily_interval",
            "theme_top": "cross_theme_top",
            "with_fundamental": "cross_with_fundamental",
            "_loop": "_cross_loop",
            "_daily_loop": "_cross_daily_loop",
            "_busy": "_busy_cross",
            "_slots": "_cross_slots",
            "_synced_date": "_cross_synced_date",
            "_final_date": "_cross_final_date",
            "_last_daily": "_cross_last_daily",
            "stats": "stats_cross",
            "last_report": "cross_last_report",
            "enabled": "_cross_enabled",
        },
        methods={
            "on_initialize": "_init_cross",
            "on_start": "_start_cross",
            "on_stop": "_stop_cross",
            "_calendar": "_cross_calendar",
            "_market_symbols": "_cross_market_symbols",
            "_startup_check": "_startup_check_cross",
            "is_busy": "_is_busy_cross",
            "_tick": "_tick_cross",
            "_release": "_release_cross",
            "_slot": "_cross_slot",
            "_schedule": "_cross_schedule",
            "_should_fetch": "_cross_should_fetch",
            "_plan_items": "_cross_plan_items",
            "_run_once": "_run_cross_once",
            "_run_once_body": "_run_cross_once_body",
            "_tick_daily": "_tick_cross_daily",
            "_run_daily": "_run_cross_daily",
            "_run_daily_body": "_run_cross_daily_body",
            "snapshot": "_snapshot_cross",
            "health_check": "_health_check_cross",
        },
        drop=("set_engine",),
        literals=(('for name in ("market_center", "strategy_manager"):',
                   'for name in ("market_service", "strategy"):'),),
    ),
    Unit(
        "dataguard.py", "DataGuardComponent", "guard",
        attrs={
            "symbols": "guard_symbols",
            "interval": "guard_interval",
            "start_delay": "guard_start_delay",
            "lookback_days": "guard_lookback_days",
            "auto_repair": "guard_auto_repair",
            "max_symbols": "guard_max_symbols",
            "max_repair": "guard_max_repair",
            "_loop": "_guard_loop",
            "_busy": "_busy_guard",
            "_report": "_guard_report",
            "stats": "stats_guard",
            "disabled_reason": "guard_disabled_reason",
            "enabled": "_guard_enabled",
            # 体检单元有一处**直接读**进度（"第一轮才开一轮"的判断）。
            # 合并后进度按单元拆成了三份，这里必须指向自己那一份，
            # 否则读的是服务级进度（永远是 round=0）→ 每只标的都重开一轮，
            # 面板上进度条只会显示 1/N。
            "_progress": "_progress_guard",
        },
        methods={
            "on_initialize": "_init_guard",
            "on_start": "_start_guard",
            "on_stop": "_stop_guard",
            "_on_step": "_on_guard_step",
            "_tick": "_tick_guard",
            "_on_error": "_on_guard_error",
            "_release": "_release_guard",
            "_run": "_run_guard",
            "check_now": "_check_guard_now",
            "is_busy": "_is_busy_guard",
            "health_check": "_health_check_guard",
            "snapshot": "_snapshot_guard",
        },
    ),
]

COLLECTION_HEADER = '''#!/usr/bin/env python3
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
        """立刻跑一轮日频截面采集。"""
        return threads.deferToThread(self._run_cross_once, slot)


# ============================================================
# 三个单元的方法（由脚本从原组件搬来，方法体未改写）
# ============================================================
'''


# =============================================================================
# 目标二：TradingService
# =============================================================================
TRADING_UNITS = [
    Unit(
        "live_gateway.py", "LiveGatewayComponent", "gateway",
        attrs={"stats": "stats_gateway"},
        methods={
            "on_initialize": "_init_gateway",
            "on_start": "_start_gateway",
            "on_stop": "_stop_gateway",
            "is_busy": "_is_busy_gateway",
            "snapshot": "_snapshot_gateway",
            "health_check": "_health_check_gateway",
        },
    ),
    Unit(
        "trading_store.py", "TradingStoreComponent", "store",
        attrs={
            "stats": "stats_store",
            "enabled": "_store_enabled",
            "_loop": "_store_loop",
        },
        methods={
            "on_initialize": "_init_store",
            "on_start": "_start_store",
            "on_stop": "_stop_store",
            "snapshot": "_snapshot_store",
            "health_check": "_health_check_store",
            "_ensure_tables": "_ensure_store_tables",
            "_on_order": "_on_store_order",
            "_record_trade": "_record_store_trade",
            "_find_task": "_find_store_task",
            "_on_collected": "_on_store_collected",
            "_on_error": "_on_store_error",
            "_sample_equity": "_sample_store_equity",
            "_register_tasks": "_register_store_tasks",
            "_runtime": "_store_runtime",
            "_record_task": "_record_store_task",
        },
        drop=("is_busy",),
        literals=(('engine.get_component("strategy_manager")',
                   'engine.get_component("strategy")'),
                  ('self.bound_engine.get_component("strategy_manager")',
                   'self.bound_engine.get_component("strategy")')),
    ),
]

TRADING_HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : trading.py
# @Description : 交易服务 —— 下单路由 + 成交/权益落库，一个服务管完
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

重构说明
========

由原 ``LiveGatewayComponent``（券商网关）与 ``TradingStoreComponent``
（订单/成交/权益落库）**合并**而成。

为什么要合并
------------

它们不是两件事，是同一件事的两端：

    下单 ----> 券商 ----> 回报 ----> 落库
      ^                              |
      +---------- 对账比对 ----------+

拆成两个平级组件时，"这笔单发出去了但没落库"这类问题
没有任何一个组件能独立回答 —— 因为账本在 A、通道在 B。
而且**对账**这件事天然要同时碰两边：拿所有任务的本地账本汇总，
去比券商账户。分家之后只能靠事件总线来回传，链路一长就丢。

合并后"一笔交易从下单到入账"在一个对象里闭环，
对账是它自己的方法，而不是跨组件的协作。

保留的差异
----------

落库单元有自己的开关（``TRADE_PERSIST_ENABLED``），
与网关是否连接无关——只读档位（不下单）也要能落库。
所以服务级 ``enabled`` 与落库级 ``_store_enabled`` 是**两个开关**，
不要混为一谈。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from twisted.internet import defer, task, threads

from app.engine.component.kinds import Cap, RestartPolicy, ServiceComponent
from app.engine.event import StandardEvents
from app.execution.endpoints import load_endpoints
from app.execution.gateway import (
    FillEvent,
    GatewayError,
    GatewayOrder,
    IBrokerGateway,
    create_gateway,
    has_gateway,
    list_gateways,
)
from app.storage.repo.trade_repo import get_repository
from app.domain.types import OrderStatus
from app.common.config import settings
from app.common.jsonio import json_safe
from app.common.logging import log_throttled, logger


class TradingService(ServiceComponent):
    """交易服务。

    **提供的能力**：
      * ``trade.route``    —— 把订单路由到券商（进程内唯一连接）
      * ``trade.account``  —— 查券商资金/持仓（带缓存）
      * ``trade.reconcile``—— 本地账本 ⟷ 券商账户 对账
      * ``trade.persist``  —— 订单/成交/权益/事件落库

    两个内部单元
    ------------
    ``_gateway``  券商通道：连接、报单、回报轮询、撤单、对账
    ``_store``    账本落库：订单状态、成交、权益曲线、重要事件

    共用一个网关连接是硬约束——券商通常不允许同账号多连接。
    所以这里持有**进程内唯一**的网关实例，分发给各任务的 LiveBroker。
    """

    name = "trading_service"
    provides = frozenset({
        Cap.ORDER_ROUTE, Cap.ACCOUNT_QUERY, Cap.RECONCILE, Cap.TRADE_PERSIST,
    })
    restart_policy = RestartPolicy.NEVER

    def __init__(
        self,
        # ---- 网关单元 ----
        gateway: Optional[IBrokerGateway] = None,
        gateway_name: str = "",
        endpoint_id: str = "",
        poll_interval: float = 1.0,
        reconcile_interval: float = 300.0,
        account_interval: float = 30.0,
        strict_reconcile: Optional[bool] = None,
        # ---- 落库单元 ----
        store_enabled: Optional[bool] = None,
        equity_interval: Optional[float] = None,
        run_id: str = "",
        engine: Any = None,
        # ---- 服务级 ----
        enabled: Optional[bool] = None,
        gateway_enabled: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()

        # ---------------- 网关单元 ----------------
        #: 网关单元是否启用。**只有 LIVE 模式才该是 True**。
        #: 模拟/回测下这个服务仍然装配 —— 它还要落库 ——
        #: 但不能去连真券商。为 False 时网关单元整体不初始化、不启动、不体检，
        #: 落库照常工作。
        self.gateway_enabled = True if gateway_enabled is None else bool(gateway_enabled)
        self.gateway = gateway
        self.gateway_name = str(gateway_name or settings.BROKER_GATEWAY)
        self.endpoint_id = str(endpoint_id or settings.BROKER_ENDPOINT or "")
        self.poll_interval = float(poll_interval)
        self.reconcile_interval = float(reconcile_interval)
        self.account_interval = float(account_interval)
        self.strict_reconcile = (
            settings.LIVE_STRICT_RECONCILE if strict_reconcile is None
            else bool(strict_reconcile)
        )
        self._brokers: Dict[str, Any] = {}
        self._poll_loop: Optional[task.LoopingCall] = None
        self._reconcile_loop: Optional[task.LoopingCall] = None
        self._account_loop: Optional[task.LoopingCall] = None
        self._busy = False
        self.account: Dict[str, Any] = {}
        self.positions: Dict[str, Any] = {}
        self.account_at: str = ""
        self.account_error: str = ""
        self.endpoint_info: Dict[str, Any] = {}
        self.stats_gateway: Dict[str, int] = {
            "polls": 0, "fills": 0, "updates": 0, "unknown": 0,
            "reconciles": 0, "mismatches": 0, "errors": 0, "account_refreshes": 0,
        }
        self.last_reconcile: Dict[str, Any] = {}
        self._seeded_tasks: set = set()
        self._capital_taken_over = False

        # ---------------- 落库单元 ----------------
        self._store_enabled = (
            settings.TRADE_PERSIST_ENABLED if store_enabled is None
            else bool(store_enabled)
        )
        self.equity_interval = float(
            equity_interval if equity_interval is not None
            else settings.EQUITY_SAMPLE_INTERVAL
        )
        self.bound_run_id = run_id or ""
        self.bound_engine = engine
        self.repo = get_repository()
        self._store_loop: Optional[task.LoopingCall] = None
        self._last_equity_at: Dict[str, float] = {}
        self.stats_store: Dict[str, int] = {
            "orders": 0, "trades": 0, "rejected": 0,
            "equity_points": 0, "events": 0, "errors": 0,
        }

        # ---------------- 服务级 ----------------
        if enabled is not None:
            self.enabled = bool(enabled)

    # ============================================================
    # 开关（只读视图）
    # ============================================================
    @property
    def store_enabled(self) -> bool:
        """落库单元是否启用。

        注意它和**服务级** ``enabled`` 是两件事：只读档位（不下单）
        也要能落库，所以网关关着的时候这里依然可能是 True。
        """
        return bool(self._store_enabled)

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        # 落库先就绪：网关初始化会读一次账户并可能写事件，
        # 落库没订阅上就会漏掉"服务启动"这一条最有价值的记录。
        self._init_store()
        if self.gateway_enabled:
            self._init_gateway()
        else:
            logger.info(
                "交易服务已装配（仅落库）| 券商网关未启用 "
                "（非 LIVE 模式，或网关被显式关闭）"
            )

    @defer.inlineCallbacks
    def on_start(self) -> defer.Deferred:
        yield self._start_store()
        if self.gateway_enabled:
            yield self._start_gateway()

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        # 逆序：先断网关（它会撤掉留在券商的挂单），再收尾落库
        if self.gateway_enabled:
            yield self._stop_gateway()
        yield self._stop_store()

    def is_busy(self) -> bool:
        return self._is_busy_gateway() if self.gateway_enabled else False

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base["gateway_enabled"] = self.gateway_enabled
        if self.gateway_enabled:
            try:
                base["gateway"] = self._snapshot_gateway()
            except Exception as exc:
                base["gateway_error"] = str(exc)
        try:
            base["store"] = self._snapshot_store()
        except Exception as exc:
            base["store_error"] = str(exc)
        return base

    def health_check(self):
        if not self.enabled:
            return True, "disabled"
        if self.state != "RUNNING":
            return False, self.state
        units = [("落库", self._health_check_store)]
        if self.gateway_enabled:
            units.insert(0, ("网关", self._health_check_gateway))
        bad: List[str] = []
        for label, fn in units:
            try:
                ok, detail = fn()
                if not ok:
                    bad.append(f"{label}: {detail}")
            except Exception as exc:
                bad.append(f"{label}: 检查异常 {exc}")
        if bad:
            return False, " | ".join(bad)
        return True, "交易服务正常"


# ============================================================
# 两个单元的方法（由脚本从原组件搬来，方法体未改写）
# ============================================================
'''


# =============================================================================
# 生成
# =============================================================================
TARGETS = {
    "collection": (SVC / "collection.py", COLLECTION_UNITS, COLLECTION_HEADER),
    "trading": (SVC / "trading.py", TRADING_UNITS, TRADING_HEADER),
}


def emit(target: str, apply: bool = False) -> int:
    out_path, units, header = TARGETS[target]
    preambles: List[str] = []
    bodies: List[str] = []
    expected: Dict[str, int] = {}

    for u in units:
        pre, body, emitted = build_unit(u)
        if pre.strip():
            preambles.append(f"# ===== 来自 {u.file} 的模块级代码 =====\n{pre.strip()}")
        bodies.append(
            f"\n\n# ------------------------------------------------------------------\n"
            f"# 单元：{u.key}（原 {u.cls}）\n"
            f"# ------------------------------------------------------------------\n"
        )
        bodies.extend(body)
        expected[u.file] = len(emitted)
        print(f"  {u.file:20s} {u.cls:28s} → {len(emitted):2d} 个方法")

    # 模块级辅助代码（常量与自由函数）放到类之后，避免与 header 的 import 交错
    tail = "\n\n\n" + (("\n\n".join(preambles) + "\n\n\n") if preambles else "")
    exports = {
        "collection": '__all__ = ["CollectionService", "parse_interval"]\n',
        "trading": '__all__ = ["TradingService"]\n',
    }[target]
    content = header + "\n".join(bodies) + tail + exports

    # ---- 校验 ----
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        print(f"❌ {target} 产物语法错误: {exc}")
        return 1

    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef)), None)
    meths = [
        n.name for n in (cls.body if cls else [])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    got = set(meths)

    # 重名即静默覆盖 —— 合并类最凶险的一类错误，必须显式拦下
    dupes = sorted({m for m in meths if meths.count(m) > 1})
    if dupes:
        print(f"❌ {target} 存在重名方法（后面的会覆盖前面的）: {dupes}")
        return 1
    # 每个源方法都必须以新名字出现
    missing: List[str] = []
    for u in units:
        for old, new in (build_unit(u)[2]).items():
            if new not in got:
                missing.append(f"{u.file}:{old}→{new}")
    if missing:
        print(f"❌ {target} 有方法没落地: {missing}")
        return 1

    # 不能残留旧属性名。
    # 只扫**搬过来的方法体**——header 里手写的复合逻辑是服务级的，
    # 它用 self.enabled / self.disabled_reason 指的就是整个服务的开关，
    # 与单元内部的旧名同字但不同义，不该算残留。
    moved = "\n".join(bodies) + tail
    for u in units:
        for old in u.attrs:
            if re.search(rf"self\.{re.escape(old)}\b", moved):
                print(f"❌ {target} 搬来的方法里仍残留 self.{old}")
                return 1

    print(f"✅ {target}: {len(got)} 个方法，语法与完整性校验通过")

    if apply:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"   已写入 {out_path.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("target", nargs="?", default="all")
    a = ap.parse_args()

    targets = list(TARGETS) if a.target == "all" else [a.target]
    rc = 0
    for t in targets:
        print(f"=== {t} ===")
        rc |= emit(t, apply=a.apply)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
