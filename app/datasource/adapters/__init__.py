#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 所有内置数据源 Adapter
#
#                **加一个新数据源只需要两步**：
#                  1. 在本目录加一个文件，写一个 DataProvider 子类，
#                     用 @capability 声明它到底能提供什么；
#                  2. 把它的类名加进下面的 :data:`BUILTIN_PROVIDERS`。
#
#                路由、熔断、排序、质量校验、原始存档全部自动生效，
#                不需要改引擎/策略/因子的任何一行 ——
#                这就是"数据源是可替换的基础设施"这件事的兑现方式。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, List, Optional, Type

from app.datasource.provider.base import DataProvider
from app.datasource.provider.registry import ProviderRegistry

#: 内置 Provider 的顺序 = 注册顺序（真正决定先后的是配置里的 routes 与实测健康分）
BUILTIN_PROVIDERS: List[Type[DataProvider]] = []


def _load() -> List[Type[DataProvider]]:
    """延迟导入：适配器内部才去 import akshare/mootdx 这些重依赖。"""
    from app.datasource.adapters.akshare import AkShareProvider
    from app.datasource.adapters.astockdata import AStockDataProvider
    from app.datasource.adapters.baostock import BaoStockProvider
    from app.datasource.adapters.eastmoney import EastMoneyProvider
    from app.datasource.adapters.local import LocalProvider
    from app.datasource.adapters.mootdx import MootdxProvider
    from app.datasource.adapters.tushare import TushareProvider
    from app.datasource.adapters.xueqiu import XueqiuProvider

    return [
        BaoStockProvider,
        AkShareProvider,
        EastMoneyProvider,
        XueqiuProvider,
        AStockDataProvider,
        MootdxProvider,
        TushareProvider,
        LocalProvider,
    ]


def register_all(registry: Optional[ProviderRegistry] = None,
                 extra: Optional[List[Any]] = None) -> ProviderRegistry:
    """把全部内置 Provider 注册进注册表（幂等）。"""
    if registry is None:
        from app.datasource.provider import PROVIDERS

        registry = PROVIDERS
    for cls in list(_load()) + list(extra or []):
        try:
            registry.register(cls)
        except Exception as exc:  # 单个源注册失败不能拖垮整条采集链
            from app.common.logging import logger

            logger.warning(f"注册数据源 {getattr(cls, 'name', cls)} 失败: "
                           f"{type(exc).__name__}: {exc}")
    return registry


__all__ = ["BUILTIN_PROVIDERS", "register_all"]
