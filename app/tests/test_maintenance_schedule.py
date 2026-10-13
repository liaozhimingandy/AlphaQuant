#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_maintenance_schedule.py
# @Description : cron 解析 / 六种维护周期 / 启动触发 / 任务明细 / 事件指标
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""这一组用例钉的是"配置周期"和"核对数据"两件事。

周期那部分的重点不是"能算对下一次"，而是：

* 字段必须与周期联动 —— 六种周期共用一套字段，选错了框就等于配上了一个
  永远不会生效的参数（用户看到的正是这个现象）；
* 间隔的最小粒度是**小时**，不是秒；
* ``startup`` 不能被周期节拍反复触发。

核对那部分的重点是"明细要真的看得见"：汇总数字对不上时，
必须能翻出逐条记录，否则数字只是数字。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

from app.runtime.cron import CronExpr, CronError, describe_cron, validate_cron
from app.runtime.events.bus import EventBus
from app.runtime.maintenance import (
    DEFAULT_JOBS,
    MIN_INTERVAL_HOURS,
    SCHEDULE_CRON,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    SCHEDULE_STARTUP,
    SCHEDULE_WEEKLY,
    MaintenanceJob,
    MaintenancePlanner,
)
from app.runtime.maintenance import _legacy_adjust
from app.runtime.tasking.models import EngineTask
from app.runtime.tasking.store import TaskStore

TUESDAY = datetime(2026, 10, 13, 7, 26)      # 2026-10-13 是周二
WEDNESDAY = datetime(2026, 10, 14, 7, 26)


def _job(**kw) -> MaintenanceJob:
    base = dict(
        name="x", task_type="maintenance.validate", label="测试",
        enabled=True,
    )
    base.update(kw)
    return MaintenanceJob(**base)


# ============================================================
# cron 解析
# ============================================================
class TestCronExpr:
    def test_parse_and_next(self):
        assert CronExpr.parse("*/15 * * * *").next_after(TUESDAY) == \
            datetime(2026, 10, 13, 7, 30)

    def test_daily_cron(self):
        assert CronExpr.parse("0 3 * * *").next_after(TUESDAY) == \
            datetime(2026, 10, 14, 3, 0)

    def test_weekday_cron(self):
        # 周二 07:26 之后的下一个周一 09:30
        assert CronExpr.parse("30 9 * * 1").next_after(TUESDAY) == \
            datetime(2026, 10, 19, 9, 30)

    def test_monthday_cron(self):
        assert CronExpr.parse("0 0 1 * *").next_after(TUESDAY) == \
            datetime(2026, 11, 1, 0, 0)

    def test_impossible_date_returns_none(self):
        # 2 月 30 日不存在 —— 必须是 None，不能是死循环也不能是乱跳
        assert CronExpr.parse("0 0 30 2 *").next_after(TUESDAY) is None

    def test_leap_day(self):
        assert CronExpr.parse("0 12 29 2 *").next_after(TUESDAY) == \
            datetime(2028, 2, 29, 12, 0)

    def test_range_and_step(self):
        expr = CronExpr.parse("*/5 9-17 * * *")
        assert expr.next_after(TUESDAY) == datetime(2026, 10, 13, 9, 0)
        assert expr.hours == tuple(range(9, 18))
        assert expr.minutes == tuple(range(0, 60, 5))

    def test_list_field(self):
        assert CronExpr.parse("0,30 * * * *").minutes == (0, 30)

    def test_dom_and_dow_is_or_not_and(self):
        """「日」与「周」是 OR 语义。

        这条错了的话 ``0 3 1 * 1`` 会变成"每月 1 号且是周一"，
        一年只跑几次，而用户以为每周都在跑。
        """
        expr = CronExpr.parse("0 3 1 * 1")
        # 2026-10-19 是周一（不是 1 号）→ 必须命中
        assert expr.matches(datetime(2026, 10, 19, 3, 0)) is True
        # 2026-11-01 是 1 号（周日）→ 也必须命中
        assert expr.matches(datetime(2026, 11, 1, 3, 0)) is True
        # 2026-10-15 周四 15 号 → 都不匹配
        assert expr.matches(datetime(2026, 10, 15, 3, 0)) is False

    @pytest.mark.parametrize("text", [
        "", "   ", "* * * *", "* * * * * *", "*/0 * * * *",
        "70 * * * *", "* 25 * * *", "* * 0 * *", "* * * 13 *", "* * * * 7",
        "5-1 * * * *", "abc * * * *", "*/x * * * *", "1,,2 * * * *",
    ])
    def test_invalid_raises(self, text):
        with pytest.raises(CronError):
            CronExpr.parse(text)

    def test_error_message_points_at_the_field(self):
        """报错要说清是**哪一段**错了 —— "表达式非法"等于没说。"""
        assert "分" in validate_cron("70 * * * *")
        assert "时" in validate_cron("* 25 * * *")
        assert "周" in validate_cron("* * * * 9")
        assert validate_cron("0 3 * * *") == ""

    def test_describe(self):
        assert describe_cron("*/15 * * * *") == "每 15 分钟"
        assert describe_cron("0 3 * * *") == "每天 03:00"
        assert describe_cron("30 9 * * 1") == "每周 周一 09:30"
        assert describe_cron("0 0 1 * *") == "每月 1 号 00:00"
        assert describe_cron("0 */5 * * *") == "每 5 小时整点"
        # 说不清就回原文，不要编
        assert describe_cron("*/5 9-17 * * *") == "*/5 9-17 * * *"

    def test_inclusive_matches_now(self):
        assert CronExpr.parse("*/15 * * * *").next_after(
            datetime(2026, 10, 13, 7, 30), inclusive=True
        ) == datetime(2026, 10, 13, 7, 30)


