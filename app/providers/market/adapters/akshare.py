#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : akshare.py
# @Description : AkShare Provider —— 日线 / 资金流 / 资金榜 / 人气榜
#
#                这是**能力最多**的一个源，也是最能说明
#                "熔断必须按能力分开"的例子：
#
#                    akshare_daily   ← push2his.eastmoney.com
#                    akshare（资金流）← push2his.eastmoney.com
#                    akshare_flow    ← push2.eastmoney.com
#                    akshare_em      ← push2.eastmoney.com
#                    akshare_spot    ← 82.push2.eastmoney.com
#
#                实测里出现过 push2 被代理拦、push2his 仍可用（以及反过来）。
#                如果"akshare 挂了"是一个整体判断，就会白白扔掉另一半还能用的能力。
#
#                注意人气榜的两个端点都压在 push2 上，**实测长期不通**；
#                真正通的是 eastmoney provider 的 emapp_hot（emappdata 域名）。
#                它们仍然注册在这里，作为东财恢复后的备选。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from app.providers.market.provider.base import DataCapability, DataProvider, capability


class AkShareProvider(DataProvider):
    """AkShare：覆盖面最广的免费数据源（底层多为东财接口）。"""

    name = "akshare"
    priority = 80
    description = "AkShare（聚合东财/新浪/雪球等接口，能力最多）"
    hosts = ("push2his.eastmoney.com",)

    # ---------------- 行情 ----------------
    @capability(DataCapability.DAILY_KLINE, name="akshare_daily",
                hosts=("push2his.eastmoney.com",),
                description="东财日线（数据全，与东财共用风控面）")
    def daily(self, symbol: str, start: Any = None, end: Any = None,
              adjust: str = "qfq") -> pd.DataFrame:
        from app.providers.market.compat.kline_sources import _akshare_daily

        return _akshare_daily(symbol=symbol, start=start, end=end, adjust=adjust)

    # ---------------- 资金 ----------------
    @capability(DataCapability.FUND_FLOW, name="akshare",
                hosts=("push2his.eastmoney.com",),
                description="个股历史资金流（push2his 逐日 + 全市场快照兜底）")
    def fund_flow(self, symbol: str, days: Optional[int] = None) -> pd.DataFrame:
        from app.providers.market.service.facade import _fund_flow_akshare

        return _fund_flow_akshare(symbol=symbol, days=days)

    @capability(DataCapability.FLOW_RANK, name="akshare_flow",
                hosts=("push2.eastmoney.com",),
                description="全市场资金流排名（即时/3日/5日/10日）")
    def flow_rank(self, rank_kind: str = "即时",
                  top: Optional[int] = None) -> pd.DataFrame:
        from app.providers.market.service.facade import _flow_rank_akshare

        return _flow_rank_akshare(rank_kind=rank_kind, top=top)

    # ---------------- 人气榜 ----------------
    @capability(DataCapability.HOT_RANK, name="akshare_em", priority=40,
                hosts=("push2.eastmoney.com",),
                description="东财人气榜（stock_hot_rank_em，含 push2 二次请求）")
    def hot_rank_em(self, top: Optional[int] = None) -> pd.DataFrame:
        from app.providers.market.service.facade import _hot_rank_akshare_em

        return _hot_rank_akshare_em(top=top)

    @capability(DataCapability.HOT_RANK, name="akshare_spot", priority=20,
                hosts=("82.push2.eastmoney.com",),
                description="全市场快照按成交额降序（人气榜的成交额代理）")
    def hot_rank_spot(self, top: Optional[int] = None) -> pd.DataFrame:
        from app.providers.market.service.facade import _hot_rank_spot

        return _hot_rank_spot(top=top)


__all__ = ["AkShareProvider"]
