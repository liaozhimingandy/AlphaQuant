#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : models.py
# @Description : EngineTask —— 调度器交给执行器的**唯一**工作形态
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
任务是什么
==========

任务是"需要被完成的一件工作"，与事件（"已经发生的事实"）严格分开：

  * **事件不可执行**，任务可执行；
  * **事件可以被忽略**（没人订阅就没事发生），任务**必须有终态**
    （成功 / 失败 / 死信），否则没人知道这件事到底做没做；
  * **事件可以重复投递**（去重即可），任务重复执行会出事
    （重复下单、重复记账），所以任务要有幂等键。

字段设计上有三处是刻意的：

``dedup_key``
    业务唯一键。同一批 K 线到达两次，只该产生一次因子任务。
    没有它，"事件重投"就直接变成"因子重算 N 遍"。

``source_event_id`` / ``correlation_id``
    任务记着自己是从哪个事件来的。事后复盘"这单是怎么来的"时，
    靠这条链能把 事件 → 任务 → 事件 → 任务 整条路径还原出来。

``max_retries`` 默认按任务类型给，而不是全局一个值
    采集失败重试 3 次是合理的（网络抖动很常见）；
    下单失败重试 3 次是危险的（见 :data:`NON_RETRIABLE_TASK_TYPES`）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from app.runtime.events.types import (
    NON_RETRIABLE_TASK_TYPES,
    TASK_QUEUE,
    Queue,
)


class TaskState(str, Enum):
    """任务状态机。

    合法转移::

        PENDING → QUEUED → RUNNING → SUCCESS
                                  └→ FAILED → (RETRYING → QUEUED)
                                           └→ DEAD_LETTER
        PENDING → CANCELLED

    为什么要有独立的 ``QUEUED``：``PENDING`` 是"已创建、还没进队列"，
    ``QUEUED`` 是"已在队列里等着被领"。少了这一档，
    "任务建出来了但没人领"和"任务还没建"在监控上长得一样，
    而前者通常是队列/路由配错了，是必须能一眼看出来的故障。
    """

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return {
            "pending": "待入队",
            "queued": "排队中",
            "running": "执行中",
            "success": "成功",
            "failed": "失败",
            "retrying": "等待重试",
            "dead_letter": "死信",
            "cancelled": "已取消",
        }[self.value]

    @property
    def terminal(self) -> bool:
        """是否已到终态（不会再被调度）。"""
        return self in (
            TaskState.SUCCESS, TaskState.FAILED,
            TaskState.DEAD_LETTER, TaskState.CANCELLED,
        )


#: 优先级常量。**数字越小越先执行**（与 :class:`TaskPriority` 一致）。
class Priority:
    CRITICAL = 1   # 下单 / 撤单 / 风控拦截
    HIGH = 3       # 实盘行情采集
    NORMAL = 5     # 因子、新闻
    LOW = 7        # 回测、参数实验
    IDLE = 9       # 日志清理、归档


