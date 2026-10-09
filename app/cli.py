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
@click.option("--monitor/--no-monitor", "monitor", default=None,
              help="是否启用内嵌网页监控面板（默认取 MONITOR_ENABLED）")
@click.option("--port", default=None, type=int, help="监控面板端口（默认 8787）")
@click.option("--monitor-host", default="", help="监控面板监听地址（默认 127.0.0.1）")
@click.option("--snapshot-interval", default=None, type=float, help="快照落盘间隔（秒）")
@click.option("--namespace", default="default", show_default=True, help="运行时任务命名空间")
@click.option("--no-runtime-tasks", is_flag=True, help="不重放上次运行新增的任务")
@click.option("--collector/--no-collector", "collector", default=None,
              help="行情采集服务开关（默认：poll 模式自动开，回放/回测自动关）")
@click.option("--collect-interval", default=None,
              help="采集频率，如 30s / 5m / 1h（默认取 COLLECTOR_INTERVAL，即 5m）")
@click.option("--collect-period", default=None, type=click.Choice(
    ["1d", "1", "5", "15", "30", "60"]), help="采集粒度：1d=日线，其余为分钟线")
@click.option("--collect-symbol", multiple=True,
              help="额外纳入采集的标的，可重复（会与 config/collector.json 合并）")
@click.option("--collector-config", default=None,
              help="采集编排配置文件，默认 config/collector.json")
