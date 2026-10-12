#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_task_persistence.py
# @Description : 任务落盘 / 崩溃恢复 / 错误队列人工干预 / 启动预检
#
#                这组用例钉的不是"某个函数返回什么"，而是四类
#                **看不见就容易烂掉**的能力：
#
#                  1. 任务必须真的在盘上（否则重启 = 集体失忆）
#                  2. 恢复必须分得清"没跑过"和"跑了一半"、
#                     "该重跑"和"不该再跑"
#                  3. 死信必须跨重启存在，且能被人工处置
#                  4. 启动预检必须能在启动那一刻说出"缺什么"
#
#                它们共同的失败形态是安静的：不报错、不告警，
#                只是"该做的事从来没做过"。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.runtime.tasking.journal import TaskJournal
from app.runtime.tasking.models import EngineTask, TaskState
from app.runtime.tasking.store import TaskStore


def _journal() -> TaskJournal:
    d = tempfile.mkdtemp(prefix="journal_")
    return TaskJournal(path=os.path.join(d, "tasks.db"), flush_interval=0.2)


def _store(journal: TaskJournal | None = None) -> TaskStore:
    return TaskStore(journal=journal if journal is not None else _journal())


class JournalBasics(unittest.TestCase):
    """落盘本身。"""

    def setUp(self) -> None:
        self.j = _journal()
        self.addCleanup(self.j.close)

    def test_task_is_on_disk_right_after_enqueue(self):
        """入队那一刻就必须在盘上 —— 不等后台 flush。

        这是整组测试里最要紧的一条：等到后台线程再写，
        进程在这 1 秒里被杀，任务就从来没存在过。
        """
        s = _store(self.j)
        t = EngineTask(task_id="e1", task_type="news.collect", queue="research")
        s.enqueue(t)
        rows = self.j.load_open()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "e1")
        self.assertEqual(rows[0]["status"], "queued")

    def test_running_is_on_disk(self):
        """RUNNING 也要落盘：它的下一次更新可能永远不会来。"""
        s = _store(self.j)
        t = EngineTask(task_id="r1", task_type="factor.calculate", queue="research")
        s.enqueue(t)
        s.mark_running(t, "research")
        rows = self.j.load_open()
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(rows[0]["worker"], "research")

    def test_terminal_task_no_longer_open(self):
        s = _store(self.j)
        t = EngineTask(task_id="t1", task_type="news.collect", queue="research")
        s.enqueue(t)
        s.mark_running(t, "research")
        s.mark_success(t, {"ok": True})
        self.assertEqual(self.j.load_open(), [])

    def test_ephemeral_task_is_never_written(self):
        """一次性函数型任务不留痕。

        它的参数里带着 callable，序列化不出来；硬记只会得到一条
        恢复后必然失败的记录，反而污染错误队列。
        """
        s = _store(self.j)
        t = EngineTask(
            task_id="x1", task_type="legacy.job",
            params={"handler": lambda: None}, ephemeral=True,
        )
        s.enqueue(t)
        self.assertEqual(self.j.load_open(), [])

    def test_params_round_trip(self):
        s = _store(self.j)
        params = {"symbols": ["000001", "600519"], "day": "2026-10-13", "n": 3}
        t = EngineTask(task_id="p1", task_type="factor.calculate",
                       params=params, queue="research")
        s.enqueue(t)
        self.j.flush()
        rows = self.j.load_open()
        self.assertEqual(rows[0]["params"], params)


