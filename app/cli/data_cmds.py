#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据面命令：建库 / 采集 / 完整性校验 / 数据源与 Provider 观测。

对应链路 DataSource → Provider → Capability → Router → DataService，
这里只做「人读得懂的呈现」，业务一律下沉到 app.providers.market / app.data。
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import DEFAULT_END, cli
from app.common.config import settings
from app.common.logging import logger
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

# ============================== 数据校验 ==============================
@cli.command(name="dataguard", help="校验数据库完整性，并自动补全缺失的日线")
@click.option("--symbol", "-s", multiple=True, help="标的，可重复；不传=校验库中全部")
@click.option("--lookback", default=None, type=int,
              help=f"回溯天数（默认 {settings.DATA_GUARD_LOOKBACK_DAYS}）")
@click.option("--repair/--no-repair", "repair", default=None,
              help="是否自动补全（默认跟随 DATA_GUARD_AUTO_REPAIR）")
@click.option("--max-symbols", default=None, type=int, help="最多校验多少只")
def dataguard_cmd(symbol, lookback, repair, max_symbols) -> None:
    """体检 + 补全。

    为什么要单独给个命令：库里少一天数据不会报错，回测照跑、信号照出，
    只是结论悄悄偏了。这类问题只能靠主动体检发现，不能等它报错。
    """
    from app.modules.collection.integrity import DataIntegrityChecker, summarize

    auto = settings.DATA_GUARD_AUTO_REPAIR if repair is None else bool(repair)
    checker = DataIntegrityChecker(
        lookback_days=lookback or settings.DATA_GUARD_LOOKBACK_DAYS,
        auto_repair=auto,
        max_symbols=max_symbols or settings.DATA_GUARD_MAX_SYMBOLS,
        max_repair=settings.DATA_GUARD_MAX_REPAIR,
    )
    report = checker.scan(list(symbol) or None)

    click.secho(summarize(report),
                fg="green" if report.ok else "yellow")
    for tbl, info in (report.tables or {}).items():
        click.echo(f"  表 {tbl}: {info}")
    for item in report.symbols:
        if item.ok:
            continue
        bits = []
        if item.missing:
            bits.append("缺 " + ", ".join(d.isoformat() for d in item.missing[:8])
                        + (f" 等 {len(item.missing)} 天" if len(item.missing) > 8 else ""))
        if item.anomalies:
            bits.append(f"异常行 {len(item.anomalies)}")
        if item.repaired:
            bits.append(f"已补 {item.repaired} 行（源: {item.source or '未知'}）")
        if item.suspended:
            bits.append("疑似停牌/退市，未补")
        if item.error:
            bits.append(item.error)
        click.secho(f"  {item.symbol}: " + " | ".join(bits), fg="yellow")

    if not report.ok and not auto:
        click.secho("提示：加 --repair 可自动补全缺口", fg="cyan")
    if report.failed:
        sys.exit(1)

# 探测时给需要参数的源喂的默认值（daily/fund_flow 都要一只具体标的）
_PROBE_ARGS: Dict[str, Dict[str, Any]] = {
    "daily": {"symbol": "600000"},
    "fund_flow": {"symbol": "600000"},
}

def _probe_sources(registry: Any, kind: Optional[str]) -> None:
    """逐源实打一次，把"谁通谁不通、为什么"摊在桌面上。

    这命令存在的理由：日志里一句"所有数据源均失败"没法行动。
    打完这张表就能区分三种情况——
      ① 只有一个源挂 → 源的问题，等它恢复或降权生效；
      ② 全部连接类错误 → 本机网络/代理的问题（换端口或取消代理）；
      ③ 全部 DataUnavailable → 源是通的，只是此刻没数据（非交易日/停牌）。
    """
    kwargs = dict(_PROBE_ARGS.get(kind or "", {}))
    click.secho(f"正在实测数据源{'（' + kind + '）' if kind else '（全部）'}……",
                fg="cyan")
    rows = registry.probe(kind, **kwargs)
    if not rows:
        click.secho("没有可探测的数据源", fg="yellow")
        return

    click.secho(f"{'数据源':<18}{'结果':>6}{'耗时':>8}  说明", fg="cyan")
    bad = 0
    for r in rows:
        ok = bool(r.get("ok"))
        if not ok:
            bad += 1
        flag = "可用" if ok else "不可用"
        color = "green" if ok else "red"
        detail = r.get("error") or (f"{r.get('rows')} 行" if ok else "")
        click.secho(f"{r['name']:<18}{flag:>6}{str(r.get('ms')) + 'ms':>8}  {detail}",
                    fg=color)
    if bad == len(rows):
        from app.providers.market.compat.registry import network_hint

        click.secho("全部不可用 —— " + network_hint(), fg="yellow")
    elif bad:
        click.secho(f"{bad}/{len(rows)} 个源不可用，其余可用（会自动换源，不影响取数）",
                    fg="yellow")
    _echo_host_state()

