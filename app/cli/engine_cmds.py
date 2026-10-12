#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎面命令：前台跑引擎 / 后台常驻服务 / 启停状态 / 运行快照。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
from app.common.config import settings
from app.common.logging import logger
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
    from app.engine import component
    from app.runtime.scheduler import SchedulerComponent
    from app.runtime.executor import WorkerComponent
    from app.engine.engine import BaseQuantEngine
    from twisted.internet import reactor

    config = {
        "RUN_MODE": mode,
        "SYMBOL": symbol,
        "BACKTEST_IDLE_TIMEOUT": idle_timeout,
        "BACKTEST_AUTO_STOP": not no_auto_stop,
    }

    engine = BaseQuantEngine.create(config)
    # 定时器已并入调度组件（SchedulerComponent），不再需要单独的定时器组件
    engine.register_component(SchedulerComponent())
    engine.register_component(WorkerComponent())

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
@click.option("--snapshot/--no-snapshot", "snapshot", default=None,
              help="快照总开关（默认取 MONITOR_SNAPSHOT_ENABLED）")
@click.option("--snapshot-mode", default=None,
              type=click.Choice(["on_change", "interval", "off"]),
              help="快照策略：on_change=内容变了才写（默认）；interval=到点就写；off=不写")
@click.option("--snapshot-min-gap", default=None, type=float,
              help="两次落盘的最小间隔（秒），防止逐笔行情把磁盘写爆")
@click.option("--store/--no-store", "store", default=None,
              help="订单/成交/权益落 SQLite（默认开）")
@click.option("--broker-gateway", default=None,
              help="实盘券商网关名（LIVE 模式必填），如 simulated")
@click.option("--broker-endpoint", default=None,
              help="券商接入点 id（见 config/brokers.json），比 --broker-gateway 更具体")
@click.option("--ext-data/--no-ext-data", "ext_data", default=None,
              help="扩展数据采集（资金流/筹码/涨停池/题材）开关（默认取 EXT_DATA_ENABLED）")
@click.option("--ext-interval", default=None, type=float,
              help="资金流/筹码采集间隔（秒），默认 600")
@click.option("--data-guard/--no-data-guard", "data_guard", default=None,
              help="数据校验服务（启动后体检 + 自动补全缺失日线，默认取 "
                   "DATA_GUARD_ENABLED）")
@click.option("--guard-symbol", multiple=True,
              help="纳入校验的标的，可重复（默认跟随采集标的）")
