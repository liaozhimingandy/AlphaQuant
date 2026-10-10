#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : service_runner.py
# @Description : 后台服务入口：常驻跑引擎，支持停止标志 + 信号双通道优雅退出
#               用法：python -m app.core.engine.service_runner --config config/tasks.json ...
#               一般不用手敲，由 `python main.py start` 或 hub supervisor 拉起
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from app.core.config import settings
from app.utils.logger import logger


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.core.engine.service_runner",
        description="AlphaQuant 后台服务（常驻引擎）",
    )
    p.add_argument("--config", default="", help="任务配置文件路径")
    p.add_argument("--mode", default="", choices=["", "BACKTEST", "SIMULATE", "LIVE"])
    p.add_argument("--market-mode", default="", choices=["", "replay", "poll"])
    p.add_argument("--interval", type=float, default=None, help="行情推送间隔（秒）")
    p.add_argument("--start", dest="start_date", default="", help="回放起始日期")
    p.add_argument("--end", dest="end_date", default="", help="回放结束日期")
    p.add_argument("--data-source", default="", choices=["", "db", "csv", "auto", "remote"])
    p.add_argument("--symbol", "-s", action="append", default=[], help="标的，可重复")
    p.add_argument("--host", default="", help="面板监听地址")
    p.add_argument("--port", type=int, default=None, help="面板端口")
    p.add_argument("--snapshot-interval", type=float, default=None, help="快照间隔（秒）")
    p.add_argument("--snapshot-dir", default="", help="快照根目录（多服务各用一份）")
    p.add_argument("--namespace", default="default", help="运行时任务命名空间")
    p.add_argument("--no-news", action="store_true", help="关闭新闻组件")
    p.add_argument("--no-monitor", action="store_true", help="关闭监控面板与快照")
    # ---- 行情采集服务 ----
    p.add_argument("--no-collector", action="store_true", help="关闭行情采集服务")
    p.add_argument("--collect-interval", default="", help="采集频率，如 30s/5m/1h")
    p.add_argument("--collect-period", default="", choices=["", "1d", "1", "5", "15", "30", "60"],
                   help="采集粒度：1d=日线，其余为分钟线")
    p.add_argument("--collect-symbol", action="append", default=[],
                   help="额外纳入采集的标的，可重复")
    p.add_argument("--collector-config", default="", help="采集编排配置文件")
    p.add_argument("--broker-gateway", default="", help="实盘券商网关名（LIVE 模式用）")
    p.add_argument("--broker-endpoint", default="", help="券商接入点 id（见 config/brokers.json）")
    p.add_argument("--pid-file", default="", help="PID 文件路径")
    p.add_argument("--stop-file", default="", help="停止标志文件路径")
    p.add_argument("--no-pid", action="store_true", help="不写 PID 文件（前台调试用）")
    return p


def watch_stop_file(stop_file: str | Path, on_stop, interval: float = 0.5):
    """通用停止标志监听：文件出现即触发 ``on_stop()`` 并停止轮询。

    Windows 没有 SIGTERM，靠信号停服务不可靠；标志文件在三大平台行为一致。
    """
    from twisted.internet import task

    path = Path(stop_file)
    holder: dict = {}

    def _check():
        try:
            if path.exists():
                logger.warning(f"检测到停止标志 {path}，触发优雅退出")
                try:
                    path.unlink()
                except OSError:
                    pass
                lc = holder.get("lc")
                if lc is not None and lc.running:
                    lc.stop()
                on_stop()
                return False
        except Exception as exc:
            logger.debug(f"停止标志检查异常: {exc}")
        return True

    lc = task.LoopingCall(_check)
    holder["lc"] = lc
    lc.start(interval, now=False)
    return lc


def install_stop_watcher(engine: Any, stop_file: str | Path, interval: float = 0.5):
    """引擎版停止监听：文件出现即优雅停止引擎。"""
    return watch_stop_file(stop_file, lambda: engine.stop(graceful=True), interval)


