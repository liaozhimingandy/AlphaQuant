#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : eastmoney.py
# @Description : 东方财富 Provider —— 人气榜（**实测唯一稳定**的东财端点）
#
#                背景（这轮之前最痛的一个坑）：
#                ``akshare.stock_hot_rank_em`` 是**两段式**的：
#                  1. POST emappdata.eastmoney.com/stockrank/getAllCurrentList → 名次；
#                  2. GET  push2.eastmoney.com/api/qt/ulist.np/get          → 名称/涨幅。
#                第 1 段一直是通的，**第 2 段被代理拦死**，于是整个函数抛异常 ——
#                表现就是"数据就在门口，却报所有数据源均失败"。
#
#                这里的 emapp_hot 只用第 1 段，名称与涨跌幅改用腾讯批量报价补齐，
#                全程**不碰 push2**。这也是为什么它在 hot_rank 路由里排第一。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.providers.market.provider.base import DataCapability, DataProvider, capability


class EastMoneyProvider(DataProvider):
    """东方财富：人气榜（emappdata 域名 + 腾讯报价补全）。"""

    name = "eastmoney"
    priority = 100
    description = "东方财富（人气榜走 emappdata 域名，实测最稳）"
    hosts = ("emappdata.eastmoney.com",)

    @capability(DataCapability.HOT_RANK, name="emapp_hot", priority=100,
                hosts=("emappdata.eastmoney.com", "qt.gtimg.cn"),
                description="东财人气榜（emappdata 取榜 + 腾讯批量报价补名称/涨幅）")
    def hot_rank(self, top: Optional[int] = None) -> pd.DataFrame:
        from app.providers.market.compat.hot_sources import _hot_rank_emapp

        return _hot_rank_emapp(top=top)


__all__ = ["EastMoneyProvider"]
