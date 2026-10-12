#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Collection —— 采集、清洗、落库、完整性核验
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""Collection：数据进系统的唯一入口，保证"库里的数据是完整可信的"。

    service.py   CollectionService（K线 / 日频截面 / 完整性 三单元合一）
    spec.py      "采什么 / 多久采一次" —— 纯配置模型，不依赖 Twisted
    schedule.py  采集时间窗（开盘前 / 收盘后），进程级单例
    integrity.py 缺口扫描与补全，输出可读体检报告
"""
from app.modules.collection.integrity import (
    DataIntegrityChecker,
    IntegrityReport,
    SymbolReport,
    quick_check,
    summarize,
)
from app.modules.collection.schedule import (
    CollectionSchedule,
    get_schedule,
    load_schedule,
    save_schedule,
    set_schedule,
)
from app.modules.collection.service import CollectionService, parse_interval
from app.modules.collection.spec import (
    A_SHARE_SESSIONS,
    DAILY_PERIOD,
    CollectJob,
    CollectorSpec,
    build_job,
    humanize_frequency,
    in_trading_session,
    is_intraday,
    is_trading_day,
    load_collector_spec,
    market_state,
    normalize_period,
    parse_frequency,
)

__all__ = [
    "A_SHARE_SESSIONS", "DAILY_PERIOD", "CollectJob", "CollectorSpec",
    "build_job", "humanize_frequency", "in_trading_session", "is_intraday",
    "is_trading_day", "load_collector_spec", "market_state", "normalize_period",
    "parse_frequency",
    "CollectionService", "parse_interval",
    "CollectionSchedule", "load_schedule", "save_schedule", "get_schedule", "set_schedule",
    "DataIntegrityChecker", "IntegrityReport", "SymbolReport", "quick_check", "summarize",
]