@cli.command(name="sources", help="查看数据源权重与健康度；--probe 实测每个源通不通")
@click.option("--probe", is_flag=True,
              help="对每个源实际发一次请求（不重试、不改权重），用于排查\"所有源都失败\"")
@click.option("--kind", default=None, help="只探测该数据类型，如 hot_rank / daily")
def sources_cmd(probe: bool, kind: Optional[str]) -> None:
    # 注册发生在 import 期，必须先让内置源模块被加载，否则列表是空的
    import app.providers.market.service.facade  # noqa: F401  触发全部内置源注册

    from app.providers.market.compat.registry import REGISTRY

    if probe:
        _probe_sources(REGISTRY, kind)
        return

    rows = REGISTRY.health()
    if not rows:
        click.secho("尚未注册任何数据源（是不是没 import app.providers.market.service.facade？）",
                    fg="yellow")
        return
    click.secho(f"{'数据源':<18}{'权重':>6}{'基准':>6}  {'状态':<10}  支持", fg="cyan")
    for r in rows:
        state = "熔断中" if r["circuit_remaining"] else (
            "健康" if r["healthy"] else "停用")
        if r.get("fallback"):
            state = "兜底"
        click.echo(f"{r['name']:<18}{r['weight']:>6g}{r['base_weight']:>6g}  "
                   f"{state:<10}  {','.join(r['supports'])}")
        if r["last_error"]:
            click.secho(f"    最后错误: {r['last_error'][:100]}", fg="yellow")
    used = REGISTRY.used_sources()
    if used:
        click.secho("最近成功的源: " + ", ".join(f"{k}={v}" for k, v in used.items()),
                    fg="green")
    _echo_host_state()

@cli.command(name="providers", help="查看数据源编排：能力 / 路由 / 健康分 / 熔断状态")
@click.option("--capability", "cap", default=None,
              help="只看某种能力，如 daily_kline / hot_rank / fund_flow / flow_rank")
@click.option("--probe", is_flag=True, help="对每条路由实际发一次请求（不重试、不改健康度）")
@click.option("--reset", "do_reset", is_flag=True, help="重置健康分与熔断（修完网络环境用）")
@click.option("--reload", "do_reload", is_flag=True,
              help="热重载 config/providers.json（改完配置不用重启）")
def providers_cmd(cap: Optional[str], probe: bool, do_reset: bool,
                  do_reload: bool) -> None:
    """``alphaquant providers`` —— 新采集层的"照妖镜"。

    和 ``sources``（旧链路）的区别：这里按**能力**看。
    "mootdx 有没有人气榜" 这种问题，看这张表直接就有答案 ——
    它没有声明 HOT_RANK，所以压根不会出现在 hot_rank 的路由里，
    而不是"调用了之后才发现不支持"。
    """
    from app.providers.market.provider import PROVIDERS, bootstrap, reload

    if do_reload:
        cfg = reload()
        click.secho(f"已重载配置: {cfg.source}", fg="green")
    bootstrap()
    if do_reset:
        PROVIDERS.reset()
        click.secho("健康分与熔断已重置", fg="green")

    rows = PROVIDERS.snapshot()
    if cap:
        rows = [r for r in rows if r["capability"] == cap]
    if not rows:
        click.secho("没有已注册的路由（是不是还没装配数据源？）", fg="yellow")
        return

    if probe:
        _probe_routes(cap)
        return

    click.secho(f"{'路由':<16}{'能力':<14}{'数据源':<12}{'优先':>5}"
                f"{'最终分':>7}  {'状态':<10}成功/调用", fg="cyan", bold=True)
    for r in sorted(rows, key=lambda x: (x["capability"], -x["final_score"])):
        circuit = r.get("circuit") or {}
        state = circuit.get("state_label") or "健康"
        color = {"熔断中": "red", "半开探测": "yellow", "降级": "yellow"}.get(
            state, "green")
        if not r["enabled"]:
            state, color = "已停用", "yellow"
        click.echo(f"{r['name']:<16}{r['capability']:<14}{r['provider']:<12}"
                   f"{r['priority']:>5}{r['final_score']:>7}  ", nl=False)
        click.secho(f"{state:<10}{r['ok']}/{r['calls']}", fg=color, nl=False)
        if r["fallback"]:
            click.secho(" 兜底", fg="cyan", nl=False)
        if r.get("last_error"):
            click.secho(f"  最近错误: {r['last_error'][:60]}", fg="yellow")
        else:
            click.echo("")

    click.echo("")
    click.secho("能力 → 路由顺序（按当前健康分排好）", fg="cyan", bold=True)
    for capability in sorted(PROVIDERS.capabilities()):
        if cap and capability != cap:
            continue
        order = [r.name for r in PROVIDERS.ranking(capability)]
        click.echo(f"  {capability:<14}" + " → ".join(order))
    _echo_host_state()

