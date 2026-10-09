#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_monitor.py
# @Description : 运行快照 + 监控面板数据构建的单元测试
#               重点验证：快照能落盘、能读回、枚举被正确序列化、面板载荷结构稳定
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import math
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app.core.engine.engine import BaseQuantEngine
from app.core.engine.event import EventBus
from app.core.engine.settings import EngineContext, EngineStatus, RunMode
from app.core.market.types import Bar, Order, OrderStatus, Side
from app.core.monitor.snapshot import SnapshotStore
from app.core.monitor.web import MonitorWebComponent
from app.test.helpers import TempWorkspaceMixin
from app.utils.jsonio import read_json, write_json_atomic


def make_task_spec(task_id: str = "t1", symbol: str = "000001") -> dict:
    return {
        "task_id": task_id,
        "symbol": symbol,
        "warmup_bars": 5,
        "sizer": {"type": "percent", "pct": 0.3},
        "strategy": {"type": "ma_cross", "params": {"fast": 3, "slow": 8}},
        "risk": [{"type": "cash_reserve"}],
    }


def gen_bars(symbol: str, n: int = 40) -> list[Bar]:
    base = datetime(2024, 1, 1)
    out = []
    for i in range(n):
        c = 10 + 0.05 * i + math.sin(i / 5.0)
        out.append(Bar(symbol, base + timedelta(days=i), c - 0.1, c + 0.3, c - 0.3, c, 1000, 0))
    return out


def make_engine(tasks=None) -> BaseQuantEngine:
    """搭一台"能跑但不起 reactor"的引擎，用于验证控制面与监控面。"""
    from app.core.engine.component import TaskSchedulerComponent
    from app.core.engine.components import StrategyManagerComponent

    engine = BaseQuantEngine.create({"RUN_MODE": "BACKTEST"})
    engine.register_component(TaskSchedulerComponent())
    engine.register_component(StrategyManagerComponent(tasks=tasks or []))
    # 手动初始化策略中枢，让它拿到引擎的事件总线。
    # 否则直接喂 K 线时成交事件会发布到 None 上，日志里刷出误导性的 ERROR，
    # 把测试输出淹掉——真实错误反而看不见了。
    engine.get_component("strategy_manager").initialize(
        engine.context, engine.get_event_bus(), {}
    )
    return engine


class TestSnapshotStore(TempWorkspaceMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.base = self.workdir()
        self.engine = make_engine()
        self.engine.get_component("strategy_manager").load_tasks(
            [make_task_spec("s-a"), make_task_spec("s-b", "600000")]
        )
        self.engine.get_component("strategy_manager").runtime.start_all()
        self.store = SnapshotStore("run-test", base_dir=self.base, keep=3)

    def test_write_creates_files(self):
        self.store.write(self.engine)
        self.assertTrue(self.store.latest_path.exists())
        self.assertTrue(self.store.meta_path.exists())
        self.assertGreaterEqual(len(list(self.store.history_dir.glob("*.json"))), 1)

    def test_capture_structure(self):
        snap = self.store.capture(self.engine)
        for key in ("run_id", "captured_at", "engine", "components", "tasks", "orders_recent"):
            self.assertIn(key, snap)
        self.assertEqual(snap["run_id"], "run-test")
        self.assertEqual(len(snap["tasks"]), 2)
        self.assertEqual(snap["task_count"], 2)

    def test_tasks_have_equity_curve(self):
        sm = self.engine.get_component("strategy_manager")
        for b in gen_bars("000001", 30):
            sm.runtime.on_bar(b)
        snap = self.store.capture(self.engine)
        task = next(t for t in snap["tasks"] if t["task_id"] == "s-a")
        self.assertIn("equity_curve", task)
        self.assertGreaterEqual(len(task["equity_curve"]), 2)
        # 曲线点是 [iso时间, 数值]
        self.assertEqual(len(task["equity_curve"][0]), 2)
        self.assertIsInstance(task["equity_curve"][0][1], float)

    def test_enums_serialized_as_strings(self):
        """回归：str 子类枚举必须变成 'BUY'/'FILLED'，不能是 '<Side.BUY: ...>'。"""
        order = Order(
            order_id="1", task_id="t1", symbol="000001", side=Side.BUY,
            size=100, status=OrderStatus.FILLED, created_at=datetime.now(),
        )
        self.store.note_order(order)
        snap = self.store.capture(self.engine)
        rec = snap["orders_recent"][-1]
        self.assertEqual(rec["side"], "BUY")
        self.assertEqual(rec["status"], "FILLED")
        self.assertNotIn("Side.", str(rec))

    def test_order_feed_persists_to_disk(self):
        for i in range(3):
            self.store.note_order(
                Order(order_id=str(i), task_id="t1", symbol="000001",
                      side=Side.SELL, size=100, status=OrderStatus.REJECTED,
                      created_at=datetime.now(), reject_reason="测试")
            )
        rows = self.store.order_history(10)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["side"], "SELL")  # newest_first

    def test_history_pruned_to_keep(self):
        for _ in range(6):
            self.store.write(self.engine)
        files = list(self.store.history_dir.glob("*.json"))
        self.assertLessEqual(len(files), 3)

    def test_runs_and_load(self):
        self.store.write(self.engine)
        runs = SnapshotStore.runs(self.base)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], "run-test")
        loaded = SnapshotStore.load(self.base, "run-test")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["engine"]["mode"], "BACKTEST")

    def test_load_latest_without_run_id(self):
        self.store.write(self.engine)
        another = SnapshotStore("run-2", base_dir=self.base)
        another.write(self.engine)
        loaded = SnapshotStore.load(self.base)
        self.assertIsNotNone(loaded)
        self.assertIn(loaded["run_id"], {"run-test", "run-2"})

    def test_load_missing_returns_none(self):
        self.assertIsNone(SnapshotStore.load(self.base, "nope"))

    def test_write_is_atomic_readable(self):
        self.store.write(self.engine)
        data = read_json(self.store.latest_path)
        self.assertIsInstance(data, dict)
        self.assertIn("tasks", data)


