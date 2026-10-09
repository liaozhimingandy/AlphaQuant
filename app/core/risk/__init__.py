#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 风控层：可插拔闸门链
#                数据流: Signal -> RiskChain -> (allowed?, scale)
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.core.risk.base import IBaseRiskRule, RiskVerdict
from app.core.risk.registry import (
    RiskChain,
    build_risk_chain,
    create_risk,
    get_risk,
    list_risks,
    register_risk,
)
from app.core.risk import builtin  # noqa: F401  触发内置风控注册

__all__ = [
    "IBaseRiskRule",
    "RiskVerdict",
    "RiskChain",
    "build_risk_chain",
    "create_risk",
    "get_risk",
    "list_risks",
    "register_risk",
]
