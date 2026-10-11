#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : baostock.py
# @Description : Baostock Provider —— 日线（本项目的一贯首选）
#
#                为什么它排第一：
#                  * **不依赖东财域名**（自建服务器 + 自有协议），本项目踩过
#                    "本机代理把 push2his 拦掉"的坑，换一个风控面就是实打实的冗余；
#                  * 无需 token、无需注册；
#                  * 前复权数据稳定，是回测口径的一致来源。
#
#                能力：**只有日线**。它没有资金流、没有人气榜 ——
#                这不是缺陷，是 Capability 声明，路由看到没有就不会去调它。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from app.datasource.provider.base import DataCapability, DataProvider, capability


class BaoStockProvider(DataProvider):
    """Baostock：稳定、无 token 的日线源。"""

    name = "baostock"
    priority = 90
    description = "Baostock 证券宝（自有服务器，无需 token，日线最稳）"

    #: Baostock 走自己的 socket 协议，**不经过** requests/代理，
    #: 所以这里不声明 hosts（声明了反而会被域名健康度误判）。
    hosts = ()

    @capability(DataCapability.DAILY_KLINE, name="baostock_daily")
    def daily(self, symbol: str, start: Any = None, end: Any = None,
              adjust: str = "qfq") -> pd.DataFrame:
        """Baostock 日线。

        实现复用 ``app.datasource.compat.kline_sources._baostock_daily``：
        适配器只做**能力的声明与异常语义**，取数实现仍然只有一份。
        """
        from app.datasource.compat.kline_sources import _baostock_daily

        return _baostock_daily(symbol=symbol, start=start, end=end, adjust=adjust)


__all__ = ["BaoStockProvider"]
