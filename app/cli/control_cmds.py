#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远程控制命令（``ctl`` 组）：对运行中的服务下发指令。

一律走监控面板的 HTTP API（标准库 urllib，零依赖），
不直连数据库 —— CLI 与面板因此永远看到同一份状态。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import _print_endpoints, cli
from app.common.config import settings
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

@ctl_collector_group.command(
    name="categories",
    help="定向采集：指定标的 × 指定类别（基本面/资金流/龙虎榜…）")
@click.option("--symbols", required=True, help="逗号分隔的标的，如 600519,000858")
@click.option("--categories", default="fundamental",
              help="逗号分隔：fund_flow/chip/fundamental/lhb/margin/forecast/holders/rating/news")
@_ctl_port
def ctl_collect_categories(symbols, categories, port) -> None:
    """采**指定标的的指定类别**，不必等盘后窗口，也不必改 services.json。"""
    data = _api(port, "/api/cross/collect",
                {"symbols": symbols, "categories": categories}, timeout=300.0)
    if not data.get("ok"):
        click.secho(f"❌ 失败: {data.get('error')}", fg="red")
        sys.exit(1)
    syms = data.get("symbols") or []
    click.secho(
        f"✅ 定向采集完成 | 标的 {len(syms)} 只 | 类别 "
        f"{', '.join((data.get('detail') or {}).keys()) or '-'}",
        fg="green",
    )
    for cat, r in (data.get("detail") or {}).items():
        failed = r.get("failed") or []
        line = f"   · {cat:<12} 成功 {r.get('ok', 0)}"
        if r.get("error"):
            click.secho(f"{line} | 错误: {r['error']}", fg="red")
        elif failed:
            click.secho(f"{line} | 失败 {len(failed)}: {failed[:3]}", fg="yellow")
        else:
            click.echo(line)

@ctl_collector_group.command(name="reload", help="重新加载 config/collector.json")
@_ctl_port
def ctl_collector_reload(port) -> None:
    data = _api(port, "/api/collector/reload", {})
    if data.get("ok"):
        click.secho(f"✅ 配置已热加载 | 任务 {data.get('jobs', 0)} 个", fg="green")
    else:
        click.secho(f"❌ 失败: {data.get('error')}", fg="red")
        sys.exit(1)

# ============================== 实盘控制 ==============================
@ctl_group.group(name="live", help="实盘网关：状态 / 对账 / 撤单")
def ctl_live_group() -> None:
    """只在 RUN_MODE=LIVE 的服务上可用。"""

@ctl_live_group.command(name="status", help="查看实盘网关与对账状态")
@_ctl_port
def ctl_live_status(port) -> None:
    data = _api(port, "/api/live")
    if data.get("error"):
        click.secho(f"❌ {data['error']}", fg="red")
        sys.exit(1)
    if not data.get("available"):
        click.secho(f"未启用实盘网关：{data.get('reason','')}", fg="yellow")
        return
    st = data.get("stats") or {}
    conn = data.get("connected")
    click.secho(
        f"网关 {data.get('gateway')} | {'已连接' if conn else '已断开'} | "
        f"接入任务 {data.get('tasks')} 个",
        fg="green" if conn else "red", bold=True,
    )
    click.echo(
        f"  回报: 轮询 {st.get('polls',0)} 次 / 成交 {st.get('fills',0)} 笔 / "
        f"无法归属 {st.get('unknown',0)} 笔"
    )
    click.echo(f"  未结订单: {data.get('pending_count', 0)} 笔")
    rc = data.get("last_reconcile") or {}
    if rc:
        ok = rc.get("ok")
        click.secho(f"  对账: {'一致' if ok else '不一致'} | {rc.get('at','')}",
                    fg="green" if ok else "red")
        for d in rc.get("position_diffs") or []:
            click.secho(
                f"    ⚠ {d['symbol']} 本地 {d['local']} / 券商 {d['broker']}", fg="red")
        if rc.get("cash_diff"):
            click.secho(f"    ⚠ 资金差异 {rc['cash_diff']}", fg="red")

