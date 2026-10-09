#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : hub.py
# @Description : 多服务编排进程（hub）：拉起 N 个引擎服务 + 聚合面板 + 看护重启
#               用法：python -m app.core.hub.hub --services-config config/services.json
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.core.config import settings
from app.core.hub.spec import load_hub_spec
from app.core.hub.supervisor import ServiceSupervisor
from app.utils.jsonio import write_json_atomic
from app.utils.logger import logger


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.core.hub.hub", description="AlphaQuant 多服务编排（hub）"
    )
    p.add_argument("--services-config", default="", help="services.json 路径")
    p.add_argument("--host", default="", help="hub 面板监听地址")
    p.add_argument("--port", type=int, default=None, help="hub 面板端口")
    p.add_argument("--no-panel", action="store_true", help="不启动 hub 聚合面板")
    p.add_argument("--no-start", action="store_true", help="只做看护，不主动拉起服务")
    p.add_argument("--pid-file", default="", help="hub 的 PID 文件")
    p.add_argument("--stop-file", default="", help="hub 的停止标志文件")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings.ensure_dirs()

    hub = load_hub_spec(args.services_config or None)
    if not hub.services:
        logger.error(f"没有解析到任何服务配置（{hub.source}），请检查 config/services.json")
        return 2

    host = args.host or hub.host or settings.HUB_HOST
    port = args.port if args.port is not None else (hub.port or settings.HUB_PORT)

    hub_dir = hub.state_root
    hub_dir.mkdir(parents=True, exist_ok=True)
    pid_file = Path(args.pid_file) if args.pid_file else hub_dir / "hub.pid"
    stop_file = Path(args.stop_file) if args.stop_file else hub_dir / "hub.stop"
    log_file = hub_dir / "hub.log"

    for f in (pid_file, stop_file):
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    write_json_atomic(
        pid_file,
        {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(),
            "argv": sys.argv,
            "cwd": str(settings.BASE_DIR),
            "log_file": str(log_file),
            "stop_file": str(stop_file),
            "port": port,
            "services": [s.id for s in hub.services],
        },
        indent=2,
    )

    supervisor = ServiceSupervisor(hub)

    from twisted.internet import reactor, task

    from app.core.engine.service_runner import watch_stop_file
    from app.core.hub.panel import build_hub_site

    listening = None
    if not args.no_panel:
        try:
            listening = reactor.listenTCP(port, build_hub_site(supervisor, str(hub.bus_root)), interface=host)
            logger.info(f"🖥️  Hub 面板已启动: http://{host}:{port}/")
        except Exception as exc:
            logger.error(f"Hub 面板端口 {host}:{port} 绑定失败: {exc}")

    logger.info("=" * 70)
    logger.info(
        f"Hub 启动 | 服务 {len(hub.enabled())}/{len(hub.services)} 个 | "
        f"状态目录 {hub.state_root} | 总线目录 {hub.bus_root} | PID {os.getpid()}"
    )
    for s in hub.services:
        flags = []
        if s.publish:
            flags.append(f"发布{'+'.join(s.publish)}")
        if s.subscribe:
            flags.append(f"订阅{'+'.join(s.subscribe)}")
        logger.info(
            f"  · [{s.id}] {s.role or '-'} | {'启用' if s.enabled else '禁用'} | "
            f"模式 {s.mode} | 面板 {s.port or '-'} | {' '.join(flags) or '无总线角色'}"
        )
    logger.info("=" * 70)

    if not args.no_start:
        result = supervisor.start_all()
        for sid, r in result.items():
            logger.info(f"  启动 {sid}: {'OK pid=' + str(r.get('pid')) if r.get('ok') else 'FAIL ' + str(r.get('error'))}")

    def shutdown():
        logger.warning("Hub 正在停止全部服务...")
        supervisor.stop_all()
        if reactor.running:
            reactor.stop()

    watch_stop_file(stop_file, shutdown)

    def _signal(_signum, _frame):
        logger.warning("Hub 收到终止信号")
        shutdown()

    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):
            pass

    task.LoopingCall(supervisor.poll).start(settings.SERVICE_HEARTBEAT_INTERVAL, now=True)

    try:
        reactor.run()
    finally:
        try:
            supervisor.stop_all()
        except Exception:
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass
        try:
            stop_file.unlink()
        except OSError:
            pass
    logger.info("Hub 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
