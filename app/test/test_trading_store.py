#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_trading_store.py
# @Description : 交易落库与快照策略测试
#
#                这一组守的是"审计数据不能悄悄丢"：
#                  - 同一批里同一订单出现两次（PENDING→FILLED）不能整批丢
#                  - 单条坏记录不能让整批一起消失
#                  - 快照"内容没变就不写"必须真的生效（否则等于按时间无脑写盘）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import copy
import unittest
from datetime import datetime

from app.core.market.types import Order, OrderStatus, Side
from app.core.monitor.snapshot import content_fingerprint
from app.repository.trading_repository import (
    TradingRepository,
    _dedup_batch,
    get_repository,
)

RUN = "run_test_store"


def _order(order_id: str, status: OrderStatus = OrderStatus.PENDING,
           filled: int = 0) -> Order:
    return Order(order_id=order_id, task_id="t1", symbol="000001", side=Side.BUY,
                 size=1000, price=10.0, status=status, created_at=datetime.now(),
                 filled_size=filled, filled_price=10.0 if filled else 0.0)


class TestBatchDedup(unittest.TestCase):
    """批内去重：同一批次里同一主键只保留最后一次状态。

    不做这件事的后果很严重：模拟撮合是同步的，所以同一个订单几乎必然在同一批里
    出现两次（PENDING 然后 FILLED），两次都会被 `session.get()` 判成"新记录"
    → 两次 INSERT → 唯一约束冲突 → **整批一起回滚**，
    连同一批里的运行记录、权益点、事件全都丢掉。
    """

    def test_order_keeps_last_state(self):
        batch = [
            ("order", {"order_id": "o1", "status": "PENDING"}),
            ("order", {"order_id": "o1", "status": "FILLED"}),
        ]
        rows = _dedup_batch(batch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]["status"], "FILLED")

    def test_different_orders_both_kept(self):
        batch = [
            ("order", {"order_id": "o1"}),
            ("order", {"order_id": "o2"}),
        ]
        self.assertEqual(len(_dedup_batch(batch)), 2)

    def test_append_only_kinds_not_merged(self):
        """成交/权益/事件是 append-only：同一批里出现多次就是多行。"""
        batch = [
            ("trade", {"order_id": "o1", "size": 100}),
            ("trade", {"order_id": "o1", "size": 200}),
            ("equity", {"task_id": "t1", "equity": 1.0}),
            ("equity", {"task_id": "t1", "equity": 2.0}),
        ]
        self.assertEqual(len(_dedup_batch(batch)), 4)

    def test_preserves_relative_order(self):
        batch = [
            ("run", {"run_id": "r1"}),
            ("order", {"order_id": "o1"}),
            ("run", {"run_id": "r1", "status": "STOPPED"}),
        ]
        rows = _dedup_batch(batch)
        self.assertEqual([k for k, _ in rows], ["run", "order"])
        self.assertEqual(rows[0][1]["status"], "STOPPED")

    def test_task_keyed_by_run_and_task(self):
        batch = [
            ("task", {"run_id": "r1", "task_id": "t1"}),
            ("task", {"run_id": "r1", "task_id": "t2"}),
            ("task", {"run_id": "r2", "task_id": "t1"}),
        ]
        self.assertEqual(len(_dedup_batch(batch)), 3)


