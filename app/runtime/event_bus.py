#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : event_bus.py
# @Description : 【兼容层】事件总线的旧路径，新代码请从 app.runtime.events 导入
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么这个文件还在
==================

事件契约升级后搬到了 :mod:`app.runtime.events`（types / schema / bus 三件套）。
但 ``from app.runtime.event_bus import StandardEvents`` 散落在二十多个文件里，
一次全改会让本次重构的 diff 与风险都失控。

所以这里保留**薄转发**：名字与行为都从新包原样导出，
存量代码零改动继续工作，新代码统一走 :mod:`app.runtime.events`。

新旧名共存的机制（关键）:
    旧名 ``bar_received`` 与新名 ``market.bar.received`` 由
    ``LEGACY_EVENT_ALIASES`` 映射。按旧名发布时，总线**同时**投递给
    新旧两批订阅者，所以迁移可以逐个文件进行，不必一次性切换。
"""
from app.runtime.events import (
    CANONICAL_TO_LEGACY,
    EVENT_CATEGORIES,
    EVENT_CATEGORY_OF,
    LEGACY_EVENT_ALIASES,
    SCHEMA_VERSION,
    TASK_ECHO_EVENTS,
    TASK_QUEUE,
    NON_RETRIABLE_TASK_TYPES,
    EngineEvent,
    EventBus,
    EventType,
    Pool,
    Queue,
    StandardEvents,
    TaskType,
    canonical_event_type,
)

__all__ = [
    "EventBus",
    "EngineEvent",
    "SCHEMA_VERSION",
    "StandardEvents",
    "EventType",
    "TaskType",
    "Queue",
    "Pool",
    "EVENT_CATEGORIES",
    "EVENT_CATEGORY_OF",
    "LEGACY_EVENT_ALIASES",
    "CANONICAL_TO_LEGACY",
    "canonical_event_type",
    "TASK_QUEUE",
    "NON_RETRIABLE_TASK_TYPES",
    "TASK_ECHO_EVENTS",
]
