#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : combinator.py
# @Description : 规则组合器：And / Or / Not，支持任意层级嵌套
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from app.core.factor.context import FactorContext
from app.core.rule.base import IBaseRule


class AllRule(IBaseRule):
    """全部满足（AND）。空规则列表返回 False —— 空条件不该放行任何交易。"""
    name = "all"

    def __init__(self, rules: Sequence[IBaseRule]) -> None:
        self.rules: List[IBaseRule] = list(rules)

    def is_satisfied(self, ctx: FactorContext) -> bool:
        if not self.rules:
            return False
        return all(r.is_satisfied(ctx) for r in self.rules)

    def explain(self, ctx: FactorContext) -> Dict[str, Any]:
        return {
            "rule": "all",
            "satisfied": self.is_satisfied(ctx),
            "children": [r.explain(ctx) for r in self.rules],
        }


class AnyRule(IBaseRule):
    """任一满足（OR）。空规则列表返回 False。"""
    name = "any"

    def __init__(self, rules: Sequence[IBaseRule]) -> None:
        self.rules: List[IBaseRule] = list(rules)

    def is_satisfied(self, ctx: FactorContext) -> bool:
        if not self.rules:
            return False
        return any(r.is_satisfied(ctx) for r in self.rules)

    def explain(self, ctx: FactorContext) -> Dict[str, Any]:
        return {
            "rule": "any",
            "satisfied": self.is_satisfied(ctx),
            "children": [r.explain(ctx) for r in self.rules],
        }


class NotRule(IBaseRule):
    """取反（NOT）"""
    name = "not"

    def __init__(self, rule: IBaseRule) -> None:
        self.rule = rule

    def is_satisfied(self, ctx: FactorContext) -> bool:
        return not self.rule.is_satisfied(ctx)

    def explain(self, ctx: FactorContext) -> Dict[str, Any]:
        return {
            "rule": "not",
            "satisfied": self.is_satisfied(ctx),
            "children": [self.rule.explain(ctx)],
        }
