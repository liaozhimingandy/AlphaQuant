#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : Events（事件契约层）—— 只定义"发生了什么"，不含任何业务动作
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
Events 包
=========

    types.py    事件/任务/队列/执行池的**名字常量**（六类事件、19 种任务、7 个池）
    schema.py   ``EngineEvent`` —— 总线上流动的唯一消息形态
    bus.py      ``EventBus`` —— 同步广播、去重、通配、异常隔离

三条边界（违反任意一条，事件驱动就会退化成隐式的函数调用）::

    事件 ≠ 任务      事件是事实，任务是动作；一个事件可产生 0..N 个任务
    事件 ≠ 命令      广播出去没人订阅是正常的，不是失败
    订阅者 ≠ 干活的人 订阅者只该"决定是否建任务"，耗时的事交给执行池
"""
from app.runtime.events.bus import EventBus
from app.runtime.events.schema import SCHEMA_VERSION, EngineEvent
from app.runtime.events import types as _types
from app.runtime.events.types import (
    CANONICAL_TO_LEGACY,
    EVENT_CATEGORIES,
    EVENT_CATEGORY_OF,
    LEGACY_EVENT_ALIASES,
    NON_RETRIABLE_TASK_TYPES,
    TASK_ECHO_EVENTS,
    TASK_QUEUE,
    EventType,
    Pool,
    Queue,
    TaskType,
    canonical_event_type,
)


class StandardEvents:
    """**旧版**事件名集合，仅用于兼容存量代码。

    值保持下划线风格不变（``engine_started``），总线会自动把它们折算成
    :class:`EventType` 的规范名并同时投递给新旧订阅者，
    所以存量 ``publish(StandardEvents.X, ...)`` / ``subscribe(StandardEvents.X, ...)``
    一行都不用改。

    **新代码请一律用** :class:`EventType`：它带域前缀（可以通配订阅）、
    有分类目录、拼错能被 IDE 立刻发现。
    """

    # 引擎生命周期
    ENGINE_STARTED = "engine_started"
    ENGINE_STOPPED = "engine_stopped"
    # 行情事件
    BAR_RECEIVED = "bar_received"
    TICK_RECEIVED = "tick_received"
    DATA_COLLECTED = "data_collected"
    # 新闻/事件通道
    NEWS_RECEIVED = "news_received"
    NEWS_ANALYZED = "news_analyzed"
    # 策略事件
    SIGNAL_GENERATED = "signal_generated"
    SIGNAL_REJECTED = "signal_rejected"
    # 任务事件
    TASK_STARTED = "task_started"
    TASK_STOPPED = "task_stopped"
    TASK_PAUSED = "task_paused"
    TASK_RESUMED = "task_resumed"
    TASK_ADDED = "task_added"
    TASK_REMOVED = "task_removed"
    # 组件事件
    COMPONENT_ENABLED = "component_enabled"
    COMPONENT_DISABLED = "component_disabled"
    # 监控事件
    SNAPSHOT_SAVED = "snapshot_saved"
    CONTROL_COMMAND = "control_command"
    # 交易事件
    ORDER_CREATED = "order_created"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    POSITION_UPDATED = "position_updated"
    ACCOUNT_UPDATED = "account_updated"
    # 异常事件
    COMPONENT_ERROR = "component_error"
    ENGINE_ERROR = "engine_error"
    # 任务提交
    TASK_SUBMIT = "task_submit"

    TIMER_3MIN = "timer_3min"


__all__ = [
    "EventBus",
    "EngineEvent",
    "SCHEMA_VERSION",
    "StandardEvents",
    "EventType",
    "TaskType",
    "Queue",
    "Pool",
    "types",
    "EVENT_CATEGORIES",
    "EVENT_CATEGORY_OF",
    "LEGACY_EVENT_ALIASES",
    "CANONICAL_TO_LEGACY",
    "canonical_event_type",
    "TASK_QUEUE",
    "NON_RETRIABLE_TASK_TYPES",
    "TASK_ECHO_EVENTS",
]

# 让 ``app.runtime.events.types`` 可用别名访问
types = _types