# ============================================================
# 维护周期
# ============================================================
class TestScheduleKinds:
    def test_all_kinds_are_known(self):
        for kind in (SCHEDULE_STARTUP, SCHEDULE_INTERVAL, SCHEDULE_DAILY,
                     SCHEDULE_WEEKLY, SCHEDULE_CRON, SCHEDULE_ONCE):
            assert _job(schedule=kind).kind == kind

    def test_unknown_kind_falls_back_to_daily(self):
        """脏配置不能让任务变成"永远不跑"，回落到 daily 至少还会跑。"""
        assert _job(schedule="weekly-ish").kind == SCHEDULE_DAILY

    def test_interval_minimum_is_one_hour(self):
        job = _job(schedule=SCHEDULE_INTERVAL, interval_hours=0)
        assert job.interval_hours_value() == MIN_INTERVAL_HOURS
        job2 = _job(schedule=SCHEDULE_INTERVAL, interval_hours=-5)
        assert job2.interval_hours_value() == MIN_INTERVAL_HOURS

    def test_interval_next_run(self):
        job = _job(schedule=SCHEDULE_INTERVAL, interval_hours=6)
        # 没跑过 → 下次检查就跑，而不是干等 6 小时
        assert job.next_run_at(TUESDAY) == TUESDAY
        job.last_run_at = TUESDAY.timestamp()
        assert job.next_run_at(TUESDAY) == TUESDAY + timedelta(hours=6)

    def test_daily_before_and_after(self):
        job = _job(schedule=SCHEDULE_DAILY, at="03:10")
        assert job.next_run_at(TUESDAY) == datetime(2026, 10, 14, 3, 10)
        # 今天 03:10 刚过（在宽限内）→ 现在就跑
        just_after = datetime(2026, 10, 13, 3, 12)
        assert job.next_run_at(just_after) == datetime(2026, 10, 13, 3, 10)
        # 超过宽限 → 算错过，等明天
        late = datetime(2026, 10, 13, 3, 40)
        assert job.next_run_at(late) == datetime(2026, 10, 14, 3, 10)

    def test_weekly_targets_the_right_weekday(self):
        # weekday=1 周一；周二 07:26 之后的下一个周一 03:10
        job = _job(schedule=SCHEDULE_WEEKLY, at="03:10", weekday=1)
        assert job.next_run_at(TUESDAY) == datetime(2026, 10, 19, 3, 10)
        # weekday=3 周三 → 明天
        job2 = _job(schedule=SCHEDULE_WEEKLY, at="03:10", weekday=3)
        assert job2.next_run_at(TUESDAY) == datetime(2026, 10, 14, 3, 10)

    def test_cron_next_run(self):
        job = _job(schedule=SCHEDULE_CRON, cron="0 3 * * 1")
        nxt = job.next_run_at(TUESDAY)
        assert nxt is not None and nxt.isoweekday() == 1 and nxt.hour == 3

    def test_bad_cron_reports_error_and_never_fires(self):
        job = _job(schedule=SCHEDULE_CRON, cron="不存在的写法")
        assert job.cron_error()
        assert job.cron_expr() is None
        # 表达式写错 = 没有下次，而不是"每次 tick 都跑"
        assert job.next_run_at(TUESDAY) is None
        assert job.is_due(TUESDAY) is False
        assert job.to_dict(TUESDAY)["config_error"]

    def test_startup_has_no_scheduled_next_run(self):
        job = _job(schedule=SCHEDULE_STARTUP)
        assert job.next_run_at(TUESDAY) is None
        # 关键：不能被 30s 一拍的 tick 反复触发
        assert job.is_due(TUESDAY) is False
        assert job.is_startup is True

    def test_once_within_grace(self):
        job = _job(schedule=SCHEDULE_ONCE, once_at="2026-10-13 07:25")
        assert job.next_run_at(TUESDAY) == datetime(2026, 10, 13, 7, 25)
        assert job.is_due(TUESDAY) is True

    def test_once_missed_is_not_retried(self):
        """错过的一次性任务不该在几天后启动时补跑。

        补跑"上周三该做的校验"没有任何意义，只会制造一条过期结果。
        """
        job = _job(schedule=SCHEDULE_ONCE, once_at="2026-10-13 07:00")
        assert job.next_run_at(TUESDAY) is None
        assert job.is_due(TUESDAY) is False

    def test_once_bad_format_reports(self):
        job = _job(schedule=SCHEDULE_ONCE, once_at="明天下午")
        assert job.parse_once_at() is None
        assert job.to_dict(TUESDAY)["config_error"]

    def test_disabled_never_due(self):
        for kind in (SCHEDULE_INTERVAL, SCHEDULE_DAILY, SCHEDULE_WEEKLY,
                     SCHEDULE_CRON, SCHEDULE_ONCE, SCHEDULE_STARTUP):
            job = _job(schedule=kind, enabled=False, cron="* * * * *",
                       once_at="2026-10-13 07:25", interval_hours=1)
            assert job.next_run_at(TUESDAY) is None
            assert job.is_due(TUESDAY) is False


