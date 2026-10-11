#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 服务层 —— ServiceComponent 的具体实现
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

这一层放的是**服务**：常驻、对外提供能力、被别人依赖的组件。

对应架构图：:

    IBaseComponent
    └── ServiceComponent
          ├── MarketService        market.py      行情
          ├── CollectionService    collection.py  采集
          ├── TradingService       trading.py     交易
          ├── RiskService          risk.py        风控
          └── PortfolioService     portfolio.py   组合（账户聚合 + 额度预留）

（另有一个 ``NewsService`` 也是服务，只是不在上面那张图的四项里 ——
  资讯确实是引擎对外提供的一项能力，把它降级成"组件"反而说不通。
  ``PortfolioService`` 同理：它不在最初那张图的四项里，但没有它就没有
  "账户"这个概念，风控只能在单个任务的账本上做判断。）

判断一个东西该不该放在这里，只有一条标准：

    **它消费别人的输出吗？**
    不消费、只提供 → 服务，放这里。
    要消费行情/风控/账户 → 那是策略或工作单元，不该放这里。

这条标准是硬的。以前采集、行情、交易、策略全平铺在一个目录里，
就是因为没人问过这个问题，于是"谁先启动"只能靠注册顺序碰运气。
"""

from .market import MarketService, df_to_bars
from .collection import CollectionService, parse_interval
from .trading import TradingService
from .risk import RiskService
from .portfolio import PortfolioService, Reservation
from .news import NewsService

__all__ = [
    "MarketService",
    "df_to_bars",
    "CollectionService",
    "parse_interval",
    "TradingService",
    "RiskService",
    "PortfolioService",
    "Reservation",
    "NewsService",
]
