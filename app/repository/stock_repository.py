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
from sqlalchemy.dialects.sqlite import insert

from app.db.models import StockDaily
from app.utils.logger import logger


class StockRepository:

    @staticmethod
    def batch_upsert(db, records: list[dict]):

        if not records:
            return

        stmt = insert(StockDaily).values(records)

        stmt = stmt.on_conflict_do_nothing(
            index_elements=["symbol", "trade_date"]
        )

        db.execute(stmt)

        db.commit()

        logger.info(f"批量入库完成，共 {len(records)} 条")

def main(name: str = ''):
    print(f'Hi, {name}')


if __name__ == '__main__':
    main()
