#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : maintenance.py
# @Description : 维护计划：日志清理 / 快照归档 / 定期校验 的开关、周期与参数
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
维护任务计划器
==============

为什么需要单独一层
------------------

重构后 ``system.maintenance.due`` 有三条规则（清理 / 归档 / 校验），
但**没有任何东西发布这个事件** —— 三条规则是死的，日志清理从来没跑过。
根因是"周期性维护"既不是行情事件也不是任务事件，它需要一个**时间源**。

这里的时间源就是本模块：

    scheduler 定时器 ──> system.maintenance.tick（每 30s）
                              │
                    MaintenancePlanner.tick()
                              │  哪个 job 到点了？
                              └─> system.maintenance.due {job, params}
                                        │
                              规则表（按 job 名过滤）──> 建任务 ──> maintenance 池

**计划器不发任务，只发"该干活了"这个事实**。建任务仍然归规则表管，
所以维护任务同样享有队列、幂等、重试、死信那一整套，和采集/因子任务一致。

为什么拆成 tick 和 due 两个事件
--------------------------------

一个事件也能做（tick 里直接带"该跑哪些"），但那样事件流里只能看到
"到点了"，看不出"到的是哪个点"。拆开之后运维能一眼分辨：
节拍在跑但没任务 = 计划没到点 / 被禁用；有 due 没任务 = 规则被关了。

安全默认
--------

清理与归档都**默认只报告不删**（处理器里 ``apply=False``）。
"定时任务自动删文件"是运维事故高发区：配错一天就删掉一周日志。
所以面板上要真的删，必须显式勾选，并且默认不勾。

六种周期
--------

维护任务该多久跑一次，答案是"看它是什么活"，所以周期有六档：

============  ==========================================  ==============
schedule      含义                                        用到的字段
============  ==========================================  ==============
``startup``   引擎**每次启动**跑一次（不是周期）            无
``interval``  每 N **小时**（最小 1 小时）                  ``interval_hours``
``daily``     每天 ``HH:MM``                               ``at``
``weekly``    每周某天 ``HH:MM``                           ``weekday`` + ``at``
``cron``      cron 表达式（分 时 日 月 周）                 ``cron``
``once``      在指定时刻跑一次，跑完自动停用                  ``once_at``
============  ==========================================  ==============

两点是特意这么定的：

* **间隔的最小粒度是小时**，不是秒。维护任务是"扫一遍日志目录"这种活，
  做成分钟级只会让引擎一直在做无用功；而秒级输入框会让人填出
  "每 300 秒清一次日志"这种配置，跑起来才发现没意义。
* **``startup`` 存在的理由**：像"运行校验"这种活，它的价值就是
  "这次启动的环境是好的"，每小时再校验一遍既不增加信息量，
  又会在日志里堆一堆"正常"。它该在启动时跑，且只在启动时跑。

cron 是自己实现的（见 :mod:`app.runtime.cron`），支持 ``*`` / ``*/n`` /
``a-b`` / ``a,b`` / ``a-b/n``。**「日」与「周」是 OR 语义** ——
``0 3 1 * 1`` 是"每月 1 号**或**每周一"，不是"1 号且是周一"。

配置持久化在 ``config/maintenance.json``，改完立刻生效，重启后仍在。
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.common.logging import logger
from app.runtime.cron import CronError, CronExpr, validate_cron
from app.runtime.events.types import EventType, TaskType

#: 计划器建议的检查节拍（秒）。调度器用它作为定时器间隔。
#: 30s 的代价可以忽略，且保证"每天 03:10"的误差不超过半分钟 ——
#: 维护窗口本来就允许分钟级误差，没必要为秒级精度做 cron 解析。
TICK_SECONDS: float = 30.0

#: 日任务的"错过宽限"（秒）。到点后这么久之内仍会补跑一次；
#: 超过就算错过，顺延明天。取 5 分钟：远比检查节拍宽（不会漏），
#: 又比"引擎晚启动一会儿就补跑一轮"窄（补跑维护任务收益极低）。
MISS_GRACE_SECONDS: float = 300.0

