#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : worker.py
# @Description : 单个后台服务进程的入口（由 hub supervisor 拉起）
#               职责：装配本服务的引擎 + 总线角色 + 心跳 + 远程指令轮询 + 优雅退出
#               用法：python -m app.core.hub.worker --service market --services-config config/services.json
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.bus.components import BusPublisherComponent, BusSubscriberComponent
from app.core.config import settings
from app.core.hub.spec import ServiceSpec, load_hub_spec
from app.utils.jsonio import json_safe, read_json, write_json_atomic
from app.utils.logger import logger


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.core.hub.worker", description="AlphaQuant 多服务：单服务进程"
    )
    p.add_argument("--service", required=True, help="服务 id")
    p.add_argument("--services-config", default="", help="services.json 路径")
    p.add_argument("--no-pid", action="store_true", help="不写 PID 文件")
    return p


class ServiceWorker:
    """把 ServiceSpec 落成一个真实运行的服务。"""

    def __init__(self, spec: ServiceSpec, hub_state_dir: Path, hub_bus_dir: Path) -> None:
        self.spec = spec
        self.state_dir = Path(hub_state_dir) / spec.id
        self.bus_dir = Path(hub_bus_dir)
        self.snapshots_dir = self.state_dir / "snapshots"
        self.commands_dir = self.state_dir / "commands"
        self.done_dir = self.commands_dir / "done"
        self.pid_file = self.state_dir / "service.pid"
        self.stop_file = self.state_dir / "service.stop"
        self.log_file = self.state_dir / "service.log"
        self.tasks_file = self.state_dir / "tasks.json"
        self.state_file = self.state_dir / "state.json"

        self.engine: Any = None
        self._heartbeat = None
        self._command_loop = None
        self.started_at = datetime.now().isoformat()
        self.last_error = ""

        for d in (self.state_dir, self.snapshots_dir, self.commands_dir, self.done_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ============================================================
    # 任务配置落盘（让服务配置可被人工查看和直接编辑）
    # ============================================================
    def _materialize_tasks(self) -> str:
        payload: Dict[str, Any] = {
            "_comment": f"由 services.json 中服务 '{self.spec.id}' 生成，可直接编辑",
            "mode": self.spec.mode,
            "market_mode": self.spec.market_mode,
            "market_interval": self.spec.interval if self.spec.interval is not None else 1.0,
            "start": self.spec.start,
            "end": self.spec.end,
            "data_source": self.spec.data_source,
            "tasks": self.spec.tasks,
        }
        if self.spec.task_config:
            base = read_json(self.spec.task_config)
            if isinstance(base, dict):
                merged = {**base, **{k: v for k, v in payload.items() if v not in ("", None, [])}}
                if not merged.get("tasks"):
                    merged["tasks"] = base.get("tasks") or []
                payload = merged
        write_json_atomic(self.tasks_file, payload, indent=2)
        return str(self.tasks_file)

    # ============================================================
    # 装配
    # ============================================================
    def build(self):
        from app.core.engine.builder import build_engine

        task_config = self._materialize_tasks()
        # market_mode = none/bus/off 表示"本服务不连行情源"，
        # 行情完全由总线订阅组件供给（多服务协作的典型角色划分）
        local_market = self.spec.market_mode.lower() not in ("none", "bus", "off")
        self.engine = build_engine(
            task_config=task_config,
            mode=self.spec.mode,
            symbols=list(self.spec.symbols) or None,
            market_mode=self.spec.market_mode if local_market else "poll",
            market_interval=self.spec.interval,
            start=self.spec.start,
            end=self.spec.end,
            data_source=self.spec.data_source,
            with_news=self.spec.news,
            with_market=local_market,
            with_monitor=self.spec.monitor,
            monitor_host=self.spec.host,
            monitor_port=self.spec.port,
            monitor_interval=self.spec.snapshot_interval,
            snapshot_dir=str(self.snapshots_dir),
            namespace=self.spec.namespace or self.spec.id,
            auto_load_runtime=True,
        )

        # 总线角色：行情/新闻跨服务传递
        if self.spec.publish:
            self.engine.register_component(
                BusPublisherComponent(
                    bus_dir=str(self.bus_dir),
                    node_id=self.spec.id,
                    publish_bars="bar" in self.spec.publish,
                    publish_news="news" in self.spec.publish,
                )
            )
        if self.spec.subscribe:
            self.engine.register_component(
                BusSubscriberComponent(
                    bus_dir=str(self.bus_dir),
                    node_id=self.spec.id,
                    subscribe=list(self.spec.subscribe),
                    interval=self.spec.bus_interval,
                    seek_end_on_start=self.spec.bus_seek_end,
                )
            )

        # 子进程把自己注册进 PID 文件（supervisor 也需要它来停服务）
        payload = {
            "pid": os.getpid(),
            "service_id": self.spec.id,
            "role": self.spec.role,
            "started_at": self.started_at,
            "cwd": str(settings.BASE_DIR),
            "log_file": str(self.log_file),
            "stop_file": str(self.stop_file),
            "port": self.spec.port,
            "namespace": self.spec.namespace or self.spec.id,
        }
        write_json_atomic(self.pid_file, payload, indent=2)
        return self.engine

    # ============================================================
    # 心跳
    # ============================================================
    def _state_payload(self) -> Dict[str, Any]:
        engine_part: Dict[str, Any] = {}
        tasks: List[Dict[str, Any]] = []
        components: List[Dict[str, Any]] = []
        bus: Dict[str, Any] = {}

        if self.engine is not None:
            try:
                snap = self.engine.snapshot()
                engine_part = snap.get("engine") or {}
                components = [
                    {"name": k, **{kk: vv for kk, vv in v.items() if kk != "snapshot"}}
                    for k, v in (snap.get("components") or {}).items()
                ]
            except Exception as exc:
                engine_part = {"error": str(exc)}
            try:
                tasks = self.engine.control.list_tasks()
            except Exception:
                tasks = []
            for name in ("bus_publisher", "bus_subscriber"):
                comp = self.engine.get_component(name)
                if comp is not None:
                    try:
                        bus[name] = comp.snapshot()
                    except Exception:
                        pass

        total_equity = 0.0
        total_pnl = 0.0
        trades = 0
        for t in tasks:
            total_equity += float(t.get("equity") or 0.0)
            total_pnl += float(t.get("total_pnl") or 0.0)
            trades += int(t.get("trade_count") or 0)

        monitor = self.engine.get_component("monitor") if self.engine else None
        monitor_stats = monitor.stats_dict() if monitor is not None else {}

        return {
            "service_id": self.spec.id,
            "role": self.spec.role,
            "running": True,
            "pid": os.getpid(),
            "heartbeat_at": datetime.now().isoformat(),
            "started_at": self.started_at,
            "mode": self.spec.mode,
            "market_mode": self.spec.market_mode,
            "port": self.spec.port,
            "monitor_ok": bool(monitor_stats.get("listen_ok")),
            "panel_url": (
                f"http://{self.spec.host or settings.MONITOR_HOST}:{self.spec.port}/"
                if self.spec.monitor and self.spec.port
                else ""
            ),
            "engine": engine_part,
            "components": components,
            "tasks": tasks,
            "summary": {
                "task_count": len(tasks),
                "total_equity": round(total_equity, 2),
                "total_pnl": round(total_pnl, 2),
                "trade_count": trades,
            },
            "bus": bus,
            "monitor": monitor_stats,
            "last_error": self.last_error,
        }

    def _write_state(self) -> bool:
        """写心跳。返回 True 让 LoopingCall 继续（返回 False 会停表）。"""
        try:
            payload = json_safe(self._state_payload())
            # 心跳写盘很小，但仍在 reactor 线程上；用原子写避免半截文件
            write_json_atomic(self.state_file, payload, indent=None)
        except Exception as exc:
            logger.debug(f"心跳写入失败: {exc}")
        return True

    # ============================================================
    # 远程指令
    # ============================================================
    def _poll_commands(self) -> bool:
        """扫描 commands/ 目录执行指令，执行后移入 done/。"""
        if self.engine is None:
            return True
        try:
            files = sorted(self.commands_dir.glob("*.json"))
        except OSError:
            return True

        for path in files:
            cmd = read_json(path)
            result: Dict[str, Any]
            if not isinstance(cmd, dict):
                result = {"ok": False, "error": "指令不是 JSON object"}
            else:
                try:
                    result = self.engine.control.apply_command(cmd)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
            logger.info(f"[{self.spec.id}] 远程指令 {cmd if isinstance(cmd, dict) else path.name} → {result}")
            try:
                done_path = self.done_dir / path.name
                archived = dict(cmd) if isinstance(cmd, dict) else {"raw": str(cmd)}
                archived["_result"] = result
                archived["_done_at"] = datetime.now().isoformat()
                write_json_atomic(done_path, archived, indent=None)
                path.unlink()
            except OSError as exc:
                logger.debug(f"指令归档失败: {exc}")
        return True

    # ============================================================
    # 运行
    # ============================================================
    def run(self) -> int:
        from twisted.internet import reactor, task

        from app.core.engine.service_runner import install_stop_watcher

        try:
            self.build()
        except Exception as exc:
            self.last_error = f"装配失败: {exc}"
            logger.critical(self.last_error, exc_info=True)
            self._write_state_final(False)
            return 1

        sm = self.engine.get_component("strategy_manager")
        logger.info("=" * 70)
        logger.info(
            f"服务启动 [{self.spec.id}] {self.spec.role} | 任务 {len(sm) if sm else 0} 个 | "
            f"模式 {self.spec.mode} | 行情 {self.spec.market_mode} | "
            f"发布 {self.spec.publish} | 订阅 {self.spec.subscribe} | PID {os.getpid()}"
        )
        if self.spec.monitor and self.spec.port:
            logger.info(f"本服务面板: http://{self.spec.host or settings.MONITOR_HOST}:{self.spec.port}/")
        logger.info("=" * 70)

        install_stop_watcher(self.engine, self.stop_file)

        # 心跳：先立即写一次，supervisor 才能尽快看到本服务
        self._write_state()
        self._heartbeat = task.LoopingCall(self._write_state)
        self._heartbeat.start(settings.SERVICE_HEARTBEAT_INTERVAL, now=False)

        # 指令轮询
        self._command_loop = task.LoopingCall(self._poll_commands)
        self._command_loop.start(1.0, now=False)

        exit_code = 0
        try:
            ctx = self.engine.start()
            logger.info(f"[{self.spec.id}] 服务退出 | run_id={ctx.run_id}")
            if sm is not None:
                logger.info("\n" + sm.summary())
        except KeyboardInterrupt:  # pragma: no cover
            logger.warning(f"[{self.spec.id}] 收到中断")
            self.engine.stop(graceful=True)
        except Exception as exc:
            self.last_error = str(exc)
            logger.critical(f"[{self.spec.id}] 运行异常: {exc}", exc_info=True)
            exit_code = 1
        finally:
            if reactor.running:  # pragma: no cover
                reactor.stop()
            self._write_state_final(False)
            from app.core.engine.daemon import clear_pid

            clear_pid(self.pid_file)
            try:
                self.stop_file.unlink()
            except OSError:
                pass
        return exit_code

    def _write_state_final(self, running: bool) -> None:
        try:
            payload = json_safe(self._state_payload())
            payload["running"] = running
            payload["stopped_at"] = datetime.now().isoformat()
            payload["last_error"] = self.last_error
            write_json_atomic(self.state_file, payload, indent=None)
        except Exception as exc:
            logger.debug(f"最终状态写入失败: {exc}")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings.ensure_dirs()

    hub = load_hub_spec(args.services_config or None)
    spec = hub.get(args.service)
    if spec is None:
        logger.critical(f"未找到服务配置: {args.service!r}（文件 {hub.source}）")
        return 2
    if not spec.enabled:
        logger.warning(f"服务 {spec.id} 已禁用，退出")
        return 0

    worker = ServiceWorker(spec, hub.state_root, hub.bus_root)
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
