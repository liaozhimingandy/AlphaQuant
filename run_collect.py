#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : run_collect.py
# @Description : 兼容旧入口：等价于 `python main.py collect -s <symbol>`
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
import sys

from app.cli import cli

if __name__ == "__main__":
    # 没有额外参数时默认采集 000001，保持旧脚本行为
    if len(sys.argv) == 1:
        sys.argv += ["collect", "-s", "000001"]
    cli()
