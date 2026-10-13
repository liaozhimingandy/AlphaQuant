#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : pools.py
# @Description : 执行池：Market / Research / Strategy / Trading / Backtest / Maintenance
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么每个池一个**独立线程池**
==============================

用一个公共线程池 + 一个总并发上限，看起来更"省资源"，实际会出这种事：

    运维在面板上点了一次"三年全市场参数寻优"，
    回测任务把公共池的 10 个线程全占满并跑 20 分钟；
    期间行情采集排在队尾，K 线迟迟不来；
    策略因此不动，本该触发的止损一直没触发。

分池之后，回测再忙也只能用完 **backtest** 池自己的那 1 个槽位，
行情池、交易池完全不受影响。这是"防止回测挤占实盘资源"最直接的落点。

并发上限为什么是这些数
----------------------

===========  ====  ==========================================
池           槽位   理由
===========  ====  ==========================================
system       2     恢复/快照，偶发但希望并行
market       3     采集要并发，但数据源限流，再多就是自杀
research     2     因子/舆情吃 CPU，给多会把 reactor 拖慢
strategy     1     **必须串行**：两个策略同时改账本，净值曲线就不确定
trading      1     **必须串行**：并发下单 + 网络超时 = 重复报单
backtest     1     长任务，本来就该排队
maintenance  1     慢活，不该抢任何人的资源
===========  ====  ==========================================

关于超时
--------

``ThreadPoolExecutor`` 无法真正杀掉一个跑飞的线程（Python 没有安全的
线程强杀）。这里的超时是**记账层面的**：超时后释放槽位、把任务判失败
并进重试/死信，让系统继续运转；那个孤儿线程跑完时结果会被丢弃。
这不是完美方案，但比"一个任务卡死，整个池永久少一个槽位"好得多。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from twisted.internet import defer, reactor

from app.common.logging import logger
from app.runtime.events.types import Pool


def _reactor_running() -> bool:
    try:
        return bool(reactor.running)
    except Exception:  # pragma: no cover - reactor 未安装
        return False


#: ``config/runtime.json`` 里管线程池的那一段叫什么。
POOL_CONFIG_KEY: str = "worker_pools"


def load_worker_pool_config(
    path: Optional[str] = None,
) -> "Dict[str, Any]":
    """读 ``config/runtime.json`` 的 ``worker_pools`` 段。

    为什么是**文件**而不是环境变量：池容量是一组七个互相关联的数字
    （"strategy 只能 1"得和"market 给到 4"放在一起看才说得通），
    散在七个环境变量里没法评论、没法版本化，也没法一眼看出
    "trading 变成 2 了是谁改的"。

    为什么**不支持热更新**：见 :func:`WorkerPool` —— 线程池容量的语义是
    "在建池那一刻把槽位焊死"。这里读到的值只在进程启动时被消费一次。

    :return: ``{"capacities", "enabled", "source", "warnings"}``。
             文件缺失/写坏 → 全部取默认并给出告警，**绝不让配置问题挡住启动**。
    """
    from app.common.config import settings

    file = Path(path or getattr(settings, "RUNTIME_CONFIG", "")
                or (settings.CONFIG_DIR / "runtime.json"))
    out: "Dict[str, Any]" = {
        "capacities": None, "enabled": None, "source": str(file), "warnings": [],
    }
    if not file.exists():
        return out
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except Exception as exc:
        out["warnings"].append(f"{file.name} 解析失败（用默认容量）: {exc}")
        logger.warning(f"运行时配置解析失败，执行池用默认容量: {exc}")
        return out
    section = (raw or {}).get(POOL_CONFIG_KEY) if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return out

    caps: "Dict[str, int]" = {}
    for name, value in (section.get("capacities") or {}).items():
        key = str(name).strip()
        if key.startswith("_"):
            continue
        if key not in Pool.ALL:
            out["warnings"].append(
                f"未知执行池 {key!r}（可用: {'、'.join(Pool.ALL)}），已忽略"
            )
            continue
        try:
            want = int(value)
        except Exception:
            out["warnings"].append(f"执行池 {key} 容量 {value!r} 不是整数，已忽略")
            continue
        if want < 1:
            out["warnings"].append(
                f"执行池 {key} 容量 {want} < 1（0 个 worker = 活永远没人干），按 1 处理"
            )
            want = 1
        caps[key] = want
    if caps:
        out["capacities"] = caps

    enabled = section.get("enabled")
    if isinstance(enabled, (list, tuple)):
        picked = [str(x).strip() for x in enabled if str(x).strip()]
        # 空数组按"没配"处理：配错成 ``[]`` 就把所有池 disable 掉，
        # 引擎起来之后一个任务都跑不了，而报错信息还跟池容量完全无关。
        if picked:
            unknown = [x for x in picked if x not in Pool.ALL]
            if unknown:
                out["warnings"].append(
                    f"enabled 里有未知执行池 {unknown}，已忽略"
                )
            picked = [x for x in picked if x in Pool.ALL]
            if picked:
                # ``default`` 是**兜底池**：路由判断不出业务归属的任务
                # （典型是 ``legacy.job``）都落在它上面。少建它不报错，
                # 只是这类任务没人领、悄悄进死信 —— 是那种最后才看得见的失败。
                # 所以这里补回来，而不是尊重用户"我没启用 default"的意思。
                if Pool.DEFAULT not in picked:
                    picked.append(Pool.DEFAULT)
                out["enabled"] = picked
    return out


