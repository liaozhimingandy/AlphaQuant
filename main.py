#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : main.py
# @Description : AlphaQuant 统一入口
#               原先这里堆了 5 个实验性质的 run_backtest1~5，
#               已全部收敛到 app/cli.py，回测逻辑统一在 app/backtest/。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
import sys

from app.cli import cli

if __name__ == "__main__":
    # 不带参数时直接打印帮助，而不是报错退出
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    cli()