class Recovery(unittest.TestCase):
    """重启之后"该干什么"。"""

    def setUp(self) -> None:
        self.j = _journal()
        self.addCleanup(self.j.close)

    def test_running_task_is_resumed_as_interrupted(self):
        """跑了一半的活：重新入队，且标记"被中断过"。"""
        s1 = _store(self.j)
        t = EngineTask(task_id="i1", task_type="news.collect",
                       queue="research", max_retries=2)
        s1.enqueue(t)
        s1.mark_running(t, "research")
        s1.flush()

        s2 = _store(self.j)
        rep = s2.recover()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["queued"], 1)
        self.assertEqual(rep["interrupted"], 1)

        got = s2.get("i1")
        self.assertIs(got.status, TaskState.QUEUED)
        self.assertTrue(got.interrupted, "中断标记必须带着 —— 那次可能真执行了")
        self.assertEqual(got.attempts, 1, "尝试次数不回滚：那次算试过了")

    def test_queued_task_is_resumed(self):
        s1 = _store(self.j)
        t = EngineTask(task_id="q1", task_type="factor.calculate", queue="research")
        s1.enqueue(t)
        s1.flush()

        s2 = _store(self.j)
        rep = s2.recover()
        self.assertEqual(rep["queued"], 1)
        self.assertEqual(rep["interrupted"], 0)
        self.assertEqual([x.task_id for x in s2.pending_tasks()], ["q1"])

    def test_retrying_task_is_resumed(self):
        s1 = _store(self.j)
        t = EngineTask(task_id="rt1", task_type="news.collect",
                       queue="research", max_retries=3)
        s1.enqueue(t)
        s1.mark_running(t, "research")
        decision = s1.mark_failure(t, "网络抖动")
        self.assertEqual(decision, "retry")
        self.j.flush()

        s2 = _store(self.j)
        rep = s2.recover()
        self.assertEqual(rep["queued"], 1)
        self.assertIs(s2.get("rt1").status, TaskState.QUEUED)

    def test_stale_task_goes_to_dead_letter(self):
        """三天前没干完的活不再自动重跑。

        今天再跑它通常没意义（行情变了、上下文没了），
        而且会造成"重启一次就重放一遍三个月前的所有任务"。
        """
        s1 = _store(self.j)
        t = EngineTask(task_id="old1", task_type="factor.calculate", queue="research")
        s1.enqueue(t)
        # 直接把库里的创建时间改老，模拟"滞留很久"
        s1.journal.flush()
        with s1.journal._lock:
            conn = s1.journal._conn
            conn.execute(
                "UPDATE engine_tasks SET created_at=? WHERE task_id=?",
                ((datetime.now() - timedelta(hours=200)).isoformat(timespec="seconds"),
                 "old1"),
            )
            conn.commit()

        s2 = _store(self.j)
        rep = s2.recover(max_age_hours=72.0)
        self.assertEqual(rep["expired"], 1)
        self.assertEqual(rep["queued"], 0)
        self.assertIs(s2.get("old1").status, TaskState.DEAD_LETTER)
        self.assertIn("滞留", s2.get("old1").error)

    def test_exhausted_task_goes_to_dead_letter(self):
        """重试额度用尽 → 不该再自动跑（那是"已判定走不通"）。"""
        s1 = _store(self.j)
        t = EngineTask(task_id="ex1", task_type="news.collect",
                       queue="research", max_retries=1)
        s1.enqueue(t)
        s1.mark_running(t, "research")
        s1.mark_failure(t, "boom")            # attempts=1 → retry
        s1.mark_running(t, "research")
        s1.mark_failure(t, "boom again")      # attempts=2 > 1 → dead
        self.assertIs(t.status, TaskState.DEAD_LETTER)

    def test_dead_letters_survive_restart(self):
        """错误队列必须跨重启存在。

        否则"重启一下死信就没了"等于给了掩盖问题的最佳手段。
        """
        s1 = _store(self.j)
        t = EngineTask(task_id="d1", task_type="universe.rank", queue="strategy")
        s1.enqueue(t)
        s1.dead_letter(t, "没有注册处理器")
        s1.flush()

        s2 = _store(self.j)
        rep = s2.recover()
        self.assertEqual(rep["dead_restored"], 1)
        self.assertEqual(s2.dead_letter_count(), 1)
        self.assertEqual(s2.dead_letters()[0]["task_type"], "universe.rank")

    def test_dedup_key_is_rebuilt_on_recovery(self):
        """恢复后幂等键仍生效 —— 否则同一批数据会被重复处理。"""
        s1 = _store(self.j)
        t = EngineTask(task_id="dk1", task_type="factor.calculate",
                       queue="research", dedup_key="factor:000001:2026-10-13")
        s1.enqueue(t)
        s1.flush()

        s2 = _store(self.j)
        s2.recover()
        again = EngineTask(task_id="dk2", task_type="factor.calculate",
                           queue="research", dedup_key="factor:000001:2026-10-13")
        ok, reason = s2.enqueue(again)
        self.assertFalse(ok, "恢复出来的幂等键必须还能挡住重复任务")
        self.assertIn("已有在途任务", reason)

    def test_recovery_without_persist_is_a_noop(self):
        s = TaskStore(persist=False)
        rep = s.recover()
        self.assertFalse(rep["ok"])
        self.assertIn("持久化", rep["reason"])


