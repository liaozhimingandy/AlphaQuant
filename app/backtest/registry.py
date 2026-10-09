#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 策略注册表：回测按名字取策略，CLI 可自动列出可选策略
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Callable, Dict, List

import backtrader as bt

from app.utils.logger import logger

# 延迟导入：backtrader 策略类只有在真正回测时才需要
_REGISTRY: Dict[str, Callable[[], type[bt.Strategy]]] = {}


def _load() -> None:
    if _REGISTRY:
        return
    from app.strategy.ma_cross import MaCrossStrategy
    from app.strategy.strategy import PreciseMaCrossStrategyI
    from app.strategy.trend_ma_cross import TrendMaCrossStrategy

    _REGISTRY.update(
        {
            # 带趋势过滤 + 止损的均线交叉
            "ma_cross": lambda: MaCrossStrategy,
            # 因子/规则可组合版：仅金叉买、仅死叉卖
            "precise_ma_cross": lambda: PreciseMaCrossStrategyI,
            # 三均线趋势跟随
            "trend_ma_cross": lambda: TrendMaCrossStrategy,
        }
    )


def register_strategy(name: str, factory: Callable[[], type[bt.Strategy]]) -> None:
    """注册自定义策略：register_strategy("my_strategy", lambda: MyStrategy)"""
    _load()
    if name in _REGISTRY:
        logger.warning(f"策略 {name} 已存在，将被覆盖")
    _REGISTRY[name] = factory


def get_strategy(name: str) -> type[bt.Strategy]:
    _load()
    if name not in _REGISTRY:
        raise KeyError(
            f"未找到策略: {name} | 可用策略: {list_strategies()}"
        )
    return _REGISTRY[name]()


def list_strategies() -> List[str]:
    _load()
    return sorted(_REGISTRY.keys())
