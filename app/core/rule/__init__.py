#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 规则层导出
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.core.rule.base import IBaseRule
from app.core.rule.combinator import AllRule, AnyRule, NotRule
from app.core.rule.builtin import (
    AlwaysRule,
    CrossDownRule,
    CrossUpRule,
    NeverRule,
    ThresholdRule,
)
from app.core.rule.spec import RuleSpec, build_rule

#: 声明式配置可识别的规则类型（供 CLI/文档自描述，新增规则时同步这里）
RULE_TYPES = (
    "all",
    "any",
    "not",
    "factor",       # 阈值比较
    "cross_up",     # 金叉
    "cross_down",   # 死叉
    "always",
    "never",
)


def list_rules() -> tuple:
    return RULE_TYPES


__all__ = [
    "IBaseRule",
    "AllRule",
    "AnyRule",
    "NotRule",
    "AlwaysRule",
    "NeverRule",
    "ThresholdRule",
    "CrossUpRule",
    "CrossDownRule",
    "build_rule",
    "RuleSpec",
    "RULE_TYPES",
    "list_rules",
]
