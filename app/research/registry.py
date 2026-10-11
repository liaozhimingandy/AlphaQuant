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

from app.common.config import settings
from app.common.logging import logger

# 延迟导入：backtrader 策略类只有在真正回测时才需要
_REGISTRY: Dict[str, Callable[[], type[bt.Strategy]]] = {}
_LOADED_DIRS: set = set()
_BUILTIN_LOADED = False


def _self_developed_strategies() -> Dict[str, Callable[[], type[bt.Strategy]]]:
    """把 :mod:`app.strategy` 里的自研策略桥接成 backtrader 策略。

    这是"回测即实盘"的关键一步：内置策略不再有 backtrader 专属实现，
    回测跑的就是实盘那一份代码，差异只剩撮合环境本身。
    """
    from app.research.adapter import create_bridged_strategy
    from app.strategy import list_strategies, create_strategy

    built: Dict[str, Callable[[], type[bt.Strategy]]] = {}
    for name in list_strategies():
        try:
            create_strategy(name)  # 先探一下能不能实例化，坏策略不该污染注册表
        except Exception as exc:
            logger.warning(f"策略 {name} 不可用，回测跳过: {exc}")
            continue
        built[name] = (lambda n=name: create_bridged_strategy(n))
    return built


def _load_builtin() -> None:
    """注册框架自带策略。

    必须与"加载用户策略"分开：如果 `_load()` 用 `if _REGISTRY: return` 做守卫，
    那么**先加载用户策略**时（`_REGISTRY` 已经非空）内置策略就永远补不上了 ——
    用户会看到 `strategies` 里只剩自己那两个策略，框架自带的全部消失。

    两类来源：

    * **自研策略**（首选）—— 经 ``research/adapter`` 桥接，与实盘共用同一份实现；
    * ``*_bt`` / ``precise_*`` / ``trend_*`` / ``declarative`` —— backtrader 原生实现，
      仅作对照与回归基线保留，**新代码不要再往这类里加**。
    """
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    from app.research.strategies.ma_cross import MaCrossStrategy as BtMaCross
    from app.research.strategies.spec_strategy import DeclarativeStrategy
    from app.research.strategies.strategy import PreciseMaCrossStrategyI
    from app.research.strategies.trend_ma_cross import TrendMaCrossStrategy

    # 优先放自研策略：同名时自研版胜出，legacy 一律带 _bt 后缀
    _REGISTRY.update(_self_developed_strategies())
    _REGISTRY.update(
        {
            # 带趋势过滤 + 止损的均线交叉（backtrader 原生实现）
            "ma_cross_bt": lambda: BtMaCross,
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
    from app.research.adapter import create_bridged_strategy
    from app.strategy.discovery import collect_subclasses, iter_user_modules

    # 先确保内置策略已注册 —— 否则"先加载用户策略"会让内置的全部缺失
    _load_builtin()

    targets = tuple(str(d) for d in (dirs or settings.user_strategy_dirs()))
    if not force and targets in _LOADED_DIRS:
        return {}
    if not settings.USER_STRATEGY_AUTOLOAD and not force:
        return {}

    from app.strategy.base import IBaseStrategy

    found: Dict[str, type] = {}
    for module in iter_user_modules(dirs, force=force):
        # 新写法：继承 IBaseStrategy —— 回测与实盘通用，优先
        for key, klass in collect_subclasses(module, IBaseStrategy).items():
            proxy = create_bridged_strategy(key)
            found[key] = proxy
            _REGISTRY[key] = (lambda k=proxy: k)
            _USER_NAMES.add(key)
        # 旧写法：直接继承 bt.Strategy —— 只能回测，兼容保留
        for key, klass in collect_subclasses(module, bt.Strategy).items():
            if key in found:
                continue
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