class WorkerPool:
    """一个执行池 = 一个独立线程池 + 一个并发上限 + 一份能力声明。"""

    def __init__(
        self,
        name: str,
        capacity: int = 1,
        queue: str = "",
    ) -> None:
        self.name = name
        self.queue = queue or Pool.POOL_QUEUE.get(name, "")
        self.capacity = max(0, int(capacity))
        self._lock = threading.RLock()
        self._inflight: "Dict[str, float]" = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.capacity),
            thread_name_prefix=f"aq-{name}",
        )
        self.stats: Dict[str, int] = {
            "submitted": 0, "succeeded": 0, "failed": 0, "rejected": 0, "timeout": 0,
        }
        self._closed = False

    # ============================================================
    # 槽位
    # ============================================================
    @property
    def inflight(self) -> int:
        with self._lock:
            return len(self._inflight)

    def has_slot(self) -> bool:
        with self._lock:
            if self.capacity <= 0:
                return True
            return len(self._inflight) < self.capacity

    # ============================================================
    # 投递
    # ============================================================
    def try_submit(
        self,
        task_id: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Optional[defer.Deferred]:
        """有空槽就投递并返回 Deferred；**没空槽返回 None**。

        为什么不用"排队等"：调度器握着队列，它来决定"等还是拒"。
        池只回答"我现在能不能接"。返回 None 而不是抛异常/返回失败的
        Deferred，是因为"满了"是**正常的调度状态**，不是错误。
        """
        with self._lock:
            if self._closed:
                return None
            if 0 < self.capacity <= len(self._inflight):
                self.stats["rejected"] += 1
                return None
            self._inflight[task_id] = time.monotonic()
            self.stats["submitted"] += 1

        d: defer.Deferred = defer.Deferred()
        try:
            future = self._executor.submit(fn, *args, **kwargs)
        except Exception as exc:
            with self._lock:
                self._inflight.pop(task_id, None)
            # 提交失败（池已关闭）→ 立刻把失败交给调用方，别让任务悬着
            d.errback(exc)
            return d

        future.add_done_callback(lambda f: self._on_done(task_id, d, f))
        return d

    # ============================================================
    # 完成回调
    # ============================================================
    def _on_done(self, task_id: str, d: defer.Deferred, future: "Future[Any]") -> None:
        with self._lock:
            started = self._inflight.pop(task_id, None)
            elapsed = (time.monotonic() - started) if started is not None else 0.0
        try:
            result = future.result()
        except Exception as exc:
            with self._lock:
                self.stats["failed"] += 1
            self._fire_failure(d, exc, task_id, elapsed)
        else:
            with self._lock:
                self.stats["succeeded"] += 1
            self._fire_success(d, result, task_id, elapsed)

    def _fire_success(
        self, d: defer.Deferred, result: Any, task_id: str, elapsed: float
    ) -> None:
        logger.debug(f"[{self.name}] 任务 {task_id[:8]} 完成（{elapsed:.2f}s）")
        # 处理器可能返回一个 Deferred（典型：采集服务的 ``collect_now``）。
        # 这时"函数返回"不等于"活干完了"——必须等这个 Deferred 落地，
        # 否则任务会在数据还没落库时就被记成成功，下游因子读到的还是旧数据。
        if isinstance(result, defer.Deferred):
            result.addCallbacks(
                lambda r: self._deliver(d, True, r),
                lambda f: self._deliver(d, False, f),
            )
            return
        self._deliver(d, True, result)

    def _deliver(self, d: defer.Deferred, ok: bool, payload: Any) -> None:
        """把结果/失败安全地交回 reactor 线程。"""
        if _reactor_running():
            reactor.callFromThread(d.callback if ok else d.errback, payload)
        else:
            (d.callback if ok else d.errback)(payload)

    def _fire_failure(
        self, d: defer.Deferred, exc: BaseException, task_id: str, elapsed: float
    ) -> None:
        logger.warning(
            f"[{self.name}] 任务 {task_id[:8]} 失败（{elapsed:.2f}s）: {exc}"
        )
        # 显式用 exc 构造 Failure：回调是在线程池里跑的，
        # 依赖 sys.exc_info() 的隐式捕获在跨线程时并不可靠。
        from twisted.python.failure import Failure
        failure = Failure(exc)
        if _reactor_running():
            reactor.callFromThread(d.errback, failure)
        else:
            d.errback(failure)

    # ============================================================
    # 超时与释放
    # ============================================================
    def running_older_than(self, seconds: float) -> "List[str]":
        """返回已运行超过 N 秒的任务 ID（超时巡检用）。"""
        cutoff = time.monotonic() - max(0.0, float(seconds))
        with self._lock:
            return [tid for tid, at in self._inflight.items() if at < cutoff]

    def release(self, task_id: str) -> bool:
        """强制释放槽位（任务被判超时/取消时用）。"""
        with self._lock:
            return self._inflight.pop(task_id, None) is not None

    def running_ids(self) -> "List[str]":
        with self._lock:
            return list(self._inflight.keys())

    # ============================================================
    # 生命周期 / 可观测
    # ============================================================
    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
        try:
            self._executor.shutdown(wait=wait)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"执行池 {self.name} 关闭异常（可忽略）: {exc}")

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pool": self.name,
                "label": Pool.LABEL.get(self.name, self.name),
                "queue": self.queue,
                "capacity": self.capacity,
                "inflight": len(self._inflight),
                "running": list(self._inflight.keys()),
                "stats": dict(self.stats),
            }