# ---------------- 周期种类 ----------------
SCHEDULE_STARTUP = "startup"      # 引擎启动时跑一次
SCHEDULE_INTERVAL = "interval"    # 每 N 小时
SCHEDULE_DAILY = "daily"          # 每天 HH:MM
SCHEDULE_WEEKLY = "weekly"        # 每周某天 HH:MM
SCHEDULE_CRON = "cron"            # cron 表达式
SCHEDULE_ONCE = "once"            # 指定时刻跑一次

SCHEDULE_KINDS: tuple = (
    SCHEDULE_STARTUP, SCHEDULE_INTERVAL, SCHEDULE_DAILY,
    SCHEDULE_WEEKLY, SCHEDULE_CRON, SCHEDULE_ONCE,
)

SCHEDULE_LABELS: Dict[str, str] = {
    SCHEDULE_STARTUP: "引擎启动时",
    SCHEDULE_INTERVAL: "每 N 小时",
    SCHEDULE_DAILY: "每天",
    SCHEDULE_WEEKLY: "每周",
    SCHEDULE_CRON: "Cron 表达式",
    SCHEDULE_ONCE: "指定时刻一次",
}

#: 间隔周期的最小粒度（小时）。见模块 docstring「六种周期」。
MIN_INTERVAL_HOURS: int = 1

#: 星期几的中文名，下标 = ``weekday``（1=周一 … 7=周日）
WEEKDAY_NAMES: tuple = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: ``once_at`` 的书写格式
ONCE_AT_FORMAT = "%Y-%m-%d %H:%M"


def _norm_schedule(value: Any, default: str = SCHEDULE_DAILY) -> str:
    """归一化周期种类；不认识的一律回落，不让脏配置把任务变成"永不执行"。"""
    text = str(value or "").strip().lower()
    return text if text in SCHEDULE_KINDS else default


def _parse_hhmm(value: Any, default: str = "03:10") -> "tuple":
    """解析 ``HH:MM``。写错回落默认值 —— 一个时刻填错不该让任务消失。"""
    raw = str(value or "").strip()
    for sep in (":", "："):
        if sep in raw:
            left, _, right = raw.partition(sep)
            try:
                hour, minute = int(left.strip()), int(right.strip())
            except ValueError:
                return _parse_hhmm(default, "03:10")
            return min(max(hour, 0), 23), min(max(minute, 0), 59)
    # 只有 "3" 或 "0300" 这种
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 4:
        return min(int(digits[:2]), 23), min(int(digits[2:]), 59)
    if digits:
        return min(int(digits), 23), 0
    return _parse_hhmm(default, "03:10")


