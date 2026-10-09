#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 指标层导出，并自动注册内置指标
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.core.indicator.base import IBaseIndicator
from app.core.indicator.registry import (
    clear_indicators,
    create_indicator,
    has_indicator,
    indicator_registry,
    list_indicators,
    register_indicator,
)

# 自动注册内置指标：新增指标只要在 builtin.BUILTIN_INDICATORS 里加一行
from app.core.indicator.builtin import BUILTIN_INDICATORS

for _cls in BUILTIN_INDICATORS:
    register_indicator(_cls)

__all__ = [
    "IBaseIndicator",
    "register_indicator",
    "create_indicator",
    "has_indicator",
    "list_indicators",
    "indicator_registry",
    "clear_indicators",
    "BUILTIN_INDICATORS",
]