@ctl_live_group.command(name="reconcile", help="立即与券商对账")
@_ctl_port
def ctl_live_reconcile(port) -> None:
    data = _api(port, "/api/live/reconcile", {}, timeout=60.0)
    if data.get("ok") is False and data.get("error"):
        click.secho(f"❌ {data['error']}", fg="red")
        sys.exit(1)
    ok = data.get("ok")
    click.secho(f"{'✅ 对账一致' if ok else '⚠ 对账不一致'}",
                fg="green" if ok else "red", bold=True)
    click.echo(
        f"  本地 {data.get('local_cash')} / 券商 {data.get('broker_cash')} | "
        f"持仓 {data.get('local_positions')}"
    )
    for d in data.get("position_diffs") or []:
        click.secho(f"    ⚠ {d['symbol']} 本地 {d['local']} / 券商 {d['broker']}", fg="red")
    for d in data.get("open_order_diffs") or []:
        click.secho(f"    ⚠ {d['kind']}: {d['orders']}", fg="red")
    if data.get("cash_diff"):
        click.secho(f"    ⚠ 资金差异 {data['cash_diff']}", fg="red")

@ctl_live_group.command(name="cancel-all", help="撤销全部未结订单")
@click.option("--task-id", default="", help="只撤某个任务的，留空=全部")
@_ctl_port
@click.option("--yes", is_flag=True, help="跳过确认")
def ctl_live_cancel_all(task_id, yes, port) -> None:
    if not yes and not click.confirm("确认撤销未结订单？实盘上这会立即向券商发出撤单请求。"):
        return
    data = _api(port, "/api/live/cancel-all", {"task_id": task_id})
    if data.get("ok"):
        click.secho(f"✅ 已请求撤销 {data.get('cancelled', 0)} 笔", fg="green")
    else:
        click.secho(f"❌ {data.get('error')}", fg="red")
        sys.exit(1)

@ctl_group.command(name="snapshot-mode", help="查看/切换快照策略（on_event/on_change/interval/off）")
@click.argument("mode", required=False, default="",
                type=click.Choice(["", "on_event", "on_change", "interval", "off"]))
@click.option("--enable/--disable", "enable", default=None,
              help="同时切换快照总开关")
@_ctl_port
def ctl_snapshot_mode(mode, enable, port) -> None:
    """不带参数时只查看当前策略与落盘统计。"""
    if not mode:
        data = _api(port, "/api/config")
        snap = (data or {}).get("snapshot") or {}
        if not snap:
            click.secho("❌ 读不到快照配置（服务没在跑？）", fg="red")
            sys.exit(1)
        _print_snapshot_mode(snap)
        return
    body: Dict = {"mode": mode}
    if enable is not None:
        body["enabled"] = enable
    data = _api(port, "/api/snapshot/mode", body)
    if data.get("ok"):
        click.secho("✅ 快照策略已切换", fg="green")
        _print_snapshot_mode(data)
    else:
        click.secho(f"❌ {data.get('error')}", fg="red")
        sys.exit(1)

@ctl_group.group(name="db", help="查 SQLite 里的交易数据（订单/成交/权益/事件）")
def ctl_db_group() -> None:
    """与文件快照的分工：快照看"当时长什么样"，这里查"按条件筛出来的行"。"""

@ctl_live_group.command(name="account", help="查看券商账户（资金/持仓，来自券商源）")
@click.option("--refresh", is_flag=True, help="先让服务去券商拉一次最新数据")
@_ctl_port
def ctl_live_account(refresh, port) -> None:
    if refresh:
        r = _api(port, "/api/live/refresh", {}, timeout=60.0)
        if not r.get("ok"):
            click.secho(f"❌ 刷新失败: {r.get('error')}", fg="red")
        else:
            click.secho("✅ 已从券商刷新账户", fg="green")
    data = _api(port, "/api/live")
    if not data.get("available"):
        click.secho(f"未启用实盘网关：{data.get('reason','')}", fg="yellow")
        return
    acct = data.get("broker_account") or {}
    if not acct:
        click.secho("尚无账户数据（可能还没刷新成功）", fg="yellow")
        if data.get("account_error"):
            click.secho(f"  最近错误: {data['account_error']}", fg="red")
        return
    click.secho(
        f"券商账户 | 接入点 {data.get('endpoint') or '-'} | "
        f"账号 {data.get('account_id') or '-'}"
        + ("（只读）" if data.get("readonly") else ""),
        fg="cyan", bold=True,
    )
    click.echo(
        f"  总资产 {acct.get('total_asset')} | 可用 {acct.get('available')} | "
        f"冻结 {acct.get('frozen')} | 市值 {acct.get('market_value')}"
    )
    click.echo(f"  更新时间: {data.get('account_at') or '-'}")
    poss = data.get("broker_positions") or {}
    if not poss:
        click.secho("  持仓: 空", fg="yellow")
        return
    click.secho(f"  {'标的':<10}{'持仓':>10}{'可卖':>10}{'成本':>10}", fg="cyan")
    for sym, p in sorted(poss.items()):
        click.echo(f"  {sym:<10}{p.get('size',0):>10}{p.get('sellable',0):>10}"
                   f"{p.get('avg_price',0):>10.3f}")

