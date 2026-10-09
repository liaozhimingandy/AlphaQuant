#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : helpers.py
# @Description : 测试辅助：合成行情数据，避免测试依赖网络
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import numpy as np
import pandas as pd


def make_ohlcv(
    days: int = 240,
    start: str = "2023-01-01",
    seed: int = 42,
    base_price: float = 10.0,
    amplitude: float = 2.0,
    cycles: float = 6.0,
) -> pd.DataFrame:
    """生成一段确定性的人造行情（正弦波动 + 微小噪声），索引为 date。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=days, name="date")

    t = np.linspace(0, cycles * np.pi, days)
    close = base_price + amplitude * np.sin(t) + rng.normal(0, 0.05, days)
    close = np.maximum(close, 0.5)  # 价格必须为正

    # 用当日波幅构造 open/high/low，保证 low <= open/close <= high
    span = np.abs(rng.normal(0, 0.15, days))
    open_ = close + rng.normal(0, 0.08, days)
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    low = np.maximum(low, 0.1)

    volume = rng.integers(1_000_000, 5_000_000, size=days).astype(float)

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": volume * close,
        },
        index=idx,
    )


def make_uptrend(days: int = 240, start: str = "2023-01-01") -> pd.DataFrame:
    """带回调的上涨行情：用于验证买入路径会被触发。

    注意：不能用"完美线性上涨"——那种数据里 5 日线始终在 20 日线上方，
    永远不会产生金叉事件，策略自然一笔交易都不会有。
    """
    idx = pd.bdate_range(start=start, periods=days, name="date")
    t = np.linspace(0, 8 * np.pi, days)
    close = np.linspace(10.0, 20.0, days) + 1.2 * np.sin(t)
    close = np.maximum(close, 0.5)
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.full(days, 1_000_000.0),
            "amount": np.full(days, 1_000_000.0) * close,
        },
        index=idx,
    )
