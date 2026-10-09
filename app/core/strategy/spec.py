#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : spec.py
# @Description : 从声明式配置构造策略（YAML / DB / API 都走这里）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Mapping

from app.core.rule.spec import build_rule
from app.core.strategy.base import IBaseStrategy
from app.core.strategy.registry import create_strategy


def build_strategy(spec: Mapping[str, Any]) -> IBaseStrategy:
    """从配置构造策略。

    标准格式::

        {
          "type": "combo",          # 或任意已注册策略名
          "params": {...},          # 传给策略构造函数的其它参数
          "entry": {...},           # 行情入场规则
          "exit": {...},            # 行情出场规则
          "event_entry": {...},     # 事件入场规则（可选）
          "event_exit": {...},      # 事件出场规则（可选）
        }
    """
    if not isinstance(spec, Mapping):
        raise ValueError(f"策略配置必须是 dict，收到: {type(spec).__name__}")

    stype = str(spec.get("type") or spec.get("name") or "combo")
    kwargs = dict(spec.get("params") or {})

    if "entry" in spec:
        kwargs["entry"] = build_rule(spec["entry"])
    if "exit" in spec:
        kwargs["exit"] = build_rule(spec["exit"])
    if "event_entry" in spec:
        kwargs["event_entry"] = build_rule(spec["event_entry"])
    if "event_exit" in spec:
        kwargs["event_exit"] = build_rule(spec["event_exit"])

    return create_strategy(stype, **kwargs)