@ctl_group.group(name="endpoints", help="券商接入点：列出 / 试连接")
def ctl_endpoints_group() -> None:
    """接入点 = 网关类型 + 资金账号 + 连接参数。切账户不用改代码。"""

@ctl_endpoints_group.command(name="list", help="列出全部接入点（凭据已脱敏）")
@_ctl_port
def ctl_endpoints_list(port) -> None:
    data = _api(port, "/api/endpoints")
    _print_endpoints(data)

@ctl_endpoints_group.command(name="check", help="试连接一个接入点（只查询，不下单）")
@click.argument("endpoint", required=False, default="")
@_ctl_port
def ctl_endpoints_check(endpoint, port) -> None:
    data = _api(port, "/api/endpoints/check", {"endpoint": endpoint}, timeout=60.0)
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        if data.get("available_gateways"):
            click.echo(f"  可用网关: {', '.join(data['available_gateways'])}")
        sys.exit(1)
    ep = data.get("endpoint") or {}
    click.secho(
        f"✅ 连接成功 | {ep.get('name')} | 网关 {data.get('gateway')} | "
        f"账号 {ep.get('account') or '-'}", fg="green", bold=True,
    )
    acct = data.get("account") or {}
    click.echo(
        f"  总资产 {acct.get('total_asset')} | 可用 {acct.get('available')} | "
        f"市值 {acct.get('market_value')}"
    )
    poss = data.get("positions") or {}
    click.echo(f"  持仓 {len(poss)} 个标的" + (f": {', '.join(sorted(poss))}" if poss else ""))

@ctl_group.command(name="strategies", help="查看/热重载用户自定义策略")
@click.option("--reload", is_flag=True, help="重新扫描用户策略目录")
@_ctl_port
def ctl_strategies(reload, port) -> None:
    if reload:
        r = _api(port, "/api/strategies/reload", {}, timeout=30.0)
        if r.get("ok"):
            click.secho(
                f"✅ 已重扫 | 回测: {r.get('backtest')} | 引擎: {r.get('engine')}",
                fg="green",
            )
            if r.get("backtest_error") or r.get("engine_error"):
                click.secho(f"  ⚠ {r.get('backtest_error') or r.get('engine_error')}", fg="yellow")
        else:
            click.secho(f"❌ {r.get('error')}", fg="red")
            sys.exit(1)
    data = _api(port, "/api/strategies")
    click.secho("回测策略:", fg="cyan", bold=True)
    user = set(data.get("user_strategies") or [])
    for n in data.get("backtest_strategies") or []:
        click.echo(f"  - {n}" + ("  [自定义]" if n in user else ""))
    click.secho("实盘策略:", fg="cyan", bold=True)
    for n in data.get("live_strategies") or []:
        click.echo(f"  - {n}")
    if data.get("user_strategy_dir"):
        click.secho(f"用户策略目录: {data['user_strategy_dir']}", fg="blue")

