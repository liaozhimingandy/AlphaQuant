#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 策略注册表：新增策略 = 一个类 + 一个装饰器
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Type, TypeVar

from app.core.strategy.base import IBaseStrategy
from app.utils.logger import logger

T = TypeVar("T", bound=IBaseStrategy)

_STRATEGIES: Dict[str, Type[IBaseStrategy]] = {}


def register_strategy(
    cls: Optional[Type[T]] = None, *, name: Optional[str] = None
) -> Callable:
    """注册策略类（可装饰器/可函数调用）。"""

    def _do(klass: Type[T]) -> Type[T]:
        key = name or getattr(klass, "name", "")
        if not key:
            raise ValueError(f"策略 {klass.__name__} 必须定义 name 或显式传入 name=")
        if key in _STRATEGIES and _STRATEGIES[key] is not klass:
            logger.warning(f"策略 {key} 被覆盖: {_STRATEGIES[key]} -> {klass}")
        _STRATEGIES[key] = klass
        return klass

    return _do(cls) if cls is not None else _do


def create_strategy(name: str, **params) -> IBaseStrategy:
    klass = _STRATEGIES.get(name)
    if klass is None:
        raise KeyError(
            f"未注册的策略: {name} | 可用: {', '.join(sorted(_STRATEGIES))}"
        )
    return klass(**params)


def has_strategy(name: str) -> bool:
    return name in _STRATEGIES


def list_strategies() -> List[str]:
    return sorted(_STRATEGIES)


def strategy_registry() -> Dict[str, Type[IBaseStrategy]]:
    return dict(_STRATEGIES)


def clear_strategies() -> None:
    """仅供测试使用。"""
    _STRATEGIES.clear()