# ============================================================
# 配置兼容
# ============================================================
class TestConfigCompat:
    def test_legacy_interval_sec_is_upgraded_to_hours(self):
        """旧配置写的是秒；换算必须**向上取整**。

        向下取整会把 1.5 小时变成 1 小时，等于悄悄把周期缩短 ——
        维护任务跑太频繁比跑少了更烦人。
        """
        job = MaintenanceJob.from_dict(
            "x", {"schedule": "interval", "interval_sec": 5400},
            DEFAULT_JOBS["log_cleanup"],
        )
        assert job.interval_hours == 2

    def test_legacy_one_hour(self):
        job = MaintenanceJob.from_dict(
            "x", {"schedule": "interval", "interval_sec": 3600},
            DEFAULT_JOBS["log_cleanup"],
        )
        assert job.interval_hours == 1

    def test_interval_hours_wins_when_present(self):
        job = MaintenanceJob.from_dict(
            "x", {"schedule": "interval", "interval_sec": 99999,
                  "interval_hours": 3},
            DEFAULT_JOBS["log_cleanup"],
        )
        assert job.interval_hours == 3

    def test_legacy_validate_migrates_to_startup(self):
        """没被改过的旧默认配置要迁到新默认值。

        否则用户从没手动改过周期、只是面板保存过一次，那份旧配置
        就会把"启动时执行"又拽回"每小时"，需求等于没生效。
        """
        out = _legacy_adjust("validate", {"schedule": "interval",
                                          "interval_sec": 3600})
        assert out["schedule"] == SCHEDULE_STARTUP

    def test_user_modified_validate_is_left_alone(self):
        """用户改过的配置一律不动 —— 绝不替用户做决定。"""
        raw = {"schedule": "cron", "cron": "0 4 * * *"}
        assert _legacy_adjust("validate", raw) is raw
        raw2 = {"schedule": "interval", "interval_sec": 7200}
        assert _legacy_adjust("validate", raw2) is raw2

    def test_other_jobs_untouched(self):
        raw = {"schedule": "interval", "interval_sec": 3600}
        assert _legacy_adjust("log_cleanup", raw) is raw

    def test_roundtrip_persists_new_fields(self, tmp_path):
        path = tmp_path / "maintenance.json"
        planner = MaintenancePlanner(publish=lambda *a, **kw: None, path=str(path))
        planner.update({"jobs": {
            "log_cleanup": {
                "enabled": True, "schedule": SCHEDULE_CRON,
                "cron": "0 4 * * 2", "params": {"retention_days": 3},
            },
            "archive": {
                "enabled": True, "schedule": SCHEDULE_WEEKLY,
                "at": "05:30", "weekday": 5,
            },
        }})
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["jobs"]["log_cleanup"]["cron"] == "0 4 * * 2"
        assert raw["jobs"]["archive"]["weekday"] == 5
        # 新写出的配置不该再有 interval_sec
        assert "interval_sec" not in raw["jobs"]["log_cleanup"]

        again = MaintenancePlanner(publish=lambda *a, **kw: None, path=str(path))
        by_name = {j.name: j for j in again.jobs()}
        assert by_name["log_cleanup"].cron == "0 4 * * 2"
        assert by_name["archive"].weekday == 5


