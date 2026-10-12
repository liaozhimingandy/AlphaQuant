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
import os
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.common.logging import logger
from app.runtime.events.types import NON_RETRIABLE_TASK_TYPES, Queue
from app.runtime.tasking.models import EngineTask, TaskState


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class TaskStore:
    """任务存储与状态机。

    并发模型：所有状态变更在锁内完成，队列用堆按 (优先级, 创建时间) 出队。
    堆里可能残留已取消/已终结的任务（取消不便从堆中间删除），
    因此出队时会**惰性丢弃**它们 —— 这比遍历整个堆删除便宜得多。

    持久化由 :class:`~app.runtime.tasking.journal.TaskJournal` 承担：
    内存里的一切都是"当前状态"，盘上的才是"能活过重启的东西"。
    """

    def __init__(
        self,
        history_size: int = 300,
        max_tasks: int = 5000,
        persist: Optional[bool] = None,
        journal: Any = None,
        journal_path: str = "",
    ) -> None:
        self._lock = threading.RLock()
        self._queues: "Dict[str, List[EngineTask]]" = {q: [] for q in Queue.ALL}
        self._tasks: "OrderedDict[str, EngineTask]" = OrderedDict()
        self._running: "Dict[str, EngineTask]" = {}
        #: 幂等键 → 任务 ID。仅在任务**未终结**期间有效，
        #: 终结后释放，于是"同一批 K 线第二天再来"仍能触发新任务。
        self._dedup: "Dict[str, str]" = {}
        self._history_size = max(0, int(history_size))
        self._max_tasks = max(1, int(max_tasks))

        # ---- 落盘 ----
        # 默认开启：任务不落盘，一次重启就让"该做的事"集体消失，
        # 而且消失得毫无痕迹 —— 这是最难查的一类故障。
        # 关掉它只应该有一个理由：测试不想污染真实输出目录。
        self.persist = _env_bool("AQ_TASK_PERSIST", True) if persist is None else persist
        self.journal = journal
        if self.journal is None and self.persist:
            try:
                from app.runtime.tasking.journal import TaskJournal

                self.journal = TaskJournal(path=journal_path)
            except Exception as exc:  # pragma: no cover - 导入失败即降级
                logger.error(f"任务流水账不可用，降级为纯内存: {exc}")
                self.journal = None
                self.persist = False

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
            # 恢复相关
            "recovered": 0,
            "recovered_interrupted": 0,
            "recovered_expired": 0,
            "manual_retry": 0,
            "manual_discard": 0,
        }

    # ============================================================
    # 落盘
    # ============================================================
    def _persist(self, task: EngineTask, immediate: bool = False) -> None:
        """把一次状态变更记进流水账。"""
        if self.journal is None:
            return
        try:
            self.journal.write(task, immediate=immediate)
        except Exception as exc:  # pragma: no cover - 落盘失败不能拖垮调度
            logger.debug(f"任务 {task.task_id[:8]} 落盘失败（已忽略）: {exc}")

    def flush(self) -> int:
        """把积压的变更全部写盘（引擎停止、人工操作后调用）。"""
        if self.journal is None:
            return 0
        try:
            return self.journal.flush()
        except Exception as exc:
            logger.error(f"任务流水账强制刷盘失败: {exc}")
            return 0

    def close(self) -> None:
        if self.journal is None:
            return
        try:
            self.flush()
            self.journal.close()
        except Exception as exc:  # pragma: no cover
            logger.debug(f"关闭任务流水账异常（已忽略）: {exc}")

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
            # 立即落盘：任务"建出来了却从没执行"是最坏的一种失败，
            # 因为它不留痕迹 —— 没人会去查一件没发生过的事。
            self._persist(task, immediate=True)
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
            # 终态任务已经"划掉"，盘上那行也就没有恢复价值了
            if self.journal is not None:
                try:
                    self.journal.delete(victim_id)
                except Exception:  # pragma: no cover
                    pass

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
            # RUNNING 必须落盘：它下一次被更新可能是"进程被杀"。
            # 没这条记录，恢复时就不知道有活干了一半。
            self._persist(task, immediate=True)

    def mark_success(self, task: EngineTask, result: Any = None) -> None:
        with self._lock:
            task.mark_success(result)
            self._running.pop(task.task_id, None)
            self._release_dedup(task)
            self.stats["success"] += 1
            self._persist(task, immediate=True)

    def mark_failure(self, task: EngineTask, error: Any = "") -> str:
        """标记失败，并**决定下一步**：重试、还是进死信。

        :return: ``"retry"`` / ``"dead_letter"``
        """
        with self._lock:
            self._running.pop(task.task_id, None)
            task.mark_failed(error)
            # "失败"必须无条件累加：重试和进死信是**失败之后**的去向，
            # 不是"没失败"。少了这一句，面板上的失败数永远是 0，
            # 而失败恰恰是最该被看见的那个数。
            self.stats["failed"] += 1
            if task.can_retry():
                task.mark_retrying()
                self.stats["retried"] += 1
                # 等待重试也是"未完成的活"，重启后要接着等
                self._persist(task, immediate=True)
                return "retry"
            task.mark_dead_letter(error)
            self._release_dedup(task)
            # 任务可能还没被领走过（比如直接 dead_letter 的场景），
            # 那它就还在堆里 —— 不摘掉，"队列积压 N"会把一条已经
            # 判定失败的任务一直算进去。
            self._drop_from_queue(task)
            self.stats["dead_letter"] += 1
            self._persist(task, immediate=True)
            if self.journal is not None:
                try:
                    self.journal.log_dead(task)
                except Exception:  # pragma: no cover
                    pass
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
            self._drop_from_queue(task)   # 同 mark_failure：别让死信占着积压数
            self.stats["dead_letter"] += 1
            self._persist(task, immediate=True)
            if self.journal is not None:
                try:
                    self.journal.log_dead(task)
                except Exception:  # pragma: no cover
                    pass

    def requeue(self, task: EngineTask) -> bool:
        """把等待重试的任务放回队列。"""
        with self._lock:
            if task.is_terminal:
                return False
            task.mark_queued()
            heapq.heappush(self._queues.setdefault(task.queue, []), task)
            self._persist(task)
            return True

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.is_terminal:
                return False
            task.mark_cancelled()
            self._running.pop(task_id, None)
            self._drop_from_queue(task)
            self._release_dedup(task)
            self.stats["cancelled"] += 1
            self._persist(task, immediate=True)
            return True

    def _drop_from_queue(self, task: EngineTask) -> None:
        """把任务从它的队列里摘掉。

        :meth:`pop` 会惰性丢弃终态任务，所以**不摘也不会执行错**；
        但"队列积压 N 条"这个数会一直把已取消的算进去，
        面板上就出现"积压 3 条但其实没事可做"。取消必须真的出队。
        """
        heap = self._queues.get(task.queue)
        if not heap:
            return
        kept = [t for t in heap if t.task_id != task.task_id]
        if len(kept) == len(heap):
            return
        kept.sort()
        self._queues[task.queue] = kept

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

    def running_tasks(self) -> "List[EngineTask]":
        """在跑的任务**对象**。停机落盘用：这些是"干了一半"的活。"""
        with self._lock:
            return list(self._running.values())

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
        """错误队列。**顺序是倒序（最新在前）**：这是给人看的待办清单，
        最新的最可能需要立刻处理。

        每条都带 ``actionable``：面板据此决定"继续"按钮能不能点。
        下单类任务点了会重复下单，按钮必须置灰并说明原因，
        而不是让人点了才报错。
        """
        with self._lock:
            items = [
                t for t in self._tasks.values()
                if t.status is TaskState.DEAD_LETTER
            ]
            return [
                dict(t.to_dict(), actionable=self.is_actionable(t))
                for t in reversed(items[-max(0, int(limit)):])
            ]

    def dead_letter_count(self) -> int:
        """当前**尚未处理**的死信条数。

        与 ``stats["dead_letter"]`` 的区别：后者是"历史上共进过多少死信"
        的累计计数，清空死信不该把它归零（那是发生过的事实）；
        而健康判定要看的是"现在还有几条没处理"，所以用这个方法。
        """
        with self._lock:
            return sum(
                1 for t in self._tasks.values() if t.status is TaskState.DEAD_LETTER
            )

    def clear_dead_letters(self, resolution: str = "discard") -> int:
        """清空死信队列。

        死信是**给运维看的待办清单**，不是账本：处理完就该清掉，
        否则它会一直挂着，面板上的"死信 N 条"永远退不回去，
        健康判定也跟着一直是"有死信"，告警就失去意义了。

        :param resolution: 记进死信处置历史的处置方式
                           （``discard`` = 认了，不重跑）
        :return: 清掉的条数
        """
        with self._lock:
            dead = [
                tid for tid, t in self._tasks.items()
                if t.status is TaskState.DEAD_LETTER
            ]
            for tid in dead:
                self._tasks.pop(tid, None)
                if self.journal is not None:
                    try:
                        self.journal.resolve_dead(tid, resolution)
                        self.journal.delete(tid)
                    except Exception:  # pragma: no cover
                        pass
            if dead:
                self.stats["manual_discard"] += len(dead)
            return len(dead)

    # ============================================================
    # 错误队列：人工干预
    # ============================================================
    def is_actionable(self, task: EngineTask) -> bool:
        """这条死信还能不能点"继续"。

        不可重投的情形只有一种：它是**不可重试任务**（下单）。
        自动重试会重复下单，人工点继续同样会 ——
        这类任务必须让人去看订单状态再决定，而不是一键重投。
        """
        return bool(task.task_type not in NON_RETRIABLE_TASK_TYPES)

    def retry_dead(self, task_id: str) -> "Tuple[bool, str]":
        """把一条死信**重新投回队列**（人工点"继续"）。

        与自动重试的区别见 :meth:`EngineTask.reset_for_manual_retry`：
        尝试次数清零，因为这是新的一次机会，不是上次失败的延续。

        :return: ``(是否成功, 原因)``
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False, "任务不存在（可能已被清理）"
            if task.status is not TaskState.DEAD_LETTER:
                return False, f"任务当前状态 {task.status.label}，不是死信"
            if not self.is_actionable(task):
                return (
                    False,
                    f"{task.task_type} 属于不可自动重试任务，重投可能导致重复执行"
                    f"（请先核对业务状态，再手工处理）",
                )
            if task.dedup_key and self._dedup.get(task.dedup_key) not in (None, task_id):
                return False, f"幂等键 {task.dedup_key} 已被其它在途任务占用"

            task.reset_for_manual_retry()
            task.mark_queued()   # 已经真的在队列里了，状态必须跟上
            heapq.heappush(self._queues.setdefault(task.queue, []), task)
            if task.dedup_key:
                self._dedup[task.dedup_key] = task.task_id
            self.stats["manual_retry"] += 1
            self._persist(task, immediate=True)
            if self.journal is not None:
                try:
                    self.journal.resolve_dead(task_id, "retry")
                except Exception:  # pragma: no cover
                    pass
            return True, "已重新入队"

    def discard_dead(self, task_id: str) -> "Tuple[bool, str]":
        """认掉一条死信：**不重跑**，只是从待办清单里划掉。

        和"重投"并列存在的理由：不是每条死信都该重跑。
        "三天前的因子计算"重跑毫无意义，"昨天的下单"重跑很危险。
        让人能明确地说"这条我不处理了"，清单才不会永远堆着。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False, "任务不存在（可能已被清理）"
            if task.status is not TaskState.DEAD_LETTER:
                return False, f"任务当前状态 {task.status.label}，不是死信"
            self._tasks.pop(task_id, None)
            self._drop_from_queue(task)
            self.stats["manual_discard"] += 1
            if self.journal is not None:
                try:
                    self.journal.resolve_dead(task_id, "discard")
                    self.journal.delete(task_id)
                except Exception:  # pragma: no cover
                    pass
            return True, "已标记为已处理"

    def retry_all_dead(self, task_type: str = "") -> "Tuple[int, int]":
        """批量重投死信。

        :param task_type: 只重投这种类型；空表示全部（不可重投类型会跳过）
        :return: ``(重投成功数, 跳过数)``
        """
        with self._lock:
            targets = [
                t for t in self._tasks.values()
                if t.status is TaskState.DEAD_LETTER
                and (not task_type or t.task_type == task_type)
            ]
        done = skipped = 0
        for t in targets:
            ok, _reason = self.retry_dead(t.task_id)
            if ok:
                done += 1
            else:
                skipped += 1
        return done, skipped

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

    # ============================================================
    # 崩溃恢复
    # ============================================================
    def recover(
        self,
        max_age_hours: float = 72.0,
        limit: int = 500,
        restore_dead: bool = True,
    ) -> "Dict[str, Any]":
        """从流水账把"上次没干完的活"读回来。

        恢复什么、不恢复什么，这里的判断比"读出来"更重要：

        * ``running`` → 标成**中断过**再入队。那次可能真的执行了
          （下单请求可能已发出），所以尝试次数不回滚，只是结果未知。
        * 超龄（默认 72 小时）→ 直接进死信。三天前没干完的活，
          今天再干通常没意义（行情变了、上下文没了），
          让它安静地躺在错误队列里等人看一眼，好过悄悄重跑一遍。
        * 重试额度已用尽 → 进死信。这不是"上次失败了"，
          是"这条路已经走过上限次数"，再走一次不会有不同结果。
        * 死信 → 读回内存但**不入队**。错误队列必须跨重启存在，
          否则"重启一下死信就没了"等于给了掩盖问题的最佳手段。

        :return: 恢复报告（面板与日志共用）
        """
        report: Dict[str, Any] = {
            "ok": False,
            "reason": "",
            "loaded": 0,
            "queued": 0,
            "interrupted": 0,
            "expired": 0,
            "exhausted": 0,
            "dead_restored": 0,
            "skipped": 0,
            "items": [],
        }
        if self.journal is None:
            report["reason"] = "未启用任务持久化，无从恢复"
            return report

        from app.runtime.tasking.journal import task_from_row

        try:
            rows = self.journal.load_open(limit=limit)
        except Exception as exc:
            report["reason"] = f"读取流水账失败: {exc}"
            logger.error(report["reason"])
            return report

        report["ok"] = True
        report["loaded"] = len(rows)
        cutoff = datetime.now() - timedelta(hours=max(0.0, float(max_age_hours)))

        for row in rows:
            try:
                task = task_from_row(row, EngineTask)
            except Exception as exc:
                report["skipped"] += 1
                logger.warning(f"任务 {str(row.get('task_id'))[:8]} 恢复失败，跳过: {exc}")
                continue
            if not task.task_type or not task.task_id:
                report["skipped"] += 1
                continue

            item = {"task_id": task.task_id, "task_type": task.task_type}
            # 顺序有讲究：**中断优先于"重试耗尽"**。
            # 一个 max_retries=0 的任务被中断时 attempts 已经是 1，
            # 若先按"额度用尽"判，它会被直接丢进死信 ——
            # 而它根本没有失败过，只是进程没了。中断必须能再跑一次。
            if task.created_at < cutoff:
                task.mark_dead_letter(
                    f"任务滞留超过 {max_age_hours:.0f} 小时未完成，"
                    f"重启后不再自动重跑（创建于 {task.created_at:%Y-%m-%d %H:%M}）"
                )
                self._track(task)
                self._persist(task, immediate=True)
                if self.journal is not None:
                    try:
                        self.journal.log_dead(task)
                    except Exception:
                        pass
                report["expired"] += 1
                item["action"] = "expired"
            elif task.interrupted or task.status is TaskState.RUNNING:
                # 上次是"正在执行"时进程没了（或停机时已标成中断）——
                # 唯一能确定的是结果未知，所以一律重新入队。
                if task.status is TaskState.RUNNING:
                    task.mark_interrupted()
                self._track(task)
                heapq.heappush(self._queues.setdefault(task.queue, []), task)
                if task.dedup_key:
                    self._dedup[task.dedup_key] = task.task_id
                self._persist(task, immediate=True)
                report["interrupted"] += 1
                report["queued"] += 1
                item["action"] = "interrupted"
            elif not task.can_retry() and task.attempts > 0:
                task.mark_dead_letter(
                    f"重启前已用尽重试额度（{task.attempts}/{task.max_retries} 次）"
                )
                self._track(task)
                self._persist(task, immediate=True)
                if self.journal is not None:
                    try:
                        self.journal.log_dead(task)
                    except Exception:
                        pass
                report["exhausted"] += 1
                item["action"] = "exhausted"
            else:
                self._track(task)
                task.mark_queued()
                heapq.heappush(self._queues.setdefault(task.queue, []), task)
                if task.dedup_key:
                    self._dedup[task.dedup_key] = task.task_id
                self._persist(task, immediate=True)
                report["queued"] += 1
                item["action"] = "queued"
                # 停机时已标成"中断"的任务，落盘时状态是 queued，
                # 但它的 interrupted 标记还在 —— 不在这里补记一次，
                # 报告就会说"恢复 3 条，其中被中断 0 条"，
                # 而实际上这 3 条全是中断过的。
                if task.interrupted:
                    report["interrupted"] += 1
            report["items"].append(item)

        if restore_dead:
            try:
                for row in self.journal.load_dead(limit=limit):
                    try:
                        dead = task_from_row(row, EngineTask)
                    except Exception:
                        report["skipped"] += 1
                        continue
                    if not dead.task_id or dead.task_id in self._tasks:
                        continue
                    self._tasks[dead.task_id] = dead
                    report["dead_restored"] += 1
            except Exception as exc:
                logger.warning(f"恢复死信列表失败: {exc}")

        self.stats["recovered"] += report["queued"]
        self.stats["recovered_interrupted"] += report["interrupted"]
        self.stats["recovered_expired"] += report["expired"]
        return report

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
            snap = {
                "stats": dict(self.stats),
                "queues": self.queues(),
                "running": len(self._running),
                "tracked": len(self._tasks),
                "dedup_keys": len(self._dedup),
                "idle": self.is_idle(),
                "persist": bool(self.persist),
            }
        if self.journal is not None:
            try:
                snap["journal"] = self.journal.counts()
            except Exception as exc:
                snap["journal"] = {"error": str(exc)}
        return snap


__all__ = ["TaskStore"]
