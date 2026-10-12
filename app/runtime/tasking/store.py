#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : store.py
# @Description : TaskStore —— 按队列存放待执行任务，管状态机、幂等与死信
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么需要"任务队列"而不是直接调函数
====================================

直接调用（``service.do_something()``）在单线程里能工作，
但它缺少四样东西，而这四样在 7×24 的实盘系统里每样都会咬人：

  1. **背压**：下游慢了，调用方还在猛塞，最后一起超时；
  2. **优先级**：回测和撤单抢同一批线程，撤单排在回测后面；
  3. **重试**：失败没有统一策略，于是每处调用自己写 while 重试，各不相同；
  4. **可见性**："现在卡在哪一步"这个问题没有人能回答。

本文件实现的是**逻辑队列 + 进程内存储**。它刻意不引入 Redis / MQ：
初期单实例不需要，而且内存态的失败模式更容易被测试覆盖。
队列名（``market`` / ``research`` / …）保持与未来分布式实现的
routing key 一致，将来换后端时路由表不用改。
"""
from __future__ import annotations

import heapq
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from app.common.logging import logger
from app.runtime.events.types import Queue
from app.runtime.tasking.models import EngineTask, TaskState


class TaskStore:
    """任务存储与状态机。

    并发模型：所有状态变更在锁内完成，队列用堆按 (优先级, 创建时间) 出队。
    堆里可能残留已取消/已终结的任务（取消不便从堆中间删除），
    因此出队时会**惰性丢弃**它们 —— 这比遍历整个堆删除便宜得多。
    """

    def __init__(self, history_size: int = 300, max_tasks: int = 5000) -> None:
        self._lock = threading.RLock()
        self._queues: "Dict[str, List[EngineTask]]" = {q: [] for q in Queue.ALL}
        self._tasks: "OrderedDict[str, EngineTask]" = OrderedDict()
        self._running: "Dict[str, EngineTask]" = {}
        #: 幂等键 → 任务 ID。仅在任务**未终结**期间有效，
        #: 终结后释放，于是"同一批 K 线第二天再来"仍能触发新任务。
        self._dedup: "Dict[str, str]" = {}
        self._history_size = max(0, int(history_size))
        self._max_tasks = max(1, int(max_tasks))
        self.stats: Dict[str, int] = {
            "created": 0,
            "enqueued": 0,
            "dedup_dropped": 0,
            "dispatched": 0,
            "success": 0,
            "failed": 0,
            "retried": 0,
            "dead_letter": 0,
            "cancelled": 0,
        }

    # ============================================================
    # 入队
    # ============================================================
    def enqueue(self, task: EngineTask) -> Tuple[bool, str]:
        """把一个任务放进它的队列。

        :return: ``(是否入队, 原因)``。被幂等键挡下时返回 False ——
                 这不是错误，而是"这件事已经有人在做/已经做完了"。
        """
        if task.is_terminal:
            return False, f"任务已处于终态 {task.status.value}"

        with self._lock:
            if task.dedup_key:
                owner = self._dedup.get(task.dedup_key)
                if owner and owner != task.task_id:
                    existing = self._tasks.get(owner)
                    if existing is not None and not existing.is_terminal:
                        self.stats["dedup_dropped"] += 1
                        return False, (
                            f"幂等键 {task.dedup_key} 已有在途任务 "
                            f"{owner[:8]}（{existing.status.label}）"
                        )
                    # 旧任务已终结 → 释放键位，让新任务接上
                    self._dedup.pop(task.dedup_key, None)

            self._track(task)
            task.mark_queued()
            heapq.heappush(self._queues.setdefault(task.queue, []), task)
            if task.dedup_key:
                self._dedup[task.dedup_key] = task.task_id
            self.stats["enqueued"] += 1
            return True, "已入队"

    def create(self, task: EngineTask) -> EngineTask:
        """只登记不入队（供调用方先拿到 task_id 再决定时机）。"""
        with self._lock:
            self._track(task)
        return task

    def _track(self, task: EngineTask) -> None:
        self._tasks[task.task_id] = task
        self.stats["created"] += 1
        # 有界存储：长跑服务不能因为"记着做过什么"而无限吃内存。
        # 淘汰只删**终态**任务，在途任务永远保留。
        while len(self._tasks) > self._max_tasks:
            victim_id = None
            for tid, t in self._tasks.items():
                if t.is_terminal and tid not in self._running:
                    victim_id = tid
                    break
            if victim_id is None:
                break
            self._tasks.pop(victim_id, None)

    # ============================================================
    # 出队 / 领取
    # ============================================================
    def pop(self, queue: str) -> Optional[EngineTask]:
        """从指定队列弹出一个可执行的任务（优先级最高者）。"""
        with self._lock:
            heap = self._queues.get(queue)
            if not heap:
                return None
            while heap:
                task = heapq.heappop(heap)
                if task.is_terminal or task.status is TaskState.RETRYING:
                    continue  # 惰性丢弃：取消/终结/等待重试的不该被领走
                self.stats["dispatched"] += 1
                return task
            return None

    def qsize(self, queue: str) -> int:
        with self._lock:
            return len(self._queues.get(queue) or [])

    def pending_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._queues.values())

    def running_count(self, pool: str = "") -> int:
        with self._lock:
            if not pool:
                return len(self._running)
            return sum(1 for t in self._running.values() if t.worker == pool)

    def queues(self) -> "Dict[str, int]":
        with self._lock:
            return {q: len(v) for q, v in self._queues.items()}

    # ============================================================
    # 状态转移
    # ============================================================
    def mark_running(self, task: EngineTask, worker: str = "") -> None:
        with self._lock:
            task.mark_running(worker)
            self._running[task.task_id] = task

    def mark_success(self, task: EngineTask, result: Any = None) -> None:
        with self._lock:
            task.mark_success(result)
            self._running.pop(task.task_id, None)
            self._release_dedup(task)
            self.stats["success"] += 1

    def mark_failure(self, task: EngineTask, error: Any = "") -> str:
        """标记失败，并**决定下一步**：重试、还是进死信。

        :return: ``"retry"`` / ``"dead_letter"``
        """
        with self._lock:
            self._running.pop(task.task_id, None)
            task.mark_failed(error)
            if task.can_retry():
                task.mark_retrying()
                self.stats["retried"] += 1
                return "retry"
            task.mark_dead_letter(error)
            self._release_dedup(task)
            self.stats["dead_letter"] += 1
            return "dead_letter"

    def dead_letter(self, task: EngineTask, error: Any = "") -> None:
        """直接进死信（**不重试**）。

        用于"重试也没用"的失败：没有注册处理器、没有池能处理、
        参数非法。这类问题重试一百次结果一样，只会把真正的故障
        淹没在重试日志里。
        """
        with self._lock:
            self._running.pop(task.task_id, None)
            task.mark_dead_letter(error)
            self._release_dedup(task)
            self.stats["dead_letter"] += 1

    def requeue(self, task: EngineTask) -> bool:
        """把等待重试的任务放回队列。"""
        with self._lock:
            if task.is_terminal:
                return False
            task.mark_queued()
            heapq.heappush(self._queues.setdefault(task.queue, []), task)
            return True

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.is_terminal:
                return False
            task.mark_cancelled()
            self._running.pop(task_id, None)
            self._release_dedup(task)
            self.stats["cancelled"] += 1
            return True

    def _release_dedup(self, task: EngineTask) -> None:
        if task.dedup_key and self._dedup.get(task.dedup_key) == task.task_id:
            self._dedup.pop(task.dedup_key, None)

    # ============================================================
    # 查询 / 可观测
    # ============================================================
    def get(self, task_id: str) -> Optional[EngineTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def running(self, limit: int = 50) -> "List[Dict[str, Any]]":
        with self._lock:
            return [t.to_dict() for t in list(self._running.values())[:limit]]

    def pending(self, queue: str = "", limit: int = 50) -> "List[Dict[str, Any]]":
        with self._lock:
            if queue:
                items = sorted(self._queues.get(queue) or [])
            else:
                items = sorted(t for q in self._queues.values() for t in q)
            return [t.to_dict() for t in items[:limit]]

    def recent(self, limit: int = 30) -> "List[Dict[str, Any]]":
        with self._lock:
            items = list(self._tasks.values())[-max(0, int(limit)):]
            return [t.to_dict() for t in items]

    def dead_letters(self, limit: int = 30) -> "List[Dict[str, Any]]":
        with self._lock:
            items = [
                t for t in self._tasks.values()
                if t.status is TaskState.DEAD_LETTER
            ]
            return [t.to_dict() for t in items[-max(0, int(limit)):]]

    def pending_tasks(self) -> "List[EngineTask]":
        """待执行的任务**对象**（区别于 :meth:`pending` 返回的字典）。"""
        with self._lock:
            return sorted(t for q in self._queues.values() for t in q)

    def non_terminal(self) -> "List[EngineTask]":
        """所有**未终结**的任务（恢复扫描用）。

        引擎重启后："哪些活上次没干完"这个问题的答案就在这里。
        注意它包含 QUEUED 与 RETRYING —— 这两类在内存态下重启即丢失，
        正是最需要被重新入队的那一批。
        """
        with self._lock:
            return [t for t in self._tasks.values() if not t.is_terminal]

    def by_correlation(self, correlation_id: str) -> "List[Dict[str, Any]]":
        """按业务流程 ID 查整条链路上的任务（排障主入口）。"""
        with self._lock:
            return [
                t.to_dict() for t in self._tasks.values()
                if t.correlation_id == correlation_id
            ]

    def is_idle(self) -> bool:
        with self._lock:
            return self.pending_count() == 0 and len(self._running) == 0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "stats": dict(self.stats),
                "queues": self.queues(),
                "running": len(self._running),
                "tracked": len(self._tasks),
                "dedup_keys": len(self._dedup),
                "idle": self.is_idle(),
            }


__all__ = ["TaskStore"]
