#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 策略注册表：回测按名字取策略，CLI 可自动列出可选策略
#                除了框架自带策略，还会自动发现用户策略（strategies/ 目录）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

import backtrader as bt

from app.core.config import settings
from app.utils.logger import logger

# 延迟导入：backtrader 策略类只有在真正回测时才需要
_REGISTRY: Dict[str, Callable[[], type[bt.Strategy]]] = {}
_LOADED_DIRS: set = set()
_BUILTIN_LOADED = False


def _load_builtin() -> None:
    """注册框架自带策略。

    必须与"加载用户策略"分开：如果 `_load()` 用 `if _REGISTRY: return` 做守卫，
    那么**先加载用户策略**时（`_REGISTRY` 已经非空）内置策略就永远补不上了 ——
    用户会看到 `strategies` 里只剩自己那两个策略，框架自带的全部消失。
    """
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    from app.strategy.ma_cross import MaCrossStrategy
    from app.strategy.spec_strategy import DeclarativeStrategy
    from app.strategy.strategy import PreciseMaCrossStrategyI
    from app.strategy.trend_ma_cross import TrendMaCrossStrategy

    _REGISTRY.update(
        {
            # 带趋势过滤 + 止损的均线交叉
            "ma_cross": lambda: MaCrossStrategy,
            # 因子/规则可组合版：仅金叉买、仅死叉卖
            "precise_ma_cross": lambda: PreciseMaCrossStrategyI,
            # 三均线趋势跟随
            "trend_ma_cross": lambda: TrendMaCrossStrategy,
            # 声明式：把 entry/exit 规则写成 JSON/配置即可，零代码
            "declarative": lambda: DeclarativeStrategy,
        }
    )
    _BUILTIN_LOADED = True


def _load() -> None:
    _load_builtin()
    load_user_strategies()


def register_strategy(name: str, factory: Callable[[], type[bt.Strategy]]) -> None:
    """注册自定义策略：register_strategy("my_strategy", lambda: MyStrategy)"""
    _load_builtin()
    if name in _REGISTRY:
        logger.warning(f"策略 {name} 已存在，将被覆盖")
    _REGISTRY[name] = factory


def get_strategy(name: str) -> type[bt.Strategy]:
    _load()
    if name not in _REGISTRY:
        raise KeyError(
            f"未找到策略: {name} | 可用策略: {list_strategies()}"
        )
    return _REGISTRY[name]()


def list_strategies() -> List[str]:
    _load()
    return sorted(_REGISTRY.keys())


def user_strategy_names() -> List[str]:
    """用户自定义策略名（面板上打"自定义"标签用）。"""
    _load()
    return [n for n in sorted(_REGISTRY.keys()) if n in _USER_NAMES]


#: 用户策略名集合（面板上用来打"自定义"标签）
_USER_NAMES: set = set()


def load_user_strategies(
    dirs: Optional[Sequence[str]] = None, force: bool = False
) -> Dict[str, type]:
    """发现用户策略目录里的 backtrader 策略并注册。

    发现失败（语法错误、依赖缺失）只跳过该文件并记日志 ——
    一个写坏的策略文件不该让整个回测功能不可用。
    """
    from app.core.strategy.discovery import collect_subclasses, iter_user_modules

    # 先确保内置策略已注册 —— 否则"先加载用户策略"会让内置的全部缺失
    _load_builtin()

    targets = tuple(str(d) for d in (dirs or settings.user_strategy_dirs()))
    if not force and targets in _LOADED_DIRS:
        return {}
    if not settings.USER_STRATEGY_AUTOLOAD and not force:
        return {}

    found: Dict[str, type] = {}
    for module in iter_user_modules(dirs, force=force):
        for key, klass in collect_subclasses(module, bt.Strategy).items():
            found[key] = klass
            _REGISTRY[key] = (lambda klass=klass: klass)
            _USER_NAMES.add(key)

    _LOADED_DIRS.add(targets)
    if found:
        logger.info(
            f"已加载 {len(found)} 个用户策略（回测链路）: {', '.join(sorted(found))}"
        )
    return found


def user_strategy_dir() -> str:
    return str(settings.USER_STRATEGY_DIR)


__all__ = [
    "get_strategy",
    "list_strategies",
    "load_user_strategies",
    "register_strategy",
    "user_strategy_dir",
    "user_strategy_names",
]
