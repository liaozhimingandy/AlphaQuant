#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_hub_service_health.py
# @Description : hub 的服务健康判定必须分三层：进程 → 引擎容器 → 组件
#
#                "进程在跑"是最弱的一层信号。worker 是在 engine.start() **之前**
#                就开始写心跳的（要让 supervisor 尽快看到本服务），而引擎对组件
#                的初始化异常是逐个隔离的（一个组件挂了不影响其它）——
#                于是"进程活着、面板一片绿、但一笔数据都没采"是完全可能的状态。
#
#                这组测试钉住：这种状态必须被标成 degraded 并说明原因，
#                而不是报健康把人骗过去。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.common.jsonio import write_json_atomic
from app.engine.hub.spec import HubSpec, ServiceSpec, parse_service
from app.engine.hub.supervisor import ServiceSupervisor


class _TmpMixin:
    def _tmp(self) -> Path:
        d = tempfile.mkdtemp(prefix="aq-hub-health-")
        self.addCleanup(lambda: _rmtree(Path(d)))
        return Path(d)


def _rmtree(p: Path) -> None:
    import shutil

    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


class TestThreeLayerHealth(_TmpMixin, unittest.TestCase):
    """三层都过才是健康；任一层挂了都要降级并说清原因。"""

    def _hub(self, root: Path) -> HubSpec:
        return HubSpec(
            services=[ServiceSpec(id="svc", role="测试服务", enabled=True)],
            state_dir=str(root),
            heartbeat_timeout=15,
        )

    def _status(self, root: Path, **state) -> dict:
        d = root / "svc"
        d.mkdir(parents=True, exist_ok=True)
        # PID 指向自己：进程一定活着，于是判定的差异只来自后面两层
        write_json_atomic(d / "service.pid", {"pid": os.getpid()}, indent=None)
        payload = {
            "service_id": "svc",
            "heartbeat_at": datetime.now().isoformat(),
        }
        payload.update(state)
        write_json_atomic(d / "state.json", payload, indent=None)
        return ServiceSupervisor(self._hub(root)).service_status("svc")

    def test_all_three_layers_ok_is_healthy(self):
        root = self._tmp()
        st = self._status(
            root,
            engine_status="running",
            engine_running=True,
            components_bad=[],
            boot_seconds=120.0,
        )
        self.assertEqual(st["health"], "healthy")
        self.assertEqual(st["degraded_reasons"], [])
        self.assertTrue(st["alive"])
        self.assertTrue(st["engine_running"])

    def test_process_alive_but_engine_never_started_is_degraded(self):
        """最常见的一类骗局：进程在，引擎容器根本没起来。"""
        st = self._status(
            self._tmp(),
            engine_status="initializing",
            engine_running=False,
            components_bad=[],
            boot_seconds=120.0,
        )
        self.assertEqual(st["health"], "degraded")
        self.assertTrue(st["alive"])
        self.assertIn("引擎容器未运行", " ".join(st["degraded_reasons"]))

    def test_boot_grace_period_not_degraded(self):
        """刚 fork 出来几秒内引擎还在装配，这是正常启动过程，不该报警。"""
        st = self._status(
            self._tmp(),
            engine_status="initializing",
            engine_running=False,
            boot_seconds=3.0,
        )
        self.assertEqual(st["health"], "healthy")

    def test_unhealthy_component_is_degraded(self):
        st = self._status(
            self._tmp(),
            engine_status="running",
            engine_running=True,
            components_bad=["collection_service", "news_service"],
            boot_seconds=120.0,
        )
        self.assertEqual(st["health"], "degraded")
        self.assertEqual(len(st["components_bad"]), 2)
        self.assertIn("组件异常", " ".join(st["degraded_reasons"]))

    def test_engine_snapshot_error_is_degraded(self):
        st = self._status(
            self._tmp(),
            engine_status="running",
            engine_running=True,
            engine={"error": "调度器缺失"},
            components_bad=[],
            boot_seconds=120.0,
        )
        self.assertEqual(st["health"], "degraded")
        self.assertIn("调度器缺失", " ".join(st["degraded_reasons"]))

    def test_legacy_heartbeat_without_engine_field_is_unknown(self):
        """旧版心跳没有引擎层字段：不谎报健康，也不误报降级。

        hub 往往是长跑的，worker 却是分批重启的 —— 升级后总要经历一段
        "新 hub + 旧 worker"的时期，这段时期不能把在干活的服务标成故障。
        """
        st = self._status_legacy(self._tmp())
        self.assertEqual(st["health"], "unknown")
        self.assertTrue(st["alive"])
        self.assertIn("旧版", " ".join(st["degraded_reasons"]))

    def _status_legacy(self, root: Path) -> dict:
        """写一份旧版格式的心跳（只有进程级字段）。"""
        d = root / "svc"
        d.mkdir(parents=True, exist_ok=True)
        write_json_atomic(d / "service.pid", {"pid": os.getpid()}, indent=None)
        write_json_atomic(d / "state.json", {
            "service_id": "svc",
            "running": True,
            "heartbeat_at": datetime.now().isoformat(),
            "started_at": "2020-01-01T00:00:00",
        }, indent=None)
        return ServiceSupervisor(self._hub(root)).service_status("svc")

    def test_dead_process_is_stopped_not_degraded(self):
        """进程都没了就没有"降级"可言，明确报 stopped。"""
        root = self._tmp()
        st = ServiceSupervisor(self._hub(root)).service_status("svc")
        self.assertEqual(st["health"], "stopped")
        self.assertFalse(st["alive"])
        self.assertEqual(st["degraded_reasons"], [])

    def test_stale_heartbeat_is_stale(self):
        st = self._status(
            self._tmp(),
            heartbeat_at="2020-01-01T00:00:00",
            engine_status="running",
            engine_running=True,
        )
        self.assertEqual(st["health"], "stale")


class TestServiceSpecDispatch(_TmpMixin, unittest.TestCase):
    """服务编排里的采集分工与资讯配置解析。"""

    def test_collector_and_news_config_parsed(self):
        spec = parse_service({
            "id": "fundamental",
            "collector": {
                "enabled": True,
                "symbols": ["600519", "000858"],
                "with_fundamental": True,
                "with_flow": False,
                "categories": [],
            },
            "news_config": {"interval": 60, "sources": [{"type": "rss", "urls": []}]},
        })
        self.assertEqual(spec.collector["symbols"], ["600519", "000858"])
        self.assertFalse(spec.collector["with_flow"])
        self.assertEqual(spec.news_config["interval"], 60)

    def test_non_object_values_are_ignored(self):
        """`"collector": true` 语义不明，宁可当没配，也不猜。"""
        spec = parse_service({"id": "x", "collector": True, "news_config": "rss"})
        self.assertEqual(spec.collector, {})
        self.assertEqual(spec.news_config, {})

    def test_defaults_are_empty_and_serializable(self):
        spec = parse_service({"id": "x"})
        payload = spec.to_dict()
        self.assertEqual(payload["collector"], {})
        self.assertEqual(payload["news_config"], {})


if __name__ == "__main__":
    unittest.main()
