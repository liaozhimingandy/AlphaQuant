#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : data_service.py
# @Description : Data Service —— 上层（因子/策略/回测/采集组件）**唯一**的数据入口
#
#                它长什么样，决定了因子长什么样：
#
#                    # 因子里不该出现 akshare / baostock / 东财
#                    klines = data_service.get_kline("600519", "2025-01-01", "2026-01-01")
#
#                这一层刻意**只做三件事**：
#                  1. 把业务调用翻译成 DataRequest；
#                  2. 交给 Router（它自己不知道有几个源、谁先谁后）；
#                  3. 把标准 DataFrame 转成标准模型（Kline / RankRow / FundFlowRow）。
#
#                任何"这个源该不该试""失败了换谁"的逻辑都不许出现在这里 ——
#                那是 Router 的活。这样加数据源时，本文件**一行都不用改**。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from app.datasource.model.enums import AdjustType, Frequency
from app.datasource.model.fundflow import FundFlowRequest, FundFlowRow
from app.datasource.model.kline import Kline, KlineRequest
from app.datasource.model.rank import RankKind, RankRequest, RankRow
from app.datasource.provider import get_router
from app.datasource.provider.base import DataCapability, ProviderError
from app.datasource.routing.router import DataRouter, RouteResult
from app.common.logging import logger