def serve_cmd(config, mode, symbol, market_mode, interval, start, end, data_source,
              no_news, monitor, port, monitor_host, snapshot_interval, namespace,
              no_runtime_tasks, collector, collect_interval, collect_period,
              collect_symbol, collector_config) -> None:
    """前台运行后台服务（Ctrl+C 退出）。要放到后台跑请用 `start`。

    一个进程 = 一个引擎 = N 个任务。任务之间状态完全隔离，
    行情按标的广播、新闻按关注列表定向投递。

    ``poll`` 模式下会自动挂上**行情采集服务**：按你设定的频率把数据拉进本地库。
    没有它，poll 每次读到的都是同一批旧行——服务像在跑，行情其实从未更新。
    """
    from twisted.internet import reactor

    from app.core.engine.builder import build_engine
    from app.core.config import settings as _settings

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
            with_monitor=monitor,
            monitor_host=monitor_host,
            monitor_port=port,
            monitor_interval=snapshot_interval,
            namespace=namespace,
            auto_load_runtime=not no_runtime_tasks,
            with_collector=collector,
            collector_interval=collect_interval,
            collector_period=collect_period,
            collector_symbols=list(collect_symbol) or None,
            collector_config=collector_config,
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
    mon = engine.get_component("monitor")
    if mon is not None:
        click.secho(
            f"🖥️  监控面板: http://{mon.host}:{mon.port}/  （浏览器打开即可看组件/任务/快照）",
            fg="cyan",
        )

    col = engine.get_component("data_collector")
    if col is not None:
        from app.core.collect.spec import humanize_frequency

        buckets: dict = {}
        for job in col.spec.jobs:
            buckets[job.interval] = buckets.get(job.interval, 0) + 1
        click.secho(
            "📥 行情采集: "
            + ", ".join(f"{humanize_frequency(k)}×{v}" for k, v in sorted(buckets.items()))
            + f" | 共 {len(col.spec.jobs)} 个任务",
            fg="cyan",
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


# ============================== 后台守护 ==============================
@cli.command(name="start", help="把服务放到后台运行（守护进程）")
@click.option("--config", default=None, help="任务配置文件，默认 config/tasks.json")
@click.option("--mode", type=click.Choice(["BACKTEST", "SIMULATE", "LIVE"]),
              default="SIMULATE", show_default=True, help="运行模式")
@click.option("--market-mode", type=click.Choice(["replay", "poll"]), default=None,
              help="replay=回放历史；poll=定时抓最新")
@click.option("--interval", default=None, type=float, help="行情推送间隔（秒）")
@click.option("--start", "start_date", default=None, help="回放起始日期")
@click.option("--end", "end_date", default=None, help="回放结束日期")
@click.option("--data-source", type=click.Choice(["db", "csv", "auto", "remote"]),
              default="db", show_default=True, help="行情数据来源")
@click.option("--symbol", "-s", multiple=True, help="标的，可重复")
@click.option("--port", default=None, type=int, help="监控面板端口（默认 8787）")
@click.option("--snapshot-interval", default=None, type=float, help="快照间隔（秒）")
@click.option("--namespace", default="default", show_default=True, help="运行时任务命名空间")
@click.option("--no-news", is_flag=True, help="关闭新闻组件")
@click.option("--no-monitor", is_flag=True, help="关闭监控面板")
@click.option("--no-collector", is_flag=True, help="关闭行情采集服务")
@click.option("--collect-interval", default=None,
              help="采集频率，如 30s / 5m / 1h（默认取 COLLECTOR_INTERVAL）")
@click.option("--collect-period", default=None, type=click.Choice(
    ["1d", "1", "5", "15", "30", "60"]), help="采集粒度：1d=日线，其余为分钟线")
@click.option("--collect-symbol", multiple=True, help="额外纳入采集的标的，可重复")
@click.option("--collector-config", default=None, help="采集编排配置文件")
def start_cmd(config, mode, market_mode, interval, start_date, end_date, data_source,
              symbol, port, snapshot_interval, namespace, no_news, no_monitor,
              no_collector, collect_interval, collect_period, collect_symbol,
              collector_config) -> None:
    """后台启动：脱离终端运行，日志写入 logs/service.out.log。"""
    from app.core.engine import daemon
    from app.core.config import settings as _settings

    cmd = daemon.service_cmd(
        config=config or "",
        mode=mode,
        market_mode=market_mode or "",
        interval=interval,
        start_date=start_date or "",
        end_date=end_date or "",
        data_source=data_source,
        symbols=list(symbol),
        port=port,
        snapshot_interval=snapshot_interval,
        namespace=namespace,
        no_news=no_news,
        no_monitor=no_monitor,
        no_collector=no_collector,
        collect_interval=collect_interval or "",
        collect_period=collect_period or "",
        collect_symbols=list(collect_symbol),
        collector_config=collector_config or "",
    )
    result = daemon.start(cmd)
    if not result.get("ok"):
        click.secho(f"❌ 启动失败: {result.get('error')}", fg="red")
        sys.exit(1)
    click.secho(f"✅ 服务已在后台启动 | PID {result['pid']}", fg="green", bold=True)
    click.echo(f"   日志: {result['log_file']}")
    click.echo(f"   面板: http://{_settings.MONITOR_HOST}:{port or _settings.MONITOR_PORT}/")
    click.echo("   停止: python main.py stop   |  状态: python main.py status")


@cli.command(name="stop", help="停止后台服务（优雅退出）")
@click.option("--timeout", default=None, type=float, help="等待优雅退出的秒数")
def stop_cmd(timeout) -> None:
    from app.core.engine import daemon

    result = daemon.stop(timeout=timeout)
    color = "green" if result.get("ok") else "red"
    click.secho(("✅ " if result.get("ok") else "❌ ") + str(result.get("message")), fg=color)
    if result.get("forced"):
        click.secho(f"   （已强制结束: {result['forced']}）", fg="yellow")


@cli.command(name="status", help="查看后台服务状态")
def status_cmd() -> None:
    from app.core.engine import daemon

    st = daemon.status()
    if not st.get("running"):
        click.secho(f"⚪ 未运行 | {st.get('message')}", fg="yellow")
        if st.get("stale"):
            click.echo("   提示：可用 python main.py stop 清理残留 PID 文件")
        return
    info = st.get("info") or {}
    detail = st.get("detail") or {}
    click.secho(f"🟢 运行中 | PID {st['pid']}", fg="green", bold=True)
    click.echo(f"   启动于: {info.get('started_at', '-')}")
    click.echo(f"   内存:   {detail.get('rss_mb', '-')} MB | 线程 {detail.get('num_threads', '-')}")
    click.echo(f"   面板:   http://127.0.0.1:{info.get('port') or 8787}/")
    click.echo(f"   日志:   {info.get('log_file', '-')}")


@cli.command(name="restart", help="重启后台服务")
@click.option("--config", default=None, help="任务配置文件")
@click.option("--mode", type=click.Choice(["BACKTEST", "SIMULATE", "LIVE"]),
              default="SIMULATE", show_default=True)
@click.option("--port", default=None, type=int, help="监控面板端口")
@click.option("--namespace", default="default", show_default=True)
def restart_cmd(config, mode, port, namespace) -> None:
    from app.core.engine import daemon

    cmd = daemon.service_cmd(config=config or "", mode=mode, port=port, namespace=namespace)
    result = daemon.restart(cmd=cmd)
    if not result.get("ok"):
        click.secho(f"❌ 重启失败: {result.get('error')}", fg="red")
        sys.exit(1)
    click.secho(f"✅ 已重启 | PID {result['pid']}", fg="green", bold=True)


# ============================== 快照 ==============================
@cli.command(name="snapshot", help="查看/导出运行快照（复盘用）")
@click.option("--run-id", default="", help="指定 run，默认最近一次")
@click.option("--list", "do_list", is_flag=True, help="只列出所有 run")
@click.option("--tasks", is_flag=True, help="打印每个任务的最终状态")
@click.option("--orders", default=0, type=int, help="打印最近 N 条订单")
@click.option("--export", "export_to", default="", help="把 latest.json 复制到指定路径")
@click.option("--json", "as_json", is_flag=True, help="输出完整 JSON")
def snapshot_cmd(run_id, do_list, tasks, orders, export_to, as_json) -> None:
    """快照保存在 output/snapshots/<run_id>/ 下。"""
    import json as _json
    import shutil

    from app.core.monitor.snapshot import SnapshotStore

    if do_list:
        runs = SnapshotStore.runs()
        if not runs:
            click.secho("还没有任何快照", fg="yellow")
            return
        click.secho(f"{'run_id':<24}{'状态':<12}{'模式':<10}{'任务':>5}  最后写入", fg="cyan", bold=True)
        for r in runs:
            click.echo(
                f"{r['run_id']:<24}{str(r.get('status') or '-'):<12}"
                f"{str(r.get('mode') or '-'):<10}{r.get('task_count', 0):>5}  "
                f"{r.get('last_write') or '-'}"
            )
        return

    snap = SnapshotStore.load(run_id=run_id)
    if not snap:
        click.secho("没有找到快照，请确认服务已启用监控组件", fg="yellow")
        return

    if export_to:
        from app.core.monitor.snapshot import SnapshotStore as _S
        from app.core.config import settings as _settings

        src = _settings.SNAPSHOT_DIR / snap["run_id"] / "latest.json"
        shutil.copyfile(src, export_to)
        click.secho(f"✅ 已导出: {export_to}", fg="green")
        return

    if as_json:
        click.echo(_json.dumps(snap, ensure_ascii=False, indent=2))
        return

    eng = snap.get("engine") or {}
    click.secho(f"run_id   : {snap.get('run_id')}", fg="cyan", bold=True)
    click.echo(f"运行模式 : {eng.get('mode')}")
    click.echo(f"状态     : {eng.get('status')} | 已运行 {eng.get('uptime_sec')}s")
    click.echo(f"采集时间 : {snap.get('captured_at')}")
    comps = snap.get("components") or {}
    click.echo(f"组件     : {', '.join(comps.keys())}")

    if tasks or True:
        click.secho(
            f"\n{'任务ID':<22}{'标的':<10}{'策略':<14}{'状态':<10}"
            f"{'权益':>12}{'总盈亏':>12}{'成交':>6}{'回撤':>9}",
            fg="cyan", bold=True,
        )
        for t in snap.get("tasks") or []:
            click.echo(
                f"{str(t.get('task_id')):<22}{str(t.get('symbol')):<10}"
                f"{str(t.get('strategy')):<14}{str(t.get('status')):<10}"
                f"{float(t.get('equity') or 0):>12.2f}{float(t.get('total_pnl') or 0):>+12.2f}"
                f"{int(t.get('trade_count') or 0):>6}{float(t.get('drawdown') or 0):>8.2%}"
            )

    if orders:
        rows = snap.get("orders_recent") or []
        click.secho(f"\n最近 {min(orders, len(rows))} 条订单:", fg="cyan", bold=True)
        click.secho(f"{'时间':<22}{'任务':<18}{'方向':<6}{'数量':>8}{'状态':<10}原因",
                    fg="cyan", bold=True)
        for o in rows[-orders:]:
            click.echo(
                f"{str(o.get('created_at') or '')[:19]:<22}"
                f"{str(o.get('task_id'))[:16]:<18}{str(o.get('side')):<6}"
                f"{int(o.get('size') or 0):>8}{str(o.get('status')):<10}"
                f"{(o.get('reject_reason') or o.get('reason') or '')[:40]}"
            )


# ============================== 远程控制 ==============================
def _api(port: Optional[int], path: str, body: Optional[dict] = None, timeout: float = 8.0):
    """调用监控面板 API（零依赖，用标准库 urllib）。

    显式禁用代理：Windows 上 urllib 会读注册表里的系统代理，
    企业环境下 localhost 请求会被代理劫持并返回 502。
    """
    import json as _json
    import urllib.error
    import urllib.request

    from app.core.config import settings as _settings

    base = f"http://{_settings.MONITOR_HOST}:{port or _settings.MONITOR_PORT}"
    url = base + path
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return _json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"error": f"无法连接运行中的服务({base}): {exc}"}


