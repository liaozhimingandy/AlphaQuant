#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关注标的（自选股）测试：仓储 / 服务单元 / 面板接口。

三条边界（决定了这些测什么、怎么测）
-----------------------------------
1. **不联网**。涉及真实取数的一律替换成桩 —— 测试要验的是"该发给谁、
   失败了怎么记账"，不是数据源今天的心情。
2. **不起 reactor**。``LoopingCall`` 换成记录参数的假对象：
   我们关心的是"定时器有没有按配置的周期起来"，而不是时间真的流逝。
3. **不污染真实库/配置**。库和配置文件都临时记忆expectedly，用完还原。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import sessionmaker

from app.common.config import settings
from app.data.engine import Base, create_sqlite_engine
from app.data.repo import watchlist_repo as wr


# =============================================================================
# 仓储层
# =============================================================================
class WatchlistRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aq_wl_repo_")
        self.engine = create_sqlite_engine(
            f"sqlite:///{(Path(self.tmp) / 'wl.db').as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine,
                                    autocommit=False, autoflush=False)
        # repo 用的是模块级的 SessionLocal，临时把它指到测试库
        self._orig_session = wr.SessionLocal
        wr.SessionLocal = self.Session
        self.addCleanup(setattr, wr, "SessionLocal", self._orig_session)

    # ---------------- 增删改 ----------------
    def test_add_is_idempotent_and_unique(self):
        wr.add_watch("000001", freq="30m")
        wr.add_watch("000001", freq="2h")
        rows = wr.list_watchlist()
        self.assertEqual(len(rows), 1, "重复添加不能产生第二行")
        # 再次添加等价于更新：频率要跟着改
        self.assertEqual(rows[0]["freq"], "2h")
        self.assertAlmostEqual(rows[0]["freq_seconds"], 7200.0)

    def test_symbol_is_normalized(self):
        wr.add_watch("1", freq="1h")
        rows = wr.list_watchlist()
        self.assertEqual(rows[0]["symbol"], "000001")

    def test_add_requires_symbol(self):
        with self.assertRaises(ValueError):
            wr.add_watch("", freq="1h")

    def test_remove_is_idempotent(self):
        wr.add_watch("600519")
        self.assertTrue(wr.remove_watch("600519"))
        self.assertFalse(wr.remove_watch("600519"), "重复删除应返回 False 而不是报错")

    def test_list_includes_disabled_by_default(self):
        wr.add_watch("000001")
        wr.add_watch("600519")
        wr.set_watch_enabled("600519", False)
        self.assertEqual(len(wr.list_watchlist()), 2)
        self.assertEqual(len(wr.list_watchlist(enabled_only=True)), 1)

    # ---------------- 频率必须写得进去就是合法的 ----------------
    def test_illegal_freq_falls_back_entirely(self):
        """写错频率不能出现"界面显示 bad、实际按 1h 跑"的错配。"""
        for bad in ("1x", "bad", "-5", "0", ""):
            text, seconds = wr._parse_freq(bad)
            self.assertEqual(text, wr.DEFAULT_FREQ,
                             f"{bad!r} 应整体回退到 {wr.DEFAULT_FREQ}")
            self.assertEqual(seconds, wr.DEFAULT_FREQ_SECONDS)

    def test_valid_freq_keeps_user_text(self):
        self.assertEqual(wr._parse_freq("30m"), ("30m", 1800.0))
        self.assertEqual(wr._parse_freq("2h"), ("2h", 7200.0))
        self.assertEqual(wr._parse_freq("600"), ("600", 600.0))

    def test_add_with_illegal_freq_stores_default(self):
        item = wr.add_watch("000858", freq="1x")
        self.assertEqual(item["freq"], "1h")
        self.assertEqual(item["freq_seconds"], 3600.0)

    # ---------------- 到期判定 ----------------
    def test_never_collected_is_due_immediately(self):
        """刚加进来的票该马上能采，用户不该等第一个周期过去。"""
        wr.add_watch("000001", freq="1h")
        due = wr.due_watchlist()
        self.assertEqual([d["symbol"] for d in due], ["000001"])

    def test_success_clears_due(self):
        wr.add_watch("000001", freq="1h")
        wr.mark_watch_result("000001", ok=True)
        self.assertEqual(wr.due_watchlist(), [], "采成功后短期内不该再到期")

    def test_failure_schedules_an_earlier_retry_than_the_freq(self):
        """失败的票不该让用户干等整整一个频率周期。"""
        wr.add_watch("000001", freq="1h")
        wr.mark_watch_result("000001", ok=True)
        self.assertEqual(wr.due_watchlist(), [])
        wr.mark_watch_result("000001", ok=False, error="boom")

        # 刚失败的当下不重试（否则就成了"追着失败打"）
        self.assertEqual(wr.due_watchlist(), [])
        # 但重试点比正常频率（1h）早得多 —— 上限 300s
        due = wr.due_watchlist(now=datetime.now() + timedelta(seconds=301))
        self.assertEqual([d["symbol"] for d in due], ["000001"])
        self.assertEqual(due[0]["last_error"], "boom")
        self.assertEqual(due[0]["errors"], 1)

    def test_failure_retry_is_capped_at_300s(self):
        wr.add_watch("000001", freq="1d")          # 86400s
        wr.mark_watch_result("000001", ok=True)
        wr.mark_watch_result("000001", ok=False, error="x")
        item = wr.get_watch("000001")
        gap = (datetime.fromisoformat(item["next_retry_at"]) -
               datetime.now()).total_seconds()
        self.assertLessEqual(gap, 300.0, "退避必须有上限，不能照搬原频率")

    def test_success_cancels_pending_retry(self):
        wr.add_watch("000001", freq="1h")
        wr.mark_watch_result("000001", ok=False, error="x")
        self.assertIsNotNone(wr.get_watch("000001")["next_retry_at"])
        wr.mark_watch_result("000001", ok=True)
        self.assertIsNone(wr.get_watch("000001")["next_retry_at"])

    def test_mark_success_increments_runs_and_clears_error(self):
        wr.add_watch("000001")
        wr.mark_watch_result("000001", ok=False, error="x")
        wr.mark_watch_result("000001", ok=True)
        item = wr.get_watch("000001")
        self.assertEqual(item["runs"], 1)
        self.assertEqual(item["errors"], 1)
        self.assertIsNone(item["last_error"])
        self.assertIsNotNone(item["last_collected_at"])

    def test_disabled_is_never_due(self):
        wr.add_watch("000001")
        wr.set_watch_enabled("000001", False)
        self.assertEqual(wr.due_watchlist(), [])

    def test_freq_change_moves_the_due_window(self):
        wr.add_watch("000001", freq="1h")
        wr.mark_watch_result("000001", ok=True)
        self.assertEqual(wr.due_watchlist(), [])
        wr.set_watch_freq("000001", "1s")
        # 时间推进一点点再来问：刚 mark 成功的那一刻 elapsed 可能还不到 1 秒
        soon = datetime.now() + timedelta(seconds=2)
        self.assertTrue(wr.due_watchlist(now=soon), "改成 1s 之后必须立刻到期")
        wr.set_watch_freq("000001", "4h")
        self.assertEqual(wr.due_watchlist(), [])
        future = datetime.now() + timedelta(hours=5)
        self.assertEqual([d["symbol"] for d in wr.due_watchlist(now=future)],
                         ["000001"])

    def test_due_limit(self):
        for i in range(5):
            wr.add_watch(f"00000{i}", freq="1h")
        self.assertEqual(len(wr.due_watchlist(limit=2)), 2)

    def test_corrupted_freq_seconds_does_not_cause_hot_loop(self):
        """库里 seconds 被写坏（0）也不该变成"每轮必采"。"""
        wr.add_watch("000001", freq="1h")
        with self.Session() as db:
            from app.data.schema import Watchlist

            row = db.query(Watchlist).filter(Watchlist.symbol == "000001").first()
            row.freq_seconds = 0
            row.enabled = True
            db.commit()
        wr.mark_watch_result("000001", ok=True)
        self.assertEqual(wr.due_watchlist(), [],
                         "freq_seconds<=0 必须按默认频率处理，不能每轮都采")

    # ---------------- 其它字段 ----------------
    def test_categories_roundtrip(self):
        wr.add_watch("000001", categories=["lhb", "margin"])
        self.assertEqual(wr.get_watch("000001")["categories"],
                         ["lhb", "margin"])
        wr.set_watch_categories("000001", [])
        self.assertEqual(wr.get_watch("000001")["categories"], [])
        wr.set_watch_categories("000001", None)
        self.assertEqual(wr.get_watch("000001")["categories"], [])

    def test_setters_on_missing_symbol_raise_keyerror(self):
        for fn in (wr.set_watch_freq, wr.set_watch_categories):
            with self.assertRaises(KeyError):
                fn("999999", "1h")
        with self.assertRaises(KeyError):
            wr.set_watch_enabled("999999", True)

    def test_init_from_config_only_fills_gaps(self):
        """用户在面板改过的频率，重启不该被配置冲掉。"""
        wr.add_watch("000001", freq="5m")
        added = wr.init_watchlist_from(["000001", "600519"], freq="1h")
        self.assertEqual(added, 1, "只应补缺，不该重算已有的")
        self.assertEqual(wr.get_watch("000001")["freq"], "5m")
        self.assertEqual(wr.get_watch("600519")["freq"], "1h")


