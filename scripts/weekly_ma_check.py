#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : weekly_ma_check.py
# @Description : 周均线/周线因子的离线自检：
#                用 pandas 的 resample('W') 独立算一遍，与 core 里的实现逐点对齐，
#                确认"周聚合 + 当周实时价"的口径与行情软件一致，并演示金叉/趋势因子
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""跑法::

    python scripts/weekly_ma_check.py

不联网、不写库，纯内存。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.factor import create_factor
from app.core.factor.context import FactorContext
from app.core.indicator import create_indicator
from app.core.market.series import BarSeries
from app.core.market.types import Bar
from app.test.helpers import make_uptrend


def build_series(df: pd.DataFrame, symbol: str = "000001") -> BarSeries:
    s = BarSeries(symbol, maxlen=2000)
    for ts, row in df.iterrows():
        s.append(
            Bar(
                symbol=symbol,
                dt=ts.to_pydatetime(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                amount=float(row.get("amount", 0.0)),
            )
        )
    return s


def pandas_weekly_ma_at(closes: pd.Series, period: int) -> np.ndarray:
    """逐点参照实现：在第 i 根只能用 closes[:i+1]，再聚合成周线做 N 周滚动均值。

    必须逐点截断——先在整段数据上 resample 再回填，等价于"站在周一就知道周五收盘价"，
    那本身就是未来函数，拿它当参照只会得出错误结论。
    """
    out = np.full(len(closes), np.nan, dtype=float)
    for i in range(len(closes)):
        weekly = closes.iloc[: i + 1].resample("W").last().dropna()
        if weekly.size < period:
            continue
        out[i] = float(weekly.rolling(period).mean().iloc[-1])
    return out


def main() -> int:
    days = 260
    df = make_uptrend(days=days, start="2023-01-02")
    closes = df["close"]
    series = build_series(df)

    failures = 0
    for period in (5, 10):
        mine = create_indicator("wma", period=period, field="close").compute(series)
        ref = pandas_weekly_ma_at(closes, period)

        mask = ~np.isnan(mine) & ~np.isnan(ref)
        n_common = int(mask.sum())
        if n_common == 0:
            print(f"❌ 周均线{period}: 与参照实现没有任何可比点位")
            failures += 1
            continue
        diff = np.abs(mine[mask] - ref[mask])
        worst = float(diff.max()) if diff.size else 0.0
        ok = worst < 1e-9
        print(
            f"{'✅' if ok else '❌'} 周均线{period}: 可比 {n_common} 点, "
            f"最大偏差 {worst:.3e}"
        )
        failures += 0 if ok else 1

        # 有效点位集合必须完全一致：差一个位置就意味着口径错了一位
        if n_common != int((~np.isnan(ref)).sum()) or n_common != int((~np.isnan(mine)).sum()):
            print(
                f"❌ 周均线{period}: 有效点位不一致 mine={int((~np.isnan(mine)).sum())} "
                f"ref={int((~np.isnan(ref)).sum())}"
            )
            failures += 1

        if np.isnan(mine[-1]):
            print(f"❌ 周均线{period}: 末尾仍为 nan（周数明显够用了）")
            failures += 1

    # ---------------- 因子演示 ----------------
    cross_up = create_factor("wma_cross_up", fast=5, slow=10)
    cross_dn = create_factor("wma_cross_down", fast=5, slow=10)
    trend_up = create_factor("wma_trend_up", fast=5, slow=10)
    trend_dn = create_factor("wma_trend_down", fast=5, slow=10)
    spread = create_factor("wma_spread", fast=5, slow=10)

    ups: list[str] = []
    downs: list[str] = []
    trend_days = 0
    bars = series.bars
    for i in range(len(bars)):
        sub = BarSeries(series.symbol, maxlen=2000)
        sub.extend(bars[: i + 1])
        ctx = FactorContext(series.symbol, sub)
        if cross_up.compute(ctx) == 1.0:
            ups.append(bars[i].dt.strftime("%Y-%m-%d"))
        if cross_dn.compute(ctx) == 1.0:
            downs.append(bars[i].dt.strftime("%Y-%m-%d"))
        if trend_up.compute(ctx) == 1.0:
            trend_days += 1
        _ = trend_dn.compute(ctx)
        _ = spread.compute(ctx)

    print()
    print(f"周线金叉日 ({len(ups)}): {ups}")
    print(f"周线死叉日 ({len(downs)}): {downs}")
    print(f"趋势向上天数: {trend_days} / {len(bars)}")

    if not ups:
        print("❌ 上涨行情里一次周线金叉都没出现，口径可能有误")
        failures += 1
    if trend_days == 0:
        print("❌ 上涨行情里没有一天判定为趋势向上")
        failures += 1

    # 周线交叉必须比日线交叉稀疏得多 —— 这是"周线过滤噪音"的意义所在
    daily_cross = 0
    for i in range(len(bars)):
        sub = BarSeries(series.symbol, maxlen=2000)
        sub.extend(bars[: i + 1])
        ctx = FactorContext(series.symbol, sub)
        f0, s0 = ctx.ind("sma", period=25), ctx.ind("sma", period=50)
        f1, s1 = ctx.ind("sma", idx=1, period=25), ctx.ind("sma", idx=1, period=50)
        if None not in (f0, s0, f1, s1) and (f1 - s1) <= 0 < (f0 - s0):
            daily_cross += 1
    print(f"（对比）近似日线 25/50 金叉次数: {daily_cross}")

    print()
    print("全部通过 ✅" if failures == 0 else f"有 {failures} 项不通过 ❌")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