class TestRepositoryWrites(unittest.TestCase):
    """真写库：验证订单 upsert 与 append-only 语义。"""

    @classmethod
    def setUpClass(cls):
        from app.db.database import Base, engine
        import app.db.models  # noqa: F401

        Base.metadata.create_all(bind=engine)

    def setUp(self):
        from app.db.database import SessionLocal
        from app.db.models import (
            EquityPoint, OrderRecord, RunRecord, TaskRecord, TradeRecordTable,
        )

        with SessionLocal() as s:
            for M in (OrderRecord, EquityPoint, TradeRecordTable, TaskRecord, RunRecord):
                q = s.query(M)
                if hasattr(M, "run_id"):
                    q = q.filter(M.run_id == RUN)
                    q.delete()
            s.commit()
        self.repo = TradingRepository()

    def test_order_upsert_and_append_only(self):
        self.repo.start()
        o = _order("o-batch-1", OrderStatus.PENDING)
        self.repo.record_order(RUN, o)
        o.status = OrderStatus.FILLED
        o.filled_size = 1000
        self.repo.record_order(RUN, o)
        self.repo.record_equity(RUN, "t1", "000001", 100.0, 90.0, 1000, 1000.0)
        self.repo.record_equity(RUN, "t1", "000001", 101.0, 90.0, 1000, 1010.0)
        self.repo.stop()

        rows = TradingRepository.recent_orders(task_id="t1", run_id=RUN)
        self.assertEqual(len(rows), 1, "同一订单应被 upsert 成一行")
        self.assertEqual(rows[0]["status"], "FILLED")

        pts = [p for p in TradingRepository.equity_curve("t1", limit=50)
               if p["equity"] in (100.0, 101.0)]
        self.assertEqual(len(pts), 2, "权益点是 append-only，应保留两条")

    def test_row_failure_does_not_drop_batch(self):
        """一条坏记录不能带走整批 —— 审计记录恰恰是出问题时最需要的。"""
        self.repo.start()
        self.repo.record_run(RUN, mode="SIMULATE")
        self.repo.record_task(RUN, "t1", symbol="000001")
        # 人为塞一条必然失败的记录（symbol 为 None 触发非空约束）
        self.repo._offer("order", {
            "order_id": "bad-row", "run_id": RUN, "task_id": "t1",
            "symbol": None, "side": "BUY", "size": -1, "price": 0.0,
            "status": "FILLED", "filled_size": 0, "filled_price": 0.0,
            "reason": "", "reject_reason": "", "source": "",
            "created_at": datetime.now(), "updated_at": datetime.now(),
        })
        self.repo.stop()
        # 好记录仍然入库了
        runs = [r for r in TradingRepository.runs(20) if r["run_id"] == RUN]
        self.assertTrue(runs, "同批的好记录不该被坏记录连坐丢掉")

    def test_flush_does_not_kill_writer(self):
        """flush 只能刷队列，不能关掉后台写线程 ——
        仓储是进程内共享的，停掉之后引擎的订单就再也写不进去了。"""
        self.repo.start()
        self.repo.record_event(RUN, "flush 测试", category="test")
        self.repo.flush()
        h = self.repo.health()
        self.assertTrue(h["alive"], "flush 之后写线程必须还活着")
        self.repo.stop()

    def test_order_stats_grouping(self):
        self.repo.start()
        self.repo.record_order(RUN, _order("s1", OrderStatus.FILLED, 1000))
        self.repo.record_order(RUN, _order("s2", OrderStatus.REJECTED))
        self.repo.record_order(RUN, _order("s3", OrderStatus.SUBMITTED))
        self.repo.stop()
        stats = TradingRepository.order_stats(task_id="t1", run_id=RUN)
        self.assertEqual(stats.get("FILLED"), 1)
        self.assertEqual(stats.get("REJECTED"), 1)
        self.assertEqual(stats.get("SUBMITTED"), 1)

    def test_disabled_repository_is_noop(self):
        repo = TradingRepository(enabled=False)
        repo.start()
        repo.record_order(RUN, _order("never"))
        repo.stop()
        self.assertEqual(repo.stats["queued"], 0)


