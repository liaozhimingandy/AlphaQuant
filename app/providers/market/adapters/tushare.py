#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : tushare.py
# @Description : Tushare Provider —— 个股资金流（兜底源）
#
#                需 TUSHARE_TOKEN 且 moneyflow 接口要 2000 积分。
#                没 token / 没装库 → 适配器抛 DataUnavailable，
#                按 **NO_DATA（中性）** 处理：源没坏，跳过即可，不降分。
#
#                它的重要性在于**和东财完全无关的 API**：
#                东财整体不可用时，个股资金流只剩这一条路。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.providers.market.provider.base import DataCapability, DataProvider, capability


class TushareProvider(DataProvider):
    """Tushare：个股资金流（需 token，独立于东财）。"""

    name = "tushare"
    priority = 50
    description = "Tushare（需 TOKEN；moneyflow 资金流，独立于东财）"
    hosts = ()

    @capability(DataCapability.FUND_FLOW, name="tushare", priority=50,
                description="Tushare moneyflow（五档明细缺失，只有净额总量）")
    def fund_flow(self, symbol: str, days: Optional[int] = None) -> pd.DataFrame:
        from app.providers.market.service.facade import _fund_flow_tushare

        return _fund_flow_tushare(symbol=symbol, days=days)


__all__ = ["TushareProvider"]
