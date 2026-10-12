#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : enums.py
# @Description : 标准数据模型用到的枚举
#
#                为什么连"复权方式""频率"这种小东西都要单独定义成枚举：
#                  baostock 传 "2" 表示前复权、akshare 传 "qfq"、通达信根本不支持复权；
#                  如果这些字符串散落在各个适配器里，上层想问一句
#                  "这批 K 线到底复没复权" 就只能去翻源码。
#                标准模型的意义就是：**上层只认这些枚举，不认任何厂家的写法**。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from enum import Enum


class Frequency(str, Enum):
    """K 线频率。值是本项目内部口径，适配器负责把自家写法映射过来。"""

    DAY = "1d"
    WEEK = "1w"
    MONTH = "1M"
    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    MIN_30 = "30m"
    MIN_60 = "60m"

    @property
    def is_intraday(self) -> bool:
        return self in (Frequency.MIN_1, Frequency.MIN_5, Frequency.MIN_15,
                        Frequency.MIN_30, Frequency.MIN_60)

    @classmethod
    def parse(cls, value) -> "Frequency":
        """宽松解析：'1d'/'day'/'daily'/'日线'/'d' 都能落到 DAY。"""
        if isinstance(cls, value.__class__) and isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        table = {
            "1d": cls.DAY, "d": cls.DAY, "day": cls.DAY, "daily": cls.DAY,
            "日": cls.DAY, "日线": cls.DAY, "1day": cls.DAY,
            "1w": cls.WEEK, "w": cls.WEEK, "week": cls.WEEK, "周线": cls.WEEK,
            "1m": cls.MIN_1, "1min": cls.MIN_1, "1": cls.MIN_1, "分钟": cls.MIN_1,
            "5m": cls.MIN_5, "5": cls.MIN_5, "15m": cls.MIN_15, "15": cls.MIN_15,
            "30m": cls.MIN_30, "30": cls.MIN_30, "60m": cls.MIN_60, "60": cls.MIN_60,
            "1mo": cls.MONTH, "1M": cls.MONTH, "month": cls.MONTH, "月线": cls.MONTH,
        }
        return table.get(text, cls.DAY)


class AdjustType(str, Enum):
    """复权方式。

    ``NONE`` 是**不复权**。注意：通达信(mootdx)与部分行情接口只给不复权数据，
    适配器必须如实声明 NONE —— 悄悄返回"看起来像前复权其实没有"的序列，
    会让回测的收益凭空多出一块除权收益。
    """

    NONE = "none"
    QFQ = "qfq"      # 前复权
    HFQ = "hfq"      # 后复权

    @classmethod
    def parse(cls, value) -> "AdjustType":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        if text in ("qfq", "1", "2", "pre", "front", "前复权"):
            return cls.QFQ
        if text in ("hfq", "post", "back", "后复权"):
            return cls.HFQ
        return cls.NONE


class Market(str, Enum):
    """市场。目前只用到 CN，先预留 —— 路由按 (能力, 市场) 选源是迟早的事。"""

    CN = "CN"
    HK = "HK"
    US = "US"


__all__ = ["AdjustType", "Frequency", "Market"]
