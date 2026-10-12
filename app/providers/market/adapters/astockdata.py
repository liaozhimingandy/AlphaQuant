#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : astockdata.py
# @Description : a-stock-data Provider —— 腾讯日线
#
#                和东财完全独立的一路（web.ifzq.gtimg.cn），
#                在东财被限流/被拦时是**第二个可用**的日线源。
#
#                两个必须知道的口径细节：
#                  * **没有成交额列**（腾讯 K 线接口不给），amount 恒为空；
#                  * 返回的数组顺序是 [日期, 开, 收, 高, 低, 量] —— **收在高的前面**。
#                    照 OHLC 惯性取值会把收盘价当成最高价，而且不会报错，
#                    这是最难发现的一类错（已在适配器里显式按位置取值）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from app.providers.market.provider.base import DataCapability, DataProvider, capability


class AStockDataProvider(DataProvider):
    """a-stock-data：腾讯行情日线（东财之外的独立一路）。"""

    name = "astockdata"
    priority = 60
    description = "a-stock-data（腾讯 K 线，独立于东财；无成交额列）"
    hosts = ("web.ifzq.gtimg.cn",)

    @capability(DataCapability.DAILY_KLINE, name="astockdata", priority=60,
                hosts=("web.ifzq.gtimg.cn",),
                description="腾讯日线（无成交额，量纲为手）")
    def daily(self, symbol: str, start: Any = None, end: Any = None,
              adjust: str = "qfq") -> pd.DataFrame:
        from app.providers.market.compat.astockdata_source import _astockdata_daily

        return _astockdata_daily(symbol=symbol, start=start, end=end, adjust=adjust)


__all__ = ["AStockDataProvider"]
