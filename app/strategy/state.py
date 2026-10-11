#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : state.py
# @Description : 决策快照：策略做判断时能看到的"自己"的状态
#               策略只读它、不能改它 —— 状态由 Task 维护，避免策略之间互相污染
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class DecisionState:
    """交给策略的只读状态快照。"""

    task_id: str = ""
    symbol: str = ""
    has_position: bool = False
    position_size: int = 0
    avg_price: float = 0.0
    last_price: float = 0.0

    cash: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0

    #: 连续持仓天数，供"持股 N 天强制离场"这类规则使用
    holding_bars: int = 0
    #: 距上次成交的K线数，用于冷却期
    bars_since_last_trade: int = 999

    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def current_drawdown(self) -> float:
        """当前回撤比例（正值表示亏损幅度）"""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def position_value(self) -> float:
        return self.position_size * self.last_price

    def copy(self) -> "DecisionState":
        return DecisionState(**{
            k: (dict(v) if k == "meta" else v)
            for k, v in self.__dict__.items()
        })
