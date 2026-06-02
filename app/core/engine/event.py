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
    # 策略事件
    SIGNAL_GENERATED = "signal_generated"
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

