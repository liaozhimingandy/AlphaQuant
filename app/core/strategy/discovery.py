#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : discovery.py
# @Description : 用户策略发现：把 strategies/ 目录下的 .py 自动加载进来
#
#                两条链路共用这一套发现机制：
#                  - 回测链路（backtrader）：找 bt.Strategy 子类
#                  - 实盘链路（事件引擎）：找 IBaseStrategy 子类
#                同一个文件里可以两者都有 —— 一份策略逻辑用在两个地方，
#                这正是"回测验证过的策略直接上实盘"该有的样子。
#
#                放在独立目录而不是塞进 app/：升级框架不会覆盖用户代码。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Sequence, Type

from app.core.config import settings
from app.utils.logger import logger


def snake(name: str) -> str:
    """CamelCase → snake_case。"""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", str(name))
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def strategy_key(cls: Type[Any]) -> str:
    """给策略类起一个注册名。

    优先显式声明（``STRATEGY_NAME`` / ``strategy_name``），否则由类名推导：
    ``DualMaStrategy`` → ``dual_ma``。显式声明更好 —— 类名一改，
    配置文件里的引用就断了，而显式名字是稳定的。
    """
    for attr in ("STRATEGY_NAME", "strategy_name"):
        value = getattr(cls, attr, "")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    name = str(cls.__name__)
    if name.endswith("Strategy"):
        name = name[: -len("Strategy")]
    return snake(name).strip("_").lower()


def user_strategy_dirs(dirs: Optional[Sequence[str | Path]] = None) -> List[Path]:
    if dirs is not None:
        return [Path(d) for d in dirs]
    return settings.user_strategy_dirs()


def _import_file(path: Path, force: bool = False) -> Optional[ModuleType]:
    """按文件路径导入模块。

    模块名带上路径指纹：不同目录下的同名文件（比如两份 ``my_strategy.py``）
    必须互不覆盖 —— 否则后加载的会顶掉先加载的，排查起来莫名其妙。
    """
    mod_name = f"_aq_user_{path.stem}_{abs(hash(str(path.resolve()))) % 10 ** 9}"
    if mod_name in sys.modules and not force:
        return sys.modules[mod_name]
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            logger.warning(f"无法加载用户策略文件: {path}")
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        # 一个坏文件不该拖垮整个进程 —— 但必须报出来，否则用户会以为"我的策略没被发现"
        logger.error(f"加载用户策略 {path.name} 失败: {exc}", exc_info=True)
        sys.modules.pop(mod_name, None)
        return None


def iter_user_modules(
    dirs: Optional[Sequence[str | Path]] = None, force: bool = False
) -> Iterable[ModuleType]:
    """逐个导入用户策略文件（跳过 ``_`` 开头的文件与 ``__init__.py``）。"""
    for base in user_strategy_dirs(dirs):
        p = Path(base)
        if not p.is_dir():
            continue
        for f in sorted(p.glob("*.py")):
            if f.name.startswith("_"):
                continue
            module = _import_file(f, force=force)
            if module is not None:
                yield module


def collect_subclasses(
    module: ModuleType, base: Type[Any], exclude: Sequence[Type[Any]] = ()
) -> Dict[str, Type[Any]]:
    """从模块里挑出 ``base`` 的直接/间接子类（排除框架自带的那些）。"""
    out: Dict[str, Type[Any]] = {}
    for value in vars(module).values():
        if not isinstance(value, type):
            continue
        if value in exclude:
            continue
        if not issubclass(value, base) or value is base:
            continue
        # 只认"在本模块里定义的类"，避免把 import 进来的框架类重复注册
        if getattr(value, "__module__", "") != module.__name__:
            continue
        out[strategy_key(value)] = value
    return out


__all__ = [
    "collect_subclasses",
    "iter_user_modules",
    "snake",
    "strategy_key",
    "user_strategy_dirs",
]
