#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 存储层
#
#                分层的意义（借鉴 Medallion：Bronze / Silver / Gold）：
#
#                    RAW（Bronze）     原始响应，不改一个字节，用于审计与重放
#                         ↓ 标准化 / 清洗
#                    MARKET（Silver）  标准模型入库（StockDaily / StockHotRank …）
#                         ↓ 因子计算
#                    FEATURE（Gold）   因子面板，直接喂给策略与回测
#
#                本项目目前只落地了 RAW 这一层的独立存档（``raw_store``）；
#                Silver 沿用既有 Repository，Gold 在 app/core/data/features.py。
#                把 RAW 单独拎出来，是因为**只有它必须保存"未经处理的样子"** ——
#                另外两层都能靠重算得到，原始响应丢了就再也拿不回来。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from app.datasource.raw.raw_store import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_MAX_ROWS,
    RawStore,
)

__all__ = ["DEFAULT_KEEP_DAYS", "DEFAULT_MAX_ROWS", "RawStore"]
