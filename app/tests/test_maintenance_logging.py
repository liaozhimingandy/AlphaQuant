#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_maintenance_logging.py
# @Description : 维护计划与日志级别测试
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
这一组守的是两个"看不见就容易烂掉"的能力。

**维护计划**
重构后 ``system.maintenance.due`` 有三条规则，但**没有任何东西发布它**
—— 三条规则是死的，日志清理从来没跑过。这个缺陷不会报错、不会告警，
只是"该做的事一直没做"。所以这里钉住：计划器必须真的能把事件发出来，
且事件必须真的变成任务。

**日志级别**
级别能热切换这件事，一旦"看起来生效其实没生效"，排查线上问题时
会把人带进死胡同（以为开了 DEBUG，其实一行都没多打）。

两条共同的安全底线：
  * **默认不删东西** —— 清理/归档都只报告；
  * **危险配置要被拦** —— 保留天数 < 1 天、周期 < 5 分钟必须拒绝。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.common.logging import current_levels, level_of, set_level
from app.runtime.events.schema import EngineEvent
from app.runtime.maintenance import (
    DEFAULT_JOBS,
    MIN_INTERVAL_HOURS,
    MISS_GRACE_SECONDS,
    MaintenanceJob,
    MaintenancePlanner,
)
from app.runtime.scheduler import SchedulerComponent
from app.runtime.tasking.models import EngineTask, TaskState
from app.runtime.tasking.store import TaskStore
from app.runtime.workers.handlers import handle_archive, handle_log_cleanup


def _at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.now().replace(hour=hour, minute=minute, second=second, microsecond=0)


class MaintenanceJobScheduleTest(unittest.TestCase):
    """周期判定：日任务什么时候算"到点了"。"""

    def test_disabled_job_has_no_next_run(self):
        job = MaintenanceJob(name="x", task_type="t", label="x", enabled=False)
        self.assertIsNone(job.next_run_at())
        self.assertFalse(job.is_due())

    def test_daily_job_fires_on_the_tick_after_its_slot(self):
        """到点后第一拍必须触发。

        检查是每 30s 一拍，"03:10" 实际会在 03:10:xx 被看到。
        如果一律判成"时刻过了就顺延明天"，日任务将永远触发不了 ——
        这条就是防那个 bug 复发的。
        """
        job = MaintenanceJob(name="x", task_type="t", label="x",
                             enabled=True, schedule="daily", at="03:10")
        self.assertTrue(job.is_due(_at(3, 10, 15)))
        self.assertTrue(job.is_due(_at(3, 12, 0)))

    def test_daily_job_not_due_before_its_slot(self):
        job = MaintenanceJob(name="x", task_type="t", label="x",
                             enabled=True, schedule="daily", at="03:10")
        self.assertFalse(job.is_due(_at(1, 0, 0)))
        nxt = job.next_run_at(_at(1, 0, 0))
        self.assertIsNotNone(nxt)
        self.assertEqual((nxt.hour, nxt.minute), (3, 10))

    def test_missed_slot_rolls_to_tomorrow(self):
        """错过太久（超出宽限）不补跑 —— 维护任务补跑收益极低。"""
        job = MaintenanceJob(name="x", task_type="t", label="x",
                             enabled=True, schedule="daily", at="03:10")
        late = _at(3, 10, 0) + timedelta(seconds=MISS_GRACE_SECONDS + 60)
        self.assertFalse(job.is_due(late))
        nxt = job.next_run_at(late)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.day, (late + timedelta(days=1)).day)

    def test_daily_job_does_not_repeat_after_running(self):
        job = MaintenanceJob(name="x", task_type="t", label="x",
                             enabled=True, schedule="daily", at="03:10")
        job.last_run_at = _at(3, 10, 5).timestamp()
        self.assertFalse(job.is_due(_at(3, 30, 0)))
        nxt = job.next_run_at(_at(3, 30, 0))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.day, (_at(3, 30, 0) + timedelta(days=1)).day)

    def test_interval_job_due_after_elapsed(self):
        # 间隔的粒度是**小时**（不是秒）：维护任务做成分钟级只会让引擎
        # 一直在做无用功。旧字段 interval_sec 只在读旧配置时换算用。
        job = MaintenanceJob(name="x", task_type="t", label="x",
                             enabled=True, schedule="interval", interval_hours=1)
        job.last_run_at = time.time() - 3601
        self.assertTrue(job.is_due())
        job.last_run_at = time.time() - 60
        self.assertFalse(job.is_due())

    def test_interval_floor_is_one_hour(self):
        """配 0 或负数不能得到"每 0 小时跑一次"（那等于每拍都跑）。"""
        for raw in (0, -3):
            job = MaintenanceJob(name="x", task_type="t", label="x",
                                 enabled=True, schedule="interval",
                                 interval_hours=raw)
            self.assertEqual(job.interval_hours_value(), MIN_INTERVAL_HOURS)

    def test_bad_at_falls_back_without_crashing(self):
        job = MaintenanceJob(name="x", task_type="t", label="x",
                             enabled=True, schedule="daily", at="不是时间")
        nxt = job.next_run_at()
        self.assertIsNotNone(nxt)
        self.assertEqual((nxt.hour, nxt.minute), (3, 10))


