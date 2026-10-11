#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : Canonical Model —— 系统内部统一数据模型
#
#                一句话说明这一层为什么存在：
#
#                    **数据源是可替换的，数据标准不能替换。**
#
#                baostock / akshare / 通达信 / 东财 / 雪球，字段命名、量纲、
#                复权口径全都不一样。如果因子直接吃这些 DataFrame，
#                那么"换数据源"就等于"把所有因子重写一遍"。
#                标准模型把这件事收敛成一次：**外部格式 → 标准模型只在适配器里发生**。
#
#                本包只放三样东西：
#                  * 枚举（Frequency / AdjustType / Market）—— 上层只认这些值；
#                  * 行式模型（Kline / RankRow / FundFlowRow）—— 带血缘，可追溯；
#                  * 请求对象（KlineRequest / RankRequest / FundFlowRequest）——
#                    路由要按"请求内容"选源，所以请求必须是结构化的。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from app.datasource.model.enums import AdjustType, Frequency, Market
from app.datasource.model.fundflow import (
    FLOW_TIERS,
    FUND_FLOW_COLUMNS,
    FundFlowRequest,
    FundFlowRow,
)
from app.datasource.model.kline import (
    KLINE_COLUMNS,
    LINEAGE_COLUMNS,
    Kline,
    KlineRequest,
    kline_frame,
    with_lineage,
)
from app.datasource.model.rank import (
    RANK_COLUMNS,
    RankKind,
    RankRequest,
    RankRow,
)

__all__ = [
    "AdjustType",
    "FLOW_TIERS",
    "FUND_FLOW_COLUMNS",
    "Frequency",
    "FundFlowRequest",
    "FundFlowRow",
    "KLINE_COLUMNS",
    "Kline",
    "KlineRequest",
    "LINEAGE_COLUMNS",
    "Market",
    "RANK_COLUMNS",
    "RankKind",
    "RankRequest",
    "RankRow",
    "kline_frame",
    "with_lineage",
]
