#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 16:58
# @FileName    : stock_collector.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import akshare as ak
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.db.database import SessionLocal
from app.repository.stock_repository import StockRepository
from app.utils.logger import logger


class StockCollector:

    def __init__(self):

        self.db = SessionLocal()

    def close(self):

        self.db.close()

    @retry(
        stop=stop_after_attempt(5),  # 最多重试5次
        wait=wait_exponential(multiplier=1, min=2, max=20),  # 指数退避
        retry=retry_if_exception_type(Exception),  # 捕获所有异常
        reraise=True,
        before=lambda rs: logger.info(f"🔁 第 {rs.attempt_number} 次尝试"),
    )
    def fetch_daily(
            self,
            symbol: str,
            start_date="20200101",
            end_date="20261231",
            adjust="qfq"
    ):

        logger.info(f"开始采集股票: {symbol}")

        try:

            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust
            )

            if df.empty or df is None:
                logger.warning(f"{symbol} 无数据")
                raise ValueError("数据为空，触发重试")

            # ===== 字段映射 =====
            df = df.rename(columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount"
            })

            # ===== 保留字段 =====
            df = df[
                [
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount"
                ]
            ]

            # ===== 类型处理 =====
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

            df["symbol"] = symbol

            # ===== NaN 转 None =====
            df = df.where(pd.notnull(df), None)

            records = df.to_dict(orient="records")

            # ===== 入库 =====
            StockRepository.batch_upsert(
                self.db,
                records
            )

            logger.info(
                f"{symbol} 采集完成，共 {len(records)} 条"
            )

        except Exception as e:
            raise

def main(name: str = ''):
    collector = StockCollector()
    collector.fetch_daily("000001")
    collector.close()


if __name__ == '__main__':
    main()
