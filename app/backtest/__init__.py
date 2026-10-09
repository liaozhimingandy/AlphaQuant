#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 回测模块
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.backtest.config import BacktestConfig
from app.backtest.result import BacktestResult
from app.backtest.runner import BacktestError, BacktestRunner, run_backtest
from app.backtest.report import export_report, print_report, render_text

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "BacktestRunner",
    "BacktestError",
    "run_backtest",
    "print_report",
    "render_text",
    "export_report",
]
