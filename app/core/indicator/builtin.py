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


def _week_index(dts) -> np.ndarray:
    """把日线的时间序列映射成「周序号」：同一自然周同一个 id，跨周 +1。

    用 ISO 年 + ISO 周做键，跨年是天然正确的（不会出现第 52 周回滚成 0）。
    """
    n = len(dts)
    out = np.empty(n, dtype=np.int64)
    prev_key = None
    cur = -1
    for i, d in enumerate(dts):
        cal = d.isocalendar()
        key = (cal[0], cal[1])
        if key != prev_key:
            cur += 1
            prev_key = key
        out[i] = cur
    return out


def weekly_ma(
    series: BarSeries,
    period: int,
    field: str = "close",
    method: str = "sma",
) -> np.ndarray:
    """在**日线**序列上直接算「N 周均线」，返回值拉回到日线长度（便于按位置对齐）。

    口径与通达信/同花顺一致：

      - 每周取该周**最后一根**日线的收盘价作为周收盘价；
      - 当周还没走完时，用最新一根日线的收盘价当「本周实时价」参与计算。

    所以同一周里每天算出来的周均线会随最新价变化 —— 这正是行情软件的行为，
    也是"盘中就能看到本周金叉成型"的原因。

    ``method='sma'`` 需要至少 ``period`` 个周（含当周）；
    ``method='ema'`` 因为用前 ``period`` 周做种子，需要 ``period + 1`` 个周。

    :param period: 周数（5 = 5 周均线）
    :param field: 参与聚合的字段（close/high/low/open），每根日线取该字段值参与周聚合
    :param method: sma / ema
    """
    values = np.asarray(getattr(series, field), dtype=float)
    n = values.size
    out = np.full(n, np.nan, dtype=float)
    if n == 0 or period <= 0:
        return out

    wk = _week_index(series.dt)
    n_weeks = int(wk[-1]) + 1

    # 每周最后一根日线的下标 → 该周收盘价
    last_of_week = np.zeros(n_weeks, dtype=np.int64)
    for i in range(n):
        last_of_week[wk[i]] = i
    wc = values[last_of_week]

    # 第 i 根日线（位于第 k 周）能看到的周收盘价序列 = wc[0..k-1] + [values[i]]
    #   刻意排除 wc[k]：那是"本周还没走完的最终收盘价"，用它就是未来函数。
    #   当 i 恰好就是本周最后一根时 values[i] == wc[k]，不泄漏也不重复。
    k = wk

    if str(method).lower() == "ema":
        # EMA 可以按周递推：当周只需在"上周的周均线"上再走一步，全周常数可复用
        ema_wc = _ema(wc, period)          # EMA over 已完成周序列
        prev = np.full(n, np.nan, dtype=float)
        moved = k > 0
        prev[moved] = ema_wc[k[moved] - 1]
        alpha = 2.0 / (period + 1.0)
        with np.errstate(invalid="ignore"):
            cur = np.asarray(values, dtype=float)
            out = alpha * cur + (1.0 - alpha) * prev
        # prev 为 nan（周数不够做种子）的位置自然也是 nan，无需再mask
        return out

    # SMA：用前缀和把"取最近 period-1 个已完成周"做成 O(1)
    prefix = np.concatenate(([0.0], np.cumsum(wc)))
    lo = np.maximum(k - (period - 1), 0)
    sums = prefix[k] - prefix[lo]
    totals = sums + values
    valid = (k - lo + 1) >= period
    out[valid] = totals[valid] / period
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


class WeeklyMAIndicator(IBaseIndicator):
    """周均线（N 周均线，默认 5 周）。

    直接在日线序列上算，不需要另外准备周线数据 —— 这点很关键：
    实时链路里每来一根日线就要能立刻看到本周均线到哪了，
    如果要求"必须先把周线拼出来"，策略就会整整延迟一周。
    """

    name = "wma"

    def __init__(
        self,
        period: int = 5,
        field: str = "close",
        method: str = "sma",
    ) -> None:
        super().__init__(period=period, field=field, method=method)
        self.period = int(period)
        self.field = str(field or "close")
        self.method = str(method or "sma").lower()
        if self.method not in ("sma", "ema"):
            raise ValueError(f"周均线不支持的算法: {method} | 可用: sma, ema")

    def compute(self, series: BarSeries) -> np.ndarray:
        return weekly_ma(series, self.period, self.field, self.method)


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
    WeeklyMAIndicator,
]
