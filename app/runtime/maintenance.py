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

配置持久化在 ``config/maintenance.json``，改完立刻生效，重启后仍在。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.common.logging import logger
from app.runtime.events.types import EventType, TaskType

#: 计划器建议的检查节拍（秒）。调度器用它作为定时器间隔。
#: 30s 的代价可以忽略，且保证"每天 03:10"的误差不超过半分钟 ——
#: 维护窗口本来就允许分钟级误差，没必要为秒级精度做 cron 解析。
TICK_SECONDS: float = 30.0

#: 日任务的"错过宽限"（秒）。到点后这么久之内仍会补跑一次；
#: 超过就算错过，顺延明天。取 5 分钟：远比检查节拍宽（不会漏），
#: 又比"引擎晚启动一会儿就补跑一轮"窄（补跑维护任务收益极低）。
MISS_GRACE_SECONDS: float = 300.0


@dataclass
class MaintenanceJob:
    """一个维护任务的定义 + 运行态。

    :param schedule: ``daily``（每天 ``at`` 时刻）或 ``interval``（每 ``interval_sec`` 秒）
    :param at: ``daily`` 用的 ``HH:MM``（24 小时制）
    :param params: 直接透传给任务处理器（``retention_days`` / ``apply`` …）
    """

    name: str
    task_type: str
    label: str
    enabled: bool = False
    schedule: str = "daily"
    at: str = "03:10"
    interval_sec: int = 86400
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    # ---- 运行态（不落盘）----
    last_run_at: float = 0.0        # epoch 秒
    last_result: str = ""
    runs: int = 0
    errors: int = 0

    # ---------------- 周期 ----------------
    def next_run_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """下次执行时刻。**禁用 = 没有下次**（返回 None）。"""
        if not self.enabled:
            return None
        base = now or datetime.now()
        if self.schedule == "interval":
            step = max(60, int(self.interval_sec or 86400))
            if self.last_run_at:
                return datetime.fromtimestamp(self.last_run_at) + timedelta(seconds=step)
            return base
        # daily
        try:
            hh, mm = (self.at or "03:10").split(":")[:2]
            hour, minute = int(hh), int(mm)
        except (ValueError, AttributeError):
            hour, minute = 3, 10
        hour = min(max(hour, 0), 23)
        minute = min(max(minute, 0), 59)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # 今天的点已经跑过 → 明天
        if self.last_run_at and self.last_run_at >= target.timestamp():
            return target + timedelta(days=1)
        # 还没到 → 今天
        if target > base:
            return target
        # 刚过（一个宽限窗口内）→ **现在就跑**。
        # 这个分支不能省：检查是每 30s 一拍，"03:10" 实际会在
        # 03:10:15 附近被看到，若一律判成"过了就顺延明天"，
        # 日任务将永远触发不了。宽限之外才算真的错过，等明天。
        if (base - target).total_seconds() <= MISS_GRACE_SECONDS:
            return target
        return target + timedelta(days=1)

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        nxt = self.next_run_at(now)
        return nxt is not None and nxt <= (now or datetime.now())

    # ---------------- 序列化 ----------------
    def to_dict(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        nxt = self.next_run_at(now)
        return {
            "name": self.name,
            "task_type": self.task_type,
            "label": self.label,
            "enabled": self.enabled,
            "schedule": self.schedule,
            "at": self.at,
            "interval_sec": self.interval_sec,
            "params": dict(self.params),
            "description": self.description,
            "runs": self.runs,
            "errors": self.errors,
            "last_run_at": self.last_run_at,
            "last_run_iso": (
                datetime.fromtimestamp(self.last_run_at).isoformat(timespec="seconds")
                if self.last_run_at else ""
            ),
            "next_run_at": nxt.timestamp() if nxt else None,
            "next_run_iso": nxt.isoformat(timespec="seconds") if nxt else "",
            "last_result": self.last_result,
        }

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any], default: "MaintenanceJob") -> "MaintenanceJob":
        job = cls(
            name=name,
            task_type=str(raw.get("task_type") or default.task_type),
            label=str(raw.get("label") or default.label),
            enabled=bool(raw.get("enabled", default.enabled)),
            schedule=str(raw.get("schedule") or default.schedule).lower(),
            at=str(raw.get("at") or default.at),
            interval_sec=int(raw.get("interval_sec") or default.interval_sec),
            params=dict(raw.get("params") or default.params),
            description=str(raw.get("description") or default.description),
        )
        if job.schedule not in ("daily", "interval"):
            job.schedule = default.schedule
        return job


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
        schedule="interval",
        interval_sec=3600,
        params={},
        description="校验日志目录、数据目录与数据库可用性（只读，不删任何东西）",
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
                name: MaintenanceJob.from_dict(name, raw.get(name) or {}, default)
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
                        "schedule": j.schedule,
                        "at": j.at,
                        "interval_sec": j.interval_sec,
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
        }


__all__ = ["MaintenanceJob", "MaintenancePlanner", "DEFAULT_JOBS", "TICK_SECONDS"]
