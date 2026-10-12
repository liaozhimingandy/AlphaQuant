#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研究面命令：因子实验室 / 指标清单 / 规则 / 任务配置 / 资金流与评分。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
# ============================== 能力清单 ==============================
@cli.command(name="factors", help="列出可用指标与因子（含每个指标的可配参数）")
@click.option("--params", is_flag=True, help="展开每个指标的可配参数与默认值")
def factors_cmd(params) -> None:
    from app.modules.factor import list_factors
    from app.modules.factor.indicator import list_indicators
    from app.modules.factor.indicator.registry import describe_indicator

    click.secho("指标:", fg="cyan", bold=True)
    for n in list_indicators():
        spec = describe_indicator(n)
        sig = "  ".join(
            f"{k}={v['default']}" if not v["required"] else f"{k}=<必填>"
            for k, v in spec.items()
        )
        if params:
            click.echo(f"  - {n:<14}{sig}")
        else:
            click.echo(f"  - {n}")
    if not params:
        click.secho(
            "  （加 --params 看可配参数；周期是参数不是指标，"
            "如 sma 配 period 即可，不需要 sma5/sma20 两个指标）",
            fg="yellow",
        )

    click.secho("因子:", fg="cyan", bold=True)
    for n in list_factors():
        click.echo(f"  - {n}")

@cli.command(name="rules", help="列出可组合的规则类型与内置风控")
def rules_cmd() -> None:
    from app.modules.risk import list_risks
    from app.modules.strategy.rule import RULE_TYPES
    from app.modules.strategy import list_strategies

    click.secho("规则类型:", fg="cyan", bold=True)
    for n in RULE_TYPES:
        click.echo(f"  - {n}")
    click.secho("内置策略:", fg="cyan", bold=True)
    for n in list_strategies():
        click.echo(f"  - {n}")
    click.secho("风控规则:", fg="cyan", bold=True)
    for n in list_risks():
        click.echo(f"  - {n}")

# ============================== 任务配置 ==============================
@cli.command(name="tasks", help="查看 config 里配置的量化任务")
@click.option("--config", default=None, help="任务配置文件路径，默认 config/tasks.json")
def tasks_cmd(config) -> None:
    from app.engine.builder import load_task_config
    from app.runtime.tasking.spec import parse_task_specs

    cfg = load_task_config(config)
    specs = parse_task_specs(cfg.get("tasks"))
    if not specs:
        click.secho("没有启用任何任务", fg="yellow")
        return

    click.secho(
        f"{'任务ID':<16}{'标的':<10}{'策略':<16}{'资金':>12}  风控", fg="cyan", bold=True
    )
    for s in specs:
        stype = str(s.strategy.get("type") or "?")
        click.echo(
            f"{s.task_id or '(自动生成)':<16}{s.symbol:<10}{stype:<16}"
            f"{s.initial_cash:>12.0f}  {[r.get('type') for r in s.risk]}"
        )

# ============================== 资金流 / 筹码 ==============================
@cli.command(name="flow", help="查看个股资金流与筹码（主力净流入、加速度、获利盘…）")
@click.argument("symbol")
@click.option("--days", default=10, show_default=True, help="看最近几天资金流")
def flow_cmd(symbol, days) -> None:
    from app.modules.factor.features import get_provider
    from app.domain.symbol import normalize_symbol

    sym = normalize_symbol(symbol)
    f = get_provider(ttl=0).get(sym)
    click.secho(f"资金流 / 筹码 · {sym}", fg="cyan", bold=True)
    rows = [
        ("主力净流入", f.main_net, lambda v: f"{v/1e8:+.2f} 亿" if v is not None else "无数据"),
        ("占成交额比", f.main_net_pct, lambda v: f"{v:+.2%}" if v is not None else "无数据"),
        ("连续净流入", float(f.net_days), lambda v: f"{int(v)} 天"),
        ("流入加速度", f.accel, lambda v: f"{v:.2f}×" if v is not None else "无数据"),
        ("资金脉冲", f.pulse, lambda v: f"{v:.2f}×" if v is not None else "无数据"),
        ("获利盘比例", f.profit_ratio, lambda v: f"{v:.0%}" if v is not None else "无数据"),
        ("筹码集中度", f.chip_concentration, lambda v: f"{v:.3f}" if v is not None else "无数据"),
        ("现价距筹码峰", f.peak_gap, lambda v: f"{v:+.1%}" if v is not None else "无数据"),
        ("题材", "、".join(f.themes) or "无", str),
        ("题材排名", f.theme_rank, lambda v: f"第 {int(v)} 名" if v is not None else "无数据"),
    ]
    for name, value, fmt in rows:
        click.echo(f"  {name:<12} {fmt(value)}")
    if f.is_limitup:
        click.echo(f"  今日涨停      {f.continuous} 连板，封板 {f.seal_amount or 0}"
                   f"，炸板 {f.broken_times} 次")
    missing = [k for k, v in f.available.items() if not v]
    if missing:
        click.secho(
            f"  ⚠ 以下维度无数据（打分时会排除，不按 0 算）: {', '.join(missing)}"
            " —— 先跑 `main.py pool sync` 同步",
            fg="yellow",
        )

