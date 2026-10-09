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
from typing import List, Optional, Tuple

import click

from app.core.config import settings
from app.utils.logger import logger, setup_logging

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


# ============================== 数据库 ==============================
@cli.command(name="init-db", help="初始化数据库（建表）")
def init_db_cmd() -> None:
    from app.data.service import init_db

    init_db()
    click.secho(f"✅ 数据库初始化完成 | {settings.DATABASE_URL}", fg="green")


# ============================== 数据采集 ==============================
@cli.command(name="collect", help="采集A股日线数据并入库（幂等，可重复执行）")
@click.option("--symbol", "-s", multiple=True, required=True, help="股票代码，可重复传入多个")
@click.option("--start", default="2020-01-01", show_default=True, help="开始日期")
@click.option("--end", default=DEFAULT_END, show_default=True, help="结束日期")
@click.option("--adjust", default="qfq", show_default=True, help="复权方式 qfq/hfq/none")
@click.option(
    "--source",
    type=click.Choice(["auto", "baostock", "akshare", "csv"]),
    default="auto",
    show_default=True,
    help="数据源，auto=按配置优先级自动降级",
)
@click.option("--save-csv", is_flag=True, help="同时导出 CSV 到 data/stock/")
def collect_cmd(symbol, start, end, adjust, source, save_csv) -> None:
    from app.data.service import MarketDataService

    source_arg = None if source == "auto" else source
    ok, fail = 0, 0
    for sym in symbol:
        try:
            df = MarketDataService.collect(
                sym, start, end, adjust=adjust, source=source_arg, save_csv=save_csv
            )
            click.secho(f"✅ {sym}: {len(df)} 条", fg="green")
            ok += 1
        except Exception as exc:
            click.secho(f"❌ {sym} 采集失败: {exc}", fg="red")
            logger.debug("采集失败详情", exc_info=True)
            fail += 1

    click.secho(f"采集结束 | 成功 {ok} | 失败 {fail}", fg="cyan")
    if fail:
        sys.exit(1)


# ============================== 数据清单 ==============================
@cli.command(name="list", help="查看本地数据库已有哪些标的与覆盖区间")
def list_cmd() -> None:
    from app.data.service import MarketDataService

    rows = MarketDataService.inventory()
    if not rows:
        click.secho("库中没有数据，请先执行: alphaquant collect -s 000001", fg="yellow")
        return

    click.secho(f"{'标的':<10}{'条数':>8}  {'起始':<12}{'结束':<12}", fg="cyan", bold=True)
    for r in rows:
        click.echo(f"{r['symbol']:<10}{r['rows']:>8}  {r['start']:<12}{r['end']:<12}")


# ============================== 策略列表 ==============================
@cli.command(name="strategies", help="列出所有可用策略")
def strategies_cmd() -> None:
    from app.backtest.registry import list_strategies

    for name in list_strategies():
        click.echo(f"  - {name}")


# ============================== 回测 ==============================
@cli.command(name="backtest", help="运行一次回测")
@click.option("--symbol", "-s", required=True, help="股票代码")
@click.option("--start", default="2020-01-01", show_default=True, help="开始日期")
@click.option("--end", default=DEFAULT_END, show_default=True, help="结束日期")
@click.option("--strategy", default="ma_cross", show_default=True, help="策略名，可用 strategies 查看")
@click.option("--cash", default=settings.DEFAULT_CASH, show_default=True, type=float, help="初始资金")
@click.option("--commission", default=settings.DEFAULT_COMMISSION, show_default=True, type=float, help="手续费率")
@click.option("--slippage", default=settings.DEFAULT_SLIPPAGE_PERC, show_default=True, type=float, help="滑点比例")
@click.option(
    "--data-source",
    type=click.Choice(["auto", "db", "csv", "remote"]),
    default="auto",
    show_default=True,
    help="auto=库里没有就联网采集；db=只读库；csv=只读本地CSV；remote=强制联网",
)
@click.option("--param", multiple=True, help="策略参数，可重复，如 --param fast=5")
@click.option("--print-log", is_flag=True, help="打印逐根K线的策略日志（默认关闭）")
@click.option("--plot", is_flag=True, help="回测结束后绘制K线图")
@click.option("--export", "do_export", is_flag=True, help="导出结果 JSON/CSV 到 output/")
@click.option("--output-dir", default=None, help="导出目录，默认 output/")
def backtest_cmd(
    symbol, start, end, strategy, cash, commission, slippage,
    data_source, param, print_log, plot, do_export, output_dir,
) -> None:
    from app.backtest import BacktestConfig, run_backtest
    from app.backtest.report import export_report, print_report

    try:
        config = BacktestConfig(
            symbol=symbol,
            start=start,
            end=end,
            strategy=strategy,
            strategy_params=_parse_params(param),
            cash=cash,
            commission=commission,
            slippage_perc=slippage,
            data_source=data_source,
            plot=plot,
            print_log=print_log,
            export=do_export,
            output_dir=output_dir,
        )
        result = run_backtest(config)
    except Exception as exc:
        click.secho(f"❌ 回测失败: {exc}", fg="red")
        logger.debug("回测失败详情", exc_info=True)
        sys.exit(1)

    print_report(result)
    if do_export:
        files = export_report(result, output_dir)
        for k, v in files.items():
            click.secho(f"  📄 {k}: {v}", fg="blue")


