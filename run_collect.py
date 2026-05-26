#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 17:17
# @FileName    : run_collect.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from tenacity import RetryError

from app.data.collector.stock_collector import StockCollector
from app.utils.logger import logger

def main(name: str = ''):
    collector = StockCollector()
    try:
        collector.fetch_daily('000001')
    except RetryError:
        logger.error(f"下载失败")
    collector.close()


if __name__ == '__main__':
    main()