# ============================== 综合评分 ==============================
@cli.command(name="score", help="看某只票的多维度综合评分明细")
@click.argument("symbol")
@click.option("--date", "day", default=None, help="评分日期，默认最新")
def score_cmd(symbol, day) -> None:
    from app.modules.factor.features import get_provider
    from app.modules.universe.score import ScoreEngine
    from app.domain.symbol import normalize_symbol

    sym = normalize_symbol(symbol)
    r = ScoreEngine(provider=get_provider(ttl=0)).score(sym, day)
    if r.total is None:
        click.secho(f"{sym}: 无法评分", fg="yellow")
        for w in r.reasons:
            click.echo(f"  · {w}")
        return
    click.secho(f"{sym} → {r.total} 分（{r.grade}）", fg="cyan", bold=True)
    names = ScoreEngine.DIM_NAMES
    for k, v in (r.dims or {}).items():
        w = (r.weights or {}).get(k)
        tag = "" if r.available.get(k) else "  [无数据]"
        if v is None:
            click.echo(f"  {names.get(k,k):<6} {'—':>6}{tag}")
        else:
            click.echo(f"  {names.get(k,k):<6} {v:>6.1f}"
                       f"   × 权重 {w:.2f}{tag}" if w else
                       f"  {names.get(k,k):<6} {v:>6.1f}{tag}")
    click.echo("  ---")
    for w in r.reasons:
        click.echo(f"  · {w}")

# ============================== 因子实验室 ==============================
@cli.group(name="factor", help="因子实验室：写公式加因子、检验有效性、机器学习挖因子")
def factor_group() -> None:
    """因子是策略的核心。这里是把"想到一个因子"变成"验证过的因子"的地方。"""

def _default_symbols(symbols) -> list:
    """没指定标的时，用库里已有的（截面检验至少要 3 个才有意义）。"""
    syms = [str(s) for s in (symbols or ())]
    if syms:
        return syms
    from app.data.engine import SessionLocal
    from app.data.repo.stock_repo import StockRepository

    with SessionLocal() as db:
        return [r["symbol"] for r in StockRepository.list_symbols(db)]

@factor_group.command(name="list", help="列出因子库里的因子")
@click.option("--category", default="", help="按分类过滤")
@click.option("--source", default="", help="按来源过滤：user/mined/preset")
def factor_list(category, source) -> None:
    from app.modules.factor.library import get_library

    lib = get_library()
    rows = lib.list(category=category, source=source)
    if not rows:
        click.secho("因子库是空的。用 `factor add` 加一条，或 `factor presets` 装预置的。",
                    fg="yellow")
        return
    click.secho(f"{'名称':<20}{'分类':<8}{'来源':<8}{'分数':>6}  表达式", fg="cyan")
    for s in rows:
        score = s.metrics.get("score")
        click.echo(
            f"{s.name:<20}{s.category:<8}{s.source:<8}"
            f"{(f'{score:.0f}' if score is not None else '—'):>6}  {s.expr[:44]}"
        )

@factor_group.command(name="ops", help="列出表达式可用算子")
def factor_ops() -> None:
    from app.modules.factor.expr import func_catalog

    for f in func_catalog():
        arity = (f"{f['min_args']}" if f["min_args"] == f["max_args"]
                 else f"{f['min_args']}~{f['max_args']}")
        click.secho(f"  {f['name']:<12}", fg="cyan", nl=False)
        click.echo(f"({arity} 参数)  {f['doc']}")

@factor_group.command(name="vars", help="列出表达式可用变量")
def factor_vars() -> None:
    from app.modules.factor.expr import variable_catalog

    for v in variable_catalog():
        click.secho(f"  {v['name']:<10}", fg="cyan", nl=False)
        click.echo(v["doc"])

@factor_group.command(name="add", help="新增一条因子（表达式）")
@click.argument("name")
@click.argument("expr")
@click.option("--desc", default="", help="说明")
@click.option("--category", default="自定义", help="分类")
@click.option("--overwrite", is_flag=True, help="已存在时覆盖")
def factor_add(name, expr, desc, category, overwrite) -> None:
    from app.modules.factor.library import get_library

    ok, msg, spec = get_library().add(
        name, expr, description=desc, category=category, overwrite=overwrite)
    if not ok:
        click.secho(f"❌ {msg}", fg="red")
        sys.exit(1)
    click.secho(f"✅ {msg} | {spec.name} = {spec.expr}", fg="green")

