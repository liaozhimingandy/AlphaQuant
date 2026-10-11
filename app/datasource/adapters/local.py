#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : local.py
# @Description : 本地兜底 Provider —— 外网全挂时的最后一道防线
#
#                两级降级：
#                  1. 库里存过人气榜 → 复用最近一天那份（真实榜单，只是可能不是今天）；
#                  2. 连人气榜都没有 → 用最近交易日的日线合成（成交额 × 涨幅排序）。
#
#                两级都没有 → 抛 DataUnavailable（**"没数据"不是"源坏了"**，
#                按 NO_DATA 分类，不会给它降分）。
#
#                ``fallback=True`` 是这里最关键的标志：
#                它几乎不会失败，如果和正常源一起按分排序，
#                迟早会涨到队首变成"明明有网却一直用本地旧数据"——
#                那比拿不到数据更难发现。所以兜底源**只在正常源全挂后才尝试**。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from app.datasource.provider.base import DataCapability, DataProvider, capability


class LocalProvider(DataProvider):
    """本地库兜底：不联网，用库里已有的数据把榜单撑住。"""

    name = "local"
    priority = 10
    fallback = True
    description = "本地库兜底（外网全挂时用库内数据出榜）"
    hosts = ()

    @capability(DataCapability.HOT_RANK, name="local_hot", priority=10,
                description="库内最近人气榜 → 日线成交额合成榜")
    def hot_rank(self, top: Optional[int] = None,
                 trade_date: Any = None) -> pd.DataFrame:
        from app.datasource.compat.local_source import _hot_rank_local

        return _hot_rank_local(top=top, trade_date=trade_date)


__all__ = ["LocalProvider"]
