#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 数据采集层（Provider / Capability / Router / Canonical）
#
#                一句话概括这一层在做什么：
#
#                    外部数据源 ──Adapter──▶ 标准模型 ──▶ 因子/策略
#                                  ▲
#                              Router 决定用谁
#
#                    因子不应该知道 akshare、baostock、财联社的存在。
#
#                本包对外只暴露三样东西（其余都是内部实现）：
#                  * ``PROVIDERS`` —— 注册表：谁在、能干什么、现在健康吗；
#                  * ``ROUTER``    —— 路由器：这次用谁、失败了换谁；
#                  * ``bootstrap`` —— 把内置数据源装配起来（延迟导入重依赖）。
#
#                为什么注册不在 import 时自动完成：
#                  akshare / mootdx 这些库 import 一次要好几百毫秒，
#                  而 CLI、Web、回测有很多入口根本不需要联网取数。
#                  所以装配是**显式**的（引擎 import marketx 时会调用一次），
#                  既省启动时间，也让"到底注册了什么"有明确的时机。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.providers.market.provider.base import (
    COOLDOWN_SECONDS,
    DataCapability,
    DataProvider,
    PENALIZE,
    ProviderError,
    ProviderErrorType,
    RETRYABLE,
    Route,
    as_provider_error,
    capability,
    classify,
)
from app.providers.market.routing.config import ProviderConfig, load_config
from app.providers.market.provider.health import CircuitBreaker, HealthBook, HealthStats, ProviderState
from app.providers.market.provider.registry import ProviderRegistry
from app.providers.market.raw.raw_store import RawStore

if TYPE_CHECKING:  # pragma: no cover
    # 这两个模块反过来依赖本包的 base/health/registry，
    # 若在 import 期急切导入就会成环（见 __getattr__ 下方说明）。
    from app.providers.market.routing.quality import QualityReport, check
    from app.providers.market.routing.router import (
        DataRouter, RouteAttempt, RouteResult,
    )

#: 全局注册表（配置从 config/providers.json 读）
PROVIDERS = ProviderRegistry()
#: Raw / Bronze 层
RAW_STORE = RawStore(
    max_rows=int(PROVIDERS.config.router_value("raw_max_rows", 200)),
    enabled=bool(PROVIDERS.config.router_value("raw_enabled", True)),
)

_router_holder: Dict[str, Any] = {}
_bootstrapped = False


def _router() -> "DataRouter":
    """延迟创建全局路由器。

    为什么不能像 PROVIDERS / RAW_STORE 那样在 import 期直接建 ``ROUTER``::

        routing/router.py
          └─> from app.providers.market.provider.base import ...
                └─> 触发本文件 provider/__init__.py
                      └─> from routing.router import DataRouter   ← 环
                            （此时 router.py 才执行到第 54 行，类还没定义）

    结果是 ``import app.providers.market.routing.router`` 会不会炸，
    取决于谁先被 import。这种"能跑但脆弱"比直接崩更难排查，
    所以把 DataRouter 及其依赖推迟到真正用到的时候。
    """
    if "obj" not in _router_holder:
        from app.providers.market.routing.router import DataRouter

        _router_holder["obj"] = DataRouter(registry=PROVIDERS, raw_store=RAW_STORE)
    return _router_holder["obj"]


def bootstrap(registry: Optional[ProviderRegistry] = None,
              force: bool = False) -> ProviderRegistry:
    """装配内置数据源（幂等）。

    引擎 import ``app.providers.market.service.facade`` 时会调用一次；CLI / Web 也会各自调用，
    重复调用不会重复注册（``register`` 按名字覆盖）。
    """
    global _bootstrapped
    target = registry or PROVIDERS
    if _bootstrapped and not force and target is PROVIDERS:
        return target
    from app.providers.market.adapters import register_all

    register_all(target)
    if target is PROVIDERS:
        _bootstrapped = True
    return target


def get_router() -> "DataRouter":
    """拿路由器（**顺带保证数据源已装配**）—— 业务代码一律用这个入口。"""
    bootstrap()
    return _router()


def capabilities() -> Dict[str, List[str]]:
    """{能力: [路由名]}。"""
    bootstrap()
    return PROVIDERS.capabilities()


def reload(path: Optional[str] = None) -> ProviderConfig:
    """热重载 config/providers.json（改完配置不用重启进程）。"""
    cfg = PROVIDERS.reload_config(path)
    RAW_STORE.enabled = bool(cfg.router_value("raw_enabled", True))
    RAW_STORE.max_rows = int(cfg.router_value("raw_max_rows", 200))
    return cfg


__all__ = [
    "COOLDOWN_SECONDS",
    "CircuitBreaker",
    "DataCapability",
    "DataProvider",
    "DataRouter",
    "HealthBook",
    "HealthStats",
    "PENALIZE",
    "PROVIDERS",
    "ProviderConfig",
    "ProviderError",
    "ProviderErrorType",
    "ProviderRegistry",
    "ProviderState",
    "QualityReport",
    "RAW_STORE",
    "RETRYABLE",
    "ROUTER",
    "RawStore",
    "Route",
    "RouteAttempt",
    "RouteResult",
    "as_provider_error",
    "bootstrap",
    "capabilities",
    "capability",
    "check",
    "classify",
    "get_router",
    "load_config",
    "reload",
]


def __getattr__(name: str) -> Any:
    """延迟解析会成环的那几个符号（详见 :func:`_router` 的说明）。"""
    if name == "ROUTER":
        return _router()
    if name == "DataRouter":
        from app.providers.market.routing.router import DataRouter as _v
        return _v
    if name in ("RouteAttempt", "RouteResult"):
        from app.providers.market.routing import router as _m
        return getattr(_m, name)
    if name == "QualityReport":
        from app.providers.market.routing.quality import QualityReport as _v
        return _v
    if name == "check":
        from app.providers.market.routing.quality import check as _v
        return _v
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