@factor_group.command(name="remove", help="删除一条因子")
@click.argument("name")
def factor_remove(name) -> None:
    from app.modules.factor.library import get_library

    ok, msg = get_library().remove(name)
    click.secho(("✅ " if ok else "❌ ") + msg, fg="green" if ok else "red")
    if not ok:
        sys.exit(1)

@factor_group.command(name="presets", help="装入预置经典因子（只补不覆盖）")
@click.option("--overwrite", is_flag=True, help="覆盖已存在的同名因子")
def factor_presets(overwrite) -> None:
    from app.modules.factor.library import install_presets

    n = install_presets(overwrite=overwrite)
    click.secho(f"✅ 已写入 {n} 个预置因子", fg="green")

@factor_group.command(name="check", help="检验一个表达式因子有没有预测力")
@click.argument("expr")
@click.option("--symbol", multiple=True, help="标的，可重复；默认用库里全部")
@click.option("--start", default=None, help="起始日期")
@click.option("--end", default=None, help="结束日期")
@click.option("--horizon", default=5, show_default=True, help="未来多少天收益作为标签")
@click.option("--groups", default=5, show_default=True, help="分层组数")
def factor_check(expr, symbol, start, end, horizon, groups) -> None:
    from app.modules.factor.evaluate import evaluate_factor

    syms = _default_symbols(symbol)
    if len(syms) < 3:
        click.secho("⚠ 截面检验至少需要 3 个标的才有意义（2 个点的相关系数恒为 ±1）",
                    fg="yellow")
    r = evaluate_factor(expr, syms, start, end, horizon=int(horizon),
                        groups=int(groups))
    click.secho(f"因子 {expr}", fg="cyan", bold=True)
    if r.score is None:
        click.secho(f"  {r.verdict}", fg="yellow")
        return
    click.echo(f"  样本 {r.rows} 行 / {r.dates} 天 / {r.symbols} 个标的"
               f"（口径：{'截面' if r.mode == 'cross_section' else '时间序列'}）")
    click.echo(f"  IC     {r.ic_mean:+.4f}（标准差 {r.ic_std:.4f}）")
    click.echo(f"  ICIR   {r.icir:+.3f}       t 值 {r.ic_t:+.2f}")
    if r.rank_ic_mean is not None:
        click.echo(f"  RankIC {r.rank_ic_mean:+.4f}（IR {r.rank_ic_ir:+.3f}）")
    click.echo(f"  IC>0 占比 {r.ic_positive_rate:.1%}")
    if r.quantile_returns:
        cells = "  ".join(f"Q{k}:{v:+.3f}" for k, v in r.quantile_returns.items())
        click.echo(f"  分层收益(%)  {cells}")
        click.echo(f"  多空(Q{groups}-Q1) {r.long_short:+.3f}%   单调: {'是' if r.monotonic else '否'}")
    click.secho(f"  综合 {r.score} 分 → {r.verdict}", fg="green" if r.score >= 70 else "yellow")

