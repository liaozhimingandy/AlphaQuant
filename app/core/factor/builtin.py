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
# 周线因子（周均线 / 5周10周金叉死叉 / 趋势）
# ============================================================
class _WeeklyBaseFactor(IBaseFactor):
    """周线类因子的公共部分：周数参数 + 取值口径。"""

    def __init__(
        self,
        fast: int = 5,
        slow: int = 10,
        method: str = "sma",
        field: str = "close",
        **kw,
    ) -> None:
        super().__init__(fast=fast, slow=slow, method=method, field=field, **kw)
        self.fast, self.slow = int(fast), int(slow)
        self.method = str(method or "sma").lower()
        self.field = str(field or "close")

    def _shared(self) -> dict:
        return {"method": self.method, "field": self.field}

    def _wma(self, ctx: FactorContext, period: int, idx: int = 0) -> Optional[float]:
        return ctx.ind("wma", idx=idx, period=period, **self._shared())


@register_factor
class WeeklyMAFactor(_WeeklyBaseFactor):
    """N 周均线值。默认 5 周。"""
    name = "wma"
    description = "N 周均线值（默认5周）"

    def __init__(self, period: int = 5, **kw) -> None:
        super().__init__(**kw)
        self.period = int(period)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return self._wma(ctx, self.period)


@register_factor
class WeeklyMaSpreadFactor(_WeeklyBaseFactor):
    """快慢周均线的相对距离：(快周线 - 慢周线) / 慢周线。

    比"金叉"平滑，适合做阈值型过滤（例如要求 > 0.01 才算真正多头排列）。
    """
    name = "wma_spread"
    description = "快慢周均线相对距离，(5周-10周)/10周"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        f = self._wma(ctx, self.fast)
        s = self._wma(ctx, self.slow)
        if f is None or s is None or s == 0:
            return None
        return (f - s) / s


@register_factor
class WeeklyCrossUpFactor(_WeeklyBaseFactor):
    """周线金叉：快周线上穿慢周线那一根返回 1，其余返回 0。

    这是**事件型**因子（只在穿越当根为 1）。要"持续多头"请用 wma_trend_up，
    要"刚发生金叉后的 N 根内都算"请用规则层的 cooldown 风控配合。
    """
    name = "wma_cross_up"
    description = "周线金叉（5周上穿10周），发生当根为1"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return _week_cross(ctx, self, want="up")


@register_factor
class WeeklyCrossDownFactor(_WeeklyBaseFactor):
    """周线死叉：快周线下穿慢周线那一根返回 1，其余返回 0。"""
    name = "wma_cross_down"
    description = "周线死叉（5周下穿10周），发生当根为1"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return _week_cross(ctx, self, want="down")


def _week_cross(ctx: FactorContext, owner: _WeeklyBaseFactor, want: str) -> Optional[float]:
    """判定周线交叉。want='up' 金叉 / 'down' 死叉。"""
    f_now = owner._wma(ctx, owner.fast, idx=0)
    f_pre = owner._wma(ctx, owner.fast, idx=1)
    s_now = owner._wma(ctx, owner.slow, idx=0)
    s_pre = owner._wma(ctx, owner.slow, idx=1)
    if None in (f_now, f_pre, s_now, s_pre):
        return None
    d_now = float(f_now) - float(s_now)
    d_pre = float(f_pre) - float(s_pre)
    if want == "up":
        return 1.0 if (d_pre <= 0 < d_now) else 0.0
    return 1.0 if (d_pre >= 0 > d_now) else 0.0


@register_factor
class WeeklyTrendUpFactor(_WeeklyBaseFactor):
    """周线趋势向上：**状态型**因子，满足条件的每天都返回 1。

    判定口径（每一项都可以在配置里关掉）：

      - ``need_ma_order``  快周线在慢周线之上（默认开）
      - ``need_slope``     快周线相对上一根在上行（默认开）
      - ``need_price_above`` 收盘价站在快周线之上（默认开）
      - ``min_spread``     快慢周线的最小分离度，过滤刚交叉时的毛刺（默认 0）
    """
    name = "wma_trend_up"
    description = "周线趋势向上（多头排列+斜率向上），状态型"

    def __init__(self, min_spread: float = 0.0, **kw) -> None:
        super().__init__(**kw)
        self.min_spread = float(min_spread)
        self.need_ma_order = _as_bool(kw.get("need_ma_order"), True)
        self.need_slope = _as_bool(kw.get("need_slope"), True)
        self.need_price_above = _as_bool(kw.get("need_price_above"), True)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return _week_trend(ctx, self, direction=1)


@register_factor
class WeeklyTrendDownFactor(_WeeklyBaseFactor):
    """周线趋势向下（``wma_trend_up`` 的镜像），用于做空过滤或清仓条件。"""
    name = "wma_trend_down"
    description = "周线趋势向下（空头排列+斜率向下），状态型"

    def __init__(self, min_spread: float = 0.0, **kw) -> None:
        super().__init__(**kw)
        self.min_spread = float(min_spread)
        self.need_ma_order = _as_bool(kw.get("need_ma_order"), True)
        self.need_slope = _as_bool(kw.get("need_slope"), True)
        self.need_price_above = _as_bool(kw.get("need_price_above"), True)

    def compute(self, ctx: FactorContext) -> Optional[float]:
        return _week_trend(ctx, self, direction=-1)


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _week_trend(ctx: FactorContext, owner: _WeeklyBaseFactor, direction: int) -> Optional[float]:
    """趋势判定。direction=1 向上 / -1 向下。返回 1.0 / 0.0 / None。"""
    f_now = owner._wma(ctx, owner.fast, idx=0)
    s_now = owner._wma(ctx, owner.slow, idx=0)
    if f_now is None or s_now is None:
        return None

    if owner.need_ma_order:
        if (f_now - s_now) * direction <= 0:
            return 0.0

    if getattr(owner, "min_spread", 0.0) > 0:
        if s_now == 0:
            return None
        spread = (f_now - s_now) / s_now
        if abs(spread) < owner.min_spread:
            return 0.0

    if owner.need_slope:
        f_pre = owner._wma(ctx, owner.fast, idx=1)
        if f_pre is None:
            return None
        if (f_now - f_pre) * direction <= 0:
            return 0.0

    if owner.need_price_above:
        price = ctx.close
        if price is None or (isinstance(price, float) and np.isnan(price)):
            return None
        # 向上要求价格站上快线；向下要求价格跌破快线
        if (price - f_now) * direction <= 0:
            return 0.0

    return 1.0


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
    "wma",
    "wma_spread",
    "wma_cross_up",
    "wma_cross_down",
    "wma_trend_up",
    "wma_trend_down",
    "news_sentiment",
    "news_confidence",
    "news_impact",
    "news_count",
]
