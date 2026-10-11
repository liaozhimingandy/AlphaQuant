#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : mootdx.py
# @Description : mootdx（通达信）Provider —— 日线
#
#                值得单独占一个 Provider 的理由：
#                  * **不依赖东财域名**，走 TCP 直连通达信行情服务器；
#                  * 配了通达信数据目录时**完全离线**（读本地 .day 文件），
#                    断网、限流、接口改版都不怕；
#                  * 券商终端用的就是这一套，数据本身没问题。
#
#                它的能力只有日线，且**只给不复权**（线上/本地文件都不带复权因子）。
#                请求前复权时适配器会按不复权返回 —— 与其悄悄给一个
#                "看起来复权其实没有"的序列，不如如实声明（见 AdjustType.NONE）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from app.datasource.provider.base import DataCapability, DataProvider, capability


class MootdxProvider(DataProvider):
    """mootdx：通达信行情（TCP 线上 / 本地 .day 文件）。"""

    name = "mootdx"
    priority = 55
    description = "mootdx 通达信（不走东财；可离线读本地数据目录）"
    #: 走 TCP 与本地文件，不经过 HTTP 传输层，不声明 hosts
    hosts = ()

    @capability(DataCapability.DAILY_KLINE, name="mootdx", priority=55,
                description="通达信日线（本地数据目录优先，否则线上行情；仅不复权）")
    def daily(self, symbol: str, start: Any = None, end: Any = None,
              adjust: str = "qfq") -> pd.DataFrame:
        from app.datasource.compat.mootdx_source import _mootdx_daily

        return _mootdx_daily(symbol=symbol, start=start, end=end, adjust=adjust)


__all__ = ["MootdxProvider"]
