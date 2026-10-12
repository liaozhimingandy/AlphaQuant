#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : Engine —— 容器层入口（延迟导出，避免半个import环）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
Engine（容器层）
===============

Engine 代表**一个完整的量化运行环境**，而不是一个账户。它只负责：

    生命周期     initialize / start / stop
    模块装配     builder.build_engine()：配置 → 组件
    依赖解析     registry.ServiceRegistry：按 Cap 能力做拓扑排序与注入
    能力契约     capability.Cap：组件能声明自己"提供什么 / 需要什么"

业务实现不在本包内 —— 它们已归位到 ``app/modules/`` 与 ``app/runtime/``。

为什么本文件用延迟导出
----------------------

``app/engine/__init__.py`` 在 import 期**急切**加载 ``engine.py`` 会成环::

    app.runtime.scheduler
      └─> from app.engine.capability import ...
            └─> 触发 app.engine.__init__
                  └─> from app.engine.engine import ...
                        └─> from app.runtime.scheduler import SchedulerComponent   ← 环
                              （此时 scheduler 才执行到第 45 行，类还没定义）

结果就是 `from app.runtime.scheduler import SchedulerComponent` 是否报错，
取决于谁先被 import —— 这种"能跑但脆弱"比直接崩更难排查。

改成 PEP 562 延迟导出后，import ``app.engine`` 不做任何重活，
符号在真正访问时才解析，环自然消失。

::

    from app.engine import BaseQuantEngine          # 仍然可用（延迟解析）
    from app.engine.engine import BaseQuantEngine   # 显式路径，更快更明确
"""
from typing import TYPE_CHECKING, Any

__all__ = ["async_sleep", "IQuantEngine", "BaseQuantEngine", "EngineService"]

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期
    from app.engine.engine import BaseQuantEngine, IQuantEngine
    from app.engine.service import EngineService
    from app.runtime.utils import async_sleep


def __getattr__(name: str) -> Any:
    """延迟解析包级符号，避免 import 期就把整个容器拉起来。"""
    if name == "async_sleep":
        from app.runtime.utils import async_sleep as _v
        return _v
    if name in ("IQuantEngine", "BaseQuantEngine"):
        from app.engine.engine import BaseQuantEngine as _B, IQuantEngine as _I
        return _I if name == "IQuantEngine" else _B
    if name == "EngineService":
        from app.engine.service import EngineService as _v
        return _v
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
