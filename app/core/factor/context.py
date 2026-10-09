#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : context.py
# @Description : 因子计算上下文：因子唯一的数据入口
#               因子不直接碰数据库/网络/事件总线，只能通过 ctx 读数据 —— 保证因子可回测、可复现
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from app.core.indicator.registry import create_indicator, has_indicator
from app.core.market.series import BarSeries
from app.core.market.types import Bar


class FactorContext:
    """一次因子计算的完整输入。

    设计要点：
      1. **按 bar 创建**：每根K线（或每个事件）新建一个 ctx，天然避免跨周期的脏缓存
      2. **指标惰性计算 + 局部缓存**：同一根K线里多个因子都要 MA20 时只算一次
      3. **features 是事件通道的注入口**：新闻情感、LLM 评分等由运行时写进来，
         因子像读普通数值一样读它 —— 这是"新闻驱动交易"能复用同一套因子体系的关键
    """

    def __init__(
        self,
        symbol: str,
        series: BarSeries,
        params: Optional[Dict[str, Any]] = None,
        features: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> None:
        self.symbol = symbol
        self.series = series
        self.params: Dict[str, Any] = params or {}
        self.features: Dict[str, Any] = features or {}
        self.now = now
        self._ind_cache: Dict[str, Optional[float]] = {}
        self._arr_cache: Dict[str, np.ndarray] = {}

    # ---------------- 行情访问 ----------------
    @property
    def last_bar(self) -> Optional[Bar]:
        return self.series.last

    @property
    def close(self) -> float:
        b = self.series.last
        return float(b.close) if b else float("nan")

    @property
    def bar_count(self) -> int:
        return len(self.series)

    def closes(self, n: int) -> np.ndarray:
        arr = self.series.close
        return arr[-n:] if n > 0 else arr

    # ---------------- 指标访问 ----------------
    def _key(self, name: str, params: Dict[str, Any]) -> str:
        if not params:
            return name
        inner = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{name}({inner})"

    def ind(self, name: str, idx: int = 0, **params: Any) -> Optional[float]:
        """取指标值。idx=0 为最新一根，idx=1 为上一根。"""
        if not has_indicator(name):
            return None
        key = f"{self._key(name, params)}@{idx}"
        if key not in self._ind_cache:
            try:
                indicator = create_indicator(name, **params)
                self._ind_cache[key] = indicator.value(self.series, idx=idx)
            except Exception:
                # 因子层不该因为一个指标算不出来就让整个任务崩掉
                self._ind_cache[key] = None
        return self._ind_cache[key]

    def ind_series(self, name: str, **params: Any) -> Optional[np.ndarray]:
        """取整条指标序列（少数因子需要，例如斜率）"""
        if not has_indicator(name):
            return None
        key = self._key(name, params)
        if key not in self._arr_cache:
            try:
                self._arr_cache[key] = create_indicator(name, **params).compute(
                    self.series
                )
            except Exception:
                self._arr_cache[key] = None
        return self._arr_cache[key]

    # ---------------- 历史回看 ----------------
    def previous(self, n: int = 1) -> Optional["FactorContext"]:
        """回退 n 根K线，生成一个新的上下文。

        穿越类规则（金叉/死叉）需要"上一根"的因子值。这里用**截断序列**
        来实现，好处是对任何因子都通用，不需要因子自己支持 idx 参数。
        """
        bars = list(self.series.bars)
        if len(bars) <= n:
            return None
        sub = BarSeries(self.symbol, maxlen=self.series.maxlen)
        sub.extend(bars[: len(bars) - n])
        return FactorContext(
            self.symbol, sub, params=self.params, features=self.features, now=self.now
        )

    # ---------------- 事件特征访问 ----------------
    def feature(self, key: str, default: Any = None) -> Any:
        return self.features.get(key, default)

    def has_feature(self, key: str) -> bool:
        return key in self.features

    def to_dict(self) -> Dict[str, Any]:
        """快照，便于落盘排查"为什么当时没触发" """
        b = self.last_bar
        return {
            "symbol": self.symbol,
            "dt": b.dt.isoformat() if b else None,
            "close": self.close,
            "bar_count": self.bar_count,
            "params": dict(self.params),
            "features": dict(self.features),
        }
