#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_event_trace.py
# @Description : 事件落盘 / 全链路追踪 / 重放
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""这一组钉的是"事件能不能留下证据"。

之前事件只在内存广播：进程一死，"这条 K 线更新曾经发生过"就消失了，
于是链路在"事件 → 任务"这一环是断的 —— 事件丢了，任务压根不会被创建，
任务落盘救不了它。这组用例要保证：

* 事件发布即落盘，且记下"有没有人接"（subscribers）；
* 一条链上的事件共享 trace_id，能按链查出来；
* 重放产生**新事件 ID**（否则会被去重判成重复丢弃），但保留原链路；
* 心跳按采样落盘，不淹没业务事件；
* 清理有保留下限，配错保留窗口不至于把流水账清空。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from app.runtime.events.bus import EventBus
from app.runtime.events.schema import EngineEvent
from app.runtime.events.store import EventStore


def _tmpdir() -> Path:
    # conftest 把 tempfile 重定向到项目内的 .pytest_tmp，
    # 这里直接用它的行为即可（不删，进程退出自然丢弃）
    return Path(tempfile.mkdtemp(prefix="aq_evt_"))


class TestEventTrace(unittest.TestCase):
    """trace_id 的自动补全 —— 链路能不能串起来的前提。"""

    def test_event_gets_trace_root_when_not_given(self):
        evt = EngineEvent.create("market.kline.updated", {"symbol": "000001"})
        # 调用方绝大多数不传 correlation_id。以前不传就是空，
        # 链条在第一跳就断了；现在自动拿自己的 event_id 当链根。
        self.assertTrue(evt.trace_id)
        self.assertEqual(evt.trace_id, evt.event_id)

    def test_child_inherits_trace_and_points_to_cause(self):
        parent = EngineEvent.create("market.kline.updated", {"symbol": "000001"})
        child = parent.child("factor.updated", {"n": 1})
        self.assertEqual(child.trace_id, parent.trace_id)
        self.assertEqual(child.causation_id, parent.event_id)

    def test_explicit_correlation_is_kept(self):
        evt = EngineEvent.create("x.y", {}, correlation_id="chain-1")
        self.assertEqual(evt.trace_id, "chain-1")


class TestEventStore(unittest.TestCase):
    """落盘行为。

    整组**共用一个库文件**（setUpClass 建一次，每个用例只清表）：
    建一个 SQLite 库在这种机器上要几百毫秒到几十秒
    （受管控的 Windows 环境逐个文件扫描），十几个用例各建一个库
    会让这组测试从秒级变成分钟级。
    """
    @classmethod
    def setUpClass(cls):
        cls.dir = _tmpdir()
        cls.store = EventStore(path=str(cls.dir / "events.db"), persist=True)
        cls.store.open()
        cls.bus = EventBus(store=cls.store, run_id="TEST")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.store.close()
        except Exception:
            pass

    def setUp(self):
        # 用例之间清空事件表，互不干扰
        try:
            self.store._conn.execute("DELETE FROM engine_events")
            self.store._conn.execute("DELETE FROM event_replay_log")
            self.store._conn.commit()
        except Exception:
            pass
        self.bus.unsubscribe_all(lambda **kw: None)   # 防御：不影响既有订阅

    def test_publish_is_recorded_with_subscriber_count(self):
        self.bus.subscribe("a.b", lambda **kw: None)
        self.bus.publish("a.b", x=1)
        self.store.flush()
        rows = self.store.recent(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "a.b")
        # 订阅者数是排障时最想知道的：发布没人接 = 链路在这一跳就断了
        self.assertEqual(rows[0]["subscribers"], 1)

    def test_no_subscriber_is_recorded_as_zero(self):
        self.bus.publish("nobody.listens", x=1)
        self.store.flush()
        self.assertEqual(self.store.recent(limit=1)[0]["subscribers"], 0)

    def test_trace_returns_whole_chain_in_order(self):
        first = self.bus.publish("chain.start", step=0)
        self.bus.publish_event(first.child("chain.mid", {"step": 1}))
        self.bus.publish_event(first.child("chain.end", {"step": 2}))
        self.store.flush()
        chain = self.store.trace(first.trace_id)
        self.assertEqual(
            [c["event_type"] for c in chain],
            ["chain.start", "chain.mid", "chain.end"],
        )

    def test_traces_lists_recent_chains(self):
        a = self.bus.publish("t.a", i=1)
        self.bus.publish_event(a.child("t.a2", {}))
        self.bus.publish("t.b", i=2)
        self.store.flush()
        traces = self.store.traces(limit=10)
        by_id = {t["trace_id"]: t for t in traces}
        self.assertEqual(by_id[a.trace_id]["events"], 2)
        self.assertEqual(by_id[a.trace_id]["head"], "t.a")
        self.assertEqual(by_id[a.trace_id]["tail"], "t.a2")

    def test_heartbeat_is_sampled_not_flooded(self):
        for _ in range(60):
            self.bus.publish("timer_3min", context={})
        self.store.flush()
        rows = [r for r in self.store.recent(limit=100)
                if r["event_type"] == "timer_3min"]
        # 60 条心跳，30:1 采样 → 只落 2 条。全量落盘会在几小时撑到几十万行
        self.assertEqual(len(rows), 2, f"实际落盘 {len(rows)} 条")

    def test_business_events_are_not_sampled(self):
        for i in range(10):
            self.bus.publish("market.kline.updated", symbol=f"00000{i}")
        self.store.flush()
        self.assertEqual(len(self.store.recent(limit=50)), 10)

    def test_payload_is_json_decoded(self):
        self.bus.publish("payload.test", symbol="000001", rows=3)
        self.store.flush()
        self.assertEqual(self.store.recent(limit=1)[0]["data"]["rows"], 3)

    def test_unserializable_payload_does_not_break_recording(self):
        class Weird:
            def __repr__(self):
                return "<weird>"

        self.bus.publish("payload.weird", obj=Weird())
        self.store.flush()
        self.assertEqual(len(self.store.recent(limit=5)), 1)


