#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Market —— 实时行情入口
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""Market：把外部行情喂进系统，产出标准 Bar 广播给下游。

提供能力 ``Cap.BAR_FEED`` / ``Cap.WATCHLIST``。
交易时段闸门与同 bar 去重都在服务内部完成（见 ``service.py``）。
"""
from app.modules.market.service import MarketService, df_to_bars

__all__ = ["MarketService", "df_to_bars"]
