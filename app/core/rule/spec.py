#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : spec.py
# @Description : 规则/策略的声明式构造：把 dict（来自 YAML/DB/API）变成可执行对象
#               这是"策略可自由组合、可热加载"的关键——改策略不用改代码
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Union

from app.core.rule.base import IBaseRule
from app.core.rule.builtin import (
    AlwaysRule,
    CrossDownRule,
    CrossUpRule,
    NeverRule,
    ThresholdRule,
)
from app.core.rule.combinator import AllRule, AnyRule, NotRule

RuleSpec = Union[str, Mapping[str, Any]]


def build_rule(spec: RuleSpec) -> IBaseRule:
    """从声明式配置构造规则。

    支持的形式::

        {"all": [ ... , ... ]}
        {"any": [ ... ]}
        {"not": {...}}
        {"factor": "ma_spread", "op": ">", "value": 0.02, "params": {"fast": 5, "slow": 20}}
        {"cross_up":  {"left": "ma", "right": "ma",
                       "left_params": {"period": 5}, "right_params": {"period": 20}}}
        {"cross_down": {...}}
        "always" | "never"
    """
    if spec is None:
        raise ValueError("规则配置不能为空")

    # 字符串简写
    if isinstance(spec, str):
        key = spec.strip().lower()
        if key == "always":
            return AlwaysRule()
        if key == "never":
            return NeverRule()
        raise ValueError(f"无法识别的规则简写: {spec}")

    if not isinstance(spec, Mapping):
        raise ValueError(f"规则配置必须是 dict 或 str，收到: {type(spec).__name__}")

    if "all" in spec:
        return AllRule([build_rule(s) for s in _as_list(spec["all"])])
    if "any" in spec:
        return AnyRule([build_rule(s) for s in _as_list(spec["any"])])
    if "not" in spec:
        return NotRule(build_rule(spec["not"]))

    if "factor" in spec:
        return ThresholdRule(
            factor=spec["factor"],
            op=str(spec.get("op", "gt")),
            threshold=float(spec.get("value", spec.get("threshold", 0.0))),
            params=dict(spec.get("params") or {}),
        )

    for key, klass in (("cross_up", CrossUpRule), ("cross_down", CrossDownRule)):
        if key in spec:
            body = spec[key] or {}
            if not isinstance(body, Mapping):
                raise ValueError(f"{key} 的配置必须是 dict")
            return klass(
                left=body.get("left"),
                right=body.get("right"),
                left_params=dict(body.get("left_params") or {}),
                right_params=dict(body.get("right_params") or {}),
            )

    if spec.get("always") is True:
        return AlwaysRule()
    if spec.get("never") is True:
        return NeverRule()

    raise ValueError(f"无法识别的规则配置: {dict(spec)}")


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]
