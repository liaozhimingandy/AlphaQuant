#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 领域模型 —— 整个系统里唯一描述「业务是什么」的地方。

这里的类有一个硬约束：**不含任何取数逻辑**。
Bar 不知道自己来自 baostock 还是东财，Signal 不知道自己会被回测还是实盘消费。
正因为这条边界，上层才能在换数据源、换撮合网关时零改动。

内容分四块：

    types      领域实体：Bar / Tick / Signal / Order / Position / Account + 枚举
    series     时间序列容器：BarSeries（因子与指标的公共输入）
    symbol     证券代码标准化：normalize_symbol —— 600519 / sh600519 / 600519.SH 归一
    clean      数据清洗：停牌、异常价格、缺失值的判定

依赖方向：只依赖 common。**不依赖 datasource / storage / feature / strategy 任何一层。**
"""
from app.domain.types import (
    Account,
    Bar,
    NewsAnalysis,
    NewsItem,
    Order,
    OrderStatus,
    Position,
    RunMode,
    Side,
    Signal,
    SignalSource,
    TaskState,
    Tick,
)
from app.domain.series import BarSeries
from app.domain.symbol import normalize_symbol, normalize_date
from app.domain.clean import BarCleaner, CleanReport, clean_bars, clean_frame, default_cleaner, is_tradable

__all__ = [
    # 领域实体
    "Bar",
    "Tick",
    "Signal",
    "Order",
    "Position",
    "Account",
    "NewsItem",
    "NewsAnalysis",
    # 枚举
    "Side",
    "OrderStatus",
    "TaskState",
    "RunMode",
    "SignalSource",
    # 序列容器
    "BarSeries",
    # 代码标准化
    "normalize_symbol",
    "normalize_date",
    # 清洗
    "BarCleaner",
    "CleanReport",
    "clean_bars",
    "clean_frame",
    "default_cleaner",
    "is_tradable",
]
