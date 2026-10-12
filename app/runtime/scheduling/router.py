#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : router.py
# @Description : 能力路由：任务类型 → 队列 → 执行池
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
能力路由（Capability Routing）
=============================

调度器**不该**知道执行器的类名、函数名、住在哪个进程。
它只需要知道：

    哪种任务 → 进哪个队列 → 哪个池声明能干这种活

于是加一个新的执行器 = 注册一行能力声明，调度器一行都不用改。
这也是未来拆多进程时最省事的一处设计：队列名直接就是 MQ 的
routing key，池名就是 consumer group。

关于"竞争消费" vs "指定执行器"
------------------------------

默认是**竞争消费**：同一队列的所有执行者谁有空谁领。
只有在下面几种情况才需要精确指定：

  * 任务需要特定资源（GPU / 指定交易账户 / 独占的行情连接）；
  * 任务有数据权限约束（某些账户不能碰某些标的）；
  * 任务需要亲和性（同一标的的连续任务尽量落在同一 worker，复用缓存）。

这些用 :meth:`set_task_pool` 做定向覆盖即可，不必改变整体机制。
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set

from app.runtime.events.types import Pool, Queue, TASK_QUEUE


class CapabilityRouter:
    """任务类型 → 队列 → 执行池 的路由表。"""

    def __init__(self) -> None:
        #: 任务类型 → 队列（可被 :meth:`set_queue` 覆盖）
        self._task_queue: Dict[str, str] = dict(TASK_QUEUE)
        #: 队列 → 池
        self._queue_pool: Dict[str, str] = {
            queue: pool for pool, queue in Pool.POOL_QUEUE.items()
        }
        #: 池 → 能干的任务类型
        self._pool_caps: Dict[str, Set[str]] = {
            pool: set(caps) for pool, caps in Pool.DEFAULT_CAPABILITIES.items()
        }
        #: 任务类型 → 指定池（定向覆盖，绕过"队列 → 池"的默认映射）
        self._pool_override: Dict[str, str] = {}

    # ============================================================
    # 注册
    # ============================================================
    def register_pool(
        self,
        pool: str,
        capabilities: Iterable[str],
        queue: str = "",
    ) -> None:
        """注册一个执行池的能力。

        同名重复注册是**合并**而不是替换：池可以在不同文件里
        分批声明自己会干什么，装配顺序不该影响结果。
        """
        caps = self._pool_caps.setdefault(pool, set())
        caps.update(str(c) for c in capabilities)
        if queue:
            self._queue_pool[queue] = pool
            # 让该池声明的能力默认都流向这个队列
            for cap in caps:
                self._task_queue.setdefault(cap, queue)

    def set_queue(self, task_type: str, queue: str) -> None:
        self._task_queue[str(task_type)] = str(queue)

    def set_task_pool(self, task_type: str, pool: str) -> None:
        """把某种任务定向到某个池（精确匹配，用于独占资源场景）。"""
        self._pool_override[str(task_type)] = str(pool)

    def capabilities(self, pool: str) -> FrozenSet[str]:
        return frozenset(self._pool_caps.get(pool) or ())

    def pools(self) -> List[str]:
        return sorted(self._pool_caps.keys())

    # ============================================================
    # 路由
    # ============================================================
    def queue_for(self, task_type: str) -> str:
        return self._task_queue.get(task_type, Queue.DEFAULT)

    def pool_for(self, task_type: str) -> Optional[str]:
        """哪种池能干这个任务。找不到返回 None（**不抛**，由调用方决定后果）。"""
        override = self._pool_override.get(task_type)
        if override:
            return override
        pool = self._queue_pool.get(self.queue_for(task_type))
        if pool and self.can_handle(pool, task_type):
            return pool
        # 兜底：队列映射没配好时，直接找一个声明了此能力的池
        for name, caps in self._pool_caps.items():
            if task_type in caps:
                return name
        return None

    def can_handle(self, pool: str, task_type: str) -> bool:
        return task_type in (self._pool_caps.get(pool) or ())

    def pools_for_queue(self, queue: str) -> List[str]:
        """能消费这个队列的池（目前一队列一池，保留扩展空间）。"""
        return [p for p, q in self._queue_pool.items() if q == queue]

    def unroutable(self, task_type: str) -> bool:
        return self.pool_for(task_type) is None

    # ============================================================
    # 可观测
    # ============================================================
    def describe(self) -> List[Dict[str, Any]]:
        """路由全表。面板与文档都用它 —— 只有一份真源，不会两边说不一样的话。"""
        rows: List[Dict[str, Any]] = []
        for pool in self.pools():
            caps = sorted(self.capabilities(pool))
            rows.append({
                "pool": pool,
                "label": Pool.LABEL.get(pool, pool),
                "queue": Pool.POOL_QUEUE.get(pool, ""),
                "task_types": caps,
                "count": len(caps),
            })
        return rows

    def snapshot(self) -> Dict[str, Any]:
        return {
            "pools": self.describe(),
            "queue_of_task": dict(sorted(self._task_queue.items())),
            "pool_of_queue": dict(sorted(self._queue_pool.items())),
            "overrides": dict(sorted(self._pool_override.items())),
        }


__all__ = ["CapabilityRouter"]
