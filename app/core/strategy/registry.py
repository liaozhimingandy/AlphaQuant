#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 策略注册表：新增策略 = 一个类 + 一个装饰器
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Type, TypeVar

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
    _ensure_user_strategies()
    klass = _STRATEGIES.get(name)
    if klass is None:
        raise KeyError(
            f"未注册的策略: {name} | 可用: {', '.join(sorted(_STRATEGIES))}"
        )
    return klass(**params)


def has_strategy(name: str) -> bool:
    _ensure_user_strategies()
    return name in _STRATEGIES


def list_strategies() -> List[str]:
    _ensure_user_strategies()
    return sorted(_STRATEGIES)


def strategy_registry() -> Dict[str, Type[IBaseStrategy]]:
    return dict(_STRATEGIES)


def clear_strategies() -> None:
    """仅供测试使用。"""
    _STRATEGIES.clear()


# ===========================================================================
# 用户自定义策略
# ===========================================================================
#: 记住哪些目录已经加载过，避免每次列清单都重新 import 一遍
_LOADED_DIRS: set = set()
_LOAD_FAILED: List[str] = []


def load_user_strategies(
    dirs: Optional[Sequence[str]] = None, force: bool = False, register: bool = True
) -> Dict[str, Type[IBaseStrategy]]:
    """发现并注册用户策略（事件引擎链路）。

    :param force: 重新导入文件（改完策略不想重启服务时用）
    :return: 本次发现的 {名字: 类}；已加载过的目录默认跳过

    一个用户文件里可以同时定义 backtrader 策略和 IBaseStrategy 策略 ——
    各注册表只挑自己认识的那些。这样一个策略逻辑能在回测与实盘上用同一份源码。
    """
    from app.core.strategy.discovery import collect_subclasses, iter_user_modules
    from app.core.config import settings

    found: Dict[str, Type[IBaseStrategy]] = {}
    # 归一成"实际要扫的目录列表"再比对：用 None 调用时也要能记住"已扫过"，
    # 否则每次列个策略清单都会重新走一遍发现流程（日志也会重复刷）。
    targets = tuple(str(d) for d in (dirs or settings.user_strategy_dirs()))

    if not force and targets in _LOADED_DIRS:
        return found

    for module in iter_user_modules(dirs, force=force):
        for key, klass in collect_subclasses(module, IBaseStrategy).items():
            found[key] = klass
            if register:
                register_strategy(klass, name=key)

    _LOADED_DIRS.add(targets)
    if found:
        logger.info(
            f"已加载 {len(found)} 个用户策略（事件引擎链路）: {', '.join(sorted(found))}"
        )
    return found


def _ensure_user_strategies() -> None:
    """惰性自动发现。用配置开关控制，测试环境可关掉。"""
    from app.core.config import settings

    if not settings.USER_STRATEGY_AUTOLOAD:
        return
    try:
        load_user_strategies()
    except Exception as exc:  # pragma: no cover - 用户策略目录出问题不该拖垮主流程
        logger.warning(f"用户策略自动发现失败（忽略）: {exc}")