class MaintenancePlannerTest(unittest.TestCase):
    """计划器：发事件、改配置、立即执行。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aq_maint_")
        self.path = os.path.join(self.tmp, "maintenance.json")
        self.fired: List[Dict[str, Any]] = []
        self.planner = MaintenancePlanner(
            publish=lambda et, **kw: self.fired.append({"event": et, **kw}),
            path=self.path,
        )

    def test_defaults_are_non_destructive(self):
        """会碰文件的任务（日志清理/归档）默认 apply 必须是 False。

        dead_history 是唯一例外：它清的不是文件，是死信**处置历史**里
        已处置的数据库行（未处置的一条不动，且有 keep 条下限），
        "只报告不删"对一个计数报表没有任何价值。
        """
        for job in self.planner.jobs():
            if job.params.get("apply") is None:
                continue
            if job.name == "dead_history":
                self.assertTrue(job.params["apply"],
                                "dead_history 默认就该清已处置的老记录")
            else:
                self.assertFalse(
                    job.params["apply"],
                    f"{job.name} 默认就开了 apply —— 定时任务自动删文件是运维事故高发区",
                )

    def test_tick_only_fires_due_jobs(self):
        # 把两个 daily 推到明天；validate 默认是 startup（不参与周期），
        # 这里临时改成 interval 才能测"周期到点"这条路径
        for name in ("log_cleanup", "archive"):
            self.planner._jobs[name].last_run_at = time.time()
        self.planner._jobs["validate"].schedule = "interval"
        self.planner._jobs["validate"].interval_hours = 1
        self.planner._jobs["validate"].last_run_at = time.time() - 99999
        fired = self.planner.tick()
        self.assertEqual(fired, ["validate"])
        self.assertEqual(self.fired[0]["event"], "system.maintenance.due")
        self.assertEqual(self.fired[0]["job"], "validate")
        self.assertIn("params", self.fired[0])

    def test_tick_never_fires_startup_jobs(self):
        """startup 只由引擎启动触发，周期节拍不能碰它。

        这条漏了的后果是"运行校验"每 30 秒跑一次 —— 用户提的
        "只在引擎启动时执行"等于没生效，而且日志会被灌满。
        """
        self.planner._jobs["validate"].schedule = "startup"
        self.planner._jobs["validate"].last_run_at = 0.0
        self.assertEqual(self.planner.tick(), [])
        self.assertEqual(self.fired, [])
        # 但启动路径必须能触发它
        self.assertEqual(self.planner.run_startup(), ["validate"])

    def test_run_now_ignores_enabled_flag(self):
        """手动"立即执行"被 enabled=false 挡住是最让人困惑的设计。"""
        self.planner.set_enabled("archive", False)
        result = self.planner.run_now("archive")
        self.assertTrue(result["ok"])
        self.assertEqual(self.fired[0]["reason"], "manual")

    def test_run_unknown_job_reports_known_names(self):
        result = self.planner.run_now("nope")
        self.assertFalse(result["ok"])
        self.assertIn("log_cleanup", result["known"])

    def test_update_keeps_runtime_state(self):
        self.planner.run_now("log_cleanup")
        before = self.planner._jobs["log_cleanup"]
        runs, last = before.runs, before.last_run_at
        self.assertTrue(runs >= 1)

        self.planner.update({"jobs": {"log_cleanup": {"enabled": True, "at": "04:30"}}})
        after = self.planner._jobs["log_cleanup"]
        self.assertEqual(after.at, "04:30")
        # 改一下周期不该把"上次什么时候跑的"清空，否则保存完立刻补跑一轮
        self.assertEqual(after.runs, runs)
        self.assertEqual(after.last_run_at, last)

    def test_update_persists_and_reloads(self):
        self.planner.update({"jobs": {"log_cleanup": {
            "enabled": False, "schedule": "interval", "interval_sec": 7200,
            "params": {"retention_days": 21, "apply": False},
        }}})
        reloaded = MaintenancePlanner(publish=lambda *a, **k: None, path=self.path)
        job = reloaded._jobs["log_cleanup"]
        self.assertFalse(job.enabled)
        self.assertEqual(job.schedule, "interval")
        self.assertEqual(job.params["retention_days"], 21)

    def test_unknown_job_name_in_config_is_ignored_but_not_loaded(self):
        import json

        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"jobs": {"drop_everything": {"enabled": True}}}, fh)
        reloaded = MaintenancePlanner(publish=lambda *a, **k: None, path=self.path)
        self.assertNotIn("drop_everything", reloaded._jobs)
        self.assertEqual(len(reloaded._jobs), len(DEFAULT_JOBS))

    def test_invalid_schedule_falls_back_to_default(self):
        # "weekly" 现在是合法周期了，用一个真正不认识的值。
        # 回落到**该任务自己的默认值**，而不是全局写死 daily ——
        # 运行校验的默认就是 startup，回落成 daily 反而会让它半夜跑起来。
        self.planner.update({"jobs": {"validate": {"schedule": "every-full-moon"}}})
        self.assertEqual(
            self.planner._jobs["validate"].schedule,
            DEFAULT_JOBS["validate"].kind,
        )

    def test_emit_failure_counts_as_error(self):
        def boom(*a, **k):
            raise RuntimeError("总线挂了")

        self.planner.bind(boom)
        result = self.planner.run_now("validate")
        self.assertFalse(result["ok"])
        self.assertGreater(self.planner._jobs["validate"].errors, 0)

    def test_unbound_planner_reports_clearly(self):
        p = MaintenancePlanner(publish=None, path=os.path.join(self.tmp, "x.json"))
        result = p.run_now("validate")
        self.assertFalse(result["ok"])
        self.assertIn("事件总线未就绪", result["error"])


class MaintenanceRuleTest(unittest.TestCase):
    """事件 → 规则 → 任务：运维窗口必须真的产出任务。"""

    def _scheduler(self) -> SchedulerComponent:
        sch = SchedulerComponent()
        sch.run_mode = sch.run_mode  # 保持默认
        return sch

    def _fire(self, sch: SchedulerComponent, job: str) -> List[EngineTask]:
        sch.handle_event(EngineEvent(
            event_type="system.maintenance.due",
            data={"job": job, "params": {"retention_days": 3}},
        ))
        return sch.store.pending_tasks()

    def _drain(self, sch: SchedulerComponent) -> None:
        for t in sch.store.pending_tasks():
            sch.store.cancel(t.task_id)

    def test_job_name_routes_to_exactly_one_task(self):
        sch = self._scheduler()
        for job, expect in (("log_cleanup", "maintenance.log_cleanup"),
                            ("archive", "maintenance.archive"),
                            ("validate", "maintenance.validate")):
            self._drain(sch)
            tasks = self._fire(sch, job)
            self.assertEqual([t.task_type for t in tasks], [expect],
                             f"{job} 应该只产出 {expect}")

    def test_bare_event_runs_all_maintenance(self):
        """裸的 maintenance.due（无 job）= "把运维活都跑一遍"。"""
        sch = self._scheduler()
        types = sorted(t.task_type for t in self._fire(sch, ""))
        self.assertEqual(types, ["maintenance.archive", "maintenance.log_cleanup",
                                 "maintenance.validate"])

    def test_params_reach_the_task(self):
        sch = self._scheduler()
        tasks = self._fire(sch, "log_cleanup")
        self.assertEqual(tasks[0].params.get("retention_days"), 3)
        self.assertEqual(tasks[0].params.get("job"), "log_cleanup")

    def test_maintenance_tasks_land_in_maintenance_queue(self):
        sch = self._scheduler()
        for t in self._fire(sch, "archive"):
            self.assertEqual(t.queue, "maintenance")


class DestructiveGuardTest(unittest.TestCase):
    """危险维护配置必须被拒绝，不能落到执行器里才炸。"""

    def _guard(self, payload):
        from app.control.control_plane import ControlCenter

        return ControlCenter._guard_destructive(payload)

    def test_zero_retention_rejected(self):
        bad = self._guard({"jobs": {"log_cleanup": {"params": {"retention_days": 0}}}})
        self.assertIsNotNone(bad)
        self.assertFalse(bad["ok"])

    def test_too_fast_interval_rejected(self):
        bad = self._guard({"jobs": {"validate": {"schedule": "interval",
                                                 "interval_sec": 10}}})
        self.assertIsNotNone(bad)

    def test_safe_config_passes(self):
        self.assertIsNone(self._guard({"jobs": {"log_cleanup": {
            "schedule": "daily", "at": "03:10",
            "params": {"retention_days": 7, "apply": False}}}}))


class MaintenanceHandlerTest(unittest.TestCase):
    """处理器：默认不删，勾了才删，且真的动了文件。"""

    def test_log_cleanup_reports_only_by_default(self):
        result = handle_log_cleanup(
            EngineTask(task_id="t", task_type="maintenance.log_cleanup",
                       params={"retention_days": 1}),
            ctx=None,
        )
        self.assertEqual(result["removed"], 0)
        self.assertFalse(result["apply"])

    def test_log_cleanup_refuses_tiny_retention(self):
        result = handle_log_cleanup(
            EngineTask(task_id="t", task_type="maintenance.log_cleanup",
                       params={"retention_days": 0}),
            ctx=None,
        )
        self.assertEqual(result["removed"], 0)
        self.assertIn("过小", result["note"])

    def test_archive_reports_only_by_default(self):
        result = handle_archive(
            EngineTask(task_id="t", task_type="maintenance.archive",
                       params={"before_days": 1}),
            ctx=None,
        )
        self.assertEqual(result["archived"], 0)
        self.assertFalse(result["apply"])

    def test_archive_really_moves_and_skips_already_archived(self):
        """归档区必须被排除，否则第二次运行会把已归档文件再"归档"一遍。"""
        import json
        from pathlib import Path
        from unittest import mock

        from app.common.config import settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "snapshots"
            (root / "run_a").mkdir(parents=True)
            old = root / "run_a" / "snap.json"
            old.write_text(json.dumps({"x": 1}), encoding="utf-8")
            # 把 mtime 推到 60 天前，确保在归档窗口内
            os.utime(old, (time.time() - 86400 * 60, time.time() - 86400 * 60))

            with mock.patch.object(settings, "SNAPSHOT_DIR", root):
                first = handle_archive(
                    EngineTask(task_id="t1", task_type="maintenance.archive",
                               params={"before_days": 30, "apply": True}),
                    ctx=None,
                )
                self.assertEqual(first["archived"], 1, first)
                target = root / "archive" / "run_a" / "snap.json"
                self.assertTrue(target.exists(), "归档后文件应在 archive/ 下")
                self.assertFalse(old.exists())

                second = handle_archive(
                    EngineTask(task_id="t2", task_type="maintenance.archive",
                               params={"before_days": 30, "apply": True}),
                    ctx=None,
                )
                self.assertEqual(second["candidates"], 0,
                                 "已归档的文件不该再次成为候选")


class LogLevelTest(unittest.TestCase):
    """日志级别热切换。"""

    def setUp(self) -> None:
        self.orig = current_levels()

    def tearDown(self) -> None:
        set_level(console=self.orig["console"], file=self.orig["file"],
                  throttle_window=self.orig["throttle_window"], persist=False)

    def test_set_level_changes_console(self):
        result = set_level(console="WARNING", persist=False)
        self.assertEqual(result["console"], "WARNING")
        self.assertEqual(current_levels()["console"], "WARNING")

    def test_empty_file_level_follows_console(self):
        set_level(console="ERROR", file="", persist=False)
        self.assertEqual(current_levels()["file"], "ERROR")

    def test_file_level_can_be_stricter(self):
        set_level(console="DEBUG", file="WARNING", persist=False)
        lv = current_levels()
        self.assertEqual(lv["console"], "DEBUG")
        self.assertEqual(lv["file"], "WARNING")

    def test_invalid_level_falls_back(self):
        result = set_level(console="LOUD", persist=False)
        self.assertIn(result["console"], ("TRACE", "DEBUG", "INFO", "SUCCESS",
                                          "WARNING", "ERROR", "CRITICAL"))
        self.assertEqual(level_of("LOUD", "INFO"), "INFO")

    def test_throttle_window_is_applied(self):
        set_level(throttle_window=2.5, persist=False)
        self.assertEqual(current_levels()["throttle_window"], 2.5)

    def test_debug_mode_follows_current_level(self):
        from app.common.logging import is_debug

        set_level(console="DEBUG", persist=False)
        self.assertTrue(is_debug())
        set_level(console="ERROR", persist=False)
        self.assertFalse(is_debug())


class DeadLetterTest(unittest.TestCase):
    """死信是待办清单：清掉之后健康判定必须恢复正常。"""

    def _task(self, tid: str) -> EngineTask:
        return EngineTask(task_id=tid, task_type="maintenance.validate", queue="maintenance")

    def test_cancel_really_leaves_the_queue(self):
        """取消只打标记、不出队的话，"队列积压 N" 会把没事可做的任务算进去。"""
        store = TaskStore()
        t = self._task("a")
        store.enqueue(t)
        self.assertEqual(store.pending_count(), 1)
        self.assertTrue(store.cancel("a"))
        self.assertEqual(store.pending_count(), 0, "取消必须真的出队")
        self.assertIsNone(store.pop("maintenance"))

    def test_clear_dead_letters_resets_count_but_not_cumulative(self):
        store = TaskStore()
        for tid in ("a", "b"):
            t = self._task(tid)
            store.enqueue(t)
            store.dead_letter(t, "boom")
        self.assertEqual(store.dead_letter_count(), 2)
        self.assertEqual(store.stats["dead_letter"], 2)

        cleared = store.clear_dead_letters()
        self.assertEqual(cleared, 2)
        self.assertEqual(store.dead_letter_count(), 0)
        # 累计计数是"发生过的事实"，不该被清空抹掉
        self.assertEqual(store.stats["dead_letter"], 2)

    def test_trading_dead_letter_is_unhealthy(self):
        """死信里含交易任务 = 一笔该下的单没下去，必须判不健康。"""
        sch = SchedulerComponent()
        sch.enabled = True          # 未启用的组件一律"健康"，测不出东西
        t = EngineTask(task_id="a", task_type="trade.execute", queue="trading")
        sch.store.enqueue(t)
        sch.store.dead_letter(t, "券商超时")
        ok, msg = sch.health_check()
        self.assertFalse(ok)
        self.assertIn("交易任务", msg)

    def test_failure_is_counted_even_when_retried(self):
        """失败数必须无条件累加 —— 重试与死信是失败**之后**的去向。

        少了它会让面板上的"失败"永远是 0，而失败恰恰是最该被看见的数。
        """
        store = TaskStore()
        t = EngineTask(task_id="a", task_type="factor.calculate",
                       queue="research", max_retries=2)
        store.enqueue(t)
        store.mark_running(t)
        outcome = store.mark_failure(t, "炸了")
        self.assertEqual(outcome, "retry")
        self.assertEqual(store.stats["failed"], 1)

    def test_health_note_disappears_after_clearing(self):
        """清掉死信之后，"死信 N 条"这个提示必须消失。

        否则告警一旦响过就永久响下去，运维再也不会看它。
        """
        sch = SchedulerComponent()
        sch.enabled = True
        t = self._task("a")
        sch.store.enqueue(t)
        sch.store.dead_letter(t, "boom")
        ok, msg = sch.health_check()
        self.assertTrue(ok)                 # 非交易类死信只是提示，不算不健康
        self.assertIn("死信", msg)

        sch.store.clear_dead_letters()
        ok2, msg2 = sch.health_check()
        self.assertTrue(ok2)
        self.assertNotIn("死信", msg2, f"清空后不该再提死信: {msg2}")


if __name__ == "__main__":
    unittest.main()
