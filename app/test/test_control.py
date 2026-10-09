#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_control.py
# @Description : 运行时控制中心单元测试
#               核心不变量：不重启就能增删任务、新任务自动纳入行情订阅、
#               控制指令留痕、运行时任务可持久化并在重启后重放
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
import uuid

from app.core.engine.engine import BaseQuantEngine
from app.core.engine.event import StandardEvents
from app.core.market.types import TaskState
from app.test.helpers import TempWorkspaceMixin
from app.utils.jsonio import read_json


def make_task_spec(task_id: str, symbol: str = "000001", **over) -> dict:
    spec = {
        "task_id": task_id,
        "symbol": symbol,
        "warmup_bars": 5,
        "sizer": {"type": "percent", "pct": 0.2},
        "strategy": {"type": "ma_cross", "params": {"fast": 3, "slow": 8}},
        "risk": [{"type": "cash_reserve"}],
    }
    spec.update(over)
    return spec


def make_engine(tasks=None, with_market: bool = True) -> BaseQuantEngine:
    from app.core.engine.component import TaskSchedulerComponent
    from app.core.engine.components import MarketCenterComponent, StrategyManagerComponent

    engine = BaseQuantEngine.create({"RUN_MODE": "BACKTEST"})
    if with_market:
        engine.register_component(MarketCenterComponent(symbols=[], mode="poll"))
    engine.register_component(TaskSchedulerComponent())
    engine.register_component(StrategyManagerComponent(tasks=tasks or []))
    return engine