# ============================================================
# 启动触发
# ============================================================
class TestStartupTrigger:
    def _planner(self, tmp_path, monkeypatch=None):
        path = tmp_path / "maintenance.json"
        return MaintenancePlanner(publish=lambda *a, **kw: None, path=str(path))

    def test_validate_defaults_to_startup(self):
        assert DEFAULT_JOBS["validate"].kind == SCHEDULE_STARTUP

    def test_run_startup_fires_only_startup_jobs(self, tmp_path):
        planner = self._planner(tmp_path)
        seen = []
        planner.bind(lambda *a, **kw: seen.append(kw.get("job")))
        fired = planner.run_startup()
        assert fired == ["validate"]
        assert seen == ["validate"]

    def test_tick_ignores_startup_jobs(self, tmp_path):
        """tick 里 startup 必须被跳过，否则它会每 30 秒跑一次。"""
        planner = self._planner(tmp_path)
        fired = []
        planner.bind(lambda *a, **kw: fired.append(kw.get("job")))
        assert planner.tick() == []
        assert fired == []

    def test_run_startup_respects_enabled(self, tmp_path):
        planner = self._planner(tmp_path)
        planner.set_enabled("validate", False)
        assert planner.run_startup() == []

    def test_once_job_auto_disables_after_firing(self, tmp_path):
        """一次性任务跑完必须自动停用。

        不停用的话面板上永远显示"启用"，而它永远不会再跑 ——
        一个显示启用却不动的任务比"已停用"更让人困惑。
        """
        path = tmp_path / "maintenance.json"
        planner = MaintenancePlanner(publish=lambda *a, **kw: None, path=str(path))
        planner.update({"jobs": {"archive": {
            "enabled": True, "schedule": SCHEDULE_ONCE,
            "once_at": (datetime.now() + timedelta(minutes=5)
                        ).strftime("%Y-%m-%d %H:%M"),
        }}})
        job = {j.name: j for j in planner.jobs()}["archive"]
        job.last_run_at = 0.0
        # 直接让它"到点"
        assert job.is_due(datetime.now() + timedelta(minutes=6)) is True
        planner.tick(datetime.now() + timedelta(minutes=6))
        after = {j.name: j for j in planner.jobs()}["archive"]
        assert after.enabled is False
        assert after.runs == 1

    def test_run_now_works_even_when_disabled(self, tmp_path):
        """手动「立即执行」被 enabled=false 挡住是最让人困惑的设计。"""
        planner = self._planner(tmp_path)
        planner.set_enabled("archive", False)
        out = planner.run_now("archive")
        assert out["ok"] is True

    def test_snapshot_exposes_kinds_for_the_panel(self, tmp_path):
        snap = self._planner(tmp_path).snapshot()
        values = [k["value"] for k in snap["kinds"]]
        assert SCHEDULE_STARTUP in values and SCHEDULE_CRON in values
        assert snap["min_interval_hours"] == MIN_INTERVAL_HOURS
        assert "validate" in snap["startup_jobs"]