class TestSnapshotFingerprint(unittest.TestCase):
    """快照指纹：决定"要不要落盘"。

    没做这件事就是按时间无脑写盘；做错了（把易变字段也算进去）
    则永远判定为"变了"，等于没做。
    """

    def _base(self):
        return {
            "run_id": "r1",
            "captured_at": "2026-01-01T00:00:00",
            "engine": {"status": "RUNNING", "uptime_sec": 10, "snapshot_seq": 1,
                       "memory_mb": 100, "threads": 20},
            "task_count": 1,
            "tasks": [{"task_id": "t1", "equity": 100000.0, "position_size": 0,
                       "last_price": 10.0}],
        }

    def test_volatile_fields_ignored(self):
        a = content_fingerprint(self._base())
        b = copy.deepcopy(self._base())
        b["captured_at"] = "2026-01-01T00:00:05"
        b["engine"].update(uptime_sec=99, snapshot_seq=7, memory_mb=999, threads=88)
        self.assertEqual(a, content_fingerprint(b),
                         "只有时间戳/心跳/内存这类字段变化时不应视为内容变了")

    def test_equity_change_detected(self):
        a = content_fingerprint(self._base())
        b = copy.deepcopy(self._base())
        b["tasks"][0]["equity"] = 100123.45
        self.assertNotEqual(a, content_fingerprint(b))

    def test_position_change_detected(self):
        a = content_fingerprint(self._base())
        b = copy.deepcopy(self._base())
        b["tasks"][0]["position_size"] = 1000
        self.assertNotEqual(a, content_fingerprint(b))

    def test_task_add_remove_detected(self):
        a = content_fingerprint(self._base())
        b = copy.deepcopy(self._base())
        b["tasks"].append({"task_id": "t2", "equity": 1.0})
        self.assertNotEqual(a, content_fingerprint(b))
        c = copy.deepcopy(self._base())
        c["tasks"] = []
        self.assertNotEqual(a, content_fingerprint(c))

    def test_order_change_detected(self):
        a = content_fingerprint(self._base())
        b = copy.deepcopy(self._base())
        b["orders_recent"] = [{"order_id": "o1", "status": "FILLED"}]
        self.assertNotEqual(a, content_fingerprint(b))

    def test_stable_across_calls(self):
        s = self._base()
        self.assertEqual(content_fingerprint(s), content_fingerprint(s))

    def test_serializable_values_ok(self):
        """快照里可能有 datetime / Enum，指纹不能因此崩掉。"""
        s = self._base()
        s["tasks"][0]["dt"] = datetime(2026, 1, 1)
        s["tasks"][0]["side"] = Side.BUY
        self.assertIsInstance(content_fingerprint(s), str)