def _write_pid(pid_file: str, stop_file: str, port: Optional[int], args: argparse.Namespace) -> None:
    from app.core.engine.daemon import read_pid, write_pid

    existing = read_pid(pid_file) or {}
    payload = {
        **existing,
        "pid": os.getpid(),
        "started_at": existing.get("started_at") or datetime.now().isoformat(),
        "argv": [sys.executable, "-m", "app.core.engine.service_runner"] + sys.argv[1:],
        "cwd": str(settings.BASE_DIR),
        "log_file": existing.get("log_file", settings.DAEMON_LOG),
        "stop_file": stop_file,
        "port": port,
        "namespace": args.namespace,
        "config": args.config,
    }
    write_pid(payload, pid_file)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings.ensure_dirs()

    pid_file = args.pid_file or settings.PID_FILE
    stop_file = args.stop_file or settings.STOP_FILE

    if not args.no_pid:
        _write_pid(pid_file, stop_file, args.port, args)

    from app.core.engine.builder import build_engine

    try:
        engine = build_engine(
            task_config=args.config or None,
            mode=args.mode,
            symbols=list(args.symbol) or None,
            market_mode=args.market_mode,
            market_interval=args.interval,
            start=args.start_date,
            end=args.end_date,
            data_source=args.data_source or "db",
            with_news=not args.no_news,
            with_monitor=not args.no_monitor,
            monitor_host=args.host,
            monitor_port=args.port,
            monitor_interval=args.snapshot_interval,
            namespace=args.namespace,
            with_collector=False if args.no_collector else None,
            collector_interval=args.collect_interval or None,
            collector_period=args.collect_period or None,
            collector_symbols=list(args.collect_symbol) or None,
            collector_config=args.collector_config or None,
            broker_gateway=args.broker_gateway or "",
            broker_endpoint=args.broker_endpoint or "",
        )
        if args.snapshot_dir:
            mon = engine.get_component("monitor")
            if mon is not None:
                mon.snapshot_base = args.snapshot_dir
    except Exception as exc:
        logger.critical(f"引擎装配失败: {exc}", exc_info=True)
        return 1

    sm = engine.get_component("strategy_manager")
    logger.info("=" * 70)
    logger.info(
        f"后台服务启动 | 任务 {len(sm) if sm else 0} 个 | "
        f"模式 {args.mode or '配置默认'} | 行情 {args.market_mode or '配置默认'} | "
        f"PID {os.getpid()}"
    )
    if not args.no_monitor:
        logger.info(f"监控面板: http://{args.host or settings.MONITOR_HOST}:{args.port or settings.MONITOR_PORT}/")
    col = engine.get_component("data_collector")
    if col is not None:
        from app.core.collect.spec import humanize_frequency

        buckets: dict = {}
        for job in col.spec.jobs:
            buckets[job.interval] = buckets.get(job.interval, 0) + 1
        logger.info(
            "行情采集: "
            + ", ".join(f"{humanize_frequency(k)}×{v}" for k, v in sorted(buckets.items()))
            + f" | 共 {len(col.spec.jobs)} 个任务"
        )
    logger.info("=" * 70)

    # 停止标志轮询要在 start() 之前挂上（start 内部会 reactor.run() 阻塞）
    from twisted.internet import reactor

    install_stop_watcher(engine, stop_file)

    exit_code = 0
    try:
        ctx = engine.start()
        logger.info(f"服务已退出 | run_id={ctx.run_id} | status={ctx.engine_status.value}")
        if sm is not None:
            logger.info("\n" + sm.summary())
    except KeyboardInterrupt:  # pragma: no cover
        logger.warning("收到中断，正在退出")
        engine.stop(graceful=True)
    except Exception as exc:
        logger.critical(f"服务运行异常: {exc}", exc_info=True)
        exit_code = 1
    finally:
        if reactor.running:  # pragma: no cover
            reactor.stop()
        if not args.no_pid:
            from app.core.engine.daemon import clear_pid

            clear_pid(pid_file)
        try:
            Path(stop_file).unlink()
        except OSError:
            pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