class ManualIntervention(unittest.TestCase):
    """人工从错误队列点"继续" / "丢弃"。"""

    def setUp(self) -> None:
        self.j = _journal()
        self.addCleanup(self.j.close)
        self.store = _store(self.j)

    def _dead(self, task_id: str = "m1", task_type: str = "news.collect") -> EngineTask:
        t = EngineTask(task_id=task_id, task_type=task_type, queue="research",
                       max_retries=2)
        self.store.enqueue(t)
        self.store.mark_running(t, "research")
        self.store.mark_failure(t, "第一次失败")
        self.store.mark_running(t, "research")
        self.store.mark_failure(t, "第二次失败")
        self.store.mark_running(t, "research")
        self.store.mark_failure(t, "第三次失败")
        self.assertIs(t.status, TaskState.DEAD_LETTER)
        return t

    def test_retry_resets_attempts(self):
        """人工重投是**新的一次机会**，不该带着上次的欠账上路。

        自动重试有上限是刻意的（重试不通就该报警），
        但人看过原因之后给的这次机会额度必须重置。
        """
        t = self._dead()
        self.assertEqual(t.attempts, 3)
        ok, reason = self.store.retry_dead("m1")
        self.assertTrue(ok, reason)
        self.assertEqual(t.attempts, 0)
        self.assertEqual(t.manual_retries, 1)
        self.assertIs(t.status, TaskState.QUEUED)
        self.assertIn("m1", [x.task_id for x in self.store.pending_tasks()])

    def test_retry_is_recorded_in_history(self):
        t = self._dead()
        self.store.retry_dead("m1")
        hist = self.j.dead_history()
        self.assertTrue(hist)
        self.assertEqual(hist[0]["resolution"], "retry")
        self.assertIsNotNone(hist[0]["resolved_at"])

    def test_trade_task_cannot_be_retried(self):
        """下单类死信不给"继续" —— 点了会重复下单。"""
        t = EngineTask(task_id="trade1", task_type="trade.execute", queue="trading")
        self.store.enqueue(t)
        self.store.dead_letter(t, "券商拒绝")
        ok, reason = self.store.retry_dead("trade1")
        self.assertFalse(ok)
        self.assertIn("不可", reason)
        self.assertIs(t.status, TaskState.DEAD_LETTER)

    def test_trade_task_is_flagged_not_actionable(self):
        t = EngineTask(task_id="trade2", task_type="trade.execute", queue="trading")
        self.store.enqueue(t)
        self.store.dead_letter(t, "券商拒绝")
        rows = self.store.dead_letters()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["actionable"])

    def test_discard_removes_without_rerun(self):
        t = self._dead()
        ok, reason = self.store.discard_dead("m1")
        self.assertTrue(ok, reason)
        self.assertEqual(self.store.dead_letter_count(), 0)
        self.assertIsNone(self.store.get("m1"))
        self.assertEqual([x.task_id for x in self.store.pending_tasks()], [])

    def test_retry_all_skips_unretryable(self):
        self._dead("m1")
        trade = EngineTask(task_id="tr1", task_type="trade.execute", queue="trading")
        self.store.enqueue(trade)
        self.store.dead_letter(trade, "券商拒绝")
        done, skipped = self.store.retry_all_dead()
        self.assertEqual(done, 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(self.store.dead_letter_count(), 1)

    def test_retry_unknown_id(self):
        ok, reason = self.store.retry_dead("nope")
        self.assertFalse(ok)
        self.assertIn("不存在", reason)

    def test_retry_non_dead_task(self):
        t = EngineTask(task_id="alive", task_type="news.collect", queue="research")
        self.store.enqueue(t)
        ok, reason = self.store.retry_dead("alive")
        self.assertFalse(ok)
        self.assertIn("不是死信", reason)

    def test_health_recovers_after_manual_handling(self):
        """处置完就该恢复健康 —— 否则告警一旦响过就永久响。"""
        self._dead()
        self.store.retry_dead("m1")
        self.assertEqual(self.store.dead_letter_count(), 0)

    def test_dead_letters_newest_first(self):
        self._dead("m1")
        self._dead("m2")
        rows = self.store.dead_letters()
        self.assertEqual([r["task_id"] for r in rows][:2], ["m2", "m1"])


class InterruptedOnStop(unittest.TestCase):
    """停机时"干了一半"的活必须被标成中断。"""

    def setUp(self) -> None:
        self.j = _journal()
        self.addCleanup(self.j.close)

    def test_persist_on_stop_marks_interrupted(self):
        s = _store(self.j)
        t = EngineTask(task_id="s1", task_type="news.collect", queue="research")
        s.enqueue(t)
        s.mark_running(t, "research")
        for x in s.running_tasks():
            x.mark_interrupted()
            s._persist(x)
        s.flush()
        self.assertTrue(t.interrupted)
        self.assertIs(t.status, TaskState.QUEUED)

        rows = self.j.load_open()
        self.assertEqual(rows[0]["interrupted"], 1)

    def test_recovery_reports_interrupted_count(self):
        s1 = _store(self.j)
        t = EngineTask(task_id="s2", task_type="news.collect", queue="research")
        s1.enqueue(t)
        s1.mark_running(t, "research")
        for x in s1.running_tasks():
            x.mark_interrupted()
            s1._persist(x)
        s1.flush()

        s2 = _store(self.j)
        rep = s2.recover()
        # 停机时已标成中断（状态回到 queued），恢复时必须仍能统计出来，
        # 否则报告会说"恢复 1 条，其中被中断 0 条"，而它恰恰是中断的。
        self.assertEqual(rep["interrupted"], 1)


class SchedulerRecovery(unittest.TestCase):
    """调度器层面的恢复与死信干预入口。"""

    def setUp(self) -> None:
        self.j = _journal()
        self.addCleanup(self.j.close)

    def _scheduler(self, **kw):
        from app.runtime.scheduler import SchedulerComponent

        sch = SchedulerComponent(**kw)
        sch.store.journal = self.j
        sch.store.persist = True
        self.addCleanup(sch.store.close)
        return sch

    def test_recover_on_start_is_reported(self):
        sch = self._scheduler()
        t = EngineTask(task_id="sr1", task_type="news.collect", queue="research")
        sch.store.enqueue(t)
        sch.store.flush()

        sch2 = self._scheduler()
        rep = sch2._recover_from_journal()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["queued"], 1)
        self.assertEqual(sch2.snapshot()["recovery"]["queued"], 1)

    def test_recover_can_be_disabled(self):
        sch = self._scheduler(recover=False)
        rep = sch._recover_from_journal()
        self.assertFalse(rep["ok"])
        self.assertIn("不恢复", rep["reason"])

    def test_dead_retry_through_scheduler(self):
        sch = self._scheduler()
        t = EngineTask(task_id="sd1", task_type="news.collect", queue="research")
        sch.store.enqueue(t)
        sch.store.dead_letter(t, "boom")
        ok, reason = sch.dead_retry("sd1")
        self.assertTrue(ok, reason)
        self.assertIs(sch.store.get("sd1").status, TaskState.QUEUED)

    def test_dead_discard_through_scheduler(self):
        sch = self._scheduler()
        t = EngineTask(task_id="sd2", task_type="news.collect", queue="research")
        sch.store.enqueue(t)
        sch.store.dead_letter(t, "boom")
        ok, _reason = sch.dead_discard("sd2")
        self.assertTrue(ok)
        self.assertEqual(sch.store.dead_letter_count(), 0)


