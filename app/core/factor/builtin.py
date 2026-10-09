#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : builtin.py
# @Description : 内置基础因子
#               分两类：行情因子（从 ctx.series 算）与事件因子（从 ctx.features 读）
#               两类用同一套注册表和同一套规则组合器，这是"新闻能驱动交易"的基础
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Optional

import numpy as np

from app.core.factor.base import IBaseFactor
from app.core.factor.context import FactorContext
from app.core.factor.registry import register_factor


# ============================================================
# 行情因子
# ============================================================
@register_factor
class PriceFactor(IBaseFactor):
    """最新收盘价"""
    name = "price"
    description = "最新收盘价"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return ctx.close


@register_factor
class MAFactor(IBaseFactor):
    """均线值"""
    name = "ma"
    description = "N 日均线值"

    def __init__(self, period: int = 20, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return ctx.ind("sma", period=self.period)


@register_factor
class MaSpreadFactor(IBaseFactor):
    """快慢均线相对距离：(MA_fast - MA_slow) / MA_slow
    正值代表多头排列，越大越强。用它比"金叉"更平滑，也更容易调阈值。"""
    name = "ma_spread"
    description = "快慢均线相对距离，(快-慢)/慢"

    def __init__(self, fast: int = 5, slow: int = 20, **kw) -> None:
        super().__init__(fast=fast, slow=slow, **kw)
        self.fast, self.slow = int(fast), int(slow)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        f = ctx.ind("sma", period=self.fast)
        s = ctx.ind("sma", period=self.slow)
        if f is None or s is None or s == 0:
            return None
        return (f - s) / s


@register_factor
class MaBiasFactor(IBaseFactor):
    """乖离率：(close - MA) / MA"""
    name = "ma_bias"
    description = "价格相对均线的乖离率"

    def __init__(self, period: int = 20, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return ctx.ind("bias", period=self.period)


@register_factor
class RSIFactor(IBaseFactor):
    """RSI"""
    name = "rsi"
    description = "RSI 相对强弱指标"

    def __init__(self, period: int = 14, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return ctx.ind("rsi", period=self.period)


@register_factor
class MomentumFactor(IBaseFactor):
    """N 日动量"""
    name = "momentum"
    description = "N 周期动量收益率"

    def __init__(self, period: int = 20, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return ctx.ind("momentum", period=self.period)


@register_factor
class VolatilityFactor(IBaseFactor):
    """归一化波动率：N 日收益标准差"""
    name = "volatility"
    description = "N 日收益率标准差，用于波动率过滤"

    def __init__(self, period: int = 20, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        closes = ctx.closes(self.period + 1)
        if closes.size < self.period + 1:
            return None
        with np.errstate(divide="ignore", invalid="ignore"):
            ret = closes[1:] / closes[:-1] - 1.0
        return float(np.std(ret, ddof=0))


@register_factor
class VolumeRatioFactor(IBaseFactor):
    """量比：当前成交量 / N 日均量"""
    name = "volume_ratio"
    description = "成交量相对均量的倍数"

    def __init__(self, period: int = 5, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        ma = ctx.ind("volume_ma", period=self.period)
        if ma is None or ma <= 0:
            return None
        bar = ctx.last_bar
        if bar is None:
            return None
        return float(bar.volume) / ma


@register_factor
class DrawdownFactor(IBaseFactor):
    """当前价格相对 N 日最高点的回撤（负值）"""
    name = "drawdown"
    description = "相对N日最高价的回撤比例，负值"

    def __init__(self, period: int = 60, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        highs = ctx.series.high
        if highs.size == 0:
            return None
        window = highs[-self.period:]
        peak = float(np.max(window))
        if peak <= 0:
            return None
        return (ctx.close - peak) / peak


@register_factor
class PricePositionFactor(IBaseFactor):
    """价格在 N 日区间中的位置，0=最低 1=最高"""
    name = "price_position"
    description = "价格在N日区间中的分位，0最低1最高"

    def __init__(self, period: int = 20, **kw) -> None:
        super().__init__(period=period, **kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        closes = ctx.closes(self.period)
        if closes.size == 0:
            return None
        lo, hi = float(np.min(closes)), float(np.max(closes))
        if hi == lo:
            return 0.5
        return (ctx.close - lo) / (hi - lo)


# ============================================================
# 事件因子（新闻 / 大模型）
# ============================================================
@register_factor
class NewsSentimentFactor(IBaseFactor):
    """新闻情感分 [-1, 1]，由事件通道注入 ctx.features['news_sentiment']"""
    name = "news_sentiment"
    description = "新闻情感分，[-1,1]，正为利好"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        v = ctx.feature("news_sentiment")
        return None if v is None else float(v)


@register_factor
class NewsConfidenceFactor(IBaseFactor):
    """大模型置信度 [0, 1]"""
    name = "news_confidence"
    description = "新闻分析置信度，[0,1]"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        v = ctx.feature("news_confidence")
        return None if v is None else float(v)


@register_factor
class NewsImpactFactor(IBaseFactor):
    """综合影响力 = 情感 × 置信度。低置信度的强情感会被自动打折。"""
    name = "news_impact"
    description = "新闻综合影响力 = 情感分 × 置信度"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        # 允许外部（例如自定义 LLM 分析器）直接给出影响力；没有则按 情感×置信度 推导
        explicit = ctx.feature("news_impact")
        if explicit is not None:
            return float(explicit)
        s = ctx.feature("news_sentiment")
        c = ctx.feature("news_confidence")
        if s is None or c is None:
            return None
        return float(s) * float(c)


@register_factor
class NewsCountFactor(IBaseFactor):
    """窗口内命中该标的新闻条数"""
    name = "news_count"
    description = "窗口内相关新闻条数"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        v = ctx.feature("news_count")
        return None if v is None else float(v)


BUILTIN_FACTORS = [
    "price",
    "ma",
    "ma_spread",
    "ma_bias",
    "rsi",
    "momentum",
    "volatility",
    "volume_ratio",
    "drawdown",
    "price_position",
    "news_sentiment",
    "news_confidence",
    "news_impact",
    "news_count",
]