@cli.group(name="ctl", help="对运行中的服务下发控制指令（走监控面板 API）")
def ctl_group() -> None:
    """服务必须启用监控面板（默认启用）才能被远程控制。"""


def _ctl_port(f):
    return click.option("--port", default=None, type=int, help="目标服务面板端口")(f)


@ctl_group.command(name="overview", help="查看运行中的服务概览")
@_ctl_port
def ctl_overview(port) -> None:
    data = _api(port, "/api/overview")
    if data.get("error"):
        click.secho(f"❌ {data['error']}", fg="red")
        sys.exit(1)
    eng = data.get("engine") or {}
    click.secho(f"run_id={eng.get('run_id')} 状态={eng.get('status')} "
                f"模式={eng.get('mode')} 已运行={eng.get('uptime_sec')}s", fg="cyan", bold=True)
    for c in data.get("components") or []:
        mark = "●" if c.get("enabled") else "○"
        click.echo(f"  {mark} {c['name']:<20} {c.get('state'):<12} {c.get('health_detail','')}")
    click.secho(f"任务 {data.get('task_count', 0)} 个", fg="cyan")


@ctl_group.command(name="tasks", help="列出运行中的任务")
@_ctl_port
def ctl_tasks(port) -> None:
    data = _api(port, "/api/tasks")
    if data.get("error"):
        click.secho(f"❌ {data['error']}", fg="red")
        sys.exit(1)
    tasks = data.get("tasks") or []
    if not tasks:
        click.secho("没有任务", fg="yellow")
        return
    click.secho(f"{'任务ID':<22}{'标的':<10}{'策略':<14}{'状态':<10}{'权益':>12}{'总盈亏':>12}",
                fg="cyan", bold=True)
    for t in tasks:
        click.echo(f"{str(t.get('task_id')):<22}{str(t.get('symbol')):<10}"
                   f"{str(t.get('strategy')):<14}{str(t.get('status')):<10}"
                   f"{float(t.get('equity') or 0):>12.2f}{float(t.get('total_pnl') or 0):>+12.2f}")


