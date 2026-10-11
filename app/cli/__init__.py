#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaQuant 命令行入口包。

拆包动机：原先 app/cli.py 单文件 2642 行、70 个命令，改一条 help 都要
在两千多行里翻。现在按**职责**切，找命令先看文件名：

    base.py           主命令组 cli + 跨模块共用工具（不 import 任何 *_cmds）
    data_cmds.py      数据面：init-db / collect / dataguard / sources / providers / list
    strategy_cmds.py  策略面：strategies / strategy / backtest
    engine_cmds.py    引擎面：engine / serve / start / stop / status / restart / snapshot
    control_cmds.py   远程控制：ctl 全家（走监控面板 HTTP API）
    research_cmds.py  研究面：factors / rules / tasks / flow / score / factor
    pool_cmds.py      股票池：pool 全家
    hub_cmds.py       多服务编排：hub 全家
    broker_cmds.py    券商接入点与交易日历

依赖方向（单向，无环）：
    __init__  →  base  →  app.common.*
                 ↑
            *_cmds 各自只依赖 base

命令注册是 import 的**副作用**：*_cmds 用 ``@cli.command(...)`` 装饰器
在导入时把命令挂到 base.cli 上。所以 __init__ 必须导入全部子模块，
缺一个就少一批命令 —— 这也是下面用 import 而不是 __getattr__ 懒加载的原因。
"""
from __future__ import annotations

from app.cli.base import cli

# 导入即注册：顺序无关，但全都要在。
from app.cli import (  # noqa: E402,F401
    broker_cmds,
    control_cmds,
    data_cmds,
    engine_cmds,
    hub_cmds,
    pool_cmds,
    research_cmds,
    strategy_cmds,
)
from app.cli.base import DEFAULT_END  # noqa: F401

__all__ = ["cli", "DEFAULT_END", "main"]


def main() -> None:
    """控制台脚本入口（见 pyproject 的 [project.scripts]）。"""
    cli()
