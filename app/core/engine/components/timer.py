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
    name = "timer"

    def on_initialize(self) -> None:
        # 运行中的 LoopingCall 实例池
        self._looping_calls: Dict[str, LoopingCall] = {}
        # 一次性延迟任务句柄
        self._delayed_calls: Dict[str, reactor.DelayedCall] = {}
        # 组件停止标志
        self._is_stopping: bool = False
        logger.info("定时器组件初始化完成")

    def on_start(self) -> None:
        logger.info("定时器组件启动成功")
        # 自动注册默认的 3 分钟定时器
        self.register_timer(TimerConfig(
            timer_id="default_3min",
            interval=180,
            event_name=StandardEvents.TIMER_3MIN
        ))
        logger.info("已注册默认 3 分钟定时器")

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> Generator[Deferred[list[tuple[bool, Any]] | Any], Any, None]:
        logger.info("定时器组件开始停止...")
        self._is_stopping = True

        # 1. 取消所有一次性延迟任务
        for call_id, delayed_call in self._delayed_calls.items():
            if delayed_call.active():
                delayed_call.cancel()
        self._delayed_calls.clear()
        logger.info("所有一次性延迟任务已取消")

        # 2. 优雅停止所有周期性定时器
        stop_deferreds = []
        for timer_id, loop in self._looping_calls.items():
            if loop.running:
                logger.debug(f"正在停止定时器: {timer_id}")
                # LoopingCall.stop() 返回 Deferred，等待当前任务完成
                stop_deferreds.append(loop.stop())

        if graceful and stop_deferreds:
            logger.info(f"等待 {len(stop_deferreds)} 个定时器任务完成...")
            # 等待所有定时器任务完成，最多等 10 秒
            try:
                yield defer.DeferredList(stop_deferreds, fireOnOneErrback=True).addTimeout(10, reactor)
                logger.info("所有定时器任务已完成")
            except defer.TimeoutError:
                logger.warning("定时器任务等待超时，强制停止")

        self._looping_calls.clear()
        logger.info("定时器组件已完全停止")

    # ------------------------------
    # 对外核心 API
    # ------------------------------
    def register_timer(self, config: TimerConfig) -> str:
        """注册周期性定时器"""
        if self._is_stopping:
            raise RuntimeError("定时器组件正在停止，无法注册新任务")

        if config.timer_id in self._looping_calls:
            raise ValueError(f"定时器 ID 重复: {config.timer_id}")

        # 创建 LoopingCall 实例
        loop = LoopingCall(self._run_timer_task, config)
        # 异常隔离：单个任务报错不停止定时器
        loop.addErrback(lambda failure: logger.error(
            f"定时器任务异常 | ID: {config.timer_id} | 错误: {failure.value}",
            exc_info=failure.getTraceback()
        ))

        # 启动定时器
        loop.start(
            interval=config.interval,
            now=config.run_immediately  # 是否立即执行第一次
        )

        self._looping_calls[config.timer_id] = loop
        logger.info(
            f"定时器已注册 | ID: {config.timer_id} | 事件: {config.event_name} | 间隔: {config.interval}s"
        )
        return config.timer_id

    def cancel_timer(self, timer_id: str) -> bool:
        """取消周期性定时器"""
        if timer_id in self._looping_calls:
            loop = self._looping_calls.pop(timer_id)
            if loop.running:
                loop.stop()
            logger.info(f"定时器已取消 | ID: {timer_id}")
            return True
        logger.warning(f"尝试取消不存在的定时器: {timer_id}")
        return False

    def call_later(self, delay: float, func: Callable, *args, **kwargs) -> str:
        """注册一次性延迟任务"""
        if self._is_stopping:
            raise RuntimeError("定时器组件正在停止，无法注册新任务")

        call_id = f"delayed_{uuid.uuid4().hex[:8]}"

        def _run_task():
            try:
                func(*args, **kwargs)
            except Exception as e:
                logger.error(f"延迟任务异常 | ID: {call_id} | 错误: {str(e)}", exc_info=True)
            finally:
                self._delayed_calls.pop(call_id, None)

        delayed_call = reactor.callLater(delay, _run_task)
        self._delayed_calls[call_id] = delayed_call
        logger.debug(f"延迟任务已注册 | ID: {call_id} | 延迟: {delay}s")
        return call_id

    # ------------------------------
    # 内部实现
    # ------------------------------
    def _run_timer_task(self, config: TimerConfig) -> None:
        """执行单个定时器任务"""
        if self._is_stopping:
            return

        start_time = time.time()
        try:
            # 构建标准事件数据
            full_event_data = {
                "timer_id": config.timer_id,
                "interval": config.interval,
                "trigger_time": time.time(),
                **(config.event_data or {})
            }

            # 发布事件
            self.event_bus.publish(config.event_name, full_event_data)
            logger.debug(
                f"定时器触发 | ID: {config.timer_id} | 事件: {config.event_name} | 耗时: {(time.time() - start_time)*1000:.2f}ms"
            )

        except Exception as e:
            logger.error(f"定时器任务执行异常 | ID: {config.timer_id} | 错误: {str(e)}", exc_info=True)
