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
# 资金流/筹码/题材/涨停因子。它们读的是日频截面数据（ctx.features），
# 不在 BarSeries 里，所以单独成模块 —— 但注册表和规则层完全共用。
import app.core.factor.flow  # noqa: F401
from app.core.factor.builtin import BUILTIN_FACTORS
from app.core.factor.flow import FLOW_FACTORS
from app.core.factor.library import get_library

# 因子库里的表达式因子也在这里注册 —— 界面/CLI 上写的因子，
# 启动后立刻就能被规则、策略、回测引用，不需要改代码。
# 这一步**失败不能影响启动**：库文件写坏了只是少几个自定义因子，
# 框架自带的指标与因子必须照样能用。
try:
    get_library()
except Exception as _exc:  # pragma: no cover
    from app.utils.logger import logger as _logger

    _logger.warning(f"因子库加载失败（忽略，不影响内置因子）: {_exc}")

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
    "FLOW_FACTORS",
    "get_library",
]