# =============================================================================
# 服务单元
# =============================================================================
class _FakeLoop:
    """``LoopingCall`` 的替身：记录参数、可控生命周期，不碰 reactor。"""

    def __init__(self, fn):
        self.fn = fn
        self.interval: Optional[float] = None
        self.now: Optional[bool] = None
        self.running = False

    def start(self, interval: float, now: bool = False) -> None:
        self.interval = interval
        self.now = now
        self.running = True

    def stop(self) -> None:
        self.running = False


class WatchlistServiceTestCase(unittest.TestCase):
    def setUp(self):
        import app.modules.collection.service as svc_mod

        self.svc_mod = svc_mod
        self._orig_loop = svc_mod.task.LoopingCall
        svc_mod.task.LoopingCall = _FakeLoop
        self.addCleanup(setattr, svc_mod.task, "LoopingCall", self._orig_loop)

        tmp = tempfile.mkdtemp(prefix="aq_wl_svc_")
        self.engine = create_sqlite_engine(
            f"sqlite:///{(Path(tmp) / 'wl.db').as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        Session = sessionmaker(bind=self.engine,
                               autocommit=False, autoflush=False)
        self._orig_session = wr.SessionLocal
        wr.SessionLocal = Session
        self.addCleanup(setattr, wr, "SessionLocal", self._orig_session)

        self._orig_collect = svc_mod.collect_due_watchlist
        self.addCleanup(setattr, svc_mod, "collect_due_watchlist",
                        self._orig_collect)
        self.collected: Dict[str, Any] = {}

        def _fake_collect(config=None, now=None, limit=0, symbols=None):
            self.collected = {"limit": limit, "symbols": symbols,
                              "now": now}
            return {"ok": True, "due": 1, "symbols": ["000001"],
                    "ok_symbols": ["000001"], "failed_symbols": [],
                    "groups": [], "error": ""}
        self.fake_collect = _fake_collect
        svc_mod.collect_due_watchlist = _fake_collect

        self.svc = svc_mod.CollectionService()

    # ---------------- 能力声明 ----------------
    def test_declares_watchlist_capability(self):
        from app.engine.capability import Cap

        self.assertIn(Cap.WATCHLIST_COLLECT, self.svc.provides)

    # ---------------- 生命周期 ----------------
    def test_start_uses_sweep_interval_and_not_immediate(self):
        self.svc._watchlist_cfg.enabled = True
        self.svc._start_watchlist()
        loop = self.svc._watchlist_loop
        self.assertIsInstance(loop, _FakeLoop)
        self.assertTrue(loop.running)
        self.assertEqual(loop.interval, self.svc._watchlist_cfg.sweep_seconds)
        self.assertFalse(loop.now, "启动瞬间不该立刻抢一轮")

    def test_start_respects_disabled(self):
        self.svc._watchlist_cfg.enabled = False
        self.svc._start_watchlist()
        self.assertIsNone(self.svc._watchlist_loop)

    def test_stop_resets_loop_and_busy_flag(self):
        self.svc._watchlist_cfg.enabled = True
        self.svc._start_watchlist()
        self.svc._busy_watchlist = True
        self.svc._stop_watchlist()
        self.assertIsNone(self.svc._watchlist_loop)
        self.assertFalse(self.svc._busy_watchlist)
        self.assertFalse(self.svc.is_busy())

    # ---------------- tick ----------------
    def test_tick_does_nothing_when_nothing_due(self):
        self.svc._tick_watchlist()
        self.assertEqual(self.svc.stats_watchlist["skipped_no_due"], 1)
        self.assertFalse(self.svc._busy_watchlist)
        self.assertFalse(self.collected, "没到期就不该发起任何采集")

    def test_tick_dispatches_when_due(self):
        from twisted.internet import defer

        wr.add_watch("000001", freq="1h")
        result = self.svc._tick_watchlist()
        self.assertIsInstance(result, defer.Deferred)
        self.assertTrue(self.svc._busy_watchlist, "跑起来就该标记为忙")

    def test_tick_skips_while_busy(self):
        wr.add_watch("000001", freq="1h")
        self.svc._busy_watchlist = True
        self.svc._tick_watchlist()
        self.assertFalse(self.collected, "上一轮没跑完不能再叠一轮")

    # ---------------- 统计与失败记账 ----------------
    def test_run_once_records_stats(self):
        report = self.svc._run_watchlist_once()
        st = self.svc.stats_watchlist
        self.assertEqual(st["runs"], 1)
        self.assertEqual(st["collected"], 1)
        self.assertEqual(st["failed"], 0)
        self.assertEqual(st["last_symbols"], ["000001"])
        self.assertIsNotNone(st["last_run_at"])
        self.assertTrue(report["ok"])

    def test_release_counts_failure_and_exception(self):
        from twisted.python import failure

        self.svc._busy_watchlist = True
        self.svc._release_watchlist(failure.Failure(RuntimeError("boom")))
        self.svc._busy_watchlist = True
        self.svc._release_watchlist(ValueError("bad"))
        self.assertEqual(self.svc.stats_watchlist["errors"], 2)
        self.assertFalse(self.svc._busy_watchlist)

    def test_collect_now_passes_selected_symbols(self):
        wr.add_watch("000001", freq="1h")
        wr.add_watch("600519", freq="1h")
        self.svc.collect_watchlist_now(["600519"])
        self.assertEqual(self.collected["symbols"], ["600519"])
        self.assertEqual(self.collected["limit"], 1)

    def test_collect_now_on_unknown_symbol_is_an_error(self):
        """明确点名却找不到，必须报错，而不是返回"成功采了 0 只"。"""
        wr.add_watch("000001", freq="1h")
        result = self.svc.collect_watchlist_now(["999999"])
        self.assertFalse(result["ok"])
        self.assertIn("不在关注列表里", result["error"])
        self.assertFalse(self.collected, "不在关注列表里就不该发起请求")

    def test_collect_now_on_empty_list_is_not_an_error(self):
        result = self.svc.collect_watchlist_now()
        self.assertTrue(result["ok"], "清单本来就是空的不算失败")
        self.assertFalse(self.collected)

    # ---------------- 可观测 ----------------
    def test_snapshot_and_health(self):
        snap = self.svc.watchlist_status()
        for key in ("enabled", "config", "busy", "running", "total",
                    "enabled_count", "due_count", "due_symbols", "stats"):
            self.assertIn(key, snap)
        ok, detail = self.svc._health_check_watchlist()
        self.assertTrue(ok)
        self.assertIn("只在采", detail)

    def test_health_red_after_repeated_failures(self):
        self.svc.stats_watchlist["errors"] = 6
        self.svc.stats_watchlist["collected"] = 0
        ok, _ = self.svc._health_check_watchlist()
        self.assertFalse(ok)

    def test_snapshot_counts_due_symbols(self):
        wr.add_watch("000001", freq="1h")
        snap = self.svc.watchlist_status()
        self.assertEqual(snap["due_symbols"], ["000001"])
        self.assertEqual(snap["enabled_count"], 1)


