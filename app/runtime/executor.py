#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : worker.py
# @Description : 工作单元：引擎里有上限的后台作业执行器
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

为什么要有工作单元
==================

重构前，"用户点了立即采集 / 立即体检 / 立即回测"这类一次性作业
是直接 ``deferToThread`` 丢出去的 —— **没有上限**。

这在纸面上没问题，直到运维在面板上连点五次"立即体检"：
五个体检同时跑，每个都在联网补全，数据源直接限流，
于是五次全部超时，而面板上只看到"体检失败"。
更糟的是这时候定时采集还在跑，一起被限流带走。

``WorkerComponent`` 就是给这类作业一个**有上限的槽位**：
  * 有空槽就接，没空槽就明确拒绝（而不是排队堆积到失控）；
  * 拒绝是**显式**的：调用方拿得到"现在忙"而不是"不知道跑哪去了"；
  * 引擎的空闲判定会算上它，于是"还有作业在跑"时引擎不会自动退出。

它和策略组件的区别
------------------

策略是"到点就该做决定"（被行情驱动），
工作单元是"有活就干，干完继续等"（被调用驱动）。
它没有自己的节拍，节拍来自外部。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from twisted.internet import defer, threads

from app.engine.capability import WorkerComponent as WorkerBase
from app.common.logging import logger


class WorkerComponent(WorkerBase):
    """通用后台作业执行器。

    用法::

        d = worker.submit("补采 000001", collector.collect_now, "000001")

    :meth:`submit` 返回 Deferred；槽位占满时返回 **已失败的** Deferred，
    errback 里是 ``RuntimeError``。这样调用方可以用统一的
    ``addCallbacks`` 处理，不需要先 ``if worker.has_slot()``。
    """

    name = "worker"

    #: 并发槽位。默认 2：既能让两个不同性质的作业并行（采集 + 体检），
    #: 又不会让同源作业把自己打到限流。
    capacity: int = 2

    def __init__(self, capacity: Optional[int] = None) -> None:
        super().__init__()
        if capacity is not None:
            self.capacity = int(capacity)
        self._history: List[Dict[str, Any]] = []
        self._HISTORY_MAX = 50
        self.stats: Dict[str, int] = {
            "submitted": 0, "done": 0, "failed": 0, "rejected": 0,
        }

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        if cfg.get("capacity") is not None:
            self.capacity = int(cfg["capacity"])
        logger.info(f"工作单元就绪 | 并发槽位: {self.capacity}")

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        if self._inflight:
            logger.warning(
                f"工作单元停止时仍有 {self._inflight} 个作业在跑（不等待，"
                f"结果可能丢失）"
            )
        return defer.succeed(None)

    # ============================================================
    # 领活 / 干活
    # ============================================================
    def claim(self, job: Any) -> bool:
        """``WorkerComponent`` 的抽象钩子。

        接受的作业形态有两种::

            ("补采 000001", fn, (arg,), {"kw": 1})   # 元组
            {"name": ..., "fn": ..., "args": ..., "kwargs": ...}

        与 :meth:`submit` 的区别：**不返回 Deferred**，
        结果只进历史记录。适合"触发就行、不关心返回值"的场合。
        """
        if isinstance(job, dict):
            name = str(job.get("name") or "job")
            fn = job.get("fn")
            args = tuple(job.get("args") or ())
            kwargs = dict(job.get("kwargs") or {})
        elif isinstance(job, (tuple, list)) and len(job) >= 2:
            name, fn = str(job[0]), job[1]
            args = tuple(job[2]) if len(job) > 2 else ()
            kwargs = dict(job[3]) if len(job) > 3 else {}
        else:
            return False
        if not callable(fn):
            return False

        self.stats["submitted"] += 1
        if not self.acquire():
            self.stats["rejected"] += 1
            logger.warning(f"作业[{name}] 被拒绝：槽位已满 {self.capacity}")
            return False

        started = time.monotonic()
        d = threads.deferToThread(self._run, fn, args, kwargs)
        d.addBoth(self._finish, name, started)
        return True

    def submit(
        self,
        name: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> defer.Deferred:
        """提交一个作业。槽位占满时返回**已失败**的 Deferred。

        :param name: 作业名，进历史记录与日志
        :param fn:   真正要跑的东西（通常是某个服务的``xxx_now``）
        """
        self.stats["submitted"] += 1
        if not self.acquire():
            self.stats["rejected"] += 1
            logger.warning(
                f"作业[{name}] 被拒绝：{self._inflight}/{self.capacity} 个槽位已满"
            )
            return defer.fail(
                RuntimeError(
                    f"工作单元繁忙（{self._inflight}/{self.capacity}），"
                    f"作业[{name}] 未被执行"
                )
            )

        started = time.monotonic()
        d = threads.deferToThread(self._run, fn, args, kwargs)
        d.addBoth(self._finish, name, started)
        return d

    def _run(self, fn: Callable[..., Any], args: Any, kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    def _finish(self, result: Any, name: str, started: float) -> Any:
        self.release()
        elapsed = time.monotonic() - started
        failed = isinstance(result, Exception)
        if failed:
            self.stats["failed"] += 1
            logger.error(f"作业[{name}] 失败（{elapsed:.1f}s）: {result}")
        else:
            self.stats["done"] += 1
            logger.info(f"作业[{name}] 完成（{elapsed:.1f}s）")
        self._remember(name, elapsed, failed)
        return result

    def _remember(self, name: str, elapsed: float, failed: bool) -> None:
        self._history.append({
            "name": name,
            "elapsed": round(elapsed, 2),
            "ok": not failed,
            "at": time.strftime("%H:%M:%S"),
        })
        if len(self._history) > self._HISTORY_MAX:
            del self._history[: len(self._history) - self._HISTORY_MAX]

    # ============================================================
    # 可观测
    # ============================================================
    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update({
            "stats": dict(self.stats),
            "history": list(self._history[-10:]),
        })
        return base

    def health_check(self):
        if not self.enabled:
            return True, "disabled"
        # 槽位长期打满通常意味着下游卡住了（数据源慢/限流），
        # 这比"作业失败"更值得告警 —— 它会拖住所有后续作业。
        if self.capacity > 0 and self._inflight >= self.capacity:
            return True, f"槽位已满 {self._inflight}/{self.capacity}（下游可能较慢）"
        return True, f"空闲 {self.capacity - self._inflight}/{self.capacity}"


__all__ = ["WorkerComponent"]
