#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : base.py
# @Description : 风控层基类：风控是"闸门"，不是"过滤器"
#               任何一道风控否决，该信号就不能成交；否决原因必须可追溯
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core.factor.context import FactorContext
from app.core.market.types import Account, Signal
from app.core.strategy.state import DecisionState


@dataclass
class RiskVerdict:
    """单条风控的判定结果。"""
    allowed: bool
    rule: str = ""
    reason: str = ""
    #: 风控可以把仓位整体缩放（0~1），例如回撤过大时只开半仓
    scale: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "rule": self.rule,
            "reason": self.reason,
            "scale": self.scale,
            "meta": dict(self.meta),
        }


class IBaseRiskRule(abc.ABC):
    """风控规则。

    设计原则：
      1. **只读**：风控不产生交易，只否决/缩放下级信号
      2. **默认放行**：拿不到数据时放行，避免风控把整个系统锁死。
         真正需要"数据缺失即拒绝"的场景，用单独的 `require_positive_data` 规则显式声明
      3. **失败可追溯**：reason 会写进订单的 reject_reason
    """

    name: str = ""
    description: str = ""

    def __init__(self, **params: Any) -> None:
        self.params: Dict[str, Any] = params

    @abc.abstractmethod
    def check(
        self,
        signal: Signal,
        state: DecisionState,
        ctx: Optional[FactorContext] = None,
        account: Optional[Account] = None,
    ) -> RiskVerdict:
        raise NotImplementedError

    def _pass(self, reason: str = "", scale: float = 1.0) -> RiskVerdict:
        return RiskVerdict(True, rule=self.name, reason=reason, scale=scale)

    def _block(self, reason: str) -> RiskVerdict:
        return RiskVerdict(False, rule=self.name, reason=reason)