@ctl_group.command(name="add", help="运行时新增任务（--spec 传 JSON 文件或 JSON 字符串）")
@click.option("--spec", required=True, help="任务 JSON：文件路径或内联 JSON")
@_ctl_port
def ctl_add(spec, port) -> None:
    import json as _json
    from pathlib import Path as _Path

    raw = spec
    if _Path(spec).exists():
        raw = _Path(spec).read_text(encoding="utf-8")
    try:
        body = _json.loads(raw)
    except Exception as exc:
        click.secho(f"❌ 任务 JSON 解析失败: {exc}", fg="red")
        sys.exit(1)
    if isinstance(body, dict) and "tasks" in body:
        click.secho("❌ 请传单个任务对象，不要传 {tasks: [...]}", fg="red")
        sys.exit(1)
    data = _api(port, "/api/tasks", body)
    if data.get("ok"):
        click.secho(f"✅ 已添加任务: {data.get('task_id')}", fg="green")
    else:
        click.secho(f"❌ 添加失败: {data.get('error')}", fg="red")
        sys.exit(1)


@ctl_group.command(name="task", help="对单个任务操作：pause/resume/remove/start/stop")
@click.argument("action", type=click.Choice(["pause", "resume", "remove", "start", "stop"]))
@click.argument("task_id")
@_ctl_port
def ctl_task(action, task_id, port) -> None:
    data = _api(port, f"/api/tasks/{task_id}/{action}", {})
    if data.get("ok"):
        click.secho(f"✅ {action} {task_id} 成功", fg="green")
    else:
        click.secho(f"❌ {action} {task_id} 失败: {data.get('error')}", fg="red")
        sys.exit(1)


