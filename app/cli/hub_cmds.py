#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多服务编排命令（``hub`` 组）：一个 hub 拉起并看护 N 个独立引擎进程。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
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
    from app.engine.hub.hub import main as hub_main

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
    from app.common.config import settings as _settings
    from app.engine import daemon
    from app.engine.hub.spec import load_hub_spec

    hub = load_hub_spec(services_config or None)
    hub_dir = hub.state_root
    hub_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "app.engine.hub.hub"]
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
    from app.engine import daemon
    from app.engine.hub.spec import load_hub_spec

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
    from app.common.config import settings as _settings
    from app.engine.hub.spec import load_hub_spec
    from app.engine.hub.supervisor import ServiceSupervisor
    from app.common.jsonio import read_json

    hub = load_hub_spec(services_config or None)
    hub_dir = hub.state_root
    pid = read_json(hub_dir / "hub.pid") or {}
    from app.engine import daemon as _d

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
    from app.engine.hub.spec import load_hub_spec

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

    from app.engine.hub.spec import load_hub_spec
    from app.engine.hub.supervisor import ServiceSupervisor

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