# ============================================================
# 任务明细
# ============================================================
class TestTaskLedger:
    def _store(self):
        return TaskStore(persist=False)

    def test_success_goes_into_history(self):
        store = self._store()
        t = EngineTask(task_type="factor.calculate", params={"symbol": "000001"})
        store.enqueue(t)
        store.mark_running(t, worker="research")
        store.mark_success(t, {"rows": 10})
        hist = store.history()
        assert len(hist) == 1
        assert hist[0]["status"] == "success"
        assert hist[0]["task_id"] == t.task_id

    def test_dead_letter_goes_into_history(self):
        store = self._store()
        t = EngineTask(task_type="market.kline.collect")
        store.enqueue(t)
        store.mark_running(t, worker="market")   # attempts 在 mark_running 里 +1
        assert store.mark_failure(t, "网络错误") == "dead_letter"
        assert store.history(status="dead_letter")[0]["error"] == "网络错误"

    def test_retrying_is_not_history(self):
        """等待重试不是终态，不该出现在历史里。

        历史是"做完了的那些"，把还在等的混进去会让"成功+失败"对不上总数。
        """
        store = self._store()
        t = EngineTask(task_type="market.kline.collect", max_retries=2)
        store.enqueue(t)
        assert store.mark_failure(t, "抖动") == "retry"
        assert store.history() == []

    def test_history_is_bounded(self):
        store = TaskStore(persist=False, history_size=5)
        for i in range(20):
            t = EngineTask(task_type="x", params={"i": i})
            store.enqueue(t)
            store.mark_success(t)
        assert len(store.history(limit=0)) == 5
        # 保留的是**最近**的 5 条
        assert store.history(limit=0)[0]["params"]["i"] == 19

    def test_history_survives_task_eviction(self):
        """``_tasks`` 满了会淘汰终态任务，明细不能跟着丢。"""
        store = TaskStore(persist=False, max_tasks=3, history_size=50)
        ids = []
        for i in range(5):
            t = EngineTask(task_type="x", params={"i": i})
            ids.append(t.task_id)
            store.enqueue(t)
            store.mark_success(t)
        # 最早那条已被淘汰出 _tasks，但历史里必须还在
        assert store.get(ids[0]) is None
        assert store.task_detail(ids[0]) is not None
        assert store.task_detail(ids[0])["params"]["i"] == 0

    def test_filter_by_status_and_type(self):
        store = self._store()
        ok = EngineTask(task_type="a.b")
        bad = EngineTask(task_type="a.b")
        other = EngineTask(task_type="c.d")
        for t in (ok, bad, other):
            store.enqueue(t)
        store.mark_success(ok)
        store.mark_failure(bad, "x")
        store.mark_success(other)
        assert len(store.history(status="success", task_type="a.b")) == 1
        assert len(store.history(status="success")) == 2

    def test_tasks_query_filters(self):
        store = self._store()
        t = EngineTask(task_type="a.b", queue="research")
        store.enqueue(t)
        assert len(store.tasks(status="queued")) == 1
        assert len(store.tasks(status="running")) == 0
        assert len(store.tasks(task_type="zzz")) == 0
        assert len(store.tasks(queue="research")) == 1
        assert len(store.tasks(keyword=t.task_id[:8])) == 1

    def test_history_summary_counts_by_status(self):
        store = self._store()
        for _ in range(3):
            t = EngineTask(task_type="a")
            store.enqueue(t)
            store.mark_success(t)
        t = EngineTask(task_type="a")
        store.enqueue(t)
        store.mark_running(t)
        store.mark_failure(t, "x")
        assert store.history_summary()["success"] == 3
        assert store.history_summary()["dead_letter"] == 1

    def test_task_types_seen(self):
        store = self._store()
        for name in ("b.c", "a.c"):
            t = EngineTask(task_type=name)
            store.enqueue(t)
            store.mark_success(t)
        assert store.task_types_seen() == ["a.c", "b.c"]

    def test_task_detail_missing(self):
        assert self._store().task_detail("nope") is None


