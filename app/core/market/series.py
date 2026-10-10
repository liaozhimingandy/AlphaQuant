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

    ``revision`` 是**内容版本号**：任何一次写入都会 +1。
    它存在的唯一理由是给上层做缓存失效判断 —— ``len()`` 不够用：
    deque 满了之后 append 会让长度一直停在 maxlen，而内容其实在往前滑，
    只看长度做缓存键会读到"上一根K线"的指标值，是静默的错误信号。
    """

    def __init__(self, symbol: str, maxlen: int = 500) -> None:
        self.symbol = symbol
        self.maxlen = maxlen
        self._bars: Deque[Bar] = deque(maxlen=maxlen)
        self.revision: int = 0

    # ---------------- 基础访问 ----------------
    def append(self, bar: Bar) -> None:
        self._bars.append(bar)
        self.revision += 1

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
