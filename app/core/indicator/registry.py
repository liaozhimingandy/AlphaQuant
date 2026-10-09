#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 指标注册表：让"加一个指标"变成一行装饰器，不用改任何框架代码
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Type, TypeVar

from app.core.indicator.base import IBaseIndicator
from app.utils.logger import logger

T = TypeVar("T", bound=IBaseIndicator)

_INDICATORS: Dict[str, Type[IBaseIndicator]] = {}


def register_indicator(
    cls: Optional[Type[T]] = None, *, name: Optional[str] = None
) -> Callable:
    """注册指标类。可用作装饰器或普通函数。

    用法::

        @register_indicator
        class MyIndicator(IBaseIndicator):
            name = "my_ind"
            ...
    """

    def _do(klass: Type[T]) -> Type[T]:
        key = name or getattr(klass, "name", "")
        if not key:
            raise ValueError(f"指标 {klass.__name__} 必须定义 name 或显式传入 name=")
        if key in _INDICATORS and _INDICATORS[key] is not klass:
            logger.warning(f"指标 {key} 被覆盖: {_INDICATORS[key]} -> {klass}")
        _INDICATORS[key] = klass
        return klass

    return _do(cls) if cls is not None else _do


def create_indicator(name: str, **params) -> IBaseIndicator:
    """按名称创建指标实例。未注册时抛 KeyError，调用方负责降级。"""
    klass = _INDICATORS.get(name)
    if klass is None:
        raise KeyError(
            f"未注册的指标: {name} | 可用: {', '.join(sorted(_INDICATORS))}"
        )
    return klass(**params)


def has_indicator(name: str) -> bool:
    return name in _INDICATORS


def list_indicators() -> List[str]:
    return sorted(_INDICATORS)


def indicator_registry() -> Dict[str, Type[IBaseIndicator]]:
    return dict(_INDICATORS)


def clear_indicators() -> None:
    """仅供测试使用。"""
    _INDICATORS.clear()