def _print_snapshot_mode(snap: Dict) -> None:
    mode = snap.get("mode")
    explain = {
        "on_event": "有操作才落盘（没操作 = 状态没变 = 不写）",
        "on_change": "定期检查，内容真的变了才写",
        "interval": "到点就写（想要完整时间轴时用）",
        "off": "不写历史快照（面板仍可实时看）",
    }.get(str(mode), "")
    click.secho(f"模式: {mode} | 启用: {snap.get('enabled')} | {explain}", fg="cyan", bold=True)
    click.echo(
        f"  已落盘 {snap.get('writes', 0)} 份 | 触发 {snap.get('triggers', 0)} 次 | "
        f"因内容未变跳过 {snap.get('skipped_unchanged', 0)} 次"
    )
    if snap.get("last_trigger"):
        click.echo(f"  最近触发原因: {snap.get('last_trigger')}")
    if snap.get("pending"):
        click.echo("  当前有待落盘变更（将在抖动窗口后写入）")

@ctl_db_group.command(name="orders", help="查订单（含状态聚合）")
@click.option("--task-id", default="", help="按任务过滤")
@click.option("--status", default="", help="按状态过滤，如 FILLED/REJECTED/SUBMITTED")
@click.option("--limit", default=30, show_default=True, type=int)
@_ctl_port
def ctl_db_orders(task_id, status, limit, port) -> None:
    q = f"/api/db/orders?limit={limit}"
    if task_id:
        q += f"&task_id={task_id}"
    if status:
        q += f"&status={status}"
    data = _api(port, q)
    if data.get("error"):
        click.secho(f"❌ {data['error']}", fg="red")
        sys.exit(1)
    click.secho(f"状态聚合: {data.get('stats')}", fg="cyan", bold=True)
    rows = data.get("orders") or []
    if not rows:
        click.secho("没有订单", fg="yellow")
        return
    click.secho(f"{'时间':<20}{'任务':<16}{'标的':<9}{'方向':<6}"
                f"{'数量':>8}{'已成':>8}  {'状态':<12}{'价格':>9}", fg="cyan")
    for o in rows:
        click.echo(
            f"{str(o.get('created_at') or '')[:19]:<20}"
            f"{str(o.get('task_id')):<16}{str(o.get('symbol')):<9}"
            f"{str(o.get('side')):<6}{o.get('size') or 0:>8}{o.get('filled_size') or 0:>8}  "
            f"{str(o.get('status')):<12}{float(o.get('filled_price') or 0):>9.3f}"
        )

@ctl_db_group.command(name="trades", help="查成交明细")
@click.option("--task-id", default="", help="按任务过滤")
@click.option("--limit", default=30, show_default=True, type=int)
@_ctl_port
def ctl_db_trades(task_id, limit, port) -> None:
    q = f"/api/db/trades?limit={limit}" + (f"&task_id={task_id}" if task_id else "")
    data = _api(port, q)
    rows = data.get("trades") or []
    if not rows:
        click.secho("没有成交记录", fg="yellow")
        return
    click.secho(f"{'时间':<20}{'任务':<16}{'方向':<6}{'数量':>8}"
                f"{'价格':>10}{'手续费':>9}{'实现盈亏':>11}", fg="cyan")
    for t in rows:
        click.echo(
            f"{str(t.get('dt') or '')[:19]:<20}{str(t.get('task_id')):<16}"
            f"{str(t.get('side')):<6}{t.get('size') or 0:>8}"
            f"{float(t.get('price') or 0):>10.3f}{float(t.get('fee') or 0):>9.2f}"
            f"{float(t.get('realized_pnl') or 0):>+11.2f}"
        )

@ctl_db_group.command(name="equity", help="查某个任务的权益曲线")
@click.argument("task_id")
@click.option("--limit", default=50, show_default=True, type=int)
@_ctl_port
def ctl_db_equity(task_id, limit, port) -> None:
    data = _api(port, f"/api/db/equity?task_id={task_id}&limit={limit}")
    pts = data.get("points") or []
    if not pts:
        click.secho("没有权益采样点（EQUITY_SAMPLE_INTERVAL 控制采样间隔）", fg="yellow")
        return
    click.secho(f"{task_id} 权益曲线（{len(pts)} 点）", fg="cyan", bold=True)
    for p in pts:
        click.echo(f"  {str(p.get('dt') or '')[:19]}  {float(p.get('equity') or 0):>12.2f}")