@ctl_group.command(name="component", help="启用/禁用组件")
@click.argument("action", type=click.Choice(["enable", "disable"]))
@click.argument("name")
@_ctl_port
def ctl_component(action, name, port) -> None:
    data = _api(port, f"/api/components/{name}/{action}", {})
    if data.get("ok"):
        click.secho(f"✅ {action} 组件 {name} 成功", fg="green")
    else:
        click.secho(f"❌ 操作失败: {data.get('error')}", fg="red")
        sys.exit(1)


@ctl_group.command(name="snapshot", help="让运行中的服务立即落一份快照")
@_ctl_port
def ctl_snapshot(port) -> None:
    data = _api(port, "/api/command", {"action": "snapshot"})
    if data.get("ok"):
        click.secho(f"✅ 快照已落盘: {data.get('path')}", fg="green")
    else:
        click.secho(f"❌ 失败: {data.get('error')}", fg="red")
        sys.exit(1)


@ctl_group.command(name="shutdown", help="让运行中的服务优雅退出")
@_ctl_port
def ctl_shutdown(port) -> None:
    if not click.confirm("确认让服务优雅退出？"):
        return
    data = _api(port, "/api/engine/stop", {"graceful": True})
    click.secho("✅ 已下发停止指令" if data.get("ok") else f"❌ {data.get('error')}",
                fg="green" if data.get("ok") else "red")


# ============================== 行情采集控制 ==============================
@ctl_group.group(name="collector", help="行情采集服务：查看/改频率/增删标的/立即补采")
def ctl_collector_group() -> None:
    """采集频率写 30s / 5m / 1h / 1d 都行，现场生效，不用重启服务。"""


@ctl_collector_group.command(name="status", help="查看采集服务状态")
@_ctl_port
def ctl_collector_status(port) -> None:
    data = _api(port, "/api/collector")
    if data.get("error"):
        click.secho(f"❌ {data['error']}", fg="red")
        sys.exit(1)
    if not data.get("available"):
        click.secho(f"本服务未装配采集服务：{data.get('reason','')}", fg="yellow")
        return

    jobs = data.get("jobs") or []
    state = data.get("state") or {}
    click.secho(
        f"采集服务 | 任务 {len(jobs)} 个 | 交易时段限定 {data.get('trading_hours_only')} "
        f"| 频率下限 {data.get('min_interval')}s",
        fg="cyan", bold=True,
    )
    click.echo(f"配置: {data.get('config')}")
    if not jobs:
        return
    head = f"{'标的':<10}{'粒度':<10}{'频率':<10}{'跑次':>6}{'入库':>10}{'失败':>6}  上次运行"
    click.secho(head, fg="cyan", bold=True)
    for j in jobs:
        st = state.get(f"{j['symbol']}:{j['period']}") or {}
        per = "日线" if j["period"] == "1d" else f"{j['period']}分钟"
        click.echo(
            f"{j['symbol']:<10}{per:<10}{j['interval_human']:<10}"
            f"{st.get('runs',0):>6}{st.get('rows',0):>10}{st.get('errors',0):>6}  "
            f"{(st.get('last_run_at') or '—').replace('T',' ')[:19]}"
        )
        if st.get("last_error"):
            click.secho(f"            └ 最近错误: {st['last_error']}", fg="red")