class TestMonitorWebData(TempWorkspaceMixin, unittest.TestCase):
    """验证面板的数据构建（不起 reactor，只测载荷）。"""

    isolated_settings = ("RUNTIME_DIR",)

    def setUp(self) -> None:
        self.snap_dir = self.workdir() / "snaps"
        self.ns = f"test-{uuid.uuid4().hex[:8]}"

        self.engine = make_engine()
        self.engine.get_component("strategy_manager").load_tasks([make_task_spec("m1")])
        self.engine.get_component("strategy_manager").runtime.start_all()

        self.comp = MonitorWebComponent(
            engine=self.engine, port=0, snapshot_dir=str(self.snap_dir), namespace=self.ns
        )
        ctx = EngineContext(
            run_id="panel-run", run_mode=RunMode.SIMULATE, engine_status=EngineStatus.RUNNING
        )
        self.comp.initialize(ctx, EventBus(), {})

    def test_overview_shape(self):
        ov = self.comp.build_overview()
        for key in ("engine", "components", "tasks", "task_count", "monitor", "audit", "orders"):
            self.assertIn(key, ov)
        self.assertEqual(ov["task_count"], 1)
        names = [c["name"] for c in ov["components"]]
        self.assertIn("strategy_manager", names)

    def test_run_id_single_sourced_from_engine(self):
        """回归：面板上的 run_id 必须只有一个来源。

        以前 ``ov["engine"]["run_id"]`` 取自引擎快照、``ov["monitor"]["run_id"]``
        取自监控组件的 context。两者一旦不一致，同一个页面上会出现两个 run_id，
        运维无法判断该去哪个目录翻快照。
        """
        ov = self.comp.build_overview()
        engine_run_id = ov["engine"]["run_id"]
        self.assertEqual(ov["monitor"]["run_id"], engine_run_id)
        self.assertEqual(self.comp.run_id, engine_run_id)
        # 快照目录也必须同名，否则"页面上显示的"和"磁盘上的"对不上
        self.assertEqual(self.comp.store.root.name, engine_run_id)

    def test_stats_dict(self):
        st = self.comp.stats_dict()
        self.assertEqual(st["port"], 0)
        self.assertFalse(st["listen_ok"])  # 未启动监听
        self.assertEqual(st["namespace"], self.ns)

    def test_snapshot_method_returns_stats(self):
        self.assertIn("run_id", self.comp.snapshot())

    def test_flush_writes_snapshot(self):
        path = self.comp.flush()
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).exists())
        data = read_json(path)
        self.assertEqual(data["run_id"], self.comp.run_id)

    def test_control_events_recorded_in_audit(self):
        self.comp.event_bus.publish("control_command", action="pause_task", detail={"task_id": "m1"})
        self.assertIn("audit", self.comp.build_overview())
        ov = self.comp.build_overview()
        self.assertEqual(len(ov["audit"]), 1)
        self.assertEqual(ov["audit"][0]["action"], "pause_task")

    def test_order_events_feed_store(self):
        order = Order(order_id="x", task_id="m1", symbol="000001", side=Side.BUY,
                      size=100, status=OrderStatus.FILLED, created_at=datetime.now())
        self.comp.event_bus.publish("order_filled", order=order)
        self.assertEqual(self.comp.stats["orders"], 1)
        self.assertEqual(len(self.comp.order_feed(10)), 1)

    def test_rejected_events_counted(self):
        order = Order(order_id="y", task_id="m1", symbol="000001", side=Side.BUY,
                      size=100, status=OrderStatus.REJECTED, created_at=datetime.now())
        self.comp.event_bus.publish("signal_rejected", order=order, reason="风控")
        self.assertEqual(self.comp.stats["rejections"], 1)

    def test_log_tail_returns_source(self):
        lines, source = self.comp.log_tail(5)
        self.assertIsInstance(lines, list)
        self.assertIsInstance(source, str)

    def test_is_busy_false(self):
        self.assertFalse(self.comp.is_busy())

    def test_dashboard_html_self_contained(self):
        from app.core.monitor.dashboard import DASHBOARD_HTML

        self.assertIn("<!DOCTYPE html>", DASHBOARD_HTML)
        self.assertIn("<canvas", DASHBOARD_HTML)
        self.assertIn("/api/overview", DASHBOARD_HTML)
        # 不能引外部资源（离线/内网必须能打开）
        self.assertNotIn("http://cdn", DASHBOARD_HTML)
        self.assertNotIn("https://cdn", DASHBOARD_HTML)
        self.assertNotIn("src=\"http", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