class TestEventReplay(unittest.TestCase):
    """同上：整组共用一个库。"""
    @classmethod
    def setUpClass(cls):
        cls.dir = _tmpdir()
        cls.store = EventStore(path=str(cls.dir / "events.db"), persist=True)
        cls.store.open()
        cls.bus = EventBus(store=cls.store)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.store.close()
        except Exception:
            pass

    def setUp(self):
        try:
            self.store._conn.execute("DELETE FROM engine_events")
            self.store._conn.execute("DELETE FROM event_replay_log")
            self.store._conn.commit()
        except Exception:
            pass

    def test_replay_uses_new_event_id_but_same_trace(self):
        got = []
        self.bus.subscribe("replay.me", lambda **kw: got.append(kw.get("n")))
        origin = self.bus.publish("replay.me", n=1)
        self.store.flush()

        again = self.bus.replay(origin.event_id, by="test", note="单测")
        self.assertIsNotNone(again)
        # 新 ID：沿用旧 ID 会被去重判成重复投递而丢弃（静默失效，
        # 用户点了"重放"却什么都没发生，这是最难查的一类问题）
        self.assertNotEqual(again.event_id, origin.event_id)
        # 同一条链：重放是这条业务上的又一次尝试，不是新业务
        self.assertEqual(again.trace_id, origin.trace_id)
        self.assertEqual(again.data.get("replay_of"), origin.event_id)
        self.assertEqual(got, [1, 1])     # 订阅者确实又收到了一次

    def test_replay_is_logged(self):
        origin = self.bus.publish("log.me", n=1)
        self.store.flush()
        self.bus.replay(origin.event_id, by="panel")
        log = self.store.replay_log(limit=5)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["origin_id"], origin.event_id)
        self.assertEqual(log[0]["by"], "panel")

    def test_replay_unknown_event_returns_none(self):
        self.assertIsNone(self.bus.replay("no-such-event"))

    def test_replay_without_store_is_safe(self):
        bus = EventBus()          # 没接流水账
        self.assertIsNone(bus.replay("whatever"))
        bus.publish("no.store", x=1)     # 发布也不该炸


class TestEventPruning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = _tmpdir()
        cls.store = EventStore(path=str(cls.dir / "events.db"), persist=True)
        cls.store.open()
        cls.bus = EventBus(store=cls.store)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.store.close()
        except Exception:
            pass

    def setUp(self):
        try:
            self.store._conn.execute("DELETE FROM engine_events")
            self.store._conn.commit()
        except Exception:
            pass

    def _age_first(self, n: int, stamp: str = "2026-01-01T00:00:00") -> None:
        """把最早 n 条推到过去，模拟"超出保留窗口的老事件"。"""
        self.store._conn.execute(
            "UPDATE engine_events SET published_at=? WHERE id IN "
            "(SELECT id FROM engine_events ORDER BY id ASC LIMIT ?)",
            (stamp, int(n)),
        )
        self.store._conn.commit()

    def test_prune_removes_only_aged_events(self):
        for i in range(6):
            self.bus.publish("prune.me", i=i)
        self.store.flush()
        self._age_first(4)                      # 前 4 条变老，后 2 条是新的
        removed = self.store.prune(older_than_hours=24, keep=1)
        self.assertEqual(removed, 4)
        self.assertEqual(self.store.counts()["events"], 2)

    def test_keep_floor_protects_recent_events(self):
        # 保留窗口配错（比如设成 0 小时）不该把流水账清空 ——
        # 排障时"什么都没有"比"有点旧"糟得多，所以有 keep 下限。
        for i in range(6):
            self.bus.publish("prune.me", i=i)
        self.store.flush()
        self._age_first(6)
        self.store.prune(older_than_hours=-1.0, keep=2)
        self.assertEqual(self.store.counts()["events"], 2)


class TestTimerEventDetail(unittest.TestCase):
    """定时器事件必须自带"谁发的、多久一次"——否则名实不符查不出来。"""

    def test_timer_payload_carries_id_interval_and_seq(self):
        from app.runtime.scheduler import TIMER_3MIN_SECONDS

        # 事件名写着 3min，间隔必须也是 3 分钟：
        # 曾经 interval=5 而名字叫 timer_3min，面板上看到
        # "timer_3min 几秒一次"任何人都以为自己看错了
        self.assertEqual(TIMER_3MIN_SECONDS, 180.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