#: 新链路（Provider Router）的探测参数：按能力给一份最小可用参数
_PROBE_ARGS_BY_CAP: Dict[str, Dict[str, Any]] = {
    "daily_kline": {"symbol": "600000"},
    "fund_flow": {"symbol": "600000", "days": 5},
    "flow_rank": {"rank_kind": "即时", "top": 10},
    "hot_rank": {"top": 10},
}

def _probe_routes(cap: Optional[str]) -> None:
    """逐条路由实打一次：区分"源挂了"和"本机出不去网"。"""
    from app.providers.market.provider import PROVIDERS
    from app.providers.market.provider.base import DataCapability, ProviderError, as_provider_error

    click.secho("正在实测路由……", fg="cyan")
    targets = ([DataCapability.parse(cap)] if cap
               else list(DataCapability))
    bad = 0
    total = 0
    for capability in targets:
        routes = PROVIDERS.ranking(capability)
        if not routes:
            continue
        click.secho(f"\n[{capability.value}] {capability.label}", fg="cyan")
        params = dict(_PROBE_ARGS_BY_CAP.get(capability.value, {}))
        for route in routes:
            import time as _t

            t0 = _t.monotonic()
            try:
                df = route.handler(**params)
                rows = len(df) if hasattr(df, "__len__") else 0
                ms = int((_t.monotonic() - t0) * 1000)
                click.secho(f"  {route.name:<16}可用{ms:>7}ms  {rows} 行", fg="green")
            except Exception as exc:
                perr = as_provider_error(exc, provider=route.provider,
                                         capability=capability, route=route.name)
                ms = int((_t.monotonic() - t0) * 1000)
                bad += 1
                click.secho(f"  {route.name:<16}不可用{ms:>6}ms  "
                            f"{perr.type.value}: {str(perr)[:60]}", fg="red")
            total += 1
    if bad and bad == total:
        click.secho("全部不可用 —— 多半是本机网络/代理的问题，而不是数据源的问题",
                    fg="yellow")
    elif bad:
        click.secho(f"{bad}/{total} 条路由不可用，其余可用（会自动换源，不影响取数）",
                    fg="yellow")

def _echo_host_state() -> None:
    """域名健康度 —— 回答"到底是源的问题还是网络的问题"。

    源级状态只能告诉你"它失败了"，而多个源常常共用同一个域名：
    push2 一挂，三个人气榜源一起挂，看源列表会误判成"数据源都不行"。
    """
    from app.providers.market.transport import net

    hosts = net.host_state()
    if not hosts:
        return
    click.secho("\n依赖域名（网络层实测）", fg="cyan")
    for name, st in sorted(hosts.items()):
        if st["blocked"]:
            tag, color = f"不可达({st['blocked_remaining']}s)", "red"
        elif st["fail"]:
            tag, color = f"不稳定(fail={st['fail']})", "yellow"
        else:
            tag, color = "正常", "green"
        click.secho(f"  {name:<32}{tag:<18}"
                    + (st["last_error"][:60] if st["last_error"] else ""), fg=color)
    alive = net.proxy_reachable()
    if alive is False:
        click.secho("  ⚠️ 代理端口无程序监听（僵尸代理），请求会全部超时", fg="red")

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
