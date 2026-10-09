#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : result.py
# @Description : 回测结果与指标计算（与 backtrader 解耦，纯 pandas/numpy）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    """一次回测的完整产出。"""

    symbol: str
    strategy: str
    start: str
    end: str

    initial_cash: float
    final_cash: float

    total_return_pct: float = 0.0
    annual_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    calmar: float = 0.0
    volatility_pct: float = 0.0

    trade_count: int = 0
    win_count: int = 0
    lose_count: int = 0
    win_rate_pct: float = 0.0
    avg_win: float = 0.0
    avg_lose: float = 0.0
    profit_factor: float = 0.0

    bar_count: int = 0
    # 回测期间出现过的最小持仓。纯多头策略应恒 >= 0，
    # 出现负值说明有"空仓卖出/僵尸止损单"之类的缺陷
    min_position: float = 0.0
    config: Dict = field(default_factory=dict)
    trades: List[Dict] = field(default_factory=list)

    # 权益曲线不进 JSON 摘要，单独导出 CSV
    equity_curve: Optional[pd.DataFrame] = field(default=None, repr=False)

    @property
    def net_profit(self) -> float:
        return self.final_cash - self.initial_cash

    def summary(self) -> Dict:
        """不含权益曲线的可序列化摘要。"""
        d = asdict(self)
        d.pop("equity_curve", None)
        d["net_profit"] = round(self.net_profit, 2)
        return d


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤（%），输入为权益序列。"""
    if equity is None or len(equity) < 2:
        return 0.0
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max.replace(0, np.nan)
    return float(abs(drawdown.min()) * 100)


def annualized_return(initial: float, final: float, days: int) -> float:
    """复利年化收益率（%）。days<=0 或结果无意义时返回 0。"""
    if days <= 0 or initial <= 0:
        return 0.0
    try:
        return float(((final / initial) ** (365.0 / days) - 1) * 100)
    except (OverflowError, ZeroDivisionError, ValueError):
        return 0.0


def sharpe_ratio(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """年化夏普比率。样本不足或波动为 0 时返回 0.0（不抛异常）。"""
    if returns is None or len(returns) < 2:
        return 0.0
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    std = float(r.std(ddof=1))
    if std == 0 or np.isnan(std):
        return 0.0
    excess = r - risk_free_rate / periods_per_year
    return float(excess.mean() / std * np.sqrt(periods_per_year))


def build_result(
    config,
    equity_curve: pd.DataFrame,
    initial_cash: float,
    final_cash: float,
    trade_stats: Dict,
    trades: Optional[List[Dict]] = None,
    min_position: float = 0.0,
) -> BacktestResult:
    """把回测原始产出组装成 BacktestResult。"""
    equity = equity_curve["value"] if "value" in equity_curve else equity_curve.iloc[:, 0]
    equity = pd.Series(equity).astype(float).dropna()

    if len(equity) >= 2:
        daily_returns = equity.pct_change().dropna().replace([np.inf, -np.inf], np.nan).dropna()
    else:
        daily_returns = pd.Series(dtype=float)

    days = 0
    if len(equity_curve) >= 2:
        idx = pd.to_datetime(equity_curve.index)
        days = max((idx[-1] - idx[0]).days, 1)

    total_return = (
        (final_cash - initial_cash) / initial_cash * 100 if initial_cash else 0.0
    )
    ann = annualized_return(initial_cash, final_cash, days)
    mdd = max_drawdown(equity)
    sr = sharpe_ratio(
        daily_returns,
        risk_free_rate=config.risk_free_rate,
        periods_per_year=config.trading_days_per_year,
    )
    vol = (
        float(daily_returns.std(ddof=1) * np.sqrt(config.trading_days_per_year) * 100)
        if len(daily_returns) >= 2
        else 0.0
    )
    calmar = (ann / mdd) if mdd > 0 else 0.0

    trade_count = int(trade_stats.get("total", 0))
    win_count = int(trade_stats.get("won", 0))
    lose_count = int(trade_stats.get("lost", 0))
    win_rate = (win_count / trade_count * 100) if trade_count else 0.0
    avg_win = float(trade_stats.get("avg_win", 0.0))
    avg_lose = float(trade_stats.get("avg_lose", 0.0))
    gross_win = float(trade_stats.get("gross_win", 0.0))
    gross_lose = float(trade_stats.get("gross_lose", 0.0))
    profit_factor = (gross_win / abs(gross_lose)) if gross_lose else 0.0

    return BacktestResult(
        symbol=config.symbol,
        strategy=config.strategy,
        start=config.start_iso,
        end=config.end_iso,
        initial_cash=round(float(initial_cash), 2),
        final_cash=round(float(final_cash), 2),
        total_return_pct=round(total_return, 2),
        annual_return_pct=round(ann, 2),
        max_drawdown_pct=round(mdd, 2),
        sharpe=round(sr, 3),
        calmar=round(calmar, 3),
        volatility_pct=round(vol, 2),
        trade_count=trade_count,
        win_count=win_count,
        lose_count=lose_count,
        win_rate_pct=round(win_rate, 2),
        avg_win=round(avg_win, 2),
        avg_lose=round(avg_lose, 2),
        profit_factor=round(profit_factor, 3),
        bar_count=int(len(equity_curve)),
        min_position=float(min_position),
        config=config.to_dict(),
        trades=trades or [],
        equity_curve=equity_curve,
    )