@dataclass
class EngineTask:
    """调度器产出的标准化任务。

    执行器**只认这个结构**：它不需要知道任务是谁建的、为什么建，
    只要按 ``task_type`` 找到处理器、执行 ``params`` 描述的动作即可。
    这正是"调度器与执行器解耦"的落点。
    """

    task_type: str
    params: Dict[str, Any] = field(default_factory=dict)

    # ---------------- 路由 ----------------
    queue: str = ""
    priority: int = Priority.NORMAL

    # ---------------- 溯源 ----------------
    source_event_id: str = ""
    source: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    #: 业务幂等键。非空时，同键的任务在**未终结**前不会被重复入队。
    dedup_key: str = ""

    # ---------------- 执行策略 ----------------
    timeout_seconds: float = 60.0
    max_retries: int = 0
    retry_interval: float = 5.0

    # ---------------- 身份与运行时 ----------------
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=datetime.now)
    status: TaskState = TaskState.PENDING
    attempts: int = 0
    result: Any = None
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    #: 由哪个执行池领走（排障用："这活卡在哪个池"）
    worker: str = ""
    last_error_at: Optional[datetime] = None

    # ---------------- 崩溃恢复 ----------------
    #: 是否被中断过（上次跑着的时候进程没了）。
    #: 记它是因为"这次执行结果未知"和"这次从没跑过"是两回事：
    #: 前者可能已经产生副作用（比如下单请求已发出），后者没有。
    interrupted: bool = False
    #: 人工从错误队列重投的次数。与 ``attempts`` 分开记 ——
    #: 自动重试有上限（重试不通就该报警），人工重投没有，
    #: 但"同一个任务被人重投了 7 次"本身就是该被看见的信号。
    manual_retries: int = 0
    #: 一次性任务（参数里带着 callable）：不落盘，也不参与恢复。
    #: 它本来就活不过重启（函数对象没法序列化），
    #: 硬记只会得到一条恢复后必然失败的记录。
    ephemeral: bool = False

    def __post_init__(self) -> None:
        if not self.task_type:
            raise ValueError("task_type 不能为空")
        # 队列没指定时按任务类型查默认路由表
        if not self.queue:
            self.queue = TASK_QUEUE.get(self.task_type, Queue.DEFAULT)
        # 下单类任务默认不可自动重试 —— 见 NON_RETRIABLE_TASK_TYPES 的说明
        if self.task_type in NON_RETRIABLE_TASK_TYPES and self.max_retries > 0:
            self.max_retries = 0
        try:
            self.priority = int(self.priority)
        except (TypeError, ValueError):
            self.priority = Priority.NORMAL

    # ---------------- 状态转移 ----------------
    def mark_queued(self) -> None:
        self.status = TaskState.QUEUED

    def mark_running(self, worker: str = "") -> None:
        self.status = TaskState.RUNNING
        self.worker = worker
        self.started_at = datetime.now()
        self.attempts += 1

    def mark_success(self, result: Any = None) -> None:
        self.status = TaskState.SUCCESS
        self.result = result
        self.finished_at = datetime.now()

    def mark_failed(self, error: Any = "") -> None:
        self.status = TaskState.FAILED
        self.error = str(error)
        self.last_error_at = datetime.now()
        self.finished_at = datetime.now()

    def mark_retrying(self) -> None:
        self.status = TaskState.RETRYING

    def mark_dead_letter(self, error: Any = "") -> None:
        self.status = TaskState.DEAD_LETTER
        if error:
            self.error = str(error)
        self.finished_at = datetime.now()

    def mark_cancelled(self) -> None:
        self.status = TaskState.CANCELLED
        self.finished_at = datetime.now()

    def mark_interrupted(self) -> None:
        """标成"跑着跑着进程没了"。

        ``attempts`` **不回滚**：那一次可能真的执行了（下单请求可能
        已经发出），把它当成没跑过就会重复下单。所以它算一次尝试，
        只是结果未知。``attempts`` 也不自增 —— 自增发生在
        :meth:`mark_running`，重跑时会自然 +1。
        """
        self.interrupted = True
        self.status = TaskState.QUEUED
        self.started_at = None
        self.finished_at = None
        self.worker = ""

    def reset_for_manual_retry(self) -> None:
        """人工重投：把任务退回"可以再跑一次"的状态。

        与自动重试的区别是**清空 ``attempts``**：自动重试的额度是给
        "暂时性故障"的（网络抖一下），用完了说明这条路走不通；
        而人工点"继续"意味着人已经看过原因（比如补了数据、改了配置），
        那是新的一次机会，不该带着上次的欠账上路。

        ``error`` 保留不清：面板上"上次失败原因"是有排障价值的，
        而任务重跑成功后它会被覆盖成空。
        """
        self.status = TaskState.PENDING
        self.attempts = 0
        self.manual_retries += 1
        self.interrupted = False
        self.started_at = None
        self.finished_at = None
        self.worker = ""

    # ---------------- 查询 ----------------
    @property
    def is_terminal(self) -> bool:
        return self.status.terminal

    def can_retry(self) -> bool:
        """还能不能重试。**下单类任务永远返回 False**。

        ``max_retries`` 的语义是"最多**额外**执行几次"，不是"最多执行几次"：
        ``max_retries=1`` 表示"失败后再试一次"，总共执行 2 次。
        ``attempts`` 记的是已执行次数，所以判定用 ``<=``。
        用 ``<`` 会让 ``max_retries=1`` 实际等于不重试 ——
        配了重试却从不重试，是最容易骗过 review 的一类偏差。
        """
        if self.task_type in NON_RETRIABLE_TASK_TYPES:
            return False
        return self.attempts <= self.max_retries

    def next_retry_delay(self) -> float:
        """指数退避 + 抖动。

        抖动不是可选项：没有抖动时，一批同时失败的任务会在
        ``retry_interval`` 之后**同时**重试，形成周期性的尖峰，
        把刚刚恢复的数据源再次打垮（ thundering herd ）。
        """
        import random

        base = max(0.1, float(self.retry_interval))
        delay = base * (2 ** max(0, self.attempts - 1))
        return delay * (0.8 + 0.4 * random.random())

    def elapsed(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now()
        return max(0.0, (end - self.started_at).total_seconds())

    # ---------------- 排序 ----------------
    @property
    def sort_key(self) -> tuple:
        """(优先级, 创建时间)：同级先来先服务。"""
        return (self.priority, self.created_at)

    def __lt__(self, other: "EngineTask") -> bool:
        return self.sort_key < other.sort_key

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "queue": self.queue,
            "priority": self.priority,
            "params": dict(self.params or {}),
            "status": self.status.value,
            "status_label": self.status.label,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "manual_retries": self.manual_retries,
            "interrupted": self.interrupted,
            "ephemeral": self.ephemeral,
            "dedup_key": self.dedup_key,
            "source_event_id": self.source_event_id,
            "source": self.source,
            "correlation_id": self.correlation_id,
            "worker": self.worker,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed": self.elapsed(),
            "error": self.error or None,
        }


__all__ = ["EngineTask", "TaskState", "Priority"]
