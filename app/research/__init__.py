#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 回测模块
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.research.config import BacktestConfig
from app.research.result import BacktestResult
from app.research.runner import BacktestError, BacktestRunner, run_backtest
from app.research.report import export_report, print_report, render_text

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