# ============================================================
# 事件指标
# ============================================================
class TestEventMetrics:
    def test_counts_per_type(self):
        bus = EventBus()
        bus.subscribe("market.kline.updated", lambda **kw: None)
        bus.publish("market.kline.updated", symbol="000001")
        bus.publish("market.kline.updated", symbol="000002")
        bus.publish("order.filled", order_id="1")
        rows = {r["event_type"]: r for r in bus.event_metrics()}
        assert rows["market.kline.updated"]["published"] == 2
        assert rows["market.kline.updated"]["delivered"] == 2
        assert rows["market.kline.updated"]["subscribers"] == 1
        assert rows["order.filled"]["published"] == 1
        assert rows["order.filled"]["subscribers"] == 0

    def test_handler_errors_are_counted_per_type(self):
        def boom(**kw):
            raise RuntimeError("炸了")

        bus = EventBus()
        bus.subscribe("a.b", boom)
        bus.subscribe("a.b", lambda **kw: None)   # 后面的订阅者仍要收到
        bus.publish("a.b")
        rows = {r["event_type"]: r for r in bus.event_metrics()}
        assert rows["a.b"]["handler_errors"] == 1
        assert rows["a.b"]["delivered"] == 1

    def test_subscriber_count_is_live(self):
        """订阅者是**实时**算的：链路在跑起来之后断开必须看得见。"""
        bus = EventBus()
        fn = lambda **kw: None  # noqa: E731
        bus.subscribe("a.b", fn)
        bus.publish("a.b")
        assert bus.event_metrics()[0]["subscribers"] == 1
        bus.unsubscribe("a.b", fn)
        assert bus.event_metrics()[0]["subscribers"] == 0

    def test_recent_filtered_by_type(self):
        bus = EventBus()
        bus.publish("a.b", x=1)
        bus.publish("c.d", x=2)
        assert len(bus.recent(10, event_type="a.b")) == 1
        assert len(bus.recent(10)) == 2

    def test_event_detail_has_samples(self):
        bus = EventBus()
        bus.publish("a.b", x=1)
        detail = bus.event_detail("a.b")
        assert detail["published"] == 1
        assert detail["samples"][0]["data"]["x"] == 1

    def test_duplicate_dropped_not_counted(self):
        from app.runtime.events.schema import EngineEvent

        bus = EventBus()
        evt = EngineEvent.create("a.b", {})
        bus.publish_event(evt)
        bus.publish_event(evt)          # 同一个 event_id → 去重
        rows = {r["event_type"]: r for r in bus.event_metrics()}
        assert rows["a.b"]["published"] == 1
        assert bus.stats["duplicate_dropped"] == 1

    def test_snapshot_includes_types(self):
        bus = EventBus()
        bus.publish("a.b")
        assert bus.snapshot()["types"][0]["event_type"] == "a.b"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