@ctl_db_group.command(name="events", help="查重要系统事件")
@click.option("--category", default="", help="collect/risk/order/engine/error")
@click.option("--level", default="", help="INFO/WARNING/ERROR")
@click.option("--limit", default=30, show_default=True, type=int)
@_ctl_port
def ctl_db_events(category, level, limit, port) -> None:
    q = f"/api/db/events?limit={limit}"
    if category:
        q += f"&category={category}"
    if level:
        q += f"&level={level}"
    data = _api(port, q)
    rows = data.get("events") or []
    if not rows:
        click.secho("没有事件记录", fg="yellow")
        return
    for e in rows:
        color = {"ERROR": "red", "WARNING": "yellow"}.get(str(e.get("level")), None)
        click.secho(
            f"{str(e.get('ts') or '')[:19]} [{e.get('level')}] "
            f"({e.get('category')}) {e.get('message')}", fg=color,
        )

@ctl_db_group.command(name="backtests", help="查历史回测结果（从库里读，重启不丢）")
@click.option("--limit", default=20, show_default=True, type=int)
@_ctl_port
def ctl_db_backtests(limit, port) -> None:
    data = _api(port, f"/api/backtest?limit={limit}")
    rows = data.get("results") or []
    if not rows:
        click.secho("还没有回测记录（面板「回测监控」页可以新建）", fg="yellow")
        return
    click.secho(f"{'时间':<20}{'标的':<9}{'策略':<18}{'区间':<24}"
                f"{'收益':>9}{'回撤':>9}{'夏普':>8}{'成交':>6}", fg="cyan", bold=True)
    for r in rows:
        click.echo(
            f"{str(r.get('created_at') or '')[:19]:<20}{str(r.get('symbol')):<9}"
            f"{str(r.get('strategy')):<18}"
            f"{str(r.get('start_date'))+'~'+str(r.get('end_date')):<24}"
            f"{float(r.get('total_return') or 0)*100:>+8.2f}%"
            f"{float(r.get('max_drawdown') or 0)*100:>8.2f}%"
            f"{float(r.get('sharpe') or 0):>8.2f}{r.get('trade_count') or 0:>6}"
        )

# ============================== 任务系统（事件 → 任务 → 执行池） ==============================
@ctl_group.group(name="runtime", help="任务系统：执行池 / 队列 / 调度规则 / 死信")
def ctl_runtime_group() -> None:
    """事件驱动架构的运维入口。

    "引擎现在在忙什么"这个问题，靠这一组命令回答：
    ``status`` 看积压与占用，``rules`` 看系统里有哪些自动流转，
    ``dead`` 看哪些活彻底失败了。
    """


@ctl_runtime_group.command(name="status", help="任务系统概览：队列积压与执行池占用")
@_ctl_port
def ctl_runtime_status(port) -> None:
    data = _api(port, "/api/runtime")
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    stats = data.get("stats") or {}
    click.secho(
        f"待执行 {data.get('pending', 0)} | 执行中 {data.get('running', 0)} | "
        f"累计建任务 {stats.get('tasks_created', 0)} | 已分发 "
        f"{stats.get('dispatched', 0)} | 去重跳过 {stats.get('tasks_deduped', 0)} | "
        f"超时 {stats.get('timeout', 0)}",
        fg="cyan", bold=True,
    )
    click.secho(f"{'队列':<14}{'积压':>6}", fg="cyan")
    for name, size in sorted((data.get("queues") or {}).items()):
        mark = "  ← 有积压" if size else ""
        click.echo(f"{name:<14}{size:>6}{mark}")
    click.echo("")
    click.secho(f"{'执行池':<14}{'占用/上限':>12}{'完成':>8}{'失败':>8}{'拒绝':>8}", fg="cyan")
    for p in (data.get("pools") or {}).get("pools") or []:
        st = p.get("stats") or {}
        click.echo(
            f"{p.get('pool',''):<14}"
            f"{str(p.get('inflight')) + '/' + str(p.get('capacity')):>12}"
            f"{st.get('succeeded', 0):>8}{st.get('failed', 0):>8}{st.get('rejected', 0):>8}"
        )
    letters = data.get("dead_letters") or []
    if letters:
        click.echo("")
        click.secho(f"⚠️ 死信 {len(letters)} 条（用 ctl runtime dead 查看）", fg="red")