class TestSnapshotMode(unittest.TestCase):
    """快照策略开关（组件级，不起 reactor）。"""

    def test_modes_defined(self):
        from app.core.monitor.web import MonitorWebComponent

        for m in ("on_event", "on_change", "interval", "off"):
            self.assertIn(m, MonitorWebComponent.SNAPSHOT_MODES)

    def test_set_mode_validates(self):
        from app.core.monitor.web import MonitorWebComponent

        comp = MonitorWebComponent()
        r = comp.set_snapshot_mode("no_such_mode")
        self.assertFalse(r["ok"])

    def test_set_mode_off(self):
        from app.core.monitor.web import MonitorWebComponent

        comp = MonitorWebComponent()
        r = comp.set_snapshot_mode("off")
        self.assertTrue(r["ok"])
        self.assertEqual(comp.snapshot_mode, "off")
        # 关键不是 enabled 变没变，而是**真的不再写盘、也不再跑定时器**
        self.assertIsNone(comp._loop)
        self.assertIsNone(comp.flush())

    def test_set_mode_off_then_on_starts_loop(self):
        """切回 on_change 后定时器要重新起来，否则"改了配置但不生效"。"""
        from app.core.monitor.web import MonitorWebComponent

        comp = MonitorWebComponent()
        comp.set_snapshot_mode("off")
        self.assertIsNone(comp._loop)
        r = comp.set_snapshot_mode("on_change", enabled=True)
        self.assertTrue(r["ok"])
        self.assertIsNotNone(comp._loop)
        if comp._loop is not None and comp._loop.running:
            comp._loop.stop()

    def test_flush_respects_disabled(self):
        from app.core.monitor.web import MonitorWebComponent

        comp = MonitorWebComponent()
        comp.snapshot_mode = "off"
        self.assertIsNone(comp.flush())

    def test_default_mode_is_on_event(self):
        """默认必须是"有操作才写盘"。

        这不是个偏好问题：面板每 2s 轮询一次，如果默认是"到点就写"，
        用户会以为快照在按 2s 刷 —— 而绝大多数时刻状态根本没变，
        那些快照全是同一份内容的副本，白占磁盘还让复盘更难找。
        """
        from app.core.monitor.web import MonitorWebComponent

        comp = MonitorWebComponent()
        self.assertEqual(comp.snapshot_mode, "on_event")

    def test_on_event_mode_has_no_timer(self):
        """on_event 模式不装定时器 —— 装了就会有人以为它还在按时间写。"""
        from app.core.monitor.web import MonitorWebComponent

        comp = MonitorWebComponent()
        comp.set_snapshot_mode("on_event")
        self.assertIsNone(comp._loop)

    def test_mark_dirty_records_reason(self):
        from app.core.monitor.web import MonitorWebComponent

        comp = MonitorWebComponent()
        comp.mark_dirty("order_filled")
        comp.mark_dirty("order_filled")
        comp.mark_dirty("task_added")
        self.assertTrue(comp._dirty)
        self.assertEqual(comp.stats["triggers"], 3)
        self.assertEqual(sorted(set(comp._dirty_reasons)), ["order_filled", "task_added"])

    def test_trigger_events_cover_operations(self):
        """触发事件必须覆盖"操作"，且**不能**包含逐根K线。

        把 BAR_RECEIVED 放进去就等于回到"行情一动就写盘"，
        那正是这次要消除的行为。
        """
        from app.core.monitor.web import MonitorWebComponent
        from app.core.engine.event import StandardEvents

        trig = set(MonitorWebComponent.TRIGGER_EVENTS)
        for ev in (StandardEvents.ORDER_CREATED, StandardEvents.ORDER_FILLED,
                   StandardEvents.ORDER_CANCELLED, StandardEvents.SIGNAL_REJECTED,
                   StandardEvents.TASK_ADDED, StandardEvents.TASK_REMOVED,
                   StandardEvents.TASK_PAUSED, StandardEvents.TASK_RESUMED,
                   StandardEvents.COMPONENT_ENABLED, StandardEvents.CONTROL_COMMAND):
            self.assertIn(ev, trig, ev)
        self.assertNotIn(StandardEvents.BAR_RECEIVED, trig)
        self.assertNotIn(StandardEvents.POSITION_UPDATED, trig)


class TestLogPolicy(unittest.TestCase):
    """日志分级策略。"""

    def test_level_normalization(self):
        from app.utils.logger import level_of

        self.assertEqual(level_of("debug"), "DEBUG")
        self.assertEqual(level_of("nonsense"), "INFO")
        self.assertEqual(level_of("WARNING"), "WARNING")

    def test_throttle_suppresses_repeats(self):
        from app.utils.logger import log_throttled, reset_throttle

        reset_throttle()
        first = log_throttled("WARNING", "重复消息", key="t1", window=30.0)
        second = log_throttled("WARNING", "重复消息", key="t1", window=30.0)
        self.assertTrue(first)
        self.assertFalse(second, "同一窗口内的重复日志应被压掉")

    def test_throttle_zero_window_always_writes(self):
        from app.utils.logger import log_throttled, reset_throttle

        reset_throttle()
        self.assertTrue(log_throttled("INFO", "x", key="z", window=0.0))

    def test_throttle_stats(self):
        from app.utils.logger import log_throttled, reset_throttle, throttle_stats

        reset_throttle()
        log_throttled("INFO", "a", key="k-a", window=30.0)
        log_throttled("INFO", "a", key="k-a", window=30.0)
        st = throttle_stats()
        self.assertGreaterEqual(st["buckets"], 1)


class TestSharedRepository(unittest.TestCase):
    def test_singleton(self):
        self.assertIs(get_repository(), get_repository())


if __name__ == "__main__":
    unittest.main()
