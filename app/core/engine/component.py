#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 16:51
# @FileName    : component.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import queue
import threading
from abc import ABC, abstractmethod
from datetime import datetime
import time
from enum import Enum
from typing import Optional, Callable, List, Any

from twisted.internet import threads, reactor

from app.core.engine.components import IBaseComponent
from app.core.engine.event import EventBus, StandardEvents
from app.core.engine.settings import EngineContext, RunMode
from app.core.engine.task import TaskPriority, TaskStatus, Task
from app.utils.logger import logger

class ComponentState(Enum):
    """组件生命周期状态"""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class TaskSchedulerComponent(IBaseComponent):
    name = "task_scheduler"

    def __init__(self):
        super().__init__()
        # 核心任务队列：优先级队列，自动按优先级+时间排序
        self._task_queue: queue.PriorityQueue[Task] = queue.PriorityQueue()  # type: ignore

        # 活跃任务计数
        self._active_tasks: int = 0
        self._lock = threading.Lock()

        # 调度配置
        self.max_concurrent: int = 0
        self.run_mode: RunMode = RunMode.BACKTEST

        self._running: bool = False


    def on_initialize(self) -> None:
        self.max_concurrent = self.context.config.get("SCHEDULER_MAX_CONCURRENT", 5)  # 最大并发数
        self.run_mode: RunMode = self.context.run_mode

    def on_start(self) -> None:
        """启动调度器，引擎启动时调用"""
        self._running = True
        logger.info(f"任务调度器已启动 | 模式: {self.run_mode.value} | 最大并发: {self.max_concurrent}")
        # 启动调度主循环
        reactor.callLater(delay=1, callable=self._schedule_loop)
        # 订阅事件,然后往队列中添加任务
        self.subscribe_event(StandardEvents.TASK_SUBMIT, handler=self.submit_task)

    def on_stop(self, graceful: bool = True) -> None:
        """停止调度器，引擎停止时调用"""
        self._running = False
        if graceful:
            # 等待所有活跃任务完成
            while self._active_tasks > 0 or not self._task_queue.empty():
                logger.debug(f"等待剩余任务完成 | 活跃: {self._active_tasks} | 待执行: {self._task_queue.qsize()}")
                time.sleep(0.1)
        logger.info("任务调度器已停止")

    def submit_task(
            self,
            task_name: str,
            handler: Callable,
            priority: TaskPriority = TaskPriority.MEDIUM,
            max_retry: int = 0,
            retry_interval: float = 1.0,
            *args,
            **kwargs
    ) -> str:
        """
        提交任务到调度器，所有组件/事件都通过这个方法提交任务
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
            retry_interval=retry_interval
        )

        self._task_queue.put(task)
        logger.debug(f"任务已提交!任务名称: {task_name} | ID: {task.task_id} | 优先级: {priority.name}")
        return task.task_id

        # ------------------------------
        # 核心调度循环
        # ------------------------------

    def _schedule_loop(self) -> None:
        """调度主循环，和引擎主循环完美融合"""
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
                while self._active_tasks < self.max_concurrent and not self._task_queue.empty():
                    task = self._task_queue.get_nowait()
                    self._execute_task(task)

        except queue.Empty:
            logger.debug(f"目前工作队列为空,无需要执行的任务!")
        except Exception as e:
            logger.error(f"调度循环异常: {str(e)}", exc_info=True)
        finally:
            # 10ms后继续调度，避免CPU空转
            reactor.callLater(delay=0.01, callable=self._schedule_loop)

        # ------------------------------
        # 任务执行与重试
        # ------------------------------

    def _execute_task(self, task: Task) -> None:
        """执行单个任务，带重试机制"""
        with self._lock:
            self._active_tasks += 1
        task.status = TaskStatus.RUNNING

        def _run_task():
            try:
                # 执行任务逻辑
                result = task.handler(*task.args, **task.kwargs)
                task.status = TaskStatus.SUCCESS
                task.result = result
                logger.debug(f"任务执行成功! 任务名称: {task.task_name} | ID: {task.task_id}")
            except Exception as e:
                task.error = e
                # 重试逻辑
                if task.current_retry < task.max_retry:
                    task.current_retry += 1
                    task.status = TaskStatus.PENDING
                    logger.warning(f"任务执行失败，第{task.current_retry}次重试: {task.task_name} | 错误: {str(e)}")
                    reactor.callLater(delay=task.retry_interval, callable=lambda: self._task_queue.put(task))
                else:
                    task.status = TaskStatus.FAILED
                    logger.error(f"任务执行失败，已达最大重试次数: {task.task_name} | 错误: {str(e)}", exc_info=True)
            finally:
                with self._lock:
                    self._active_tasks -= 1

        # 异步执行任务，不阻塞调度循环
        threads.deferToThread(_run_task)

        # ------------------------------
        # 辅助工具方法
        # ------------------------------

    @property
    def is_idle(self) -> bool:
        """空闲检测：队列空+无活跃任务，用于回测自动停止"""
        with self._lock:
            return self._task_queue.empty() and self._active_tasks == 0

    def get_pending_tasks(self) -> List[Task]:
        """获取所有待执行任务，用于监控"""
        with self._lock:
            return list(self._task_queue.queue)

class TimerComponent(IBaseComponent):

    name = "timer"

    def on_initialize(self) -> None:
        self.subscribe_event(event_name=StandardEvents.ENGINE_STARTED, handler=self._deal_signal)

    def on_start(self) -> None:
        logger.info("定时器组件开始...")
        self.event_bus.publish(StandardEvents.TASK_SUBMIT, handler=self._deal_signal,
                               priority=TaskPriority.HIGH, max_retry=3, task_name="定时器任务测试")

    def on_stop(self, graceful: bool = True) -> None:
        logger.info("定时器组件停止...")

    def _deal_signal(self, **kwargs) -> None:
        logger.debug(f'收到信号: {kwargs}')