class Preflight(unittest.TestCase):
    """启动预检。"""

    def test_report_shape(self):
        from app.engine.preflight import run_preflight

        r = run_preflight(mode="SIMULATE")
        d = r.to_dict()
        self.assertIn("ok", d)
        self.assertIn("checks", d)
        self.assertIsInstance(d["checks"], list)
        self.assertTrue(r.checks, "至少要跑出几项检查")

    def test_every_problem_has_a_hint(self):
        """只说"数据库不可写"不说怎么办的报告，等于把排障推回去。"""
        from app.engine.preflight import run_preflight

        r = run_preflight(mode="LIVE")
        for c in r.checks:
            if c.level in ("warn", "error"):
                self.assertTrue(c.hint, f"{c.name} 缺少处置建议")

    def test_live_without_gateway_is_blocking(self):
        from app.common.config import settings
        from app.engine.preflight import run_preflight

        old = settings.BROKER_GATEWAY
        settings.BROKER_GATEWAY = ""
        try:
            r = run_preflight(mode="LIVE")
        finally:
            settings.BROKER_GATEWAY = old
        self.assertFalse(r.ok)
        names = [c.name for c in r.errors]
        self.assertIn("实盘闸门", names)

    def test_simulate_does_not_require_gateway(self):
        from app.common.config import settings
        from app.engine.preflight import run_preflight

        old = settings.BROKER_GATEWAY
        settings.BROKER_GATEWAY = ""
        try:
            r = run_preflight(mode="SIMULATE", extra_checks=False)
        finally:
            settings.BROKER_GATEWAY = old
        self.assertNotIn("实盘闸门", [c.name for c in r.checks])

    def test_broken_config_is_blocking(self):
        from app.common.config import settings
        from app.engine.preflight import run_preflight

        old = settings.CONFIG_DIR
        tmp = tempfile.mkdtemp(prefix="cfg_")
        with open(os.path.join(tmp, "maintenance.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json ,,, }")
        settings.CONFIG_DIR = type(old)(tmp)
        try:
            r = run_preflight(mode="SIMULATE", extra_checks=False)
        finally:
            settings.CONFIG_DIR = old
        self.assertIn("配置文件", [c.name for c in r.errors])

    def test_engine_records_report(self):
        from app.engine.builder import build_engine

        e = build_engine(
            mode="SIMULATE", with_monitor=False, with_store=False,
            with_news=False, with_collector=False, with_ext_data=False,
            with_market=False, auto_load_runtime=False,
        )
        ok = e._run_preflight()
        self.assertTrue(ok)
        self.assertIsNotNone(e.preflight)
        self.assertIn("preflight", e.snapshot())


class JournalPruning(unittest.TestCase):
    """流水账不能无限长。"""

    def setUp(self) -> None:
        self.j = _journal()
        self.addCleanup(self.j.close)

    def test_open_and_dead_are_never_pruned(self):
        s = _store(self.j)
        keep = EngineTask(task_id="keep1", task_type="news.collect", queue="research")
        s.enqueue(keep)
        dead = EngineTask(task_id="dead1", task_type="news.collect", queue="research")
        s.enqueue(dead)
        s.dead_letter(dead, "boom")
        done = EngineTask(task_id="done1", task_type="news.collect", queue="research")
        s.enqueue(done)
        s.mark_running(done, "research")
        s.mark_success(done)
        self.j.flush()

        self.j.prune_terminal(older_than_hours=0.0, keep=0)
        self.assertEqual(self.j.load_open(limit=50)[0]["task_id"], "keep1")
        self.assertEqual(len(self.j.load_dead()), 1)


if __name__ == "__main__":
    unittest.main()
