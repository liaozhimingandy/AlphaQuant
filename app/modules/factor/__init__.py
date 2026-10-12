#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L4 特征层 —— 把 Storage 里的标准数据加工成可被策略消费的特征。

内部三段流水线，层层降维：

    indicator（向量）  compute(BarSeries) -> ndarray      等长，前导 NaN
        ↓ FactorContext 降维
    factor（标量）     compute(ctx) -> Optional[float]    单点取值，None=算不出
        ↓ ScoreEngine 加权
    alpha（分数）      0~100 综合分

三者不是重复实现：同样的 MA，indicator 给你整条曲线用于画图，
factor 只关心最新值用于判定，两者由 FactorContext 桥接。
"""
from app.modules.factor.base import IBaseFactor
from app.modules.factor.context import FactorContext
from app.modules.factor.registry import (
    clear_factors,
    create_factor,
    factor_registry,
    has_factor,
    list_factors,
    register_factor,
)

# 导入即注册内置因子（装饰器在 import 时生效）
import app.modules.factor.builtin  # noqa: F401
# 资金流/筹码/题材/涨停因子：读日频截面数据（ctx.features），不在 BarSeries 里
import app.modules.factor.flow  # noqa: F401
from app.modules.factor.builtin import BUILTIN_FACTORS
from app.modules.factor.flow import FLOW_FACTORS
from app.modules.factor.library import get_library

# 因子库里的表达式因子也在这里注册 —— 界面/CLI 上写的因子，
# 启动后立刻就能被规则、策略、回测引用，不需要改代码。
# 这一步**失败不能影响启动**：库文件写坏了只是少几个自定义因子。
try:
    get_library()
except Exception as _exc:  # pragma: no cover
    from app.common.logging import logger as _logger

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
