#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : stock_collector.py
# @Description : 采集器（薄封装，实际逻辑统一走 MarketDataService）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.data.service import MarketDataService
from app.utils.logger import logger


class StockCollector:
    """保留旧 API，内部统一委托给 MarketDataService。

    旧实现直接调 akshare 且 tenacity 使用 reraise=False，
    失败时静默返回 None（调用方再 .to_dict() 会炸），这里一并修正。
    """

    def __init__(self, db=None):
        self.db = db

    def close(self) -> None:
        pass

    def fetch_daily(
        self,
        symbol: str,
        start_date: str = "20200101",
        end_date: Optional[str] = None,
        adjust: str = "qfq",
        save_csv: bool = False,
    ) -> pd.DataFrame:
        df = MarketDataService.collect(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
            db=self.db,
            save_csv=save_csv,
        )
        logger.info(f"{symbol} 采集完成，共 {len(df)} 条")
        return df

    def fetch_many(
        self,
        symbols: list[str],
        start_date: str = "20200101",
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> dict[str, pd.DataFrame]:
        """批量采集，单只失败不影响其余标的。"""
        result: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                result[sym] = self.fetch_daily(sym, start_date, end_date, adjust)
            except Exception as exc:
                logger.error(f"{sym} 采集失败: {exc}")
        return result