# ============================== 事件引擎 ==============================
@cli.command(name="engine", help="启动事件驱动引擎（回测模式会自动空闲停止）")
@click.option(
    "--mode",
    type=click.Choice(["BACKTEST", "SIMULATE", "LIVE"]),
    default="BACKTEST",
    show_default=True,
    help="运行模式",
)
@click.option("--symbol", default="000001", show_default=True, help="标的")
@click.option(
    "--idle-timeout",
    default=settings.BACKTEST_IDLE_TIMEOUT,
    show_default=True,
    type=float,
    help="回测模式下调度器空闲多少秒后自动停止",
)
@click.option("--no-auto-stop", is_flag=True, help="禁用回测模式自动停止")
def engine_cmd(mode, symbol, idle_timeout, no_auto_stop) -> None:
    from app.core.engine.component import TaskSchedulerComponent
    from app.core.engine.components import TimerComponent
    from app.core.engine.engine import BaseQuantEngine
    from twisted.internet import reactor

    config = {
        "RUN_MODE": mode,
        "SYMBOL": symbol,
        "BACKTEST_IDLE_TIMEOUT": idle_timeout,
        "BACKTEST_AUTO_STOP": not no_auto_stop,
    }

    engine = BaseQuantEngine.create(config)
    engine.register_component(TaskSchedulerComponent())
    engine.register_component(TimerComponent())

    click.secho(f"🚀 启动引擎 | 模式={mode} | Ctrl+C 退出", fg="green", bold=True)
    try:
        ctx = engine.start()
        click.secho(
            f"✅ 引擎已停止 | run_id={ctx.run_id} | status={ctx.engine_status.value}",
            fg="green",
        )
    except KeyboardInterrupt:
        click.secho("\n🛑 收到中断信号", fg="yellow")
    finally:
        if reactor.running:  # pragma: no cover
            reactor.stop()


# ============================== 能力清单 ==============================
@cli.command(name="factors", help="列出可用指标与因子（自定义因子的接入点）")
def factors_cmd() -> None:
    from app.core.factor import list_factors
    from app.core.indicator import list_indicators

    click.secho("指标:", fg="cyan", bold=True)
    for n in list_indicators():
        click.echo(f"  - {n}")
    click.secho("因子:", fg="cyan", bold=True)
    for n in list_factors():
        click.echo(f"  - {n}")


@cli.command(name="rules", help="列出可组合的规则类型与内置风控")
def rules_cmd() -> None:
    from app.core.risk import list_risks
    from app.core.rule import RULE_TYPES
    from app.core.strategy import list_strategies

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
    from app.core.engine.builder import load_task_config
    from app.core.task.spec import parse_task_specs

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


# ============================== 后台服务 ==============================
@cli.command(name="serve", help="启动后台常驻服务：行情+新闻驱动的多任务交易引擎")
@click.option("--config", default=None, help="任务配置文件路径，默认 config/tasks.json")
@click.option(
    "--mode",
    type=click.Choice(["BACKTEST", "SIMULATE", "LIVE"]),
    default=None,
    help="运行模式，默认取配置文件里的 mode（replay 回放完会自动停）",
)
@click.option("--symbol", "-s", multiple=True, help="标的，可重复；默认取任务配置里的标的")
@click.option(
    "--market-mode",
    type=click.Choice(["replay", "poll"]),
    default=None,
    help="replay=回放历史（推完自动停）；poll=定时抓最新（常驻）",
)
@click.option("--interval", default=None, type=float, help="行情推送间隔（秒）")
@click.option("--start", default=None, help="回放起始日期")
@click.option("--end", default=None, help="回放结束日期")
@click.option(
    "--data-source",
    type=click.Choice(["db", "csv", "auto", "remote"]),
    default="db",
    show_default=True,
    help="行情数据来源",
)
@click.option("--no-news", is_flag=True, help="关闭新闻组件")
def serve_cmd(config, mode, symbol, market_mode, interval, start, end, data_source, no_news) -> None:
    """后台服务主入口。

    一个进程 = 一个引擎 = N 个任务。任务之间状态完全隔离，
    行情按标的广播、新闻按关注列表定向投递。
    """
    from twisted.internet import reactor

    from app.core.engine.builder import build_engine

    try:
        engine = build_engine(
            task_config=config,
            mode=mode or "",
            symbols=list(symbol) or None,
            market_mode=market_mode or "",
            market_interval=interval,
            start=start or "",
            end=end or "",
            data_source=data_source,
            with_news=not no_news,
        )
    except Exception as exc:
        click.secho(f"❌ 引擎装配失败: {exc}", fg="red")
        logger.debug("装配失败详情", exc_info=True)
        sys.exit(1)

    sm = engine.get_component("strategy_manager")
    click.secho(
        f"🚀 服务启动 | 任务 {len(sm)} 个 | 数据源 {data_source} | Ctrl+C 优雅退出",
        fg="green",
        bold=True,
    )

    try:
        ctx = engine.start()
        click.secho(f"\n✅ 服务已停止 | run_id={ctx.run_id}", fg="green")
        click.echo(sm.summary())
    except KeyboardInterrupt:
        click.secho("\n🛑 收到中断信号，正在优雅退出...", fg="yellow")
        engine.stop(graceful=True)
    finally:
        if reactor.running:  # pragma: no cover
            reactor.stop()


if __name__ == "__main__":
    cli()
