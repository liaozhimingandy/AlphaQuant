#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : xueqiu.py
# @Description : 雪球 Provider —— 关注热度榜
#
#                它最大的价值是**和东财不在同一个风控面上**：
#                东财域名被拦/被限流时，雪球往往是唯一还能出数的人气榜源。
#                冗余的意义不在于"多一个源"，而在于"多一个**独立失效**的源"。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.datasource.provider.base import DataCapability, DataProvider, capability


class XueqiuProvider(DataProvider):
    """雪球：关注热度榜（与东财不同域名，互为备份）。"""

    name = "xueqiu"
    priority = 70
    description = "雪球（关注热度榜，与东财不同风控面）"
    hosts = ("stock.xueqiu.com",)

    @capability(DataCapability.HOT_RANK, name="xueqiu_hot", priority=70,
                hosts=("stock.xueqiu.com",),
                description="雪球关注热度（stock_hot_follow_xq）")
    def hot_rank(self, top: Optional[int] = None) -> pd.DataFrame:
        from app.datasource.service.facade import _hot_rank_xueqiu

        return _hot_rank_xueqiu(top=top)


__all__ = ["XueqiuProvider"]
