#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 17:01
# @FileName    : stock_repository.py.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import pandas as pd
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm.session import Session

from app.db.models import StockDaily
from app.utils.logger import logger


class StockRepository:

    @staticmethod
    def batch_upsert(db:Session, records: list[dict]):
        """批量插入数据"""
        if not records:
            return
        stmt = insert(StockDaily).values(records)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["symbol", "date"]
        )
        db.execute(stmt)
        db.commit()
        logger.debug(f"批量入库完成,共计{len(records)}条")

    @staticmethod
    def get_stock_df(
            db: Session,
            symbol: str,
            start_date: str,
            end_date: str
    ) -> pd.DataFrame:

        rows = (
            db.query(StockDaily)
            .filter(
                StockDaily.symbol == symbol,
                StockDaily.date >= start_date,
                StockDaily.date <= end_date
            )
            .order_by(StockDaily.date.asc())
            .all()
        )

        if not rows:
            return pd.DataFrame()

        data = []

        for row in rows:
            data.append({
                "date": row.date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
            })

        df = pd.DataFrame(data)

        df["date"] = pd.to_datetime(df["date"])

        df = df.set_index("date")

        return df

def main(name: str = ''):
    print(f'Hi, {name}')


if __name__ == '__main__':
    main()
