#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""券商接入点与交易日历命令。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import _print_endpoints, cli
# ============================== 券商接入点 ==============================
@cli.group(name="brokers", help="券商接入点：列出 / 试连接 / 看网关能力")
def brokers_group() -> None:
    """接入点 = 网关实现 + 资金账号 + 连接参数（见 config/brokers.json）。

    与服务无关，直接读配置 —— 所以**不要求服务在跑**，
    适合"改完配置先验一下能不能连上"。
    """

@brokers_group.command(name="list", help="列出全部接入点（凭据已脱敏）")
@click.option("--config", "config_path", default=None, help="接入点配置文件路径")
def brokers_list(config_path) -> None:
    from app.execution.endpoints import describe_endpoints
    from app.execution.gateway import gateway_signature, list_gateways

    data = describe_endpoints(config_path)
    _print_endpoints({
        **data,
        "gateways": [{"name": g, "params": gateway_signature(g)} for g in list_gateways()],
    })

@brokers_group.command(name="check", help="试连接一个接入点（只查询账户/持仓，不下单）")
@click.argument("endpoint", required=False, default="")
@click.option("--config", "config_path", default=None, help="接入点配置文件路径")
def brokers_check(endpoint, config_path) -> None:
    from app.execution.endpoints import load_endpoints
    from app.execution.gateway import create_gateway, has_gateway, list_gateways

    cfg = load_endpoints(config_path)
    try:
        ep = cfg.resolve(endpoint)
    except Exception as exc:
        click.secho(f"❌ {exc}", fg="red")
        sys.exit(1)
    if ep is None:
        click.secho(
            f"❌ 未找到接入点（配置 {cfg.path} 里没有 default_endpoint）", fg="red")
        click.echo(f"  可用: {[e.id for e in cfg.list()] or '（无）'}")
        sys.exit(1)
    if not ep.enabled:
        click.secho(f"❌ 接入点 {ep.id} 已被禁用（enabled=false）", fg="red")
        sys.exit(1)
    if not has_gateway(ep.gateway):
        click.secho(f"❌ 网关 {ep.gateway!r} 未注册 | 可用: {list_gateways()}", fg="red")
        sys.exit(1)

    click.secho(f"正在连接 {ep.id}（{ep.label}）…", fg="cyan")
    gw = None
    try:
        gw = create_gateway(ep.gateway, **ep.gateway_kwargs())
        gw.connect()
        acct = gw.query_account()
        poss = gw.query_positions()
        click.secho(
            f"✅ 连接成功 | 网关 {getattr(gw, 'name', ep.gateway)} | "
            f"账号 {getattr(gw, 'account', '') or ep.account or '-'}"
            + ("（只读档位）" if getattr(gw, "readonly", False) else ""),
            fg="green", bold=True,
        )
        click.echo(
            f"  总资产 {acct.total_asset:.2f} | 可用 {acct.available:.2f} | "
            f"冻结 {acct.frozen:.2f} | 市值 {acct.market_value:.2f}"
        )
        if poss:
            click.secho(f"  {'标的':<10}{'持仓':>10}{'可卖':>10}{'成本':>10}", fg="cyan")
            for sym, p in sorted(poss.items()):
                click.echo(f"  {sym:<10}{p.size:>10}{p.sellable:>10}{p.avg_price:>10.3f}")
        else:
            click.echo("  持仓: 空")
    except Exception as exc:
        click.secho(f"❌ 连接失败: {type(exc).__name__}: {exc}", fg="red")
        sys.exit(1)
    finally:
        if gw is not None:
            try:
                gw.disconnect()
            except Exception:
                pass

@brokers_group.command(name="gateways", help="列出可用网关与其可配参数")
def brokers_gateways() -> None:
    from app.execution.gateway import gateway_signature, list_gateways

    names = list_gateways()
    if not names:
        click.secho("没有任何已注册的网关", fg="yellow")
        return
    for n in names:
        click.secho(f"- {n}", fg="cyan", bold=True)
        for k, v in (gateway_signature(n) or {}).items():
            click.echo(f"    {k:18s} 默认 {v!r}")

# ============================== 交易日历 ==============================
@cli.group(name="calendar", help="交易日历：判断今天开不开市、什么时候开")
def calendar_group() -> None:
    """闭市时不该还在请求数据 —— 这是"什么时候能采"的唯一判据。"""

@calendar_group.command(name="status", help="当前市场状态与最近交易日")
def calendar_status() -> None:
    from app.domain.calendar import STATE_NAMES, get_calendar

    cal = get_calendar()
    info = cal.to_dict()
    click.secho(
        f"市场状态：{info['state_name']}（{info['state']}）", fg="cyan", bold=True
    )
    click.echo(f"  日历来源     {info['source']}")
    click.echo(f"  是否交易日   {'是' if info['is_trading_day'] else '否'}")
    click.echo(f"  是否可采集   {'是' if info['is_open'] else '否'}")
    click.echo(f"  最近交易日   {info['last_trading_day']}")
    click.echo(f"  下次开盘     {info['next_open']}")
    click.echo(f"  休市日数量   {info['holiday_count']}")
    days = cal.recent_trading_days(5)
    click.echo(f"  近 5 个交易日 {'、'.join(str(d) for d in days)}")
    if not info["is_trading_day"]:
        click.secho("  → 非交易日，采集服务不会请求数据", fg="yellow")

@calendar_group.command(name="refresh", help="联网校准交易日历（写到 config/trade_calendar.json）")
def calendar_refresh() -> None:
    """节假日每年官方公告一次，靠内置表迟早会过期。

    刷新**失败时不动现有日历** —— 日历缺失会导致该采的时候不采，
    那比多采几次严重得多。
    """
    from app.domain.calendar import refresh_from_network

    r = refresh_from_network()
    if not r.get("ok"):
        click.secho(f"❌ 日历刷新失败（沿用现有日历）: {r.get('error')}", fg="red")
        click.echo("   内置表已含 2025/2026 年交易所公告的休市安排，可正常使用。")
        click.echo("   也可以手工编辑 config/trade_calendar.json。")
        sys.exit(1)
    click.secho(f"✅ 交易日历已更新 | 交易日 {r['trading_days']} 天 | "
                f"休市 {r['holidays']} 天", fg="green", bold=True)
    click.echo(f"   范围 {r['range'][0]} ~ {r['range'][1]}")
    click.echo(f"   文件 {r['path']}")
