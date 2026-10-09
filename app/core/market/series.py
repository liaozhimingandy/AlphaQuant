#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : series.py
# @Description : K线序列容器：实时增量 append，指标层只读它
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, List, Optional

import numpy as np

from app.core.market.types import Bar


class BarSeries:
    """一个标的的滚动K线窗口。

    实时场景不能每根K线都重算全历史，所以这里只保留最近 maxlen 根，
    指标只要够算最长周期即可（默认 500 根，足够 250 日均线）。
    """

    def __init__(self, symbol: str, maxlen: int = 500) -> None:
        self.symbol = symbol
        self.maxlen = maxlen
        self._bars: Deque[Bar] = deque(maxlen=maxlen)

    # ---------------- 基础访问 ----------------
    def append(self, bar: Bar) -> None:
        self._bars.append(bar)

    def extend(self, bars: List[Bar]) -> None:
        for b in bars:
            self.append(b)

    def __len__(self) -> int:
        return len(self._bars)

    @property
    def bars(self) -> List[Bar]:
        return list(self._bars)

    @property
    def last(self) -> Optional[Bar]:
        return self._bars[-1] if self._bars else None

    @property
    def dt(self) -> List[datetime]:
        return [b.dt for b in self._bars]

    # ---------------- 向量化列 ----------------
    def _col(self, attr: str) -> np.ndarray:
        return np.array([getattr(b, attr) for b in self._bars], dtype=float)

    @property
    def open(self) -> np.ndarray:
        return self._col("open")

    @property
    def high(self) -> np.ndarray:
        return self._col("high")

    @property
    def low(self) -> np.ndarray:
        return self._col("low")

    @property
    def close(self) -> np.ndarray:
        return self._col("close")

    @property
    def volume(self) -> np.ndarray:
        return self._col("volume")

    @property
    def amount(self) -> np.ndarray:
        return self._col("amount")
