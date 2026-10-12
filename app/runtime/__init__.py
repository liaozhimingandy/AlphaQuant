#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Runtime —— 调度与执行基础，不含任何业务语义
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
Runtime（运行时层）
==================

Runtime **不属于业务**。它只负责"什么时候跑、怎么跑、跑完通知谁"，
所以换调度器、加并发、上分布式 Worker 都不会污染业务模块。

组成::

    scheduler.py    节拍器：优先级队列 + 定时任务 + 重试
    executor.py     执行器：带槽位并发限制的作业工人
    task_runtime.py 任务容器（在 tasking/ 下）
    task.py         可调度的通用作业单元（ITask / TaskStatus）
    bus/            跨进程事件总线（FileBus，append-only JSONL）
    tasking/        任务的 etl 三件套：runtime / spec / task(QuantTask)

为什么 Scheduler 与 Strategy 分开::

    Scheduler   到点了 → 交给 Executor → 跑一个 Task
    Strategy    订阅行情 → 产出 Signal

    两者唯一的交汇点是 EventBus，而不是互相 import。
"""
from app.runtime import utils as _utils

__all__ = ["async_sleep"]

async_sleep = _utils.async_sleep
