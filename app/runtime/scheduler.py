#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : scheduler.py
# @Description : 调度组件：引擎的"心脏"——决定什么时候跑什么
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

重构说明
========

由原 ``TaskSchedulerComponent``（任务队列调度）与 ``TimerComponent``
（周期定时器）**合并**而来，改继承 :class:`~..kinds.SchedulerComponent`。

为什么要合并
------------

它们本来就是同一件事的两种形态：**决定什么时候该跑**。
任务队列是"有活就跑，按优先级排"，定时器是"到点就发事件"。
拆成两个组件时，引擎里出现两个都在往 reactor 上挂 ``callLater``
的东西，谁也不知道对方的节拍；想查"引擎为什么一直在忙"
得同时看两个组件的快照。

合并后：一份调度快照、一个空闲判定、一处停止逻辑。

契约（任何调度实现都必须满足）
------------------------------

  * :attr:`is_idle` —— 队列空且无在跑的任务，引擎才能判定空闲退出；
  * :meth:`submit`  —— 往队列里放活；
  * :meth:`start_timer` —— 注册一个周期性事件源。

注册名从 ``task_scheduler`` 改为 ``scheduler``，与种类名对齐。
"""
import queue
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from twisted.internet import defer, reactor, threads
from twisted.internet.task import LoopingCall

from app.engine.capability import SchedulerComponent as SchedulerBase
from app.runtime.event_bus import StandardEvents
from app.engine.context import RunMode
from app.runtime.task import TaskPriority, TaskStatus, Task
from app.runtime.utils import async_sleep
from app.common.logging import logger


class SchedulerComponent(SchedulerBase):
    """任务调度 + 周期定时器。

    它决定**什么时候该跑哪个任务**：按 (优先级, 时间) 排序出队，
    控制并发上限，并根据 RunMode 决定要不要真的连 reactor。
    其它组件（采集、策略、交易）都只是往它的队列里丢任务。

    运行模式差异
    ------------
    ``BACKTEST`` 严格串行：必须等上一个任务完成再执行下一个，
    否则回测结果的顺序不再确定，同样的输入跑两次可能跑出不同的净值曲线。
    其它模式按 ``max_concurrent`` 并发。
    """

    name = "scheduler"

    def __init__(self) -> None:
        super().__init__()
        # ---- 任务队列 ----
        self._task_queue: "queue.PriorityQueue[Task]" = queue.PriorityQueue()
        self._active_tasks: int = 0
        self._lock = threading.Lock()
        self.max_concurrent: int = 0
        self.run_mode: RunMode = RunMode.BACKTEST
        self._running: bool = False

        # ---- 定时器 ----
        self._timers: Dict[str, LoopingCall] = {}
        self._is_stopping = False
        # 重试用的延迟回调。必须登记，否则停止后仍会往已停的队列里丢任务。
        self._pending_retries: set = set()

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        self.max_concurrent = self.context.config.get("SCHEDULER_MAX_CONCURRENT", 5)
        self.run_mode = self.context.run_mode
        logger.info("调度组件初始化完成")

    def on_start(self) -> None:
        """启动调度器，引擎启动时调用"""
        self._running = True
        logger.info(
            f"调度组件已启动 | 模式: {self.run_mode.value} "
            f"| 最大并发: {self.max_concurrent}"
        )
        reactor.callLater(delay=1, callable=self._schedule_loop)
        # 订阅事件，然后往队列中添加任务
        self.subscribe_event(StandardEvents.TASK_SUBMIT, handler=self.submit_task)
        # 默认 3 分钟节拍（沿用原定时器组件的行为）
        self.start_timer(
            interval=5,
            event_name=StandardEvents.TIMER_3MIN,
            timer_id="default_3min",
        )

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> Any:
        """停止调度器，引擎停止时调用。

        先停定时器（不再产生新事件源），再等队列排空。
        反过来会有"队列刚排空又来一批定时事件"的窗口。

        等待必须**异步**：``on_stop`` 跑在 reactor 线程里，用
        ``time.sleep`` 会把事件循环整个冻住 —— 行情推送、面板响应、
        撤单回调全部停摆，而任务自己又在等这些回调，于是
        ``_active_tasks`` 永远归不了零，形成"停止 = 挂死"。
        因此这里改用异步让出，并**带 deadline**：宁可超时放弃
        并告警，也不能无限期等一个可能已经卡死的任务。
        """
        self._running = False
        for timer_id, lc in self._timers.items():
            try:
                if lc.running:
                    lc.stop()
                    logger.debug(f"定时器[{timer_id}]已停止")
            except Exception as exc:
                logger.error(f"停止定时器 {timer_id} 异常: {exc}")
        self._timers.clear()

        # 取消尚未触发的重试，避免停止后继续往队列里投任务
        for later in list(self._pending_retries):
            try:
                if later.active():
                    later.cancel()
            except Exception:
                pass
        self._pending_retries.clear()

        if graceful:
            deadline = time.monotonic() + self._shutdown_wait()
            waited = 0.0
            while self._active_tasks > 0 or not self._task_queue.empty():
                if time.monotonic() >= deadline:
                    logger.warning(
                        f"停止调度器时仍有任务未收尾 | 活跃: {self._active_tasks} "
                        f"| 待执行: {self._task_queue.qsize()} "
                        f"| 已等待: {waited:.1f}s（超时放弃，避免挂死）"
                    )
                    break
                logger.debug(
                    f"等待剩余任务完成 | 活跃: {self._active_tasks} "
                    f"| 待执行: {self._task_queue.qsize()}"
                )
                yield async_sleep(0.1)
                waited += 0.1
        logger.info("调度组件已停止")

    def _shutdown_wait(self) -> float:
        """关停时最多等多久在跑的任务（秒）。

        优先读引擎配置 ``SHUTDOWN_TASK_WAIT``（默认 30s），读不到就退回 5s——
        调度器只是引擎的一个组件，整体关停还有自己的预算，这里不该恋战。
        """
        try:
            cfg = getattr(self.context, "config", None) or {}
            return float(cfg.get("SHUTDOWN_TASK_WAIT", 5))
        except Exception:
            return 5.0

    # ============================================================
    # 契约一：提交任务
    # ============================================================
    def submit(self, *args: Any, **kwargs: Any) -> Any:
        """``SchedulerComponent`` 契约实现，转发到 :meth:`submit_task`。"""
        return self.submit_task(*args, **kwargs)

    def submit_task(
        self,
        task_name: str,
        handler: Any,
        priority: TaskPriority = TaskPriority.MEDIUM,
        max_retry: int = 0,
        retry_interval: float = 1.0,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """提交任务到调度器，所有组件/事件都通过这个方法提交任务。

        :return: 任务ID
        """
        if not self._running:
            raise RuntimeError("调度器未启动，无法提交任务")

        task = Task(
            priority=priority,
            create_time=datetime.now(),
            task_name=task_name,
            handler=handler,
            args=args,
            kwargs=kwargs,
            max_retry=max_retry,
            retry_interval=retry_interval,
        )

        self._task_queue.put(task)
        logger.debug(
            f"任务已提交!任务名称: {task_name} | ID: {task.task_id} "
            f"| 优先级: {priority.name}"
        )
        return task.task_id

    # ============================================================
    # 核心调度循环
    # ============================================================
    def _schedule_loop(self) -> None:
        """调度主循环，和引擎主循环融合。"""
        if not self._running:
            return

        try:
            # 回测模式：严格串行，必须等上一个任务完成再执行下一个
            if self.run_mode == RunMode.BACKTEST:
                if self._active_tasks == 0 and not self._task_queue.empty():
                    task = self._task_queue.get_nowait()
                    self._execute_task(task)
            # 实盘模式：并发调度，控制最大并发数
            else:
                while (
                    self._active_tasks < self.max_concurrent
                    and not self._task_queue.empty()
                ):
                    task = self._task_queue.get_nowait()
                    self._execute_task(task)

        except queue.Empty:
            logger.debug("目前工作队列为空,无需要执行的任务!")
        except Exception as exc:
            logger.error(f"调度循环异常: {exc}", exc_info=True)
        finally:
            # 10ms后继续调度，避免CPU空转
            reactor.callLater(delay=0.01, callable=self._schedule_loop)

    # ============================================================
    # 任务执行与重试
    # ============================================================
    def _execute_task(self, task: Task) -> None:
        """执行单个任务，带重试机制"""
        with self._lock:
            self._active_tasks += 1
        task.status = TaskStatus.RUNNING

        def _run_task() -> None:
            try:
                result = task.handler(*task.args, **task.kwargs)
                task.status = TaskStatus.SUCCESS
                task.result = result
                logger.debug(
                    f"任务执行成功! 任务名称: {task.task_name} | ID: {task.task_id}"
                )
            except Exception as exc:
                task.error = exc
                if task.current_retry < task.max_retry:
                    task.current_retry += 1
                    task.status = TaskStatus.PENDING
                    logger.warning(
                        f"任务执行失败，第{task.current_retry}次重试: "
                        f"{task.task_name} | 错误: {exc}"
                    )
                    def _enqueue_retry(t: Task = task) -> None:
                        # 顺手清掉已触发/已取消的句柄，避免集合无界增长
                        self._pending_retries = {
                            d for d in self._pending_retries if d.active()
                        }
                        self._task_queue.put(t)

                    self._pending_retries.add(
                        reactor.callLater(
                            delay=task.retry_interval, callable=_enqueue_retry
                        )
                    )
                else:
                    task.status = TaskStatus.FAILED
                    logger.error(
                        f"任务执行失败，已达最大重试次数: {task.task_name} "
                        f"| 错误: {exc}",
                        exc_info=True,
                    )
            finally:
                with self._lock:
                    self._active_tasks -= 1

        # 异步执行任务，不阻塞调度循环。
        # 必须接住返回的 Deferred：``except Exception`` 抓不到 BaseException
        # （SystemExit / KeyboardInterrupt），漏网的会变成
        # "Unhandled error in Deferred"，既不记日志也无人知晓。
        d = threads.deferToThread(_run_task)
        d.addErrback(self._on_task_crashed, task)
        return d

    def _on_task_crashed(self, failure: Any, task: Task) -> None:
        """任务跑飞（BaseException 级）时的兜底记录。"""
        task.status = TaskStatus.FAILED
        logger.error(
            f"任务异常终止: {task.task_name} | ID: {task.task_id} "
            f"| {failure.getErrorMessage() if hasattr(failure, 'getErrorMessage') else failure}"
        )

    # ============================================================
    # 契约二：周期定时器
    # ============================================================
    def start_timer(
        self,
        interval: float,
        event_name: str,
        event_data: Optional[Dict[str, Any]] = None,
        run_immediately: bool = False,
        timer_id: str = "",
    ) -> str:
        """注册一个周期性事件源。

        :param timer_id: 省略时按 ``事件名@间隔`` 自动生成，
                         同一个 (事件名, 间隔) 重复注册会被忽略，
                         避免多处调用悄悄把节拍翻倍。
        :return: 定时器 ID
        """
        tid = timer_id or f"{event_name}@{interval}"
        if tid in self._timers:
            return tid

        lc = LoopingCall(self._publish_timer_event, event_name, event_data or {})
        # now= 决定是否启动时立刻跑一次；异常由 LoopingCall 内部捕获，不中断节拍
        lc.start(max(0.01, float(interval)), now=run_immediately)
        self._timers[tid] = lc
        logger.info(f"定时器[{tid}]已启动 → {interval}s/次")
        return tid

    def stop_timer(self, timer_id: str) -> bool:
        lc = self._timers.pop(timer_id, None)
        if lc is None:
            return False
        try:
            if lc.running:
                lc.stop()
        except Exception as exc:
            logger.error(f"停止定时器 {timer_id} 异常: {exc}")
        return True

    def _publish_timer_event(self, event_name: str, event_data: dict) -> None:
        """定时器触发：发布标准事件（自带异常捕获，永不崩溃）"""
        try:
            self.event_bus.publish(event_name=event_name, context=event_data)
            logger.debug(f"定时器触发事件: {event_name}")
        except Exception as exc:
            # 异常隔离：单个任务报错不影响定时器
            logger.error(f"定时器事件执行失败: {exc}", exc_info=True)

    # ============================================================
    # 契约三：空闲判定
    # ============================================================
    @property
    def is_idle(self) -> bool:
        """空闲检测：队列空+无活跃任务，用于回测自动停止。"""
        with self._lock:
            return self._task_queue.empty() and self._active_tasks == 0

    def get_pending_tasks(self) -> List[Task]:
        """获取所有待执行任务，用于监控"""
        with self._lock:
            return list(self._task_queue.queue)

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update({
            "running": self._running,
            "max_concurrent": self.max_concurrent,
            "active_tasks": self._active_tasks,
            "pending": self._task_queue.qsize(),
            "timers": sorted(self._timers.keys()),
        })
        return base


__all__ = ["SchedulerComponent"]