class PoolManager:
    """所有执行池的管理者。

    调度器只与它打交道：问"哪个池能干这个活"、问"还有没有槽位"、
    把活递过去。池之间互不影响，这是并发隔离的物理保证。
    """

    def __init__(
        self,
        capacities: Optional[Dict[str, int]] = None,
        enabled: Optional[List[str]] = None,
        source: str = "",
        warnings: Optional[List[str]] = None,
    ) -> None:
        caps = dict(Pool.DEFAULT_CAPACITY)
        caps.update({k: int(v) for k, v in (capacities or {}).items()})
        wanted = list(enabled) if enabled else list(Pool.ALL)
        self.source = str(source or "内置默认")
        self.warnings: "List[str]" = list(warnings or [])
        self._pools: "Dict[str, WorkerPool]" = {}
        for name in wanted:
            self._pools[name] = WorkerPool(
                name=name,
                capacity=caps.get(name, 1),
                queue=Pool.POOL_QUEUE.get(name, ""),
            )

    # ---------------- 查询 ----------------
    def names(self) -> List[str]:
        return list(self._pools.keys())

    def pool(self, name: str) -> Optional[WorkerPool]:
        return self._pools.get(name)

    def has_capacity(self, name: str) -> bool:
        p = self._pools.get(name)
        return bool(p and p.has_slot())

    def inflight(self, name: str = "") -> int:
        if name:
            p = self._pools.get(name)
            return p.inflight if p else 0
        return sum(p.inflight for p in self._pools.values())

    def queue_of(self, pool_name: str) -> str:
        p = self._pools.get(pool_name)
        return p.queue if p else ""

    def pool_of_queue(self, queue: str) -> Optional[str]:
        for name, p in self._pools.items():
            if p.queue == queue:
                return name
        return None

    # ---------------- 投递 ----------------
    def dispatch(
        self,
        pool_name: str,
        task_id: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Optional[defer.Deferred]:
        """把一个任务投递给指定池。池满/池不存在返回 None。"""
        p = self._pools.get(pool_name)
        if p is None:
            logger.error(f"执行池不存在: {pool_name}")
            return None
        return p.try_submit(task_id, fn, *args, **kwargs)

    def release(self, pool_name: str, task_id: str) -> bool:
        p = self._pools.get(pool_name)
        return p.release(task_id) if p else False

    def timed_out(self, limits: "Dict[str, float]") -> "List[Tuple[str, str]]":
        """超时巡检。

        :param limits: ``{task_id: timeout_seconds}``
        :return: ``[(pool_name, task_id), ...]``
        """
        out: "List[Tuple[str, str]]" = []
        for name, p in self._pools.items():
            for task_id in p.running_ids():
                limit = limits.get(task_id)
                if limit and task_id in p.running_older_than(limit):
                    out.append((name, task_id))
        return out

    # ---------------- 生命周期 / 可观测 ----------------
    def shutdown(self, wait: bool = False) -> None:
        for p in self._pools.values():
            p.shutdown(wait=wait)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "pools": [p.snapshot() for p in self._pools.values()],
            "total_inflight": self.inflight(),
            # 「这个数是哪来的」比数字本身更容易被问到。
            # 用户在 config/runtime.json 里改了却没重启，来面板上看还是旧值 ——
            # 有个来源标注，他至少知道该往哪儿查，而不是怀疑代码不读配置。
            "config_source": self.source,
            "config_warnings": list(self.warnings),
        }


__all__ = ["POOL_CONFIG_KEY", "WorkerPool", "PoolManager", "load_worker_pool_config"]