@ctl_runtime_group.command(name="pools", help="执行池明细与能力声明")
@_ctl_port
def ctl_runtime_pools(port) -> None:
    data = _api(port, "/api/runtime")
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    # 能力声明在**路由表**里，不在池快照里（池只管槽位，不管资格）。
    # 之前直接读 p['task_types'] 永远读不到，于是这一栏一直是空的。
    caps = {}
    for row in ((data.get("router") or {}).get("pools") or []):
        caps[row.get("pool", "")] = row.get("task_types") or []
    for p in (data.get("pools") or {}).get("pools") or []:
        click.secho(
            f"{p.get('pool','')}（{p.get('label','')}）队列={p.get('queue','')} "
            f"并发={p.get('capacity')} 在跑={p.get('inflight')}",
            fg="cyan",
        )
        types = caps.get(p.get("pool", "")) or []
        if not types:
            click.echo("    · （该池未声明任何任务类型）")
        for t in types:
            click.echo(f"    · {t}")


@ctl_runtime_group.command(name="rules", help="调度规则表：什么事件会生成什么任务")
@_ctl_port
def ctl_runtime_rules(port) -> None:
    data = _api(port, "/api/runtime")
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    rules = (data.get("rules") or {}).get("rules") or []
    click.secho(f"{'事件':<34}{'任务':<26}{'队列':<12}{'重试':>5}  状态", fg="cyan")
    for r in rules:
        state = "启用" if r.get("enabled") else "停用"
        click.echo(
            f"{r.get('event_type',''):<34}{r.get('task_type',''):<26}"
            f"{r.get('queue',''):<12}{r.get('max_retries',0):>5}  {state}"
        )


@ctl_runtime_group.command(name="dead", help="死信列表：彻底失败的任务")
@_ctl_port
def ctl_runtime_dead(port) -> None:
    data = _api(port, "/api/runtime")
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    letters = data.get("dead_letters") or []
    if not letters:
        click.secho("✅ 没有死信", fg="green")
        return
    for t in letters:
        click.secho(
            f"{t.get('task_type','')}#{str(t.get('task_id',''))[:8]} | "
            f"重试 {t.get('attempts')}/{t.get('max_retries')}",
            fg="red",
        )
        click.echo(f"    {t.get('error')}")


@ctl_runtime_group.command(name="dispatch", help="投递一个引擎任务")
@click.option("--task-type", required=True, help="任务类型，如 factor.calculate")
@click.option("--params", default="{}", help="任务参数 JSON")
@click.option("--priority", default=None, type=int, help="优先级（1 最高）")
@click.option("--dedup-key", default="", help="幂等键（同键在途时不重复投递）")
@click.option("--retries", default=0, type=int, help="最大重试次数")
@_ctl_port
def ctl_runtime_dispatch(task_type, params, priority, dedup_key, retries, port) -> None:
    import json as _json

    try:
        parsed = _json.loads(params) if params else {}
    except Exception as exc:
        click.secho(f"❌ --params 不是合法 JSON: {exc}", fg="red")
        return
    if not isinstance(parsed, dict):
        click.secho("❌ --params 必须是 JSON 对象", fg="red")
        return
    data = _api(port, "/api/runtime/dispatch", {
        "task_type": task_type,
        "params": parsed,
        "priority": priority,
        "dedup_key": dedup_key,
        "max_retries": retries,
    })
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        if data.get("known"):
            click.echo(f"可选任务类型: {', '.join(data['known'])}")
        return
    click.secho(f"✅ 已投递 {task_type} → 执行池 {data.get('pool')}", fg="green")


@ctl_runtime_group.command(name="rule", help="启停一条调度规则")
@click.argument("action", type=click.Choice(["on", "off"]))
@click.argument("name")
@_ctl_port
def ctl_runtime_rule(action, name, port) -> None:
    data = _api(port, "/api/runtime/rule", {
        "name": name, "enabled": action == "on",
    })
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        if data.get("known"):
            click.echo(f"可选规则: {', '.join(data['known'])}")
        return
    click.secho(f"✅ 规则 {name} 已{'启用' if action == 'on' else '停用'}", fg="green")


