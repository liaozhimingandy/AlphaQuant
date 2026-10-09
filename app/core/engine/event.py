#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 16:55
# @FileName    : event.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from collections import defaultdict
from typing import Dict, List, Callable

from app.utils.logger import logger


# 预定义标准事件
class StandardEvents:
    """全系统标准事件，所有组件统一使用"""
    # 引擎生命周期
    ENGINE_STARTED = "engine_started"
    ENGINE_STOPPED = "engine_stopped"
    # 行情事件
    BAR_RECEIVED = "bar_received"
    TICK_RECEIVED = "tick_received"
    DATA_COLLECTED = "data_collected"  # 采集服务落库了一批行情（symbol/period/rows）
    # 新闻/事件通道
    NEWS_RECEIVED = "news_received"      # 采集到新新闻（未分析）
    NEWS_ANALYZED = "news_analyzed"      # 已完成分析（含情感/置信度）
    # 策略事件
    SIGNAL_GENERATED = "signal_generated"
    SIGNAL_REJECTED = "signal_rejected"  # 被风控否决，必须留痕
    # 任务事件
    TASK_STARTED = "task_started"
    TASK_STOPPED = "task_stopped"
    TASK_PAUSED = "task_paused"
    TASK_RESUMED = "task_resumed"
    TASK_ADDED = "task_added"          # 运行时新增任务（面板/CLI 下发）
    TASK_REMOVED = "task_removed"
    # 组件事件
    COMPONENT_ENABLED = "component_enabled"
    COMPONENT_DISABLED = "component_disabled"
    # 监控事件
    SNAPSHOT_SAVED = "snapshot_saved"
    CONTROL_COMMAND = "control_command"  # 控制指令留痕（谁在什么时候改了什么）
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


class EventBus:
    """
    全局事件总线
    发布-订阅模式,解耦所有组件
    """

    def __init__(self) -> None:
        self._receivers: Dict[str, List[Callable]] = defaultdict(list)


    def subscribe(self, event_name: str, receiver: Callable) -> None:
        """订阅事件"""
        self._receivers[event_name].append(receiver)

    def publish(self, event_name: str, **kwargs) -> None:
        """发布事件"""
        for receiver in self._receivers[event_name]:
            try:
                logger.debug(f"正在回调...{receiver.__name__}")
                receiver(**kwargs)
            except Exception as e:
                logger.error(f"事件 {event_name} 回调异常: {str(e)}", exc_info=True)

    def unsubscribe(self, event_name: str, receiver: Callable) -> None:
        """取消订阅"""
        if receiver in self._receivers[event_name]:
            self._receivers[event_name].remove(receiver)

    def get_receiver(self):
        return self._receivers

