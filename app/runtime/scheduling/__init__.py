#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : Scheduling —— 订阅事件、匹配规则、产出任务
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
Scheduler 的职责边界
====================

    rules.py     规则：什么事件 → 什么任务 → 什么条件
    router.py    路由：什么任务 → 进哪个队列 → 哪个池能干
    defaults.py  内置规则表（系统里全部自动流转的清单）

调度器**不**做这些事（做了就退化回"服务互相直接调用"）：

  * 不执行任务 —— 交给执行池；
  * 不关心任务怎么做 —— 只关心"该不该做、谁有资格做"；
  * 不主动轮询 —— 它是**被事件驱动**的，节拍由定时器事件提供。

事件 → 任务的转换有个必须守住的约束：**回声事件不建任务**。
``task.started`` 这类事件是任务系统自己发出的，
如果规则订阅了它，就会 task.started → 任务 → task.started → …
一秒之内堆满队列。守卫在 :data:`TASK_ECHO_EVENTS`，调度器先查再匹配。
"""
from app.runtime.scheduling.defaults import build_default_rules
from app.runtime.scheduling.router import CapabilityRouter
from app.runtime.scheduling.rules import (
    Rule,
    RuleRegistry,
    dedup_key,
    normalize_key,
    symbols_of,
)

__all__ = [
    "Rule",
    "RuleRegistry",
    "CapabilityRouter",
    "build_default_rules",
    "dedup_key",
    "normalize_key",
    "symbols_of",
]
