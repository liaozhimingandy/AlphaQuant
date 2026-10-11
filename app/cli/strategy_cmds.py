#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略面命令：列出策略 / 生成模板 / 跑回测。

回测与实盘走同一套自研 IBaseStrategy（见 app.research.adapter 的桥接），
所以这里只有一份策略清单，不再有「回测策略」和「实盘策略」两套。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import DEFAULT_END, _parse_params, cli
from app.common.logging import logger
# ============================== 策略列表 ==============================
@cli.command(name="strategies", help="列出所有可用策略（含 strategies/ 下的自定义策略）")
@click.option("--reload", "do_reload", is_flag=True, help="重新扫描用户策略目录（改完策略代码不用重启）")
@click.option("--dir", "strategy_dir", default=None, help="临时指定用户策略目录")
def strategies_cmd(do_reload, strategy_dir) -> None:
    from app.research.registry import (
        list_strategies as bt_list,
        load_user_strategies as bt_load,
        user_strategy_dir,
    )
    from app.strategy import list_strategies as live_list
    from app.strategy.registry import load_user_strategies as live_load

    dirs = [strategy_dir] if strategy_dir else None
    user_bt = bt_load(dirs, force=True) if (do_reload or strategy_dir) else {}
    if do_reload or strategy_dir:
        live_load(dirs, force=True)

    click.secho("回测策略（backtrader 链路）:", fg="cyan", bold=True)
    for n in bt_list():
        tag = click.style("  [自定义]", fg="yellow") if n in user_bt else ""
        click.echo(f"  - {n}{tag}")
    click.secho("实盘策略（事件引擎链路）:", fg="cyan", bold=True)
    for n in live_list():
        click.echo(f"  - {n}")
    click.secho(f"\n用户策略目录: {user_strategy_dir()}", fg="blue")

# ============================== 新建自定义策略 ==============================
@cli.group(name="strategy", help="自定义策略：新建模板 / 看目录")
def strategy_group() -> None:
    pass

@strategy_group.command(name="dir", help="打印用户策略目录")
def strategy_dir_cmd() -> None:
    from app.research.registry import user_strategy_dir

    click.echo(user_strategy_dir())

@strategy_group.command(name="new", help="在用户策略目录里生成一个策略模板")
@click.argument("name")
@click.option("--kind", type=click.Choice(["backtest", "engine", "both"]),
              default="both", show_default=True, help="模板类型")
@click.option("--force", is_flag=True, help="已存在时覆盖")
def strategy_new_cmd(name, kind, force) -> None:
    from app.research.registry import user_strategy_dir

    target_dir = Path(user_strategy_dir())
    target_dir.mkdir(parents=True, exist_ok=True)
    file_name = _snake_case(name) + ".py"
    path = target_dir / file_name
    if path.exists() and not force:
        click.secho(f"❌ 已存在: {path}（要覆盖加 --force）", fg="red")
        sys.exit(1)
    path.write_text(_strategy_template(name, kind), encoding="utf-8")
    click.secho(f"✅ 已生成: {path}", fg="green")
    click.echo("   改完之后让服务重新扫描：")
    click.echo("     python main.py strategies --reload      # 本地")
    click.echo("     python main.py ctl strategies           # 运行中的服务")

def _snake_case(name: str) -> str:
    from app.strategy.discovery import snake

    return snake(name)

def _strategy_template(name: str, kind: str) -> str:
    cls = "".join(p.capitalize() for p in _snake_case(name).split("_")) or "My"
    key = _snake_case(name)
    backtest_part = f'''# ==================== 回测链路（backtrader） ====================
class {cls}Strategy(bt.Strategy):
    """{name}

    改 next() 里的判据即可。回测：python main.py backtest -s 000001 --strategy {key}
    """

    STRATEGY_NAME = "{key}"

    params = dict(fast=5, slow=20, lot_size=100, printlog=False)

    def __init__(self) -> None:
        self.ma_fast = bt.indicators.SMA(self.data.close, period=self.p.fast)
        self.ma_slow = bt.indicators.SMA(self.data.close, period=self.p.slow)
        self.cross = bt.indicators.CrossOver(self.ma_fast, self.ma_slow)
        self.order = None

    def notify_order(self, order) -> None:
        if order.status in (order.Submitted, order.Accepted):
            return
        self.order = None

    def next(self) -> None:
        # 有未完成订单时不动，避免同一根K线反复下单
        if self.order is not None:
            return
        if not self.position and self.cross > 0:
            price = float(self.data.close[0])
            size = int(self.broker.getcash() * 0.95 / price / 100) * 100
            if size > 0:
                self.order = self.buy(size=size)
        elif self.position and self.cross < 0:
            self.order = self.close()
'''
    engine_part = f'''# ==================== 实盘链路（事件引擎） ====================
class {cls}LiveStrategy(IBaseStrategy):
    """与上面同一套逻辑的实盘版。decide() 返回 Signal 或 None 即可。"""

    STRATEGY_NAME = "{key}_live"
    name = "{key}_live"
    description = "{name}（实盘版）"

    def __init__(self, fast: int = 5, slow: int = 20, **params) -> None:
        super().__init__(fast=fast, slow=slow, **params)
        self.fast = int(fast)
        self.slow = int(slow)

    def _cross(self, ctx):
        now_f, now_s = ctx.ind("sma", period=self.fast), ctx.ind("sma", period=self.slow)
        prev = ctx.previous(1)
        if prev is None:
            return 0
        pre_f, pre_s = prev.ind("sma", period=self.fast), prev.ind("sma", period=self.slow)
        if None in (now_f, now_s, pre_f, pre_s):
            return 0
        if pre_f <= pre_s and now_f > now_s:
            return 1
        if pre_f >= pre_s and now_f < now_s:
            return -1
        return 0

    def decide(self, ctx, state, source=SignalSource.BAR, event=None):
        cross = self._cross(ctx)
        if not state.has_position and cross > 0:
            return self.make_signal(state, Side.BUY, source, reason="金叉")
        if state.has_position and cross < 0:
            return self.make_signal(state, Side.SELL, source, reason="死叉")
        return None
'''
    header = (
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        f'"""{name} —— 自定义策略（由 `main.py strategy new` 生成）。\n\n'
        "回测链路与实盘链路放在同一个文件里：同一个想法应该用同一套参数，\n"
        "而不是回测用 5/20、实盘手滑写成 5/30。\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from app.feature.context import FactorContext  # noqa: F401\n"
        "from app.domain.types import Side, Signal, SignalSource\n"
        "from app.strategy.base import IBaseStrategy\n"
    )
    parts = []
    if kind in ("backtest", "both"):
        header += "import backtrader as bt\n"
        parts.append(backtest_part)
    if kind in ("engine", "both"):
        parts.append(engine_part)
    return header + "\n\n" + "\n\n".join(parts)