@ctl_collector_group.command(name="interval", help="修改采集频率（如 30s / 5m / 1h）")
@click.argument("interval")
@click.option("--symbol", default="", help="只改某个标的，留空=全部")
@click.option("--period", default="", type=click.Choice(["", "1d", "1", "5", "15", "30", "60"]),
              help="只改某个粒度，留空=全部")
@_ctl_port
def ctl_collector_interval(interval, symbol, period, port) -> None:
    data = _api(port, "/api/collector/interval",
                {"interval": interval, "symbol": symbol, "period": period})
    if data.get("ok") and data.get("affected"):
        click.secho(f"✅ 频率已改为 {interval} | 影响 {data['affected']} 个任务", fg="green")
    else:
        click.secho(f"❌ 失败: {data.get('error') or '没有匹配的任务'}", fg="red")
        sys.exit(1)


@ctl_collector_group.command(name="add", help="新增采集标的")
@click.argument("symbol")
@click.option("--period", default="1d", show_default=True,
              type=click.Choice(["1d", "1", "5", "15", "30", "60"]), help="数据粒度")
@click.option("--interval", default=None, help="采集频率，默认取全局配置")
@click.option("--keep-days", default=None, type=int, help="分钟线保留天数，0=永久")
@_ctl_port
def ctl_collector_add(symbol, period, interval, keep_days, port) -> None:
    body = {"symbol": symbol, "period": period}
    if interval:
        body["interval"] = interval
    if keep_days is not None:
        body["keep_days"] = keep_days
    data = _api(port, "/api/collector/add", body)
    if data.get("ok"):
        job = data.get("job") or {}
        click.secho(
            f"✅ 已新增采集 {job.get('symbol')}/{job.get('period')} "
            f"频率 {job.get('interval_human')}", fg="green")
    else:
        click.secho(f"❌ 失败: {data.get('error')}", fg="red")
        sys.exit(1)


@ctl_collector_group.command(name="remove", help="移除采集标的")
@click.argument("symbol")
@click.option("--period", default="", type=click.Choice(["", "1d", "1", "5", "15", "30", "60"]),
              help="粒度，留空=移除该标的全部粒度")
@_ctl_port
def ctl_collector_remove(symbol, period, port) -> None:
    data = _api(port, "/api/collector/remove", {"symbol": symbol, "period": period})
    if data.get("ok") and data.get("affected"):
        click.secho(f"✅ 已移除 {data['affected']} 个采集任务", fg="green")
    else:
        click.secho(f"❌ 失败: {data.get('error') or '没有匹配的任务'}", fg="red")
        sys.exit(1)


@ctl_collector_group.command(name="now", help="立即补采一次")
@click.option("--symbol", default="", help="只补某个标的，留空=全部")
@_ctl_port
def ctl_collector_now(symbol, port) -> None:
    # 采集本身是秒级网络 IO，面板路由是异步的，这里要把超时放宽
    data = _api(port, "/api/collector/now", {"symbol": symbol}, timeout=300.0)
    if data.get("ok"):
        click.secho(f"✅ 补采完成 | 入库 {data.get('rows', 0)} 条 | 任务 {data.get('tasks', 0)} 个",
                    fg="green")
        for e in data.get("errors") or []:
            click.secho(f"   ⚠ {e}", fg="yellow")
    else:
        click.secho(f"❌ 失败: {data.get('error')}", fg="red")
        sys.exit(1)


@ctl_collector_group.command(name="reload", help="重新加载 config/collector.json")
@_ctl_port
def ctl_collector_reload(port) -> None:
    data = _api(port, "/api/collector/reload", {})
    if data.get("ok"):
        click.secho(f"✅ 配置已热加载 | 任务 {data.get('jobs', 0)} 个", fg="green")
    else:
        click.secho(f"❌ 失败: {data.get('error')}", fg="red")
        sys.exit(1)


