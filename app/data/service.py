#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : service.py
# @Description : 行情数据服务：采集入库、本地加载、覆盖度查询
#               回测/实盘统一从这里取数，不再各自拼 SQL 和 CSV
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.datasource import (
    CsvDataSource,
    DataFetchError,
    DataSourceFactory,
    normalize_date,
    normalize_symbol,
)
from app.db.database import SessionLocal
from app.repository.minute_repository import MinuteRepository, normalize_period
from app.repository.stock_repository import StockRepository, session_scope
from app.utils.logger import logger


class MarketDataService:
    """行情数据统一入口。"""

    # ---------------- 采集入库 ----------------
    @staticmethod
    def collect(
        symbol: str,
        start_date: str,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
        source: Optional[str] = None,
        db: Optional[Session] = None,
        save_csv: bool = False,
    ) -> pd.DataFrame:
        """拉取行情并写入数据库（幂等，可重复执行）。"""
        end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        sym = normalize_symbol(symbol)
        priority = [source] if source else None

        df = DataSourceFactory.get_stock_data(
            sym, start_date, end_date, adjust=adjust, priority=priority
        )

        with session_scope(db) as session:
            # commit=False：交由 session_scope 统一提交，异常时整体回滚
            StockRepository.save_df(session, df, commit=False)

        if save_csv:
            MarketDataService.export_csv(df, sym)

        logger.success(f"采集完成 | {sym} | {len(df)} 条 | {start_date}~{end_date}")
        return df

    @staticmethod
    def collect_incremental(
        symbol: str,
        lookback_days: int = 30,
        start_date: str = "",
        end_date: Optional[str] = None,
        adjust: str = "qfq",
        source: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> Dict:
        """**增量**采集：只拉"库里还没有的 + 最近可能被修正的"那段。

        定时采集绝不能每次全量重下：三年日线 3 万条、几十个标的，
        每分钟重下一次就是在给数据源做压力测试（然后被限流）。

        窗口怎么定：
          - 库里没数据 → 退回全量（``start_date`` 或默认 3 年前）
          - 库里有数据 → 从「最后日期 - lookback_days」开始
            后面这段是必要的重复采集：前复权价格会因为除权除息被重算，
            停牌补数据也会回填，只看"最新一天"永远修不好旧账

        :return: dict(symbol, rows, start, end, new_rows)；无事可做时 rows=0
        """
        sym = normalize_symbol(symbol)
        end = end_date or datetime.now().strftime("%Y-%m-%d")

        start = str(start_date or "")
        with session_scope(db) as session:
            cov = StockRepository.get_coverage(session, sym)
        if cov and cov.get("end"):
            anchor = datetime.strptime(str(cov["end"]), "%Y-%m-%d").date()
            window_start = anchor.fromordinal(anchor.toordinal() - max(0, int(lookback_days)))
            # 已有数据比用户指定的起点更晚时用窗口起点，避免重复下载历史
            if not start or window_start.strftime("%Y-%m-%d") > start:
                start = window_start.strftime("%Y-%m-%d")
        if not start:
            start = (datetime.now().replace(year=datetime.now().year - 3)).strftime("%Y-%m-%d")

        df = MarketDataService.collect(
            sym, start, end, adjust=adjust, source=source
        )
        return {
            "symbol": sym,
            "rows": len(df),
            "start": start,
            "end": end,
            "new_rows": len(df),
        }

    # ---------------- 分钟线 ----------------
    @staticmethod
    def collect_minute(
        symbol: str,
        period: str = "1",
        start_datetime: Optional[str] = None,
        end_datetime: Optional[str] = None,
        adjust: str = "qfq",
        source: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> int:
        """采集分钟线并入库。返回写入条数。"""
        period = normalize_period(period)
        df = DataSourceFactory.get_minute_data(
            symbol,
            period=period,
            adjust=adjust,
            priority=[source] if source else None,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )
        with session_scope(db) as session:
            MinuteRepository.save_df(session, df, period=period, commit=False)
        logger.success(f"分钟线采集完成 | {symbol} | {period}m | {len(df)} 条")
        return len(df)

    @staticmethod
    def load_minute(
        symbol: str,
        period: str = "1",
        start=None,
        end=None,
        db: Optional[Session] = None,
    ) -> pd.DataFrame:
        """读分钟线（只读本地库）。分钟线不提供 auto 联网——实时性要求高的链路
        不应该在查询路径里偷偷发起网络请求。"""
        with session_scope(db) as session:
            return MinuteRepository.get_minute_df(
                session, symbol, period=period, start=start, end=end
            )

    @staticmethod
    def minute_inventory(db: Optional[Session] = None) -> List[Dict]:
        with session_scope(db) as session:
            return MinuteRepository.list_symbols(session)

    @staticmethod
    def minute_coverage(symbol: str, period: str = "1",
                        db: Optional[Session] = None) -> Optional[Dict]:
        with session_scope(db) as session:
            return MinuteRepository.get_coverage(session, symbol, period=period)

    @staticmethod
    def export_csv(df: pd.DataFrame, symbol: str) -> str:
        """导出 CSV 到 data/stock/<symbol>.csv。"""
        settings.ensure_dirs()
        path = settings.CSV_DIR / f"{normalize_symbol(symbol)}.csv"
        out = df.copy()
        out.to_csv(path, index=False, encoding="utf-8-sig")
        logger.info(f"CSV 已导出: {path}")
        return str(path)

    # ---------------- 读取 ----------------
    @staticmethod
    def load(
        symbol: str,
        start_date: str,
        end_date: str,
        source: str = "auto",
        db: Optional[Session] = None,
    ) -> pd.DataFrame:
        """按来源加载日线数据。

        :param source: db=只读库；csv=只读本地CSV；remote=强制联网；
                       auto=库中无数据则自动联网采集后入库（默认）
        """
        sym = normalize_symbol(symbol)
        if normalize_date(start_date) > normalize_date(end_date):
            raise ValueError(f"开始日期不能晚于结束日期: {start_date} > {end_date}")

        if source == "csv":
            return CsvDataSource().fetch_data(sym, start_date, end_date)

        with session_scope(db) as session:
            if source == "remote":
                return MarketDataService.collect(
                    sym, start_date, end_date, db=session
                )

            df = StockRepository.get_stock_df(session, sym, start_date, end_date)

            if source == "db":
                if df.empty:
                    raise DataFetchError(
                        f"库中无 {sym} 的 {start_date}~{end_date} 数据，请先执行 collect"
                    )
                return df

            # auto：库里没有就去拉，拉完再读一次，保证读到的就是库里的口径
            if df.empty:
                logger.info(f"库中无 {sym} 数据，自动联网采集...")
                MarketDataService.collect(sym, start_date, end_date, db=session)
                df = StockRepository.get_stock_df(session, sym, start_date, end_date)

        if df.empty:
            raise DataFetchError(
                f"未能获取 {sym} 在 {start_date}~{end_date} 的数据"
            )
        return df

    # ---------------- 元信息 ----------------
    @staticmethod
    def inventory(db: Optional[Session] = None) -> List[Dict]:
        """库中已有数据的标的清单。"""
        with session_scope(db) as session:
            return StockRepository.list_symbols(session)

    @staticmethod
    def coverage(symbol: str, db: Optional[Session] = None) -> Optional[Dict]:
        with session_scope(db) as session:
            return StockRepository.get_coverage(session, symbol)


def init_db() -> None:
    """建表。"""
    from app.db.database import engine
    from app.db.models import Base

    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)
    logger.info(f"数据库初始化完成 | {settings.DATABASE_URL}")