@dataclass
class MaintenanceJob:
    """一个维护任务的定义 + 运行态。

    周期相关的字段**按种类生效**，其余留着不参与计算（切换周期时不用重填）。
    面板据此做联动显示：选"每 N 小时"就只显示小时框。

    :param interval_hours: ``interval`` 用的小时数，最小 :data:`MIN_INTERVAL_HOURS`
    :param at: ``daily`` / ``weekly`` 用的 ``HH:MM``（24 小时制）
    :param weekday: ``weekly`` 用的星期（1=周一 … 7=周日）
    :param cron: ``cron`` 用的 5 段表达式
    :param once_at: ``once`` 用的 ``YYYY-MM-DD HH:MM``
    :param interval_sec: **已废弃**，只为兼容早期配置，读入时换算成小时
    :param params: 直接透传给任务处理器（``retention_days`` / ``apply`` …）
    """

    name: str
    task_type: str
    label: str
    enabled: bool = False
    schedule: str = SCHEDULE_DAILY
    at: str = "03:10"
    weekday: int = 1
    interval_hours: int = 24
    cron: str = ""
    once_at: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # 早期配置用的是秒。留着它只为兼容读旧文件，新写出的配置不再有它。
    interval_sec: int = 0

    # ---- 运行态（不落盘）----
    last_run_at: float = 0.0        # epoch 秒
    last_result: str = ""
    runs: int = 0
    errors: int = 0

    # ---------------- 周期 ----------------
    @property
    def kind(self) -> str:
        """归一化后的周期种类（脏配置也不会得到陌生值）。"""
        return _norm_schedule(self.schedule)

    @property
    def is_startup(self) -> bool:
        return self.kind == SCHEDULE_STARTUP

    def cron_expr(self) -> Optional[CronExpr]:
        """解析好的 cron；不是 cron 种类或写错时返回 None。"""
        if self.kind != SCHEDULE_CRON:
            return None
        try:
            return CronExpr.parse(self.cron)
        except CronError:
            return None

    def cron_error(self) -> str:
        """cron 写错的原因；没错返回空串。面板直接显示。"""
        if self.kind != SCHEDULE_CRON:
            return ""
        return validate_cron(self.cron)

    def parse_once_at(self) -> Optional[datetime]:
        if self.kind != SCHEDULE_ONCE:
            return None
        raw = str(self.once_at or "").strip()
        if not raw:
            return None
        for fmt in (ONCE_AT_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def interval_hours_value(self) -> int:
        """:data:`MIN_INTERVAL_HOURS` 兜底的小时数。"""
        return max(MIN_INTERVAL_HOURS, int(self.interval_hours or MIN_INTERVAL_HOURS))

    def next_run_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """下次执行时刻。

        返回 ``None`` 有两种含义，调用方要分得开（面板显示也不同）：

        * **禁用** —— 没启用就没有下次；
        * **没有计划中的下次** —— ``startup``（只在启动时跑）
          和已错过的 ``once``。这类不是"永不执行"，是"不在周期里"。
        """
        if not self.enabled:
            return None
        base = now or datetime.now()
        kind = self.kind

        if kind == SCHEDULE_STARTUP:
            return None

        if kind == SCHEDULE_INTERVAL:
            if self.last_run_at:
                return (datetime.fromtimestamp(self.last_run_at)
                        + timedelta(hours=self.interval_hours_value()))
            # 没跑过 → 下一次检查就跑（而不是等满一个周期，
            # 否则刚启用的任务要干等 N 小时才第一次执行）
            return base

        if kind == SCHEDULE_CRON:
            expr = self.cron_expr()
            return expr.next_after(base) if expr else None

        if kind == SCHEDULE_ONCE:
            target = self.parse_once_at()
            if target is None:
                return None
            # 跑过了 → 没有下次（调用方会把它停用，这里只是防御）
            if self.last_run_at and self.last_run_at >= target.timestamp():
                return None
            if target > base:
                return target
            # 刚过 → 宽限内还算数；超过宽限就是真错过了，
            # 补跑一个"上周三该跑的校验"没有任何意义。
            if (base - target).total_seconds() <= MISS_GRACE_SECONDS:
                return target
            return None

        # ---- daily / weekly ----
        hour, minute = _parse_hhmm(self.at)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if kind == SCHEDULE_WEEKLY:
            target += timedelta(days=self.weekday_value() - base.isoweekday())
            step = timedelta(days=7)
        else:
            step = timedelta(days=1)
        # 这个点已经跑过 → 下一周/明天
        if self.last_run_at and self.last_run_at >= target.timestamp():
            return target + step
        # 还没到 → 就是它
        if target > base:
            return target
        # 刚过（一个宽限窗口内）→ **现在就跑**。
        # 这个分支不能省：检查是每 30s 一拍，"03:10" 实际会在
        # 03:10:15 附近被看到，若一律判成"过了就顺延明天"，
        # 日任务将永远触发不了。宽限之外才算真的错过，等下个周期。
        if (base - target).total_seconds() <= MISS_GRACE_SECONDS:
            return target
        return target + step

    def weekday_value(self) -> int:
        return min(max(int(self.weekday or 1), 1), 7)

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        # startup 不参与周期检查：它由引擎启动显式触发。
        # 让它也走 is_due 会导致"每次 tick 都跑一遍"（next_run_at 返回 None
        # 时不会触发，但返回 base 就会），语义必须在这里截断。
        if self.is_startup:
            return False
        nxt = self.next_run_at(now)
        return nxt is not None and nxt <= (now or datetime.now())

    # ---------------- 序列化 ----------------
    def to_dict(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        nxt = self.next_run_at(now)
        kind = self.kind
        missing = ""
        if kind == SCHEDULE_CRON and not self.cron:
            missing = "还没填 cron 表达式"
        elif kind == SCHEDULE_ONCE and self.parse_once_at() is None:
            missing = f"时刻格式应为 {ONCE_AT_FORMAT}"
        elif kind == SCHEDULE_CRON:
            missing = self.cron_error()
        return {
            "name": self.name,
            "task_type": self.task_type,
            "label": self.label,
            "enabled": self.enabled,
            "schedule": kind,
            "schedule_label": SCHEDULE_LABELS.get(kind, kind),
            "at": self.at,
            "weekday": self.weekday_value(),
            "weekday_label": WEEKDAY_NAMES[self.weekday_value() - 1],
            "interval_hours": self.interval_hours_value(),
            "cron": self.cron,
            "cron_text": self.cron_expr().describe() if self.cron_expr() else "",
            "once_at": self.once_at,
            "params": dict(self.params),
            "description": self.description,
            "config_error": missing,
            "runs": self.runs,
            "errors": self.errors,
            "last_run_at": self.last_run_at,
            "last_run_iso": (
                datetime.fromtimestamp(self.last_run_at).isoformat(timespec="seconds")
                if self.last_run_at else ""
            ),
            "next_run_at": nxt.timestamp() if nxt else None,
            "next_run_iso": nxt.isoformat(timespec="seconds") if nxt else "",
            "next_run_text": (
                nxt.isoformat(timespec="seconds") if nxt
                else ("引擎启动时" if kind == SCHEDULE_STARTUP else "—")
            ),
            "last_result": self.last_result,
        }

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any], default: "MaintenanceJob") -> "MaintenanceJob":
        schedule = _norm_schedule(raw.get("schedule"), default.kind)

        # 旧配置只有 interval_sec（秒）。换算成小时时**向上取整**：
        # 3600 秒 = 1 小时没问题，而 5400 秒（1.5h）向下取整会得到 1 小时，
        # 等于悄悄把周期缩短 —— 维护任务跑太频繁比跑少了更烦人。
        hours = raw.get("interval_hours")
        if hours is None or str(hours).strip() == "":
            legacy_sec = int(raw.get("interval_sec") or 0)
            hours = (
                max(MIN_INTERVAL_HOURS, int(math.ceil(legacy_sec / 3600.0)))
                if legacy_sec else default.interval_hours
            )

        job = cls(
            name=name,
            task_type=str(raw.get("task_type") or default.task_type),
            label=str(raw.get("label") or default.label),
            enabled=bool(raw.get("enabled", default.enabled)),
            schedule=schedule,
            at=str(raw.get("at") or default.at),
            weekday=int(raw.get("weekday") or default.weekday),
            interval_hours=int(hours or MIN_INTERVAL_HOURS),
            cron=str(raw.get("cron") or default.cron),
            once_at=str(raw.get("once_at") or default.once_at),
            params=dict(raw.get("params") or default.params),
            description=str(raw.get("description") or default.description),
        )
        return job


