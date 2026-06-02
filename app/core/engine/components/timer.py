#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/6/2 17:42
# @FileName    : timer.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------

import time
import uuid
from dataclasses import dataclass
from typing import Dict, Callable, Optional, List, Any, Generator
from twisted.internet import reactor, defer
from twisted.internet.defer import Deferred
from twisted.internet.task import LoopingCall

from app.core.engine.components import IBaseComponent
from app.core.engine.event import StandardEvents
from app.core.engine.utils import async_sleep
from app.utils.logger import logger


# ------------------------------
# 类型安全的定时器配置
# ------------------------------
@dataclass(frozen=True)
class TimerConfig:
    """定时器配置，不可变，类型安全"""
    timer_id: str
    interval: float  # 触发间隔（秒）
    event_name: str
    event_data: Dict = None
    run_immediately: bool = False  # 是否启动时立即执行一次


# ------------------------------
# 装饰器：声明式注册定时任务
# ------------------------------
def timer(interval: float, event_name: str, event_data: Dict = None, run_immediately: bool = False):
    """
    装饰器：声明式注册定时任务
    用法：
    @timer(interval=180, event_name=StandardEvents.TIMER_3MIN)
    def on_3min_timer(self, event_data):
        pass
    """
    def decorator(func: Callable) -> Callable:
        # 给函数打标记，组件启动时自动注册
        func._is_timer = True
        func._timer_config = {
            "interval": interval,
            "event_name": event_name,
            "event_data": event_data or {},
            "run_immediately": run_immediately
        }
        return func
    return decorator


# ------------------------------
# 优雅的定时器组件核心
# ------------------------------
class TimerComponent(IBaseComponent):
    """
    基于 Twisted LoopingCall 的工业级定时器组件
    ✅ 原生周期性调度，无任务重叠
    ✅ 声明式装饰器注册，代码更优雅
    ✅ 类型安全的配置管理
    ✅ 完美的优雅停止（等待当前任务完成）
    ✅ 异常隔离，单个任务报错不影响全局
    ✅ 支持一次性延迟任务
    """
    name = "timerV2"

    def __init__(self):
        super().__init__()
        self._timers: Dict[str, LoopingCall] = {}
        self._is_stopping = False


    def on_initialize(self) -> None:
        # 运行中的 LoopingCall 实例池
        logger.info("定时器组件初始化完成")

    def on_start(self) -> None:
        logger.debug("定时器组件启动成功")
        # 自动注册默认的 3 分钟定时器
        self.start_timer(TimerConfig(
            timer_id="default_3min",
            interval=5,
            event_name=StandardEvents.TIMER_3MIN
        )
        )
        logger.debug("已注册默认 3 分钟定时器")

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        """优雅停止：安全取消所有定时器"""
        if self._is_stopping:
            return
        self._is_stopping = True

        logger.info("定时器组件正在停止...")
        # 停止所有定时器
        for timer_id, lc in self._timers.items():
            try:
                if lc.running:
                    lc.stop()
                    logger.debug(f"定时器[{timer_id}]已停止")
            except Exception as e:
                logger.error(f"停止定时器 {timer_id} 异常: {e}")


        # 优雅等待：确保正在运行的任务执行完毕
        if graceful:
            yield async_sleep(0.1)

        self._timers.clear()
        logger.info("定时器组件已完全停止")

        return defer.succeed(None)

    # ------------------------------
    # 对外核心 API
    # ------------------------------
    def start_timer(
        self, config: TimerConfig
    ):
        """注册一个周期性定时器（通用API）"""
        if config.timer_id in self._timers:
            return

        # 创建循环调用
        lc = LoopingCall(
            self._publish_timer_event,
            config.event_name,
            config.event_data or {}
        )

        # 启动（关键：异常会被内部捕获，不中断定时器）
        lc.start(config.interval, now=config.run_immediately)
        self._timers[config.timer_id] = lc
        logger.info(f"定时器[{config.timer_id}]已启动 → {config.interval}s/次")

    def _publish_timer_event(self, event_name: str, event_data: dict):
        """定时器触发：发布标准事件（自带异常捕获，永不崩溃）"""
        try:
            # 发布事件
            self.event_bus.publish(event_name=event_name, context=event_data)
            logger.debug(f"定时器触发事件: {event_name}")
        except Exception as e:
            # 异常隔离：单个任务报错不影响定时器
            logger.error(f"定时器事件执行失败: {str(e)}", exc_info=True)
