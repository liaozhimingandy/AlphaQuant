#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : cli.py
# @Description : AlphaQuant 统一命令行入口
#               所有能力都从这里进，替换掉原先散落的 main.py / run_collect.py 实验脚本
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.common.config import settings
from app.common.logging import logger, setup_logging


DEFAULT_END = datetime.now().strftime("%Y-%m-%d")

def _parse_params(pairs: Tuple[str, ...]) -> dict:
    """把 --param fast=5 --param slow=20 解析成 dict（自动推断数值类型）。"""
    params: dict = {}
    for pair in pairs or ():
        if "=" not in pair:
            raise click.BadParameter(f"参数格式应为 key=value，收到: {pair}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        value: object = raw.strip()
        lowered = str(value).lower()
        if lowered in {"true", "false"}:
            value = lowered == "true"
        else:
            try:
                value = int(value)  # type: ignore[arg-type]
            except ValueError:
                try:
                    value = float(value)  # type: ignore[arg-type]
                except ValueError:
                    pass
        params[key] = value
    return params

@click.group(
    name="alphaquant",
    help=click.style("🚀 AlphaQuant 量化工具箱", fg="cyan", bold=True),
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option("--log-level", default=None, help="日志级别 DEBUG/INFO/WARNING/ERROR")
def cli(log_level: Optional[str]) -> None:
    """AlphaQuant 主入口。"""
    if log_level:
        setup_logging(level=log_level, force=True)

def _print_endpoints(data: Dict) -> None:
    if data.get("error"):
        click.secho(f"❌ {data['error']}", fg="red")
        sys.exit(1)
    click.secho(f"配置文件: {data.get('path') or '（未找到）'}", fg="blue")
    active = data.get("in_use") or data.get("active") or ""
    rows = data.get("endpoints") or []
    if not rows:
        click.secho("没有任何接入点。示例见 config/brokers.json", fg="yellow")
        return
    click.secho(f"{'接入点':<14}{'网关':<12}{'账号':<14}{'只读':<6}{'启用':<6}名称", fg="cyan")
    for e in rows:
        mark = " ← 使用中" if e.get("id") == active else ""
        click.echo(
            f"{e.get('id',''):<14}{e.get('gateway',''):<12}{e.get('account',''):<14}"
            f"{str(e.get('readonly')):<6}{str(e.get('enabled')):<6}{e.get('name','')}{mark}"
        )
    click.secho(f"\n可用网关: {', '.join(g['name'] for g in data.get('gateways') or [])}",
                fg="blue")
