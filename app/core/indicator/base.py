#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : base.py
# @Description : 指标层基类：只负责计算，不做任何判断
#               指标是"纯函数"——同样的K线序列必然得到同样的值，这是回测可复现的前提
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import abc
from typing import Any, Dict, Optional

import numpy as np

from app.core.market.series import BarSeries


class IBaseIndicator(abc.ABC):
    """指标基类。

    约定：
      - ``name`` 是注册表里的唯一键，子类必须定义
      - ``compute`` 返回**与输入等长**的数组，前导不足的位置用 np.nan 填充，
        调用方可以按位置对齐，不需要自己算偏移量
    """

    name: str = ""

    def __init__(self, **params: Any) -> None:
        self.params: Dict[str, Any] = params

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<{self.__class__.__name__} {self.name} {self.params}>"

    # ---------------- 子类实现 ----------------
    @abc.abstractmethod
    def compute(self, series: BarSeries) -> np.ndarray:
        """根据K线序列计算指标序列。"""
        raise NotImplementedError

    # ---------------- 通用取值 ----------------
    def value(self, series: BarSeries, idx: int = 0) -> Optional[float]:
        """取倒数第 idx 根K线的指标值（idx=0 表示最新）。不足时返回 None。"""
        arr = self.compute(series)
        if arr.size == 0:
            return None
        pos = arr.size - 1 - idx
        if pos < 0:
            return None
        v = arr[pos]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        return float(v)

    def values(self, series: BarSeries, n: int = 2) -> list[Optional[float]]:
        """取最近 n 个值（按时间正序），供金叉/死叉这类需要比较前后两根的逻辑使用。"""
        return [self.value(series, idx=i) for i in range(n - 1, -1, -1)]
