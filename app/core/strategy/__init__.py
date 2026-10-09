#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 策略层导出，自动注册内置策略
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.core.strategy.base import IBaseStrategy
from app.core.strategy.state import DecisionState
from app.core.strategy.registry import (
    clear_strategies,
    create_strategy,
    has_strategy,
    list_strategies,
    register_strategy,
    strategy_registry,
)
from app.core.strategy.spec import build_strategy

from app.core.strategy.builtin import ComboStrategy, MaCrossStrategy, NewsDrivenStrategy

for _cls in (ComboStrategy, MaCrossStrategy, NewsDrivenStrategy):
    register_strategy(_cls)

__all__ = [
    "IBaseStrategy",
    "DecisionState",
    "ComboStrategy",
    "MaCrossStrategy",
    "NewsDrivenStrategy",
    "register_strategy",
    "create_strategy",
    "has_strategy",
    "list_strategies",
    "strategy_registry",
    "clear_strategies",
    "build_strategy",
]
