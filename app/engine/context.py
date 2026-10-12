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
from typing import Dict, Any, Optional


# 运行模式**只有一个真源**：``app.domain.types.RunMode``。
#
# 这里以前自己定义了一份同名枚举，后果比"重复代码"严重得多：
# ``builder.py`` 从 domain 导入、``base.py``/``scheduler.py`` 从这里导入，
# 于是 ``builder`` 造出来的 ``RunMode.BACKTEST`` 和 ``base`` 用来比较的
# ``RunMode.BACKTEST`` 是**两个不同的类** —— ``==`` 恒为 False。
# 直接后果：``base.py`` 里"回测模式装空闲自动停止看门狗"的判断永远不成立，
# 回测跑完数据后 reactor 永久阻塞，命令永不返回。
#
# 保留这里的名字是为了不动已有 import（``from app.engine.context import
# RunMode`` 仍在用），它现在是 domain 版本的别名。
from app.domain.types import RunMode  # noqa: F401  单一真源，兼容旧导入路径


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
    end_time: Optional[datetime] = None

    # 全局配置
    config: Dict[str, Any] = field(default_factory=dict)
    # 扩展字段
    extra: Dict[str, Any] = field(default_factory=dict)