@factor_group.command(name="mine", help="遗传算法自动挖掘因子")
@click.option("--symbol", multiple=True, help="标的，可重复；默认用库里全部")
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--horizon", default=5, show_default=True, help="未来多少天收益")
@click.option("--generations", default=12, show_default=True, help="迭代代数")
@click.option("--population", default=60, show_default=True, help="每代候选数")
@click.option("--top", default=10, show_default=True, help="返回前几名")
@click.option("--seconds", default=120, show_default=True, help="时间预算（秒）")
@click.option("--min-score", default=45.0, show_default=True, help="低于此分不返回")
@click.option("--save", is_flag=True, help="把挖到的因子存入因子库（source=mined）")
def factor_mine(symbol, start, end, horizon, generations, population, top,
                seconds, min_score, save) -> None:
    """挖出来的因子**绝大多数是过拟合的** —— 所以默认只打印不入库存。

    要入库请显式加 `--save`，并且先用 `--min-score` 把门槛调高。
    """
    from app.modules.factor.evaluate import load_panel
    from app.modules.factor.library import SOURCE_MINED, get_library
    from app.modules.factor.mining.mining import FactorMiner

    syms = _default_symbols(symbol)
    panel = load_panel(syms, start, end)
    if len(panel) < 3:
        click.secho("❌ 可用标的少于 3 个，做不了截面检验（先采集更多标的）", fg="red")
        sys.exit(1)

    click.secho(f"开始挖掘 | {len(panel)} 个标的 | 预算 {seconds}s", fg="cyan")
    miner = FactorMiner(panel, horizon=int(horizon), seed=None)
    found = miner.run(generations=int(generations), population=int(population),
                      top=int(top), max_seconds=float(seconds),
                      min_score=float(min_score),
                      progress=lambda g, tot, best: click.echo(
                          f"  第 {g}/{tot} 代，当前最优 {best:.1f} 分"))
    click.secho(f"\n评估了 {miner.evaluated} 个表达式，命中 {len(found)} 个",
                fg="green" if found else "yellow")
    for i, f in enumerate(found, 1):
        click.echo(f"  {i}. {f.score:5.1f}分 IC={f.ic:+.3f} ICIR={f.icir:+.2f}")
        click.echo(f"     {f.expr}")
    if save and found:
        lib = get_library()
        n = 0
        for f in found:
            ok, _, _ = lib.add(f"mined_{n + 1}_{abs(hash(f.expr)) % 10000:04d}",
                               f.expr, description=f"自动挖掘（{f.score}分）",
                               category="挖掘", source=SOURCE_MINED,
                               metrics={"ic": f.ic, "icir": f.icir,
                                        "score": f.score}, overwrite=True)
            n += bool(ok)
        click.secho(f"✅ 已入库 {n} 个挖掘因子", fg="green")

@factor_group.command(name="fit", help="用机器学习给因子库里的因子配权重（Lasso 会筛）")
@click.option("--symbol", multiple=True, help="标的，可重复；默认用库里全部")
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--horizon", default=5, show_default=True)
@click.option("--method", type=click.Choice(["lasso", "ridge"]), default="lasso",
              show_default=True, help="lasso=会筛因子 / ridge=只缩不放零")
@click.option("--source", default="preset,user,mined",
              help="用哪些来源的因子，逗号分隔")
def factor_fit(symbol, start, end, horizon, method, source) -> None:
    """把因子库里的因子一起喂给线性模型，看谁能活下来。"""
    from app.modules.factor.evaluate import load_panel
    from app.modules.factor.library import get_library
    from app.modules.factor.mining.ml import fit_factors

    syms = _default_symbols(symbol)
    panel = load_panel(syms, start, end)
    if not panel:
        click.secho("❌ 没有可用行情数据", fg="red")
        sys.exit(1)
    wanted = {s.strip() for s in str(source).split(",") if s.strip()}
    specs = [s for s in get_library().list() if s.source in wanted] or \
        get_library().list()
    if not specs:
        click.secho("❌ 因子库是空的（先 `factor presets` 或 `factor add`）", fg="red")
        sys.exit(1)

    # 把每只票的因子序列首尾相接：目标是"因子值 → 未来收益"的映射
    import numpy as np

    from app.modules.factor.evaluate import compute_factor

    cols: Dict[str, list] = {}
    ys: list = []
    for sym, df in panel.items():
        try:
            fwd = df["close"].pct_change(int(horizon)).shift(-int(horizon)).to_numpy()
        except Exception:
            continue
        for spec in specs:
            try:
                fv = compute_factor(spec.expr, df)
            except Exception:
                continue
            cols.setdefault(spec.name, []).append(
                np.asarray(fv, dtype=float)[:len(fwd)])
        ys.append(np.asarray(fwd, dtype=float))

    features = {k: np.concatenate(v) for k, v in cols.items() if v}
    target = np.concatenate(ys) if ys else np.zeros(0)
    if not features or len(target) < 50:
        click.secho("❌ 样本不足（至少需要 50 行）", fg="red")
        sys.exit(1)

    r = fit_factors(features, target * 100.0, method=method)
    if r.r2_test is None:
        click.secho("样本不足，无法拟合", fg="yellow")
        return
    click.secho(f"{method} | alpha={r.alpha} | 训练 {r.n_train} / 测试 {r.n_test}",
                fg="cyan", bold=True)
    click.echo(f"  样本内 R² {r.r2_train:.4f}   样本外 R² {r.r2_test:.4f}   样本外 IC {r.ic_test:+.4f}")
    if r.collinear:
        click.secho("  共线剔除: " + ", ".join(
            f"{c[0]}≈{c[1]}({c[2]})" for c in r.collinear), fg="yellow")
    click.secho(f"  保留 {len(r.selected)}/{len(r.weights)} 个因子:", fg="green")
    for n, w in r.weights.items():
        mark = "✓" if n in r.selected else " "
        click.echo(f"   {mark} {n:<20} {w:+.4f}")
