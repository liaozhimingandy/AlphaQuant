#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : Workers（执行层）—— 领活、干活、回报，不决定"该不该干"
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
    pools.py     执行池：market / research / strategy / trading /
                 backtest / maintenance，各自独立线程池与并发上限
    registry.py  处理器注册表：任务类型 → 干活的函数
    handlers.py  默认处理器：把任务类型接到真实业务服务上

执行层的三条边界::

    不决定"该不该做"  —— 那是调度器的事（规则 + 条件）
    不决定"做的顺序"  —— 那是队列的事（优先级）
    只负责"把它做完"  —— 取处理器 → 执行 → 回报状态

池之间**互不影响**：回测池跑满不会让行情池少一个槽位。
这是"防止回测挤占实盘资源"的物理保证，不是靠优先级调度能替代的。
"""
from app.runtime.workers.pools import PoolManager, WorkerPool
from app.runtime.workers.registry import HandlerFn, HandlerRegistry

__all__ = ["PoolManager", "WorkerPool", "HandlerRegistry", "HandlerFn"]
