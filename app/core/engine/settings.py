#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 16:48
# @FileName    : settings.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
# ------------------------------
# 1. 核心枚举定义
# ------------------------------
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Any


class RunMode(Enum):
    """引擎运行模式，完全匹配你的实盘/回测需求"""
    BACKTEST = "BACKTEST"    # 回测模式：任务完成自动停止
    SIMULATE = "SIMULATE"    # 模拟盘模式：常驻运行，不发实单
    LIVE = "LIVE"            # 实盘模式：常驻运行，发实单


class EngineStatus(Enum):
    """引擎运行状态"""
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


# ------------------------------
# 2. 全局上下文（全系统唯一数据载体）
# ------------------------------
@dataclass
class EngineContext:
    """引擎全局上下文，所有组件共享"""
    run_id: str = field(default_factory=lambda: f"run_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    run_mode: RunMode = RunMode.BACKTEST
    engine_status: EngineStatus = EngineStatus.INITIALIZING
    start_time: datetime = field(default_factory=datetime.now)

    # 全局配置
    config: Dict[str, Any] = field(default_factory=dict)
    # 扩展字段
    extra: Dict[str, Any] = field(default_factory=dict)


