#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : report.py
# @Description : 回测报告：控制台渲染 + JSON/CSV 落盘
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd

from app.backtest.result import BacktestResult
from app.core.config import settings
from app.utils.logger import logger


def render_text(result: BacktestResult) -> str:
    """渲染成人类可读的文本报告。"""
    line = "=" * 68
    half = "-" * 68

    profit_sign = "+" if result.net_profit >= 0 else ""
    rows = [
        (f"{result.symbol} | {result.strategy}", f"{result.start} ~ {result.end}"),
    ]

    out = [
        "",
        line,
        "📊 回测结果",
        line,
        f"  标的/策略    : {result.symbol}  |  {result.strategy}",
        f"  回测区间     : {result.start} ~ {result.end}  ({result.bar_count} 根K线)",
        half,
        f"  初始资金     : {result.initial_cash:>14,.2f}",
        f"  最终资产     : {result.final_cash:>14,.2f}",
        f"  净盈亏       : {profit_sign}{result.net_profit:>13,.2f}",
        f"  总收益率     : {result.total_return_pct:>14.2f}%",
        f"  年化收益率   : {result.annual_return_pct:>14.2f}%",
        half,
        f"  最大回撤     : {result.max_drawdown_pct:>14.2f}%",
        f"  年化波动率   : {result.volatility_pct:>14.2f}%",
        f"  夏普比率     : {result.sharpe:>14.3f}",
        f"  卡玛比率     : {result.calmar:>14.3f}",
        half,
        f"  交易次数     : {result.trade_count:>14d}",
        f"  盈利/亏损    : {result.win_count:>14d} / {result.lose_count}",
        f"  胜率         : {result.win_rate_pct:>14.2f}%",
        f"  平均盈利     : {result.avg_win:>14,.2f}",
        f"  平均亏损     : {result.avg_lose:>14,.2f}",
        f"  盈亏比       : {result.profit_factor:>14.3f}",
        half,
        f"  最小持仓     : {result.min_position:>14.0f} 股"
        + ("   ⚠️ 出现空头持仓" if result.min_position < 0 else ""),
        line,
    ]
    return "\n".join(out)


def print_report(result: BacktestResult) -> None:
    print(render_text(result))


def export_report(result: BacktestResult, output_dir: str | None = None) -> Dict[str, str]:
    """导出回测结果：摘要 JSON + 成交明细 CSV + 权益曲线 CSV。"""
    settings.ensure_dirs()
    outdir = Path(output_dir) if output_dir else settings.OUTPUT_DIR
    outdir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{result.symbol}_{result.strategy}_{stamp}"

    files: Dict[str, str] = {}

    summary_path = outdir / f"{base}_summary.json"
    summary_path.write_text(
        json.dumps(result.summary(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    files["summary"] = str(summary_path)

    if result.trades:
        trades_path = outdir / f"{base}_trades.csv"
        pd.DataFrame(result.trades).to_csv(
            trades_path, index=False, encoding="utf-8-sig"
        )
        files["trades"] = str(trades_path)

    if result.equity_curve is not None and not result.equity_curve.empty:
        equity_path = outdir / f"{base}_equity.csv"
        result.equity_curve.to_csv(equity_path, encoding="utf-8-sig")
        files["equity"] = str(equity_path)

    logger.info(f"回测结果已导出到: {outdir}")
    for k, v in files.items():
        logger.info(f"  - {k}: {v}")
    return files


def compare_table(results: List[BacktestResult]) -> str:
    """多标的/多策略横向对比表。"""
    if not results:
        return "(无结果)"

    headers = ["标的", "策略", "收益率%", "年化%", "回撤%", "夏普", "交易", "胜率%"]
    rows = [
        [
            r.symbol,
            r.strategy,
            f"{r.total_return_pct:.2f}",
            f"{r.annual_return_pct:.2f}",
            f"{r.max_drawdown_pct:.2f}",
            f"{r.sharpe:.2f}",
            str(r.trade_count),
            f"{r.win_rate_pct:.1f}",
        ]
        for r in results
    ]
    widths = [
        max(len(str(headers[i])), *(len(str(r[i])) for r in rows))
        for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt.format(*headers), sep, *[fmt.format(*r) for r in rows]])
