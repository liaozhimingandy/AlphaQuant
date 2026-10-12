#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Runtime（运行时层）—— 决定"什么时候跑、交给谁跑、跑完通知谁"
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
Runtime 不属于业务
==================

它只负责"什么时候跑、怎么跑、跑完通知谁"，所以换调度策略、
加并发、上分布式 Worker 都不会污染业务模块。

组成::

    events/      事件契约：types（名字常量）/ schema（EngineEvent）/ bus（总线）
    tasking/     任务：models（EngineTask 状态机）/ store（队列、幂等、死信）
    scheduling/  调度：rules（规则表）/ router（能力路由）/ defaults（内置规则）
    workers/     执行：pools（分池线程池）/ registry（处理器）/ handlers（默认处理器）
    scheduler.py 调度组件：订阅事件 → 建任务 → 分发给执行池
    executor.py  旧式工作单元（有槽位限制的后台作业执行器，存量代码仍用）

一条数据到一笔订单的完整链路::

    Engine 发布事件
        │  engine.ready / market.kline.updated / strategy.signal.generated ...
        ▼
    EventBus（同步广播、去重、新旧名兼容、异常隔离）
        ▼
    Scheduler.handle_event
        │  1. 回声事件直接丢弃（防 task.started → 任务 → task.started 死循环）
        │  2. 匹配规则（事件类型 → 规则列表）
        │  3. 逐条判条件（服务启用？市场状态？运行模式？）
        │  4. 造 EngineTask（带幂等键、溯源 ID、重试策略）
        ▼
    TaskStore（按队列存、按优先级出队、状态机、死信）
        ▼
    CapabilityRouter（任务类型 → 队列 → 执行池）
        ▼
    Market / Research / Strategy / Trading / Backtest / Maintenance 池
        │  各自独立线程池 + 独立并发上限，互不挤占
        ▼
    HandlerRegistry（任务类型 → 处理器）→ 真实业务服务
        ▼
    处理器完成后发布下一个事件，链路继续往前走

为什么要有"事件"和"任务"两层
----------------------------

    事件  记录**已经发生的事实**（K 线更新了），不可执行、可忽略、可重复投递
    任务  描述**需要完成的工作**（重算因子），可执行、必须有终态、必须幂等

混成一层的后果：为了"不丢消息"给事件加重试，于是同一批 K 线把因子重算三遍；
或者为了"处理得快"不给任务落库，重启后在途任务凭空消失。
"""
from app.runtime import utils as _utils

__all__ = ["async_sleep"]

async_sleep = _utils.async_sleep
