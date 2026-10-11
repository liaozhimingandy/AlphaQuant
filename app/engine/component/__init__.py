#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 引擎组件：抽象（base/kinds/registry）+ 具体实现（service/…）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

目录即架构图
============

::

    component/
    ├── base.py       IBaseComponent      所有组件的统一基类
    ├── kinds.py      四种角色语义        ServiceComponent 等（本包不直接导出，
    │                                     具体组件在各自模块里继承它）
    ├── registry.py   ServiceRegistry     能力登记 + 依赖解析 + 注入
    │
    ├── service/      服务层（提供能力，被依赖）
    │   ├── market.py       MarketService
    │   ├── collection.py   CollectionService
    │   ├── trading.py      TradingService
    │   ├── risk.py         RiskService
    │   ├── portfolio.py    PortfolioService
    │   └── news.py         NewsService
    ├── strategy.py   StrategyComponent   消费能力，产出订单
    ├── worker.py     WorkerComponent     消费能力，执行作业
    └── scheduler.py  SchedulerComponent  决定执行节拍

如何从这里拿东西
----------------

``kinds`` 与具体组件**同名**（都有 ``StrategyComponent``），
所以本包只导出**具体实现**——用的人拿到的就是能直接注册进引擎的那个类。
需要基类时显式从 ``app.engine.component.kinds`` 导入::

    from app.engine.component import TradingService        # 具体服务
    from app.engine.component.kinds import ServiceComponent  # 要写新服务时

这样命名冲突只存在于源码内部（具体模块里用 ``as ...Base`` 别名处理），
不传染给调用方。

判断新东西该放哪
----------------

===========================================  ==========================
特征                                          归属
===========================================  ==========================
不消费别人输出，只对外提供能力                  ``service/``
被行情/新闻驱动，产出订单                      ``strategy.py``
被调用触发，干完就等                            ``worker.py``
决定什么时候跑                                  ``scheduler.py``
===========================================  ==========================
"""

from .base import IBaseComponent
from .kinds import (
    Cap,
    ComponentKind,
    KIND_LAYER,
    RestartPolicy,
    ServiceComponent,
)
from .registry import ServiceRegistry, ServiceRegistryError

# 具体实现（与 kinds 里的同名基类不冲突：这里导出的都是"能直接用的那个"）
from .service import (
    CollectionService,
    MarketService,
    NewsService,
    PortfolioService,
    RiskService,
    TradingService,
    df_to_bars,
    parse_interval,
)
from .strategy import StrategyComponent
from .worker import WorkerComponent
from .scheduler import SchedulerComponent

__all__ = [
    # ---- 抽象 ----
    "IBaseComponent",
    "ServiceComponent",
    "ServiceRegistry",
    "ServiceRegistryError",
    "ComponentKind",
    "KIND_LAYER",
    "Cap",
    "RestartPolicy",
    # ---- 服务层 ----
    "MarketService",
    "CollectionService",
    "TradingService",
    "RiskService",
    "PortfolioService",
    "NewsService",
    # ---- 其它三种组件 ----
    "StrategyComponent",
    "WorkerComponent",
    "SchedulerComponent",
    # ---- 辅助 ----
    "df_to_bars",
    "parse_interval",
]