def serve_cmd(config, mode, symbol, market_mode, interval, start, end, data_source,
              no_news, monitor, port, monitor_host, snapshot_interval, namespace,
              no_runtime_tasks, collector, collect_interval, collect_period,
              collect_symbol, collector_config, snapshot, snapshot_mode,
              snapshot_min_gap, store, broker_gateway, broker_endpoint,
              ext_data, ext_interval, data_guard, guard_symbol) -> None:
    """前台运行后台服务（Ctrl+C 退出）。要放到后台跑请用 `start`。

    一个进程 = 一个引擎 = N 个任务。任务之间状态完全隔离，
    行情按标的广播、新闻按关注列表定向投递。

    ``poll`` 模式下会自动挂上**行情采集服务**：按你设定的频率把数据拉进本地库。
    没有它，poll 每次读到的都是同一批旧行——服务像在跑，行情其实从未更新。
    """
    from twisted.internet import reactor

    from app.engine.builder import build_engine
    from app.common.config import settings as _settings

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
            with_store=store,
            snapshot_enabled=snapshot,
            snapshot_mode=snapshot_mode or "",
            snapshot_min_gap=snapshot_min_gap,
            broker_gateway=broker_gateway or "",
            broker_endpoint=broker_endpoint or "",
            with_ext_data=ext_data,
            ext_interval=ext_interval,
            with_data_guard=data_guard,
            data_guard_symbols=list(guard_symbol) or None,
        )
    except Exception as exc:
        click.secho(f"❌ 引擎装配失败: {exc}", fg="red")
        logger.debug("装配失败详情", exc_info=True)
        sys.exit(1)

    sm = engine.get_component("strategy")
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

    # 采集服务把 K线 / 日频截面 / 完整性校验合成了一个服务，
    # 所以这里一次把三块状态都报出来 —— 以前要查三个组件。
    col = engine.get_component("collection_service")
    if col is not None:
        from app.modules.collection.spec import humanize_frequency

        buckets: dict = {}
        for job in col.kline_spec.jobs:
            buckets[job.interval] = buckets.get(job.interval, 0) + 1
        click.secho(
            "📥 采集服务: "
            + (", ".join(f"{humanize_frequency(k)}×{v}" for k, v in sorted(buckets.items()))
               or "无任务")
            + f" | 共 {len(col.kline_spec.jobs)} 个任务"
            + ("" if col.kline_enabled else f"（已停用：{col.disabled_reason}）"),
            fg="cyan",
        )
        click.secho(
            f"   ├─ 日频截面(资金流/筹码/涨停池): {'开' if col.cross_enabled else '关'}"
            + (f" | 个股 {len(col.cross_symbols)} 只 @ {col.cross_interval:.0f}s"
               f" | 市场级 @ {col.cross_daily_interval:.0f}s"
               if col.cross_enabled else ""),
            fg="cyan",
        )
        click.secho(
            f"   └─ 完整性校验: {'开' if col.guard_enabled else '关'}"
            + (f" | {col.guard_start_delay:.0f}s 后首检，每 {col.guard_interval:.0f}s"
               f" | 回溯 {col.guard_lookback_days} 天"
               if col.guard_enabled else ""),
            fg="cyan",
        )

    trade = engine.get_component("trading_service")
    if trade is not None:
        click.secho(
            f"🗄️  交易服务: 落库={'开' if trade.store_enabled else '关'}"
            + (f" | {_settings.DATABASE_URL}" if trade.store_enabled else ""),
            fg="cyan",
        )
        if trade.gateway_enabled:
            click.secho(
                f"🔌 实盘网关: {getattr(trade.gateway, 'name', '?')} | "
                f"回报轮询 {trade.poll_interval}s | 对账 {trade.reconcile_interval}s",
                fg="yellow", bold=True,
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
@click.option("--broker-gateway", default=None, help="实盘券商网关名（LIVE 模式用）")
@click.option("--broker-endpoint", default=None,
              help="券商接入点 id（见 config/brokers.json），比 --broker-gateway 更具体")
@click.option("--no-ext-data", is_flag=True,
              help="关闭扩展数据采集（资金流/筹码/涨停池/题材）")
@click.option("--ext-interval", default=None, type=float,
              help="资金流/筹码采集间隔（秒），默认 600")
@click.option("--show-window", is_flag=True,
              help="调试用：让后台进程带控制台窗口（默认完全隐藏，不弹黑框）")
def start_cmd(config, mode, market_mode, interval, start_date, end_date, data_source,
              symbol, port, snapshot_interval, namespace, no_news, no_monitor,
              no_collector, collect_interval, collect_period, collect_symbol,
              collector_config, broker_gateway, broker_endpoint, no_ext_data,
              ext_interval, show_window) -> None:
    """后台启动：脱离终端运行，日志写入 logs/service.out.log。

    Windows 上默认用 ``pythonw.exe`` 启动，**不会弹出控制台黑框**。
    要看窗口（调试）加 ``--show-window``。
    """
    from app.engine import daemon
    from app.common.config import settings as _settings

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
        broker_gateway=broker_gateway or "",
        broker_endpoint=broker_endpoint or "",
        no_ext_data=no_ext_data,
        ext_interval=ext_interval,
    )
    result = daemon.start(cmd, show_window=show_window)
    if not result.get("ok"):
        click.secho(f"❌ 启动失败: {result.get('error')}", fg="red")
        sys.exit(1)
    click.secho(f"✅ 服务已在后台启动 | PID {result['pid']}", fg="green", bold=True)
    click.echo(f"   日志: {result['log_file']}")
    click.echo(f"   面板: http://{_settings.MONITOR_HOST}:{port or _settings.MONITOR_PORT}/")
    click.echo("   停止: python main.py stop   |  状态: python main.py status")
    if not show_window and not result.get("no_window"):
        click.secho(
            "   ⚠ 未找到 pythonw.exe，后台进程可能仍会弹控制台窗口。"
            "装完整版 Python 或改用 scripts/windows/start_hidden.vbs 启动。",
            fg="yellow",
        )

@cli.command(name="stop", help="停止后台服务（优雅退出）")
@click.option("--timeout", default=None, type=float, help="等待优雅退出的秒数")
def stop_cmd(timeout) -> None:
    from app.engine import daemon

    result = daemon.stop(timeout=timeout)
    color = "green" if result.get("ok") else "red"
    click.secho(("✅ " if result.get("ok") else "❌ ") + str(result.get("message")), fg=color)
    if result.get("forced"):
        click.secho(f"   （已强制结束: {result['forced']}）", fg="yellow")

@cli.command(name="status", help="查看后台服务状态")
def status_cmd() -> None:
    from app.engine import daemon

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
    from app.engine import daemon

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

    from app.monitoring.snapshot import SnapshotStore

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
        from app.monitoring.snapshot import SnapshotStore as _S
        from app.common.config import settings as _settings

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