# ============================== 回测 ==============================
@cli.command(name="backtest", help="运行一次回测（默认参数来自 config/backtest.json）")
@click.option("--symbol", "-s", default=None, help="股票代码（默认取 config/backtest.json）")
@click.option("--start", default=None, help="开始日期（默认取 config/backtest.json）")
@click.option("--end", default=None, help="结束日期（默认取 config/backtest.json 或今天）")
@click.option("--strategy", default=None, help="策略名，可用 strategies 查看")
@click.option("--cash", default=None, type=float, help="初始资金")
@click.option("--commission", default=None, type=float, help="手续费率")
@click.option("--slippage", default=None, type=float, help="滑点比例")
@click.option("--adjust", default=None, type=click.Choice(["qfq", "hfq", "none"]), help="复权方式")
@click.option(
    "--data-source",
    type=click.Choice(["auto", "db", "csv", "remote"]),
    default=None,
    help="auto=库里没有就联网采集；db=只读库；csv=只读本地CSV；remote=强制联网",
)
@click.option("--param", multiple=True, help="策略参数，可重复，如 --param fast=5")
@click.option("--show-defaults", is_flag=True, help="只打印当前生效的默认参数，不跑回测")
@click.option("--print-log", is_flag=True, help="打印逐根K线的策略日志（默认关闭）")
@click.option("--plot", is_flag=True, help="回测结束后绘制K线图")
@click.option("--export", "do_export", is_flag=True, help="导出结果 JSON/CSV 到 output/")
@click.option("--output-dir", default=None, help="导出目录，默认 output/")
def backtest_cmd(
    symbol, start, end, strategy, cash, commission, slippage, adjust,
    data_source, param, show_defaults, print_log, plot, do_export, output_dir,
) -> None:
    from app.research import BacktestConfig, run_backtest
    from app.research.defaults import BacktestDefaults
    from app.research.report import print_report

    # 没填的字段回落到 config/backtest.json —— 三处入口（CLI/面板/作业 API）共用一份默认值
    d = BacktestDefaults.load()
    if show_defaults:
        click.secho(f"默认参数来源: {d.source}", fg="cyan")
        for k, v in d.to_dict().items():
            click.echo(f"  {k:22s} = {v}")
        return

    params = _parse_params(param) or dict(d.strategy_params)
    merged = d.merge({
        "symbol": symbol, "start": start, "end": end, "strategy": strategy,
        "cash": cash, "commission": commission, "slippage": slippage,
        "adjust": adjust, "data_source": data_source,
    })
    try:
        config = BacktestConfig(
            symbol=merged["symbol"],
            start=merged["start"],
            end=merged["end"],
            strategy=merged["strategy"],
            strategy_params=params,
            cash=merged["cash"],
            commission=merged["commission"],
            slippage_perc=merged["slippage"],
            data_source=merged["data_source"],
            adjust=merged["adjust"],
            risk_free_rate=merged.get("risk_free_rate") or 0.0,
            trading_days_per_year=int(merged.get("trading_days_per_year") or 252),
            plot=plot or bool(merged.get("plot")),
            print_log=print_log or bool(merged.get("print_log")),
            export=do_export or bool(merged.get("export")),
            output_dir=output_dir,
        )
        result = run_backtest(config)
    except Exception as exc:
        click.secho(f"❌ 回测失败: {exc}", fg="red")
        logger.debug("回测失败详情", exc_info=True)
        sys.exit(1)

    print_report(result)
    if do_export or config.export:
        from app.research.report import export_report

        files = export_report(result, output_dir)
        for k, v in files.items():
            click.secho(f"  📄 {k}: {v}", fg="blue")