class DataService:
    """统一数据服务。

    ``*_df`` 结尾的方法返回标准 DataFrame（入库/因子批量计算用）；
    不带后缀的返回标准模型列表（带血缘，排查问题时用）。
    """

    def __init__(self, router: Optional[DataRouter] = None):
        self._router = router

    @property
    def router(self) -> DataRouter:
        if self._router is None:
            self._router = get_router()
        return self._router

    # =====================================================================
    # 内部：一次路由
    # =====================================================================
    def _route(self, capability: DataCapability, params: Dict[str, Any],
               source: str = "", retries: Optional[int] = None) -> RouteResult:
        params = dict(params)
        if source:
            params["source"] = source
        if retries is not None:
            params["retries"] = retries
        return self.router.fetch(capability, **params)

    @staticmethod
    def _lineage(result: RouteResult) -> Dict[str, Any]:
        return {"source": result.route, "provider": result.provider,
                "fetched_at": result.fetched_at, "raw_id": result.raw_id}

    # =====================================================================
    # 日线
    # =====================================================================
    def get_kline(self, symbol: str, start: Any = None, end: Any = None,
                  adjust: Any = AdjustType.QFQ,
                  frequency: Any = Frequency.DAY,
                  source: str = "", retries: Optional[int] = None) -> List[Kline]:
        """取日线 → :class:`Kline` 列表（带血缘）。"""
        req = KlineRequest(symbol=symbol, start=start, end=end,
                           adjust=adjust, frequency=frequency, source=source)
        result = self._route(DataCapability.DAILY_KLINE, req.to_params(),
                             req.source, retries)
        rows = Kline.from_frame(result.data,
                                frequency=req.frequency, adjust=req.adjust,
                                **self._lineage(result))
        logger.debug(f"DataService.get_kline | {symbol} | {len(rows)} 根 | "
                     f"来源 {result.route}")
        return rows

    def get_kline_df(self, symbol: str, start: Any = None, end: Any = None,
                     adjust: Any = AdjustType.QFQ,
                     frequency: Any = Frequency.DAY,
                     source: str = "", with_lineage: bool = False,
                     retries: Optional[int] = None) -> pd.DataFrame:
        """取日线 → 标准列 DataFrame（列 = :data:`KLINE_COLUMNS`）。"""
        req = KlineRequest(symbol=symbol, start=start, end=end,
                           adjust=adjust, frequency=frequency, source=source)
        result = self._route(DataCapability.DAILY_KLINE, req.to_params(),
                             req.source, retries)
        if not with_lineage:
            return result.data
        from app.datasource.model.kline import with_lineage as _tag

        return _tag(result.data, **self._lineage(result))

    # =====================================================================
    # 榜单
    # =====================================================================
    def get_hot_rank(self, top: Optional[int] = None, trade_date: Any = None,
                     source: str = "", retries: Optional[int] = None) -> List[RankRow]:
        """人气榜 → :class:`RankRow` 列表。"""
        req = RankRequest(kind=RankKind.HOT, top=top, trade_date=trade_date,
                          source=source)
        result = self._route(DataCapability.HOT_RANK, req.to_params(),
                             req.source, retries)
        rows = RankRow.from_frame(result.data, kind=RankKind.HOT,
                                  trade_date=trade_date, **self._lineage(result))
        logger.debug(f"DataService.get_hot_rank | {len(rows)} 只 | "
                     f"来源 {result.route}"
                     + ("（兜底）" if result.fallback_used else ""))
        return rows

    def get_hot_rank_df(self, top: Optional[int] = None, trade_date: Any = None,
                        source: str = "", retries: Optional[int] = None
                        ) -> pd.DataFrame:
        req = RankRequest(kind=RankKind.HOT, top=top, trade_date=trade_date,
                          source=source)
        return self._route(DataCapability.HOT_RANK, req.to_params(),
                           req.source, retries).data

    def get_flow_rank(self, rank_kind: str = "即时", top: Optional[int] = None,
                      source: str = "", retries: Optional[int] = None
                      ) -> List[RankRow]:
        """资金榜 → :class:`RankRow` 列表（主力净额在 ``extras`` 里）。"""
        req = RankRequest(kind=RankKind.FLOW, top=top, rank_kind=rank_kind,
                          source=source)
        result = self._route(DataCapability.FLOW_RANK, req.to_params(),
                             req.source, retries)
        return RankRow.from_frame(result.data, kind=RankKind.FLOW,
                                  **self._lineage(result))

    def get_flow_rank_df(self, rank_kind: str = "即时",
                         top: Optional[int] = None,
                         source: str = "", retries: Optional[int] = None
                         ) -> pd.DataFrame:
        req = RankRequest(kind=RankKind.FLOW, top=top, rank_kind=rank_kind,
                          source=source)
        return self._route(DataCapability.FLOW_RANK, req.to_params(),
                           req.source, retries).data

    # =====================================================================
    # 个股资金流
    # =====================================================================
    def get_fund_flow(self, symbol: str, days: Optional[int] = None,
                      source: str = "", retries: Optional[int] = None
                      ) -> List[FundFlowRow]:
        req = FundFlowRequest(symbol=symbol, days=days, source=source)
        result = self._route(DataCapability.FUND_FLOW, req.to_params(),
                             req.source, retries)
        return FundFlowRow.from_frame(result.data, **self._lineage(result))

    def get_fund_flow_df(self, symbol: str, days: Optional[int] = None,
                         source: str = "", retries: Optional[int] = None
                         ) -> pd.DataFrame:
        req = FundFlowRequest(symbol=symbol, days=days, source=source)
        return self._route(DataCapability.FUND_FLOW, req.to_params(),
                           req.source, retries).data

    # =====================================================================
    # 可观测
    # =====================================================================
    def capabilities(self) -> Dict[str, List[str]]:
        """{能力: [路由]}。"""
        return self.router.registry.capabilities()

    def used_sources(self) -> Dict[str, str]:
        """每种能力最近一次成功的路由。"""
        return self.router.used_sources()

    def snapshot(self) -> List[Dict[str, Any]]:
        """每条路由的配置 + 健康 + 熔断（面板/CLI 用）。"""
        return self.router.registry.snapshot()

    def recent_events(self, n: int = 20) -> List[Dict[str, Any]]:
        return self.router.recent_events(n)

    def raw(self, raw_id: str) -> Optional[Dict[str, Any]]:
        """按血缘 id 取回原始响应（排查"这个数哪来的"）。"""
        try:
            from app.datasource.provider import RAW_STORE

            return RAW_STORE.load(raw_id)
        except Exception:
            return None


#: 全局入口。**业务代码一律用它**，不要直接碰 Router / Provider。
data_service = DataService()


__all__ = ["DataService", "data_service"]