def _legacy_adjust(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把"还是旧内置默认值"的那几条配置迁到新默认值。

    只迁**没被改过**的配置。判定依据是它还精确等于旧的默认值
    （运行校验曾经是 ``interval`` + ``interval_sec=3600``）——
    用户改过周期、填过 cron 的一律不动，绝不替用户做决定。

    不做这件事的后果：用户从没手动改过维护计划，但面板保存过一次
    （比如只勾了"真的删除"），那份旧配置就会把已经改成"启动时执行"
    的默认值又拽回"每 1 小时"，于是用户提的需求根本没生效。
    """
    if name != "validate" or not isinstance(cfg, dict):
        return cfg
    if _norm_schedule(cfg.get("schedule")) != SCHEDULE_INTERVAL:
        return cfg
    if int(cfg.get("interval_sec") or 0) != 3600:
        return cfg
    if str(cfg.get("cron") or "").strip():
        return cfg
    migrated = dict(cfg)
    migrated["schedule"] = SCHEDULE_STARTUP
    return migrated


#: 内置维护任务。**全部默认不删东西**，只报告。
DEFAULT_JOBS: Dict[str, MaintenanceJob] = {
    "log_cleanup": MaintenanceJob(
        name="log_cleanup",
        task_type=TaskType.MAINTENANCE_LOG_CLEANUP,
        label="日志清理",
        enabled=True,
        schedule="daily",
        at="03:10",
        params={"retention_days": 7, "apply": False},
        description="扫描并清理超过保留天数的日志文件；未勾选『真的删除』时只列出清单",
    ),
    "archive": MaintenanceJob(
        name="archive",
        task_type=TaskType.MAINTENANCE_ARCHIVE,
        label="快照归档",
        enabled=False,
        schedule="daily",
        at="03:40",
        params={"before_days": 30, "apply": False},
        description="把超过天数的运行快照移入 archive/；默认关闭，需先确认归档目标",
    ),
    "validate": MaintenanceJob(
        name="validate",
        task_type=TaskType.MAINTENANCE_VALIDATE,
        label="运行校验",
        enabled=True,
        # 只在**引擎启动时**跑一次。它的价值就是"这次启动的环境是好的"，
        # 每小时再校验一遍既不增加信息量，又会在日志里堆一堆"正常"。
        schedule=SCHEDULE_STARTUP,
        params={},
        description="引擎启动时校验日志目录、数据目录与数据库可用性（只读，不删任何东西）",
    ),
}


class MaintenancePlanner:
    """维护计划：读配置 → 判断到点 → 发 ``system.maintenance.due``。"""

    def __init__(
        self,
        publish: Optional[Callable[..., Any]] = None,
        path: Optional[str] = None,
    ) -> None:
        self._publish = publish
        self._path = Path(path) if path else self.default_path()
        self._lock = threading.RLock()
        self._jobs: Dict[str, MaintenanceJob] = {}
        self._load()
        self._last_tick: float = 0.0
        self._ticks: int = 0
        self._fired: int = 0

    # ---------------- 配置 ----------------
    @staticmethod
    def default_path() -> Path:
        try:
            from app.common.config import settings

            return Path(settings.CONFIG_DIR) / "maintenance.json"
        except Exception:
            return Path("config") / "maintenance.json"

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        raw: Dict[str, Any] = {}
        try:
            if self._path.exists():
                parsed = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw = parsed.get("jobs") or parsed
        except Exception as exc:
            logger.warning(
                f"读取维护配置 {self._path} 失败（{type(exc).__name__}: {exc}），使用内置默认"
            )
            raw = {}

        with self._lock:
            self._jobs = {
                name: MaintenanceJob.from_dict(
                    name, _legacy_adjust(name, raw.get(name) or {}), default
                )
                for name, default in DEFAULT_JOBS.items()
            }
            # 配置文件里出现未知 job 名：记下来但不加载。
            # 静默丢弃会让"我明明配了却不跑"变成无法排查的问题。
            unknown = [k for k in raw if k not in DEFAULT_JOBS]
            if unknown:
                logger.warning(f"维护配置里有未知的任务名，已忽略: {unknown}")

    def save(self) -> None:
        with self._lock:
            payload = {
                "_note": "维护任务计划（面板『运行时 → 维护计划』写入，保存即生效）",
                "jobs": {
                    name: {
                        "task_type": j.task_type,
                        "label": j.label,
                        "enabled": j.enabled,
                        "schedule": j.kind,
                        "at": j.at,
                        "weekday": j.weekday_value(),
                        "interval_hours": j.interval_hours_value(),
                        "cron": j.cron,
                        "once_at": j.once_at,
                        "params": dict(j.params),
                        "description": j.description,
                    }
                    for name, j in self._jobs.items()
                },
            }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(f"保存维护配置失败: {exc}")
            raise

    # ---------------- 改配置 ----------------
    def update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """整体更新配置（面板一次提交整张表）。

        :param payload: ``{"jobs": {name: {...}}}`` 或 ``{name: {...}}``
        :return: 更新后的 :meth:`snapshot`
        """
        raw = (payload or {}).get("jobs")
        if not isinstance(raw, dict):
            raw = dict(payload or {})
        changed: List[str] = []
        with self._lock:
            for name, cfg in raw.items():
                if name not in self._jobs or not isinstance(cfg, dict):
                    continue
                cur = self._jobs[name]
                new = MaintenanceJob.from_dict(name, cfg, cur)
                # 运行态要留着 —— 改一下周期不该把"上次什么时候跑的"清空，
                # 否则每次保存完立刻就会补跑一轮。
                new.last_run_at = cur.last_run_at
                new.last_result = cur.last_result
                new.runs = cur.runs
                new.errors = cur.errors
                self._jobs[name] = new
                changed.append(name)
        self.save()
        if changed:
            logger.info(f"维护计划已更新: {changed}")
        return self.snapshot()

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            job = self._jobs.get(str(name or ""))
            if job is None:
                return False
            job.enabled = bool(enabled)
        self.save()
        return True

    # ---------------- 触发 ----------------
    def bind(self, publish: Callable[..., Any]) -> None:
        """注入事件发布函数（引擎启动时调度器调用）。"""
        self._publish = publish

    def _emit(self, job: MaintenanceJob, reason: str) -> bool:
        """发一个 ``system.maintenance.due`` 事件。**失败算没跑**。"""
        if self._publish is None:
            job.errors += 1
            job.last_result = "事件总线未就绪（引擎还没启动？）"
            logger.warning(f"维护任务 {job.name} 无法触发：事件总线未就绪")
            return False
        try:
            self._publish(
                EventType.SYSTEM_MAINTENANCE_DUE,
                job=job.name,
                task_type=job.task_type,
                params=dict(job.params),
                reason=reason,
            )
        except Exception as exc:
            job.errors += 1
            job.last_result = f"发布事件失败: {exc}"
            logger.error(f"发布维护事件失败 {job.name}: {exc}")
            return False
        job.last_run_at = time.time()
        job.runs += 1
        job.last_result = f"已触发（{reason}）"
        # 一次性任务跑完就停用。不停用的话它会在下一拍再跑一遍：
        # next_run_at 对已跑过的 once 返回 None，看似安全，
        # 但"停用"是要写回配置并显示在面板上的事实 ——
        # 一个显示"启用"却永远不会再跑的任务比"已停用"更让人困惑。
        if job.kind == SCHEDULE_ONCE:
            job.enabled = False
            job.last_result = f"已触发（{reason}），一次性任务已自动停用"
            try:
                self.save()
            except Exception as exc:  # pragma: no cover
                logger.warning(f"一次性维护任务 {job.name} 停用后写盘失败: {exc}")
        return True

    def tick(self, now: Optional[datetime] = None) -> List[str]:
        """检查一遍所有计划，给到点的发事件。

        :return: 本次触发的 job 名列表
        """
        base = now or datetime.now()
        fired: List[str] = []
        with self._lock:
            self._ticks += 1
            self._last_tick = time.time()
            for job in self._jobs.values():
                if not job.is_due(base):
                    continue
                if self._emit(job, "scheduled"):
                    fired.append(job.name)
        if fired:
            logger.info(f"维护窗口到点: {fired}")
            self._fired += len(fired)
        return fired

    def run_startup(self) -> List[str]:
        """跑一遍所有「引擎启动时」类型的任务。

        与 :meth:`tick` 完全分开的两条路径：tick 是"按周期到点了"，
        这里是"进程刚起来"。分开的理由是它们失败时的含义不同 ——
        启动任务失败意味着"这次启动的环境没被校验过"，
        而周期任务失败只是"这一轮没做成"。

        它不看 ``enabled`` 以外的任何条件，也不受周期影响：
        startup 的语义就是"每次启动一次"。

        :return: 本次触发的 job 名列表
        """
        fired: List[str] = []
        with self._lock:
            for job in self._jobs.values():
                if not job.enabled or not job.is_startup:
                    continue
                if self._emit(job, "startup"):
                    fired.append(job.name)
        if fired:
            logger.info(f"启动维护任务已触发: {fired}")
            self._fired += len(fired)
        return fired

    def run_now(self, name: str) -> Dict[str, Any]:
        """立刻跑一次（面板「立即执行」）。

        与 :meth:`tick` 的区别：它**不看周期、不看开关** ——
        "手动立即执行"被 enabled=false 挡住是最让人困惑的设计。
        """
        job = self._jobs.get(str(name or ""))
        if job is None:
            return {"ok": False, "error": f"没有这个维护任务: {name}",
                    "known": sorted(self._jobs)}
        ok = self._emit(job, "manual")
        return {
            "ok": ok,
            "job": job.name,
            "task_type": job.task_type,
            "params": dict(job.params),
            "error": "" if ok else (job.last_result or "发布失败"),
        }

    # ---------------- 观测 ----------------
    def jobs(self) -> List[MaintenanceJob]:
        with self._lock:
            return list(self._jobs.values())

    def snapshot(self) -> Dict[str, Any]:
        base = datetime.now()
        with self._lock:
            jobs = [j.to_dict(base) for j in self._jobs.values()]
        return {
            "jobs": jobs,
            "enabled": sum(1 for j in jobs if j["enabled"]),
            "total": len(jobs),
            "tick_seconds": TICK_SECONDS,
            "ticks": self._ticks,
            "fired": self._fired,
            "last_tick": self._last_tick,
            "last_tick_iso": (
                datetime.fromtimestamp(self._last_tick).isoformat(timespec="seconds")
                if self._last_tick else ""
            ),
            "config_path": str(self._path),
            # 面板要靠这些常量渲染下拉框，**不能在前台硬编码** ——
            # 否则后台加了一种周期，面板上永远选不到。
            "kinds": [
                {"value": k, "label": SCHEDULE_LABELS[k]} for k in SCHEDULE_KINDS
            ],
            "weekday_names": list(WEEKDAY_NAMES),
            "min_interval_hours": MIN_INTERVAL_HOURS,
            "once_at_format": ONCE_AT_FORMAT,
            "startup_jobs": [j.name for j in self._jobs.values() if j.is_startup],
        }


__all__ = [
    "MaintenanceJob",
    "MaintenancePlanner",
    "DEFAULT_JOBS",
    "TICK_SECONDS",
    "MISS_GRACE_SECONDS",
    "SCHEDULE_KINDS",
    "SCHEDULE_LABELS",
    "SCHEDULE_STARTUP",
    "SCHEDULE_INTERVAL",
    "SCHEDULE_DAILY",
    "SCHEDULE_WEEKLY",
    "SCHEDULE_CRON",
    "SCHEDULE_ONCE",
    "MIN_INTERVAL_HOURS",
    "WEEKDAY_NAMES",
]