# ============================== 多服务编排 ==============================
@cli.group(name="hub", help="多服务编排：一个 hub 拉起 N 个独立引擎进程")
def hub_group() -> None:
    """服务定义在 config/services.json。聚合面板默认 http://127.0.0.1:8899/。"""


@hub_group.command(name="run", help="前台运行 hub（拉起并看护所有服务）")
@click.option("--services-config", default=None, help="services.json 路径")
@click.option("--host", default="", help="hub 面板监听地址")
@click.option("--port", default=None, type=int, help="hub 面板端口")
@click.option("--no-start", is_flag=True, help="只做看护，不主动拉起服务")
@click.option("--no-panel", is_flag=True, help="不启动聚合面板")
def hub_run(services_config, host, port, no_start, no_panel) -> None:
    from app.core.hub.hub import main as hub_main

    argv = []
    if services_config:
        argv += ["--services-config", services_config]
    if host:
        argv += ["--host", host]
    if port is not None:
        argv += ["--port", str(port)]
    if no_start:
        argv += ["--no-start"]
    if no_panel:
        argv += ["--no-panel"]
    sys.exit(hub_main(argv))


@hub_group.command(name="start", help="后台启动 hub")
@click.option("--services-config", default=None, help="services.json 路径")
@click.option("--port", default=None, type=int, help="hub 面板端口")
def hub_start(services_config, port) -> None:
    from app.core.config import settings as _settings
    from app.core.engine import daemon
    from app.core.hub.spec import load_hub_spec

    hub = load_hub_spec(services_config or None)
    hub_dir = hub.state_root
    hub_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "app.core.hub.hub"]
    if services_config:
        cmd += ["--services-config", services_config]
    if port is not None:
        cmd += ["--port", str(port)]

    result = daemon.start(
        cmd,
        pid_file=hub_dir / "hub.pid",
        log_file=hub_dir / "hub.log",
        stop_file=hub_dir / "hub.stop",
    )
    if not result.get("ok"):
        click.secho(f"❌ hub 启动失败: {result.get('error')}", fg="red")
        sys.exit(1)
    hp = port if port is not None else (hub.port or _settings.HUB_PORT)
    click.secho(f"✅ Hub 已在后台启动 | PID {result['pid']}", fg="green", bold=True)
    click.echo(f"   聚合面板: http://{hub.host or _settings.HUB_HOST}:{hp}/")
    click.echo(f"   日志: {result['log_file']}")
    click.echo("   状态: python main.py hub status   |   停止: python main.py hub stop")


@hub_group.command(name="stop", help="停止 hub（会一并停掉所有服务）")
@click.option("--services-config", default=None, help="services.json 路径")
def hub_stop(services_config) -> None:
    from app.core.engine import daemon
    from app.core.hub.spec import load_hub_spec

    hub = load_hub_spec(services_config or None)
    hub_dir = hub.state_root
    result = daemon.stop(hub_dir / "hub.pid", hub_dir / "hub.stop")
    click.secho(("✅ " if result.get("ok") else "❌ ") + str(result.get("message")),
                fg="green" if result.get("ok") else "red")
    # hub 退出时会 stop_all，但为稳妥起见再兜一次
    for spec in hub.services:
        d = hub_dir / spec.id
        daemon.stop(d / "service.pid", d / "service.stop", timeout=8)