class TestControlCenter(TempWorkspaceMixin, unittest.TestCase):
    #: 运行时任务目录重定向到临时工作区——测试不该往工程的 output/ 里写东西
    isolated_settings = ("RUNTIME_DIR",)

    def setUp(self) -> None:
        self.ns = f"test-{uuid.uuid4().hex[:8]}"
        self.engine = make_engine()
        self.engine.control.namespace = self.ns
        self.control = self.engine.control

    # ---------------- 任务增删 ----------------
    def test_add_task(self):
        tid = self.control.add_task(make_task_spec("c-1"))
        self.assertEqual(tid, "c-1")
        self.assertIsNotNone(self.control.get_task("c-1"))
        self.assertEqual(len(self.control.list_tasks()), 1)

    def test_add_task_duplicate_rejected(self):
        self.control.add_task(make_task_spec("c-1"))
        with self.assertRaises(ValueError):
            self.control.add_task(make_task_spec("c-1"))

    def test_add_task_bad_spec_rejected(self):
        with self.assertRaises(ValueError):
            self.control.add_task({"strategy": "ma_cross"})  # 缺 symbol

    def test_add_task_registers_market_subscription(self):
        """新任务的标的必须自动纳入行情订阅，否则永远等不到K线。"""
        self.control.add_task(make_task_spec("c-2", "600519"))
        market = self.engine.get_component("market_center")
        self.assertIn("600519", market.symbols)

    def test_add_task_with_watch_symbols(self):
        self.control.add_task(
            make_task_spec("c-3", "000001", watch_symbols=["600000", "000002"])
        )
        market = self.engine.get_component("market_center")
        self.assertIn("600000", market.symbols)
        self.assertIn("000002", market.symbols)

    def test_task_detail_has_all_sections(self):
        self.control.add_task(make_task_spec("c-4"))
        detail = self.control.task_detail("c-4")
        for key in ("task_id", "equity_curve", "trades", "orders", "news",
                    "risk_rules", "watch_symbols", "symbol", "strategy"):
            self.assertIn(key, detail)
        self.assertEqual(detail["risk_rules"][0]["name"], "cash_reserve")

    def test_task_detail_missing_returns_none(self):
        self.assertIsNone(self.control.task_detail("nope"))

    def test_pause_not_running_task_returns_false(self):
        """引擎没跑（任务还是 CREATED）时暂停必须报失败，不能谎报成功。

        早期实现无论成没成都返回 True，面板会显示"已暂停"但任务照旧下单。
        """
        self.control.add_task(make_task_spec("c-5"))
        self.assertEqual(self.control.get_task("c-5").status, TaskState.CREATED)
        self.assertFalse(self.control.pause_task("c-5"))
        self.assertEqual(self.control.get_task("c-5").status, TaskState.CREATED)

    def test_resume_not_paused_task_returns_false(self):
        self.control.add_task(make_task_spec("c-5b"))
        self.assertFalse(self.control.resume_task("c-5b"))

    def test_pause_unknown_task_returns_false(self):
        self.assertFalse(self.control.pause_task("nope"))

    def test_remove_task(self):
        self.control.add_task(make_task_spec("c-6"))
        self.assertTrue(self.control.remove_task("c-6"))
        self.assertIsNone(self.control.get_task("c-6"))
        self.assertFalse(self.control.remove_task("c-6"))

    # ---------------- 组件控制 ----------------
    def test_component_states(self):
        states = self.control.component_states()
        names = [s["name"] for s in states]
        self.assertIn("strategy_manager", names)
        self.assertIn("market_center", names)

    def test_disable_enable_component(self):
        self.assertTrue(self.control.disable_component("market_center"))
        self.assertFalse(self.engine.get_component("market_center").enabled)
        self.assertTrue(self.control.enable_component("market_center"))
        self.assertTrue(self.engine.get_component("market_center").enabled)

    def test_unknown_component_returns_false(self):
        self.assertFalse(self.control.enable_component("nope"))

    # ---------------- 统一指令入口 ----------------
    def test_apply_command_add_and_remove(self):
        r = self.control.apply_command({"action": "add_task", "spec": make_task_spec("cmd-1")})
        self.assertTrue(r["ok"])
        self.assertEqual(r["task_id"], "cmd-1")
        r = self.control.apply_command({"action": "remove_task", "task_id": "cmd-1"})
        self.assertTrue(r["ok"])
        self.assertIsNone(self.control.get_task("cmd-1"))

    def test_apply_command_pause_failure_carries_reason(self):
        """失败的指令要带原因，否则面板只能显示"失败"却不知道为什么。"""
        self.control.apply_command({"action": "add_task", "spec": make_task_spec("cmd-2")})
        r = self.control.apply_command({"action": "pause_task", "task_id": "cmd-2"})
        self.assertFalse(r["ok"])
        self.assertIn("不允许该切换", r["error"])

    def test_apply_command_unknown_action(self):
        r = self.control.apply_command({"action": "no_such_action"})
        self.assertFalse(r["ok"])
        self.assertIn("未知指令", r["error"])

    def test_apply_command_error_is_captured(self):
        r = self.control.apply_command({"action": "add_task", "spec": {"symbol": "x"}})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_apply_command_status(self):
        r = self.control.apply_command({"action": "status"})
        self.assertTrue(r["ok"])
        self.assertIn("run_id", r["engine"])

    # ---------------- 留痕 ----------------
    def test_audit_events_published(self):
        seen = []
        self.engine.get_event_bus().subscribe(
            StandardEvents.CONTROL_COMMAND, lambda action="", detail=None, **kw: seen.append(action)
        )
        self.control.add_task(make_task_spec("c-7"))
        self.control.disable_component("market_center")
        self.control.remove_task("c-7")
        self.assertEqual(seen, ["add_task", "disable_component", "remove_task"])

    def test_task_added_event(self):
        added = []
        self.engine.get_event_bus().subscribe(
            StandardEvents.TASK_ADDED, lambda task_id="", **kw: added.append(task_id)
        )
        self.control.add_task(make_task_spec("c-8"))
        self.assertEqual(added, ["c-8"])

    # ---------------- 持久化与重放 ----------------
    def test_runtime_tasks_persisted(self):
        self.control.add_task(make_task_spec("p-1"))
        self.control.add_task(make_task_spec("p-2"))
        self.control.remove_task("p-2")
        path = self.control.save_runtime_tasks()
        data = read_json(path)
        self.assertEqual(data["namespace"], self.ns)
        self.assertEqual([t["task_id"] for t in data["tasks"]], ["p-1"])
        self.assertIn("p-2", data["removed_ids"])

    def test_runtime_tasks_replayed_on_restart(self):
        self.control.add_task(make_task_spec("r-1"))
        self.control.save_runtime_tasks()

        # 模拟重启：新引擎 + 同一命名空间
        engine2 = make_engine()
        engine2.control.namespace = self.ns
        ids = engine2.control.load_runtime_tasks()
        self.assertEqual(ids, ["r-1"])
        self.assertIsNotNone(engine2.control.get_task("r-1"))

    def test_replay_missing_file_is_empty(self):
        engine2 = make_engine()
        engine2.control.namespace = f"absent-{uuid.uuid4().hex[:6]}"
        self.assertEqual(engine2.control.load_runtime_tasks(), [])

    def test_load_runtime_tasks_without_runtime(self):
        """没有策略中枢时重放应安全返回空，而不是崩。"""
        engine = BaseQuantEngine.create({"RUN_MODE": "BACKTEST"})
        self.assertEqual(engine.control.load_runtime_tasks(), [])


