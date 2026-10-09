#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 因子注册表：新增一个因子 = 一个类 + 一个装饰器，框架零改动
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Type, TypeVar

from app.core.factor.base import IBaseFactor
from app.utils.logger import logger

T = TypeVar("T", bound=IBaseFactor)

_FACTORS: Dict[str, Type[IBaseFactor]] = {}


def register_factor(
    cls: Optional[Type[T]] = None, *, name: Optional[str] = None
) -> Callable:
    """注册因子类。

    用法::

        @register_factor
        class MyFactor(IBaseFactor):
            name = "my_factor"
            def compute(self, ctx):
                return ctx.close / ctx.ind("sma", period=20)
    """

    def _do(klass: Type[T]) -> Type[T]:
        key = name or getattr(klass, "name", "")
        if not key:
            raise ValueError(f"因子 {klass.__name__} 必须定义 name 或显式传入 name=")
        if key in _FACTORS and _FACTORS[key] is not klass:
            logger.warning(f"因子 {key} 被覆盖: {_FACTORS[key]} -> {klass}")
        _FACTORS[key] = klass
        return klass

    return _do(cls) if cls is not None else _do


def create_factor(name: str, **params) -> IBaseFactor:
    klass = _FACTORS.get(name)
    if klass is None:
        raise KeyError(f"未注册的因子: {name} | 可用: {', '.join(sorted(_FACTORS))}")
    return klass(**params)


def has_factor(name: str) -> bool:
    return name in _FACTORS


def list_factors() -> List[str]:
    return sorted(_FACTORS)


def factor_registry() -> Dict[str, Type[IBaseFactor]]:
    return dict(_FACTORS)


def clear_factors() -> None:
    """仅供测试使用。"""
    _FACTORS.clear()
