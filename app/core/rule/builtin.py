#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : builtin.py
# @Description : 内置规则：阈值比较 / 穿越判定 / 常量
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import operator
from typing import Any, Callable, Dict, Optional

from app.core.factor.base import IBaseFactor
from app.core.factor.context import FactorContext
from app.core.factor.registry import create_factor
from app.core.rule.base import IBaseRule

_OPS: Dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    ">": operator.gt,
    "gte": operator.ge,
    ">=": operator.ge,
    "lt": operator.lt,
    "<": operator.lt,
    "lte": operator.le,
    "<=": operator.le,
    "eq": operator.eq,
    "==": operator.eq,
    "ne": operator.ne,
    "!=": operator.ne,
}


def _resolve_factor(factor: str | IBaseFactor, params: Dict[str, Any]) -> IBaseFactor:
    if isinstance(factor, IBaseFactor):
        return factor
    return create_factor(factor, **params)


class ThresholdRule(IBaseRule):
    """因子值与阈值比较。

    关键约定：**因子值为 None 时判定为不满足**。
    指标预热期（比如前 20 根K线没有 MA20）必须表现为"不触发"，
    否则会被当成 0 而误判成"价格远低于均线"之类的强信号。
    """
    name = "threshold"

    def __init__(
        self,
        factor: str | IBaseFactor,
        op: str = "gt",
        threshold: float = 0.0,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.factor = _resolve_factor(factor, params or {})
        self.op = op
        self.threshold = float(threshold)
        if op not in _OPS:
            raise ValueError(f"不支持的比较符: {op} | 可用: {sorted(_OPS)}")

    @property
    def factor_name(self) -> str:
        return self.factor.name

    def value(self, ctx: FactorContext) -> Optional[float]:
        v = self.factor.compute(ctx)
        if v is None:
            return None
        # bool 型因子统一转成 1/0，便于参与阈值比较
        return float(v)

    def is_satisfied(self, ctx: FactorContext) -> bool:
        v = self.value(ctx)
        if v is None:
            return False
        return _OPS[self.op](v, self.threshold)

    def explain(self, ctx: FactorContext) -> Dict[str, Any]:
        return {
            "rule": "threshold",
            "factor": self.factor_name,
            "op": self.op,
            "threshold": self.threshold,
            "value": self.value(ctx),
            "satisfied": self.is_satisfied(ctx),
        }


class _CrossRule(IBaseRule):
    """穿越判定基类：比较两根K线上 left 与 right 的差值符号是否翻转。"""

    direction: str = "up"

    def __init__(
        self,
        left: str | IBaseFactor,
        right: str | IBaseFactor,
        left_params: Optional[Dict[str, Any]] = None,
        right_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.left = _resolve_factor(left, left_params or {})
        self.right = _resolve_factor(right, right_params or {})

    def _diff(self, ctx: Optional[FactorContext]) -> Optional[float]:
        if ctx is None:
            return None
        a = self.left.compute(ctx)
        b = self.right.compute(ctx)
        if a is None or b is None:
            return None
        return float(a) - float(b)

    def is_satisfied(self, ctx: FactorContext) -> bool:
        now = self._diff(ctx)
        prev_ctx = ctx.previous(1)
        before = self._diff(prev_ctx)
        if now is None or before is None:
            return False
        return (
            before <= 0 < now if self.direction == "up" else before >= 0 > now
        )

    def explain(self, ctx: FactorContext) -> Dict[str, Any]:
        return {
            "rule": f"cross_{self.direction}",
            "left": self.left.name,
            "right": self.right.name,
            "diff_now": self._diff(ctx),
            "diff_prev": self._diff(ctx.previous(1)),
            "satisfied": self.is_satisfied(ctx),
        }


class CrossUpRule(_CrossRule):
    """金叉：left 上穿 right"""
    name = "cross_up"
    direction = "up"


class CrossDownRule(_CrossRule):
    """死叉：left 下穿 right"""
    name = "cross_down"
    direction = "down"


class AlwaysRule(IBaseRule):
    """恒真。用于"无条件"场景（例如纯新闻驱动的策略），也便于测试。"""
    name = "always"

    def is_satisfied(self, ctx: FactorContext) -> bool:
        return True


class NeverRule(IBaseRule):
    """恒假。用于临时禁用某条规则而不用删配置。"""
    name = "never"

    def is_satisfied(self, ctx: FactorContext) -> bool:
        return False