@hub_group.command(name="status", help="查看 hub 与各服务状态")
@click.option("--services-config", default=None, help="services.json 路径")
@click.option("--tasks", is_flag=True, help="同时列出所有服务里的任务")
def hub_status(services_config, tasks) -> None:
    from app.core.config import settings as _settings
    from app.core.hub.spec import load_hub_spec
    from app.core.hub.supervisor import ServiceSupervisor
    from app.utils.jsonio import read_json

    hub = load_hub_spec(services_config or None)
    hub_dir = hub.state_root
    pid = read_json(hub_dir / "hub.pid") or {}
    from app.core.engine import daemon as _d

    hub_alive = _d.is_alive(pid.get("pid"))
    click.secho(
        f"{'🟢 Hub 运行中' if hub_alive else '⚪ Hub 未运行'} | PID {pid.get('pid') or '-'} | "
        f"面板 http://{hub.host or _settings.HUB_HOST}:{hub.port or _settings.HUB_PORT}/",
        fg="green" if hub_alive else "yellow", bold=True,
    )

    sup = ServiceSupervisor(hub)
    click.secho(f"\n{'服务':<14}{'角色':<22}{'PID':>8}{'健康':<10}{'心跳':>8}{'任务':>6}{'权益':>14}",
                fg="cyan", bold=True)
    all_tasks = []
    for spec in hub.services:
        st = sup.service_status(spec.id)
        state = st.get("state") or {}
        summary = state.get("summary") or {}
        age = st.get("heartbeat_age")
        click.echo(
            f"{spec.id:<14}{(spec.role or '-'):<22}{str(st.get('pid') or '-'):>8}"
            f"{st.get('health'):<10}{(str(age) + 's') if age is not None else '-':>8}"
            f"{summary.get('task_count', 0):>6}{summary.get('total_equity', 0):>14,.2f}"
        )
        for t in state.get("tasks") or []:
            all_tasks.append((spec.id, t))

    if tasks:
        click.secho(f"\n{'服务':<12}{'任务ID':<24}{'标的':<10}{'策略':<14}{'状态':<10}{'权益':>12}",
                    fg="cyan", bold=True)
        for sid, t in all_tasks:
            click.echo(f"{sid:<12}{str(t.get('task_id')):<24}{str(t.get('symbol')):<10}"
                       f"{str(t.get('strategy')):<14}{str(t.get('status')):<10}"
                       f"{float(t.get('equity') or 0):>12.2f}")


@hub_group.command(name="services", help="列出 config/services.json 里定义的服务")
@click.option("--services-config", default=None, help="services.json 路径")
def hub_services(services_config) -> None:
    from app.core.hub.spec import load_hub_spec

    hub = load_hub_spec(services_config or None)
    if not hub.services:
        click.secho(f"没有解析到服务（{hub.source}）", fg="yellow")
        return
    click.secho(f"编排文件: {hub.source}", fg="cyan")
    click.secho(f"{'服务':<14}{'角色':<24}{'启用':<6}{'模式':<10}{'端口':>6}  {'发布':<10}{'订阅':<12}任务",
                fg="cyan", bold=True)
    for s in hub.services:
        click.echo(
            f"{s.id:<14}{(s.role or '-')[:22]:<24}{'是' if s.enabled else '否':<6}"
            f"{s.mode:<10}{str(s.port or '-'):>6}  "
            f"{','.join(s.publish) or '-':<10}{','.join(s.subscribe) or '-':<12}{len(s.tasks)}"
        )


@hub_group.command(name="cmd", help="给某个服务下发指令（add_task/pause_task/remove_task...）")
@click.argument("service_id")
@click.argument("action")
@click.option("--task-id", default="", help="任务ID")
@click.option("--spec", default="", help="add_task 用的任务 JSON（文件或内联）")
@click.option("--services-config", default=None, help="services.json 路径")
def hub_cmd(service_id, action, task_id, spec, services_config) -> None:
    import json as _json
    from pathlib import Path as _Path

    from app.core.hub.spec import load_hub_spec
    from app.core.hub.supervisor import ServiceSupervisor

    hub = load_hub_spec(services_config or None)
    sup = ServiceSupervisor(hub)
    command = {"action": action}
    if task_id:
        command["task_id"] = task_id
    if spec:
        raw = spec
        if _Path(spec).exists():
            raw = _Path(spec).read_text(encoding="utf-8")
        try:
            command["spec"] = _json.loads(raw)
        except Exception as exc:
            click.secho(f"❌ spec JSON 解析失败: {exc}", fg="red")
            sys.exit(1)
    result = sup.send_command(service_id, command)
    if result.get("ok"):
        click.secho(f"✅ 指令已投递给服务 {service_id}，1~2 秒后生效", fg="green")
    else:
        click.secho(f"❌ {result.get('error')}", fg="red")
        sys.exit(1)



if __name__ == "__main__":
    cli()
