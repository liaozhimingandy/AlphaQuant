#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 15:14
# @FileName    : models.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    UniqueConstraint
)

from app.db.database import Base


class StockDaily(Base):

    __tablename__ = "stock_daily"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "date",
            name="uk_symbol_date"
        ),
    )


class StockMinute(Base):
    """分钟线/日内K线。

    为什么和日线分开一张表：
      1. 数据量差两个数量级（1 分钟线一年约 4.8 万条/标的），
         混在 stock_daily 里会让"日线回测"的每次扫描都变慢；
      2. 分钟线的生命周期完全不同——通常只保留最近若干个交易日，
         需要按 symbol+period 整段清理，分表才删得干净。

    ``period`` 用字符串存（'1'/'5'/'15'/'30'/'60'），与数据源的参数保持一致，
    避免以后加 2 分钟、10 分钟线时还要迁移表结构。
    """

    __tablename__ = "stock_minute"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    period = Column(String(8), nullable=False)
    dt = Column(DateTime, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "period",
            "dt",
            name="uk_symbol_period_dt"
        ),
    )