class TestControlWithRunningEngine(TempWorkspaceMixin, unittest.TestCase):
    """引擎处于 RUNNING 时：新任务自动 start，暂停/恢复才真正可用。"""

    isolated_settings = ("RUNTIME_DIR",)

    def setUp(self) -> None:
        self.ns = f"test-{uuid.uuid4().hex[:8]}"
        self.engine = make_engine()
        self.engine.control.namespace = self.ns
        # 直接把内部状态置为 RUNNING，模拟引擎已启动（避免跑 reactor）
        from app.core.engine.settings import EngineStatus

        self.engine._status = EngineStatus.RUNNING

    def test_add_task_autostarts(self):
        tid = self.engine.control.add_task(make_task_spec("auto-1"))
        self.assertEqual(self.engine.control.get_task(tid).status, TaskState.RUNNING)

    def test_remove_stops_task(self):
        tid = self.engine.control.add_task(make_task_spec("auto-2"))
        self.engine.control.remove_task(tid)
        self.assertIsNone(self.engine.control.get_task(tid))

    def test_pause_resume_roundtrip(self):
        self.engine.control.add_task(make_task_spec("auto-3"))
        self.assertTrue(self.engine.control.pause_task("auto-3"))
        self.assertEqual(self.engine.control.get_task("auto-3").status, TaskState.PAUSED)
        self.assertTrue(self.engine.control.resume_task("auto-3"))
        self.assertEqual(self.engine.control.get_task("auto-3").status, TaskState.RUNNING)

    def test_pause_is_idempotent(self):
        self.engine.control.add_task(make_task_spec("auto-4"))
        self.assertTrue(self.engine.control.pause_task("auto-4"))
        # 再点一次不算失败——状态已经就是目标状态
        self.assertTrue(self.engine.control.pause_task("auto-4"))

    def test_paused_task_ignores_bars(self):
        """暂停必须真的"不交易"，而不是只改个标记位。"""
        from app.test.test_monitor import gen_bars

        self.engine.control.add_task(make_task_spec("auto-5"))
        task = self.engine.control.get_task("auto-5")
        self.engine.control.pause_task("auto-5")
        for bar in gen_bars("000001", 30):
            task.on_bar(bar)
        self.assertEqual(len(task.portfolio.trades), 0)
        self.assertEqual(task.bar_count, 0)

    def test_control_events_published_on_pause(self):
        seen = []
        self.engine.get_event_bus().subscribe(
            StandardEvents.CONTROL_COMMAND, lambda action="", detail=None, **kw: seen.append(action)
        )
        self.engine.control.add_task(make_task_spec("auto-6"))
        self.engine.control.pause_task("auto-6")
        self.engine.control.resume_task("auto-6")
        self.assertEqual(seen, ["add_task", "pause_task", "resume_task"])


class TestEngineSnapshot(unittest.TestCase):
    def test_snapshot_shape(self):
        engine = make_engine()
        snap = engine.snapshot()
        self.assertIn("engine", snap)
        self.assertIn("components", snap)
        self.assertEqual(snap["engine"]["mode"], "BACKTEST")
        self.assertEqual(snap["engine"]["status"], "INITIALIZING")
        for name in ("market_center", "task_scheduler", "strategy_manager"):
            self.assertIn(name, snap["components"])
            self.assertIn("state", snap["components"][name])
            self.assertIn("enabled", snap["components"][name])

    def test_snapshot_survives_broken_component(self):
        """单个组件快照炸掉不能连带整份快照失败。"""
        engine = make_engine()

        class Boom:
            name = "boom"
            enabled = True
            state = "RUNNING"

            def health_check(self):
                raise RuntimeError("boom")

            def snapshot(self):
                raise RuntimeError("boom")

        engine._components["boom"] = Boom()
        engine._component_order.append("boom")
        snap = engine.snapshot()
        self.assertIn("boom", snap["components"])
        self.assertIn("snapshot_error", snap["components"]["boom"])

    def test_uptime_positive(self):
        engine = make_engine()
        self.assertGreaterEqual(engine.uptime_seconds, 0.0)

    def test_component_names_order(self):
        engine = make_engine()
        self.assertEqual(
            engine.component_names(), ["market_center", "task_scheduler", "strategy_manager"]
        )


if __name__ == "__main__":
    unittest.main()