@ctl_runtime_group.command(name="dead-clear", help="清空死信（运维处理完这批之后用）")
@_ctl_port
def ctl_runtime_dead_clear(port) -> None:
    data = _api(port, "/api/runtime/dead/clear", {})
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    click.secho(f"✅ 已清空 {data.get('cleared', 0)} 条死信", fg="green")


@ctl_runtime_group.command(name="maint", help="维护计划：日志清理 / 归档 / 定期校验")
@_ctl_port
def ctl_runtime_maint(port) -> None:
    data = _api(port, "/api/maintenance")
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    click.secho(
        f"维护计划 {data.get('enabled', 0)}/{data.get('total', 0)} 启用 | "
        f"检查节拍 {data.get('tick_seconds')}s | 配置 {data.get('config_path')}",
        fg="cyan", bold=True,
    )
    for j in data.get("jobs") or []:
        flag = "✅" if j.get("enabled") else "⏸ "
        when = (f"每天 {j.get('at')}") if j.get("schedule") == "daily" \
            else f"每 {j.get('interval_sec')}s"
        click.echo(
            f"{flag} {j.get('name',''):<12}{when:<16}"
            f"下次 {j.get('next_run_iso') or '未启用':<21}"
            f"已跑 {j.get('runs', 0)} 次"
        )
        if j.get("params"):
            click.echo(f"     参数 {j['params']}")


@ctl_runtime_group.command(name="maint-run", help="立刻执行一次维护任务（不看周期与开关）")
@click.argument("name")
@_ctl_port
def ctl_runtime_maint_run(name, port) -> None:
    data = _api(port, "/api/maintenance/run", {"name": name})
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        if data.get("known"):
            click.echo(f"可选: {', '.join(data['known'])}")
        return
    click.secho(
        f"✅ 已触发 {data.get('job')} → {data.get('task_type')}（走规则表建任务，非直接执行）",
        fg="green",
    )


# ============================== 日志 ==============================
@ctl_group.group(name="log", help="日志：运行时查看与切换级别")
def ctl_log_group() -> None:
    """日志级别**不用重启服务**就能改。

    排查线上问题时最尴尬的是"想看 DEBUG 就得重启，而重启正好把
    要排查的那个状态冲掉"。
    """


@ctl_log_group.command(name="show", help="当前生效的日志配置")
@_ctl_port
def ctl_log_show(port) -> None:
    data = _api(port, "/api/logging")
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    click.secho(
        f"控制台 {data.get('console')} | 文件 {data.get('file')} | "
        f"节流窗口 {data.get('throttle_window')}s",
        fg="cyan", bold=True,
    )
    click.echo(
        f"目录 {data.get('dir')} | 保留 {data.get('retention')} | "
        f"轮转 {data.get('rotation')}"
    )
    th = data.get("throttled") or {}
    click.echo(
        f"节流条目 {th.get('buckets', 0)} 个，已压缩 {th.get('pending', 0)} 次"
        + (" | 已有持久化配置" if data.get("persisted") else " | 来自环境变量/默认值")
    )


@ctl_log_group.command(name="level", help="切换日志级别（不用重启）")
@click.argument("level", type=click.Choice(
    ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
    case_sensitive=False))
@click.option("--file", "file_level", default=None,
              help="文件级别；省略表示跟随控制台，--file '' 也表示跟随")
@click.option("--window", "window", default=None, type=float,
              help="重复日志节流窗口（秒）")
@click.option("--no-persist", is_flag=True, help="只本次生效，不写入配置")
@_ctl_port
def ctl_log_level(level, file_level, window, no_persist, port) -> None:
    body = {
        "console": level.upper(),
        "throttle_window": window,
        "persist": not no_persist,
    }
    if file_level is not None:
        body["file"] = file_level
    data = _api(port, "/api/logging/level", body)
    if not data.get("ok"):
        click.secho(f"❌ {data.get('error')}", fg="red")
        return
    click.secho(
        f"✅ 控制台={data.get('console')} 文件={data.get('file')} "
        f"节流={data.get('throttle_window')}s"
        + ("" if no_persist else "（已持久化，重启仍生效）"),
        fg="green",
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

    from app.common.config import settings as _settings

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
