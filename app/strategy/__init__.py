#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L6 策略层 —— 策略本体、规则 DSL、内置策略、用户策略发现与持久化。

这里只声明**契约**与**内置策略的自动注册**，不含任何取数逻辑：
策略消费的是 feature/alpha 的输出，产出 :class:`~app.domain.types.Signal`。

    DataSource → Provider → Capability → Adapter → Router → DataService
        → Canonical Model → Storage → Factor/Alpha → **Strategy**

边界（重要）：
    * 策略**不得**直接 import 数据源或 adapter —— 那属于 datasource 层；
    * 规则见 :mod:`app.strategy.rule`，是纯只读的布尔判定，不允许在规则里下单；
    * 回测（:mod:`app.research`）与实盘（:mod:`app.engine`）共用本包的策略实现，
      **不允许存在第二套策略代码**。
"""
from app.strategy.base import IBaseStrategy
from app.strategy.state import DecisionState
from app.strategy.registry import (
    clear_strategies,
    create_strategy,
    has_strategy,
    list_strategies,
    register_strategy,
    strategy_registry,
)
from app.strategy.spec import build_strategy

from app.strategy.builtin import ComboStrategy, MaCrossStrategy, NewsDrivenStrategy
from app.strategy.multi_factor_strategy import MultiFactorStrategy
from app.strategy.score_strategy import ScoreStrategy

for _cls in (ComboStrategy, MaCrossStrategy, NewsDrivenStrategy,
             ScoreStrategy, MultiFactorStrategy):
    register_strategy(_cls)

__all__ = [
    # 基类 / 状态
    "IBaseStrategy",
    "DecisionState",
    # 内置策略
    "ComboStrategy",
    "MaCrossStrategy",
    "MultiFactorStrategy",
    "NewsDrivenStrategy",
    "ScoreStrategy",
    # 注册表
    "register_strategy",
    "create_strategy",
    "has_strategy",
    "list_strategies",
    "strategy_registry",
    "clear_strategies",
    "build_strategy",
]
