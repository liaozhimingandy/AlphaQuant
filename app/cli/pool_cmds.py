#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票池命令：候选池收窄 → 综合评分 → 推荐。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
# ============================== 股票池 ==============================
@cli.group(name="pool", help="股票池：涨停池/题材/资金流 → 综合评分 → 推荐")
def pool_group() -> None:
    """先用一个小候选池收窄范围，再精细打分 —— 全市场逐只拉数据既慢又必被限流。"""

@pool_group.command(name="refresh", help="刷新股票池（拉候选 → 同步数据 → 打分 → 落库）")
# 刻意写成字面量而不是 import SOURCES：CLI 启动不该为了一个选项列表
# 就把 akshare 那一串依赖拉起来（app.alpha.pool 会间接导入它）。
@click.option("--source", default=None,
              type=click.Choice(["limitup", "hot", "theme", "flow", "watch", "all"]),
              help="候选池来源，默认取 config/pool.json")
@click.option("--limit", default=None, type=int, help="保留前几名")
@click.option("--date", "day", default=None, help="评分日期，默认今天")
@click.option("--sync/--no-sync", "do_sync", default=None,
              help="是否联网同步（--no-sync 只用库里已有数据，离线可用）")
def pool_refresh(source, limit, day, do_sync) -> None:
    from app.alpha.pool import StockPool

    pool = StockPool()
    report = pool.refresh(source=source, day=day, limit=limit, do_sync=do_sync)
    if report.get("error"):
        click.secho(f"❌ {report['error']}", fg="red")
        sys.exit(1)
    info = report.get("candidates") or {}
    click.secho(
        f"候选池 {report['source']}（{info.get('note','')}）→ 打了 {report['count']} 只",
        fg="cyan", bold=True,
    )
    _print_pool(report.get("results") or [])

@pool_group.command(name="top", help="查看已保存的推荐（从库里读，离线可用）")
@click.option("--limit", default=30, show_default=True, type=int)
@click.option("--date", "day", default=None, help="默认最新一天")
@click.option("--min-score", default=None, type=float)
def pool_top(limit, day, min_score) -> None:
    from app.alpha.pool import StockPool

    rows = StockPool.top(day=day, limit=limit, min_score=min_score)
    if not rows:
        click.secho("库里还没有评分。先跑 `python main.py pool refresh`。", fg="yellow")
        return
    _print_pool(rows)

@pool_group.command(name="sync", help="单独同步扩展数据（资金流/筹码/基本面/舆情/宏观…）")
@click.argument("what", required=False,
                type=click.Choice(["fundflow", "chip", "fundamental", "limitup",
                                   "boards", "concepts",
                                   "lhb", "margin", "forecast", "holders",
                                   "rating", "news", "macro", "breadth",
                                   "all"]),
                default="all")
@click.option("--symbol", multiple=True, help="标的，可重复；不传则用 config/pool.json 的 watch")
def pool_sync(what, symbol) -> None:
    from app.alpha.pool import (
        sync_boards,
        sync_category,
        sync_chip,
        sync_concept_members,
        sync_fund_flow,
        sync_fundamentals,
        sync_limitup,
    )

    #: 个股级类别（macro/breadth 是市场级，不需要标的）
    STOCK_LEVEL = ("fundflow", "chip", "fundamental", "lhb", "margin",
                   "forecast", "holders", "rating", "news")
    syms = [str(s) for s in symbol]
    if not syms:
        from app.alpha.pool import PoolConfig

        syms = PoolConfig.load().watch
    if not syms and what in STOCK_LEVEL + ("all",):
        click.secho("❌ 没有标的：用 --symbol 指定，或在 config/pool.json 的 watch 里配", fg="red")
        sys.exit(1)

    done = False
    if what in ("fundflow", "all"):
        r = sync_fund_flow(syms)
        _echo_sync("资金流", r)
        done = True
    if what in ("chip", "all"):
        _echo_sync("筹码", sync_chip(syms))
        done = True
    if what in ("fundamental", "all"):
        _echo_sync("基本面", sync_fundamentals(syms))
        done = True
    if what in ("limitup", "all"):
        _echo_sync("涨停池", sync_limitup())
        done = True
    if what in ("boards", "all"):
        _echo_sync("概念板块", sync_boards("concept"))
        _echo_sync("行业板块", sync_boards("industry"))
        done = True
    if what in ("concepts", "all"):
        _echo_sync("题材成分映射", sync_concept_members())
        done = True
    # ---- 多类别截面数据（通用因子值表）----
    for cat, label in (("lhb", "龙虎榜"), ("margin", "融资融券"),
                       ("forecast", "业绩预告"), ("holders", "股东户数"),
                       ("rating", "机构评级"), ("news", "新闻舆情")):
        if what in (cat, "all"):
            _echo_sync(label, sync_category(cat, syms))
            done = True
    # ---- 市场级 ----
    if what in ("macro", "all"):
        _echo_sync("宏观指标", sync_category("macro"))
        done = True
    if what in ("breadth", "all"):
        _echo_sync("市场宽度", sync_category("breadth"))
        done = True
    if not done:
        click.secho(f"未知项: {what}", fg="red")

