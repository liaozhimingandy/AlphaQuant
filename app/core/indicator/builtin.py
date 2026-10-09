#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : builtin.py
# @Description : 内置指标。全部用 numpy 手写，不依赖 TA-Lib（Windows 装不上）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import numpy as np

from app.core.indicator.base import IBaseIndicator
from app.core.market.series import BarSeries


def _rolling_mean(arr: np.ndarray, period: int) -> np.ndarray:
    """滚动均值，前 period-1 个位置为 nan。"""
    out = np.full(arr.shape, np.nan, dtype=float)
    if arr.size < period:
        return out
    cumsum = np.cumsum(np.insert(arr, 0, 0.0))
    out[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def _rolling_std(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full(arr.shape, np.nan, dtype=float)
    if arr.size < period:
        return out
    for i in range(period - 1, arr.size):
        out[i] = np.std(arr[i - period + 1: i + 1], ddof=0)
    return out


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均（alpha = 2/(n+1)，与通达信/同花顺口径一致）。"""
    out = np.full(arr.shape, np.nan, dtype=float)
    if arr.size < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = np.mean(arr[:period])
    for i in range(period, arr.size):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _wilder_ma(arr: np.ndarray, period: int) -> np.ndarray:
    """Wilder 平滑（RSI / ATR 用的那种），本质是 alpha=1/n 的 EMA。"""
    out = np.full(arr.shape, np.nan, dtype=float)
    if arr.size < period:
        return out
    out[period - 1] = np.mean(arr[:period])
    for i in range(period, arr.size):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return out


class SMAIndicator(IBaseIndicator):
    """简单移动平均"""
    name = "sma"

    def __init__(self, period: int = 5, field: str = "close") -> None:
        super().__init__(period=period, field=field)
        self.period = int(period)
        self.field = field

    def compute(self, series: BarSeries) -> np.ndarray:
        return _rolling_mean(getattr(series, self.field), self.period)


class EMAIndicator(IBaseIndicator):
    """指数移动平均"""
    name = "ema"

    def __init__(self, period: int = 5, field: str = "close") -> None:
        super().__init__(period=period, field=field)
        self.period = int(period)
        self.field = field

    def compute(self, series: BarSeries) -> np.ndarray:
        return _ema(getattr(series, self.field), self.period)


class RSIIndicator(IBaseIndicator):
    """RSI 相对强弱（Wilder 口径）"""
    name = "rsi"

    def __init__(self, period: int = 14) -> None:
        super().__init__(period=period)
        self.period = int(period)

    def compute(self, series: BarSeries) -> np.ndarray:
        close = series.close
        out = np.full(close.shape, np.nan, dtype=float)
        if close.size <= self.period:
            return out
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        avg_gain = _wilder_ma(gain[1:], self.period)
        avg_loss = _wilder_ma(loss[1:], self.period)
        # avg_* 长度比 close 少 1，写回时右移一位对齐
        with np.errstate(divide="ignore", invalid="ignore"):
            rs = avg_gain / avg_loss
            rsi = 100.0 - 100.0 / (1.0 + rs)
        rsi = np.where(avg_loss == 0, 100.0, rsi)
        out[1:] = rsi
        return out


class MACDIndicator(IBaseIndicator):
    """MACD 主值（DIF）"""
    name = "macd"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        super().__init__(fast=fast, slow=slow, signal=signal)
        self.fast, self.slow, self.signal = int(fast), int(slow), int(signal)

    def _dif(self, series: BarSeries) -> np.ndarray:
        return _ema(series.close, self.fast) - _ema(series.close, self.slow)

    def compute(self, series: BarSeries) -> np.ndarray:
        return self._dif(series)


class MACDSignalIndicator(MACDIndicator):
    """MACD 信号线（DEA）"""
    name = "macd_signal"

    def compute(self, series: BarSeries) -> np.ndarray:
        return _ema(np.nan_to_num(self._dif(series)), self.signal)


class MACDHistIndicator(MACDIndicator):
    """MACD 柱状图"""
    name = "macd_hist"

    def compute(self, series: BarSeries) -> np.ndarray:
        dif = self._dif(series)
        dea = _ema(np.nan_to_num(dif), self.signal)
        return dif - dea


class ATRIndicator(IBaseIndicator):
    """ATR 真实波幅"""
    name = "atr"

    def __init__(self, period: int = 14) -> None:
        super().__init__(period=period)
        self.period = int(period)

    def compute(self, series: BarSeries) -> np.ndarray:
        high, low, close = series.high, series.low, series.close
        if close.size < 2:
            return np.full(close.shape, np.nan, dtype=float)
        prev_close = np.roll(close, 1)
        prev_close[0] = close[0]
        tr = np.maximum(
            high - low,
            np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
        )
        # tr[0] 用的是自身收盘价，无意义，丢弃后再做 Wilder 平滑
        return _wilder_ma(tr[1:], self.period)


class VolumeMAIndicator(IBaseIndicator):
    """成交量均线"""
    name = "volume_ma"

    def __init__(self, period: int = 5) -> None:
        super().__init__(period=period)
        self.period = int(period)

    def compute(self, series: BarSeries) -> np.ndarray:
        return _rolling_mean(series.volume, self.period)


class MomentumIndicator(IBaseIndicator):
    """动量：当前价 / N 周期前价格 - 1"""
    name = "momentum"

    def __init__(self, period: int = 20) -> None:
        super().__init__(period=period)
        self.period = int(period)

    def compute(self, series: BarSeries) -> np.ndarray:
        close = series.close
        out = np.full(close.shape, np.nan, dtype=float)
        if close.size <= self.period:
            return out
        prev = np.roll(close, self.period)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[self.period:] = close[self.period:] / prev[self.period:] - 1.0
        return out


class BiasIndicator(IBaseIndicator):
    """乖离率：(close - MA) / MA"""
    name = "bias"

    def __init__(self, period: int = 20) -> None:
        super().__init__(period=period)
        self.period = int(period)

    def compute(self, series: BarSeries) -> np.ndarray:
        close = series.close
        ma = _rolling_mean(close, self.period)
        with np.errstate(divide="ignore", invalid="ignore"):
            return (close - ma) / ma


class StdIndicator(IBaseIndicator):
    """滚动标准差（波动率因子用）"""
    name = "std"

    def __init__(self, period: int = 20) -> None:
        super().__init__(period=period)
        self.period = int(period)

    def compute(self, series: BarSeries) -> np.ndarray:
        return _rolling_std(series.close, self.period)


BUILTIN_INDICATORS = [
    SMAIndicator,
    EMAIndicator,
    RSIIndicator,
    MACDIndicator,
    MACDSignalIndicator,
    MACDHistIndicator,
    ATRIndicator,
    VolumeMAIndicator,
    MomentumIndicator,
    BiasIndicator,
    StdIndicator,
]