# =============================================================================
# 面板接口
# =============================================================================
class _FakeComponent:
    """只带上视图 mixin 的最小替身，把 control/engine 全部剥掉。"""

    def __init__(self):
        from app.monitoring.api.collect import CollectViewsMixin

        class _Mixed(CollectViewsMixin):
            pass

        self._x = _Mixed()
        # mixin 里写的是 ``getattr(self, "audit", None)``，那个 ``self`` 就是
        # _Mixed 实例 —— 所以审计列表必须挂在它身上，外层再持有同一份引用。
        self._x.audit: List[Dict[str, Any]] = []
        self.audit = self._x.audit

    def __getattr__(self, name):
        return getattr(self._x, name)


def _mk_resource(component) -> Any:
    from twisted.web.test.requesthelper import DummyRequest

    from app.monitoring.resource import MonitorResource

    res = MonitorResource(component)

    def get(path, **qs):
        segs = [p for p in path.strip("/").split("/")]
        req = DummyRequest([p.encode() for p in segs])
        req.path = ("/" + "/".join(segs)).encode()
        req.args = {k.encode(): [str(v).encode()] for k, v in qs.items()}
        return json.loads(res.render_GET(req).decode("utf-8"))

    def post(path, payload):
        segs = [p for p in path.strip("/").split("/")]
        req = DummyRequest([p.encode() for p in segs])
        req.path = ("/" + "/".join(segs)).encode()
        req.content = io.BytesIO(json.dumps(payload).encode("utf-8"))
        return json.loads(res.render_POST(req).decode("utf-8"))

    return get, post


