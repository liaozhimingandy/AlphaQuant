#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 因子层导出，自动注册内置因子
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.core.factor.base import IBaseFactor
from app.core.factor.context import FactorContext
from app.core.factor.registry import (
    clear_factors,
    create_factor,
    factor_registry,
    has_factor,
    list_factors,
    register_factor,
)

# 导入即注册内置因子（装饰器在 import 时生效）
import app.core.factor.builtin  # noqa: F401
from app.core.factor.builtin import BUILTIN_FACTORS

__all__ = [
    "IBaseFactor",
    "FactorContext",
    "register_factor",
    "create_factor",
    "has_factor",
    "list_factors",
    "factor_registry",
    "clear_factors",
    "BUILTIN_FACTORS",
]