@pool_group.command(name="categories", help="列出可采集的数据类别与库里已有指标")
def pool_categories() -> None:
    from app.datasource.service.facade import CATEGORIES
    from app.storage.repo import kline_repo as R

    click.secho("可采集类别：", fg="cyan", bold=True)
    for key, info in CATEGORIES.items():
        click.echo(f"  {key:<10} {info['name']:<8} {info['freq']:<6} {info['doc']}")
    rows = R.factor_categories()
    if rows:
        click.secho("\n库里已有指标：", fg="cyan", bold=True)
        for r in rows:
            click.echo(f"  {r['category']:<10} {r['name']:<24} {r['rows']} 行")

@pool_group.command(name="demo", help="生成合成数据做演示（本机无网络时也能看到效果）")
@click.option("--symbols", default=12, show_default=True, help="造多少只票")
@click.option("--date", "day", default=None, help="数据日期，默认今天")
def pool_demo(symbols, day) -> None:
    """离线演示：造一批仿真数据 → 打分 → 排名。

    这不是"假数据糊弄" —— 它验证的是**打分与排序链路本身**，
    数据获取那一段（marketx）在联网后单独用 `pool sync` 验证。
    """
    from app.alpha.pool.demo import seed_demo

    d = seed_demo(int(symbols), day)
    click.secho(f"已生成 {d['symbols']} 只票的合成数据（日期 {d['date']}）", fg="green")
    from app.alpha.pool import StockPool

    # 直接指定标的，不走候选池 —— 候选池要联网拉名单，而这里就是要验证离线链路
    report = StockPool().refresh(symbols=d["all"], day=d["date"],
                                 do_sync=False, limit=20, source="demo")
    _print_pool(report.get("results") or [])
    click.echo("   （强票带 ★：持续净流入+加速+涨停+热门题材；弱票带 ☆）")

def _echo_sync(label: str, r: Dict[str, Any]) -> None:
    if not r:
        return
    failed = r.get("failed") or []
    click.secho(f"  {label}: 成功 {r.get('ok', 0)} 项", fg="green" if not failed else "yellow")
    for name, reason in failed[:5]:
        click.echo(f"    ✗ {name}: {reason[:100]}")
    if len(failed) > 5:
        click.echo(f"    … 还有 {len(failed) - 5} 项失败")

def _print_pool(results: List[Dict[str, Any]]) -> None:
    if not results:
        click.secho("没有结果", fg="yellow")
        return
    click.secho(
        f"{'#':<4}{'代码':<9}{'评分':>7} {'等级':<5} "
        "资金 题材 筹码 技术 涨停 基本面 舆情 宏观",
        fg="cyan",
    )
    for r in results:
        dims = r.get("dims") or {}
        cells = []
        for k in ("capital", "theme", "chip", "technical", "limitup",
                  "fundamental", "sentiment", "macro"):
            v = dims.get(k)
            cells.append(f"{v:>5.0f}" if v is not None else "    —")
        click.echo(
            f"{r.get('rank') or '-':<4}{r.get('symbol'):<9}"
            f"{(r.get('total') if r.get('total') is not None else 0):>7.1f} "
            f"{r.get('grade') or '-':<5} " + " ".join(cells)
        )