# =============================================================================
# 执行编排（collect_due_watchlist）
# =============================================================================
class WatchlistOrchestrationTestCase(unittest.TestCase):
    """编排层：谁该采、按什么分组、成败落到哪一只票上。"""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="aq_wl_orc_")
        self.engine = create_sqlite_engine(
            f"sqlite:///{(Path(tmp) / 'wl.db').as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        Session = sessionmaker(bind=self.engine,
                               autocommit=False, autoflush=False)
        self._orig_session = wr.SessionLocal
        wr.SessionLocal = Session
        self.addCleanup(setattr, wr, "SessionLocal", self._orig_session)

        import app.modules.collection.service as svc_mod
        import app.modules.collection.watchlist as wl_mod

        self.wl_mod = wl_mod
        self._orig_rfc = svc_mod.run_full_collection
        self.addCleanup(setattr, svc_mod, "run_full_collection",
                        self._orig_rfc)
        self.calls: List[Dict[str, Any]] = []

    def _install(self, report=None, exc=None, detail=None):
        def _fake(syms, cats):
            self.calls.append({"symbols": list(syms), "categories": cats})
            if exc is not None:
                raise exc
            return report if report is not None else {
                "ok": True, "detail": detail or {}}
        import app.modules.collection.service as svc_mod

        svc_mod.run_full_collection = _fake

    # ---------------- 分组 ----------------
    def test_nothing_due_collects_nothing(self):
        out = self.wl_mod.collect_due_watchlist()
        self.assertEqual(out["due"], 0)
        self.assertFalse(self.calls)

    def test_same_categories_are_batched_into_one_request(self):
        wr.add_watch("000001", freq="1h", categories=["lhb"])
        wr.add_watch("600519", freq="1h", categories=["lhb"])
        self._install()
        out = self.wl_mod.collect_due_watchlist()
        self.assertEqual(out["due"], 2)
        self.assertEqual(len(self.calls), 1, "同类该合并成一次请求，别每只票各打一轮")
        self.assertEqual(sorted(self.calls[0]["symbols"]), ["000001", "600519"])

    def test_different_categories_are_split(self):
        wr.add_watch("000001", freq="1h", categories=["lhb"])
        wr.add_watch("600519", freq="1h", categories=["margin"])
        self._install()
        self.wl_mod.collect_due_watchlist()
        self.assertEqual(len(self.calls), 2)

    def test_max_per_round_caps_batch_size(self):
        for i in range(5):
            wr.add_watch(f"00000{i}", freq="1h")
        cfg = self.wl_mod.WatchlistConfig(max_per_round=2)
        self._install()
        out = self.wl_mod.collect_due_watchlist(config=cfg)
        self.assertEqual(out["due"], 2, "单轮上限必须生效 —— 那是限流护栏")

    def test_symbols_filter_narrows_the_batch(self):
        wr.add_watch("000001", freq="1h")
        wr.add_watch("600519", freq="1h")
        self._install()
        out = self.wl_mod.collect_due_watchlist(symbols=["600519"])
        self.assertEqual(out["symbols"], ["600519"])

    # ---------------- 成败要落到每一只票上 ----------------
    def test_partial_failure_marks_only_the_failed_symbol(self):
        """一轮里 5 只成功、1 只失败，不该把那一只也记成"采过了"。"""
        wr.add_watch("000001", freq="1h")
        wr.add_watch("600519", freq="1h")
        self._install(detail={
            "fundflow": {"ok": 1, "failed": ["600519"]},
            "daily": {"ok": 2, "rows": 10},
        })
        out = self.wl_mod.collect_due_watchlist()
        self.assertEqual(sorted(out["ok_symbols"]), ["000001"])
        self.assertEqual(sorted(out["failed_symbols"]), ["600519"])
        self.assertFalse(out["ok"])
        # 成功的记了时间戳 → 短期内不再到期；失败的记了 error，仍会重试
        self.assertIsNotNone(wr.get_watch("000001")["last_collected_at"])
        self.assertIsNone(wr.get_watch("600519")["last_collected_at"])
        self.assertIsNotNone(wr.get_watch("600519")["next_retry_at"])

    def test_hard_error_fails_the_whole_group(self):
        wr.add_watch("000001", freq="1h")
        self._install(detail={"daily": {"ok": 0, "error": "磁盘满"}})
        out = self.wl_mod.collect_due_watchlist()
        self.assertEqual(out["failed_symbols"], ["000001"])
        self.assertEqual(wr.get_watch("000001")["last_error"], "daily: 磁盘满")

    # ---------------- 存储故障必须上浮 ----------------
    def test_storage_error_is_raised_not_swallowed(self):
        """后端挂了重试也是白试，必须让上层知道，而不是假装时间到了该下次了。"""
        from app.common.errors import StorageError

        wr.add_watch("000001", freq="1h")
        self._install(exc=StorageError("too many SQL variables"))
        with self.assertRaises(StorageError):
            self.wl_mod.collect_due_watchlist()

    def test_ordinary_error_is_recorded_per_symbol(self):
        wr.add_watch("000001", freq="1h")
        self._install(exc=RuntimeError("接口抽风"))
        out = self.wl_mod.collect_due_watchlist()
        self.assertEqual(out["failed_symbols"], ["000001"])
        self.assertIn("接口抽风", wr.get_watch("000001")["last_error"])


class WatchlistApiTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="aq_wl_api_")
        self.engine = create_sqlite_engine(
            f"sqlite:///{(Path(tmp) / 'wl.db').as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        Session = sessionmaker(bind=self.engine,
                               autocommit=False, autoflush=False)
        self._orig_session = wr.SessionLocal
        wr.SessionLocal = Session
        self.addCleanup(setattr, wr, "SessionLocal", self._orig_session)

        # 默认频率会被落成文件，测试里重定向到临时配置避免动真配置文件
        self._orig_cfg = settings.COLLECTOR_CONFIG
        self.cfg_file = Path(tmp) / "collector.json"
        settings.COLLECTOR_CONFIG = str(self.cfg_file)
        self.addCleanup(setattr, settings, "COLLECTOR_CONFIG", self._orig_cfg)

        import app.modules.collection.watchlist as wl_mod

        self.wl_mod = wl_mod
        wl_mod._CONFIG = None        # 清缓存，让它读上面的临时路径

        self.comp = _FakeComponent()
        self.get, self.post = _mk_resource(self.comp)

    # ---------------- GET ----------------
    def test_get_exposes_items_config_and_defaults(self):
        data = self.get("/api/watchlist")
        self.assertIn("items", data)
        self.assertIn("config", data)
        self.assertIn("default_categories", data)
        self.assertIn("service", data)
        self.assertEqual(data["total"], 0)

    def test_items_carry_due_information(self):
        self.post("/api/watchlist/add", {"symbol": "600519", "freq": "30m"})
        data = self.get("/api/watchlist")
        item = data["items"][0]
        self.assertTrue(item["due"])
        self.assertEqual(item["freq_seconds"], 1800.0)

    # ---------------- POST ----------------
    def test_add_then_appears_in_list(self):
        r = self.post("/api/watchlist/add", {"symbol": "600519"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["item"]["freq"], "1h", "没给频率就用配置默认值")
        self.assertEqual(self.get("/api/watchlist")["total"], 1)

    def test_add_requires_symbol(self):
        r = self.post("/api/watchlist/add", {})
        self.assertFalse(r["ok"])

    def test_set_freq_and_enabled(self):
        self.post("/api/watchlist/add", {"symbol": "000001", "freq": "1h"})
        r = self.post("/api/watchlist/freq", {"symbol": "000001", "freq": "15m"})
        self.assertEqual(r["item"]["freq_seconds"], 900.0)
        off = self.post("/api/watchlist/enable",
                        {"symbol": "000001", "enabled": False})
        self.assertFalse(off["item"]["enabled"])
        self.assertEqual(self.get("/api/watchlist")["due_count"], 0)

    def test_error_message_has_no_extra_quotes(self):
        r = self.post("/api/watchlist/freq", {"symbol": "999999", "freq": "1h"})
        self.assertFalse(r["ok"])
        self.assertFalse(r["error"].startswith("'"),
                         "KeyError 的引号不该出现在给用户的消息里")

    def test_remove(self):
        self.post("/api/watchlist/add", {"symbol": "600519"})
        self.assertTrue(self.post("/api/watchlist/remove",
                                  {"symbol": "600519"})["ok"])
        self.assertEqual(self.get("/api/watchlist")["total"], 0)

    def test_unknown_operation_returns_400_style(self):
        r = self.post("/api/watchlist/zzz", {})
        self.assertFalse(r["ok"])
        self.assertIn("未知操作", r["error"])

    def test_every_mutation_is_audited(self):
        """改了"系统接下来会自动做什么"必须留痕。"""
        self.post("/api/watchlist/add", {"symbol": "600519"})
        self.post("/api/watchlist/freq", {"symbol": "600519", "freq": "2h"})
        self.post("/api/watchlist/remove", {"symbol": "600519"})
        actions = [a["action"] for a in self.comp.audit]
        self.assertIn("watchlist_add", actions)
        self.assertIn("watchlist_freq", actions)
        self.assertIn("watchlist_remove", actions)

    def test_default_freq_persists_to_config_file(self):
        r = self.post("/api/watchlist/default_freq", {"freq": "2h"})
        self.assertTrue(r["ok"])
        saved = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["watchlist"]["interval"], "2h",
                         "默认频率必须落盘，否则重启就悄悄回去了")

    def test_collect_without_service_does_not_fake_success(self):
        r = self.comp.watchlist_collect()
        self.assertFalse(r["ok"])
        self.assertIn("采集服务未装配", r["error"])


if __name__ == "__main__":
    unittest.main()
