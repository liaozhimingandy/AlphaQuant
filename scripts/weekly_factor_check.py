#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : weekly_factor_check.py
# @Description : 周均线因子自检：5周/10周金叉死叉与趋势判断
#               构造"先跌后暴涨再回落"的日线，交叉点应当可被精确复现，
#               并且与"手工按 pandas 周线计算"的结果逐点对齐
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.factor.context import FactorContext  # noqa: E402
from app.core.factor.registry import create_factor  # noqa: E402
from app.core.market.series import BarSeries  # noqa: E402
from app.core.market.types import Bar  # noqa: E402

FAST, SLOW = 5, 10


def build_series() -> tuple[BarSeries, pd.DatetimeIndex]:
    """V 形 + 冲高回落：足够让 5 周均线在 10 周均线上下来回穿越。"""
    down = np.linspace(20.0, 10.0, 70)
    up = np.linspace(10.2, 30.0, 80)
    fall = np.linspace(29.5, 18.0, 70)
    close = np.concatenate([down, up, fall])
    idx = pd.date_range("2025-06-02", periods=len(close), freq="B")
    series = BarSeries("TEST", maxlen=1000)
    for dt, c in zip(idx, close):
        series.append(Bar(
            symbol="TEST", dt=dt.to_pydatetime(),
            open=float(c), high=float(c * 1.01),
            low=float(c * 0.99), close=float(c), volume=1e6,
        ))
    return series, idx


def pandas_weekly_cross() -> pd.Series:
    """参照实现：逐根K线独立用 pandas 重算，与本项目代码完全隔离。

    这里**刻意不使用** ``resample('W')...reindex(method='ffill')`` 那种写法——
    它有两个致命缺陷：

      - ``ffill``：周内每个交易日拿到的都是**上一周**的周线值，整周滞后。
        用它做参照，任何"周金叉"信号都会晚一周才被发现。
      - ``bfill``：周一就拿到了本周五的收盘价，是彻头彻尾的**未来函数**。
        回测用它赚到的钱，实盘一分都拿不到。

    正确做法是逐根重算：站在第 k 根上时只能用 ``closes[:k+1]``，
    当周尚未走完就用"已有最后一根"当周代表价——这也正是本项目的语义。
    """
    series, idx = build_series()
    above = []
    for k in range(idx.size):
        closes = pd.Series(series.close[: k + 1], index=idx[: k + 1])
        weekly = closes.resample("W").last().dropna()
        fast = weekly.rolling(FAST).mean()
        slow = weekly.rolling(SLOW).mean()
        cur_f, cur_s = fast.iloc[-1], slow.iloc[-1]
        above.append(0.0 if (pd.isna(cur_f) or pd.isna(cur_s) or cur_f == cur_s)
                     else float(cur_f > cur_s))
    s = pd.Series(above, index=idx)
    return s.diff()


def main() -> int:
    series, idx = build_series()
    failures = 0

    ctx_of = lambda k: FactorContext("TEST", _truncate(series, k + 1))

    up = create_factor("wma_cross_up", fast=FAST, slow=SLOW)
    down = create_factor("wma_cross_down", fast=FAST, slow=SLOW)
    trend_up = create_factor("wma_trend_up", fast=FAST, slow=SLOW)
    trend_down = create_factor("wma_trend_down", fast=FAST, slow=SLOW)
    spread = create_factor("wma_spread", fast=FAST, slow=SLOW)
    ma = create_factor("wma", period=FAST)

    mine_up, mine_dn, mine_tup, mine_tdn = [], [], [], []
    for k in range(idx.size):
        c = ctx_of(k)
        mine_up.append(1.0 if up.compute(c) else 0.0)
        mine_dn.append(1.0 if down.compute(c) else 0.0)
        mine_tup.append(1.0 if trend_up.compute(c) else 0.0)
        mine_tdn.append(1.0 if trend_down.compute(c) else 0.0)

    ref = pandas_weekly_cross()
    ref_up = ((ref == 1).astype(float)).to_numpy()
    ref_dn = ((ref == -1).astype(float)).to_numpy()

    # 1) 金叉位置必须与 pandas 参照完全一致
    hit_up = int(np.sum(np.array(mine_up) * ref_up))
    hit_dn = int(np.sum(np.array(mine_dn) * ref_dn))
    n_ref_up, n_ref_dn = int(ref_up.sum()), int(ref_dn.sum())
    n_mine_up, n_mine_dn = int(sum(mine_up)), int(sum(mine_dn))

    print("=" * 66)
    print(f"参照实现(pandas): 金叉 {n_ref_up} 次 / 死叉 {n_ref_dn} 次")
    print(f"本项目  wma    : 金叉 {n_mine_up} 次 / 死叉 {n_mine_dn} 次")
    print(f"位置完全吻合    : 金叉 {hit_up}/{n_ref_up}  死叉 {hit_dn}/{n_ref_dn}")

    ok_cross = (hit_up == n_ref_up) and (hit_dn == n_ref_dn) and n_ref_up > 0 and n_ref_dn > 0
    print(f"{'✅' if ok_cross else '❌'} 交叉点定位" + (
        "" if ok_cross else "  ← 与参照实现不一致"))
    failures += 0 if ok_cross else 1

    # 2) 金叉触发之后必须有一段"趋势向上"的持续期，而不是只闪一天
    first_up = int(np.argmax(np.array(mine_up) > 0))
    window = mine_tup[first_up:first_up + 30]
    sustained = sum(window) / max(1, len(window))
    ok_sustained = sustained > 0.9
    print(f"{'✅' if ok_sustained else '❌'} 金叉后趋势持续: 随后 30 根K线里 "
          f"{int(sum(window))} 根判定为向上({sustained:.0%})")
    failures += 0 if ok_sustained else 1

    # 3) 趋势向上/向下互斥
    both = [i for i in range(idx.size) if mine_tup[i] and mine_tdn[i]]
    ok_excl = not both
    print(f"{'✅' if ok_excl else '❌'} 趋势判定互斥" + (
        "" if ok_excl else f"  ← 有 {len(both)} 根同时判定为向上+向下"))
    failures += 0 if ok_excl else 1

    # 4) 周数不足时必须返回 nan / False，而不是 0
    early = ctx_of(3)
    early_ma = ma.compute(early)
    early_up = up.compute(early)
    ok_early = (early_ma is None or np.isnan(float(early_ma))) and not early_up
    print(f"{'✅' if ok_early else '❌'} 预热期不误报: wma={early_ma}, cross_up={early_up}")
    failures += 0 if ok_early else 1

    # 5) spread 在金叉处应当由负转正
    sp_after = spread.compute(ctx_of(first_up))
    ok_spread = sp_after is not None and float(sp_after) > 0
    print(f"{'✅' if ok_spread else '❌'} 金叉日 spread 转正: {sp_after}")
    failures += 0 if ok_spread else 1

    # 6) 因子可被规则层消费（这是能否用在 config/tasks.json 里的关键）
    from app.core.rule.spec import build_rule

    rule = build_rule({
        "factor": "wma_cross_up", "op": "eq", "value": 1,
        "params": {"fast": FAST, "slow": SLOW},
    })
    fired = rule.is_satisfied(ctx_of(first_up))
    quiet = rule.is_satisfied(ctx_of(first_up + 5))
    ok_rule = fired and not quiet
    print(f"{'✅' if ok_rule else '❌'} 规则层可用: 金叉日触发={fired}, 5天后={quiet}"
          f"（交叉是一次性事件，不能持续为真）")
    failures += 0 if ok_rule else 1

    print("=" * 66)
    if failures:
        print(f"❌ 有 {failures} 项未通过")
    else:
        print("✅ 周均线因子全部通过（与 pandas 独立实现逐点对齐）")
        print("\n可用因子: wma / wma_spread / wma_cross_up / wma_cross_down "
              "/ wma_trend_up / wma_trend_down")
        print("在 tasks.json 里这样用:")
        print('  {"factor": "wma_cross_up", "op": "eq", "value": 1,'
              ' "params": {"fast": 5, "slow": 10}}')
    return 1 if failures else 0


def _truncate(series: BarSeries, n: int) -> BarSeries:
    """取前 n 根 bar 组成的新序列（模拟"走到第 n 根"）。"""
    sub = BarSeries(series.symbol, maxlen=series.maxlen)
    sub.extend(list(series.bars)[:n])
    return sub


if __name__ == "__main__":
    sys.exit(main())
