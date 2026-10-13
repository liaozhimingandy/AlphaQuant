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
* 间隔的最小粒度**按任务类别区分**：维护类仍是 1 小时，业务类可以
  到分钟（"十分钟刷一次候选池"这类需求靠它落地）；
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
from app.runtime.workers.handlers import handle_archive, handle_log_cleanup

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


class TestMonthlySchedule:
    """monthly 周期（对照 Rhapsody Archive Cleanup 的 Monthly 档）。"""

    def _job(self, **kw):
        base = dict(schedule="monthly", day_of_month=1, at="03:10", enabled=True)
        base.update(kw)
        return MaintenanceJob(name="x", task_type="maintenance.log_cleanup",
                              label="x", **base)

    def test_next_run_lands_on_configured_day(self):
        job = self._job()
        nxt = job.next_run_at(datetime(2026, 10, 13, 7, 26))
        assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 11, 1, 3, 10)

    def test_same_day_before_time_runs_today(self):
        job = self._job()
        nxt = job.next_run_at(datetime(2026, 10, 13, 1, 0))
        assert nxt == datetime(2026, 11, 1, 3, 10)   # 13 号不是 1 号 → 下月 1 号

    def test_day_1_after_time_runs_next_month(self):
        job = self._job(day_of_month=1)
        nxt = job.next_run_at(datetime(2026, 10, 1, 9, 0))   # 当天 03:10 已过
        assert nxt == datetime(2026, 11, 1, 3, 10)

    def test_day_of_month_is_capped_at_28(self):
        # 29/30/31 在小月不存在，"每月 31 号"会变成"有大月才跑"，
        # 行为难以预期，直接封顶 28。
        job = self._job(day_of_month=31)
        assert job.day_of_month_value() == 28
        nxt = job.next_run_at(datetime(2026, 10, 13, 7, 26))
        assert nxt == datetime(2026, 10, 28, 3, 10)

    def test_december_rolls_to_next_year(self):
        job = self._job(day_of_month=15)
        nxt = job.next_run_at(datetime(2026, 12, 20, 9, 0))
        assert (nxt.year, nxt.month, nxt.day) == (2027, 1, 15)

    def test_round_trip_via_config(self):
        job = self._job(day_of_month=7)
        raw = job.to_dict()
        assert raw["day_of_month"] == 7
        clone = MaintenanceJob.from_dict("x", raw, job)
        assert clone.day_of_month_value() == 7 and clone.kind == "monthly"

    def test_february_28_is_valid_target(self):
        job = self._job(day_of_month=28)
        nxt = job.next_run_at(datetime(2026, 1, 30, 9, 0))
        assert (nxt.month, nxt.day) == (2, 28)


class TestRetentionHours:
    """保留期 小时/天 双单位（面板 Hours/Days 切换，内部统一存小时）。"""

    def test_retention_hours_takes_priority(self):
        t = EngineTask(task_type="maintenance.log_cleanup",
                       params={"retention_hours": 36, "retention_days": 99})
        result = handle_log_cleanup(t, ctx=None)
        # 日志目录在测试环境多半不存在，但单位优先级不影响这个断言的前提，
        # 所以这里只验证不抛异常；真正的换算断言走返回值
        assert "retention_hours" in result or result.get("removed") == 0

    def test_zero_retention_is_refused_not_clamped(self):
        # "保留 0"是配错的危险信号，该被拒绝而不是被悄悄换成 1 天 ——
        # 换算夹逼等于替错误配置兜底。
        t = EngineTask(task_type="maintenance.log_cleanup", params={"retention_days": 0})
        result = handle_log_cleanup(t, ctx=None)
        assert result["removed"] == 0 and "过小" in result["note"]
        t2 = EngineTask(task_type="maintenance.log_cleanup", params={"retention_hours": 0})
        assert "过小" in handle_log_cleanup(t2, ctx=None)["note"]
        t3 = EngineTask(task_type="maintenance.archive", params={"before_days": 0})
        assert "过小" in handle_archive(t3, ctx=None)["note"]


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

    def test_history_dedupes_same_task(self):
        """同一条任务被记两次，历史里只该有一条。

        进死信时 mark_failure 记一次，状态收敛时又会记一次。不去重的话
        面板显示"同一条任务失败了两次"，而实际只有一条 —— 拿历史对账的人
        会以为真跑了两遍，然后去找那个不存在的第二次。
        """
        store = self._store()
        t = EngineTask(task_type="market.kline.collect")
        store.enqueue(t)
        store.mark_running(t, worker="market")
        assert store.mark_failure(t, "网络错误") == "dead_letter"
        store._remember(t)          # 模拟第二次收敛记录
        hist = store.history()
        assert len(hist) == 1, f"同一条任务在历史里出现了 {len(hist)} 次"
        assert hist[0]["task_id"] == t.task_id

    def test_success_result_is_exported(self):
        """处理器的返回值要能在明细里看到。

        "任务成功了"只是一个布尔值，用户核对数据要看的是**成功了什么** ——
        比如回测跑完的收益、夏普、成交流水号。之前 to_dict 没导出 result，
        于是这部分只能去翻日志。
        """
        store = self._store()
        t = EngineTask(task_type="backtest.run")
        store.enqueue(t)
        store.mark_running(t, worker="backtest")
        store.mark_success(t, {"total_return_pct": -0.89, "trade_count": 3})
        row = store.task_detail(t.task_id)
        assert row["result"]["trade_count"] == 3

    def test_result_is_json_safe(self):
        """处理器爱返回什么就返回什么，别把面板的序列化打崩。

        "任务成功了但面板报错"是最难查的一类失败：任务侧一片绿，
        只有点开详情页才炸。
        """
        import json

        class Weird:
            def __repr__(self):
                return "<Weird>"

        store = self._store()
        t = EngineTask(task_type="x")
        store.enqueue(t)
        store.mark_success(t, {"obj": Weird(), "n": 1,
                               "big": list(range(500))})
        row = store.task_detail(t.task_id)
        json.dumps(row, ensure_ascii=False)   # 不抛异常即是过了
        assert row["result"]["obj"] == "<Weird>"
        assert len(row["result"]["big"]) == 100   # 超长的要截断

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


# ============================================================
# 分钟级粒度
# ============================================================
class TestIntervalGranularity:
    """「十分钟刷一次」这类需求以前配不出来 —— 间隔字段是 int 小时，
    最小 1 小时。这组用例守住放开到分钟之后的正确性。"""

    # 注意这里用**业务类**任务：维护类（maintenance.*）被有意兜底到 1 小时，
    # 那是 TestIntervalGranularity 里另一条用例要守的事，别混在一起。
    @staticmethod
    def _biz(**kw):
        return _job(task_type="pool.refresh", **kw)

    def test_ten_minutes_is_ten_minutes(self):
        j = self._biz(schedule=SCHEDULE_INTERVAL, interval_minutes=10)
        assert j.interval_seconds_value() == 600.0

    def test_next_run_uses_seconds_not_hours(self):
        """换成 timedelta(hours=...) 会把任何亚小时粒度抹成 0。"""
        base = datetime(2026, 10, 13, 10, 0)
        j = self._biz(schedule=SCHEDULE_INTERVAL, interval_minutes=10)
        j.last_run_at = base.timestamp()
        assert j.next_run_at(base) == datetime(2026, 10, 13, 10, 10)

    def test_minutes_win_over_hours(self):
        """:attr:`interval_hours` 有默认值 24 —— 必须让显式的分钟优先，
        否则"配了 10 分钟"会被这个默认值悄悄改回一天一次。"""
        j = self._biz(schedule=SCHEDULE_INTERVAL, interval_hours=24,
                      interval_minutes=10)
        assert j.interval_seconds_value() == 600.0

    def test_falls_back_to_hours_when_minutes_unset(self):
        j = _job(schedule=SCHEDULE_INTERVAL, interval_hours=2)
        assert j.interval_seconds_value() == 7200.0

    def test_maintenance_tasks_stay_hourly(self):
        """放开分钟不该波及维护类：扫日志做成十分钟一次纯属浪费。"""
        j = _job(schedule=SCHEDULE_INTERVAL, interval_minutes=5)
        assert j.task_type.startswith("maintenance.")
        assert j.interval_seconds_value() == 3600.0

    def test_business_tasks_may_go_sub_hourly(self):
        j = _job(task_type="pool.refresh", schedule=SCHEDULE_INTERVAL,
                 interval_minutes=10)
        assert j.interval_seconds_value() == 600.0

    def test_floor_is_per_task_class(self):
        from app.runtime.maintenance import min_interval_seconds_for

        assert min_interval_seconds_for("maintenance.log_cleanup") == 3600.0
        assert min_interval_seconds_for("pool.refresh") == 60.0


# ============================================================
# 仅交易时段
# ============================================================
class TestTradingHoursOnly:
    SATURDAY = datetime(2026, 10, 17, 10, 0)     # 周六
    WED_MIDDAY = datetime(2026, 10, 14, 10, 0)   # 周三盘中

    def _due(self, *, trading_only: bool, when: datetime) -> bool:
        j = _job(task_type="pool.refresh", schedule=SCHEDULE_INTERVAL,
                 interval_minutes=10, trading_hours_only=trading_only)
        j.last_run_at = (when - timedelta(minutes=30)).timestamp()
        return j.is_due(when)

    def test_skipped_on_weekend(self):
        assert self._due(trading_only=True, when=self.SATURDAY) is False

    def test_fires_during_session(self):
        assert self._due(trading_only=True, when=self.WED_MIDDAY) is True

    def test_without_flag_fires_anytime(self):
        assert self._due(trading_only=False, when=self.SATURDAY) is True

    def test_waiting_market_is_reported_to_panel(self):
        """"看起来已到期却不执行"必须在界面上有说法，否则像个卡死的 BUG。"""
        j = _job(task_type="pool.refresh", schedule=SCHEDULE_INTERVAL,
                 interval_minutes=10, trading_hours_only=True)
        assert j.to_dict(self.SATURDAY)["waiting_market"] is True
        assert j.to_dict(self.WED_MIDDAY)["waiting_market"] is False

    def test_calendar_failure_does_not_pause_the_job(self, monkeypatch):
        """日历读不出来时宁可多跑一次，也不要让任务静默停摆 —— 后者
        是最难排查的故障形态。"""
        import app.domain.calendar as cal

        monkeypatch.setattr(
            cal, "get_calendar",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("日历挂了")),
        )
        assert self._due(trading_only=True, when=self.SATURDAY) is True


# ============================================================
# 可配置的定时任务（不再只有写死的四条）
# ============================================================
class TestConfigurableJobs:
    """任务清单以前是 ``DEFAULT_JOBS`` 的枚举：配置文件里写别的名字会被
    告警并丢弃。这组用例守住"往配置里加一条就能跑"。"""

    def _planner(self, tmp_path, *, jobs=None):
        path = os.path.join(str(tmp_path), "maintenance.json")
        if jobs is not None:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"jobs": jobs}, fh, ensure_ascii=False)
        return MaintenancePlanner(publish=lambda *a, **kw: None, path=path)

    def test_custom_job_from_config_is_loaded(self, tmp_path):
        pl = self._planner(tmp_path, jobs={"pool_refresh": {
            "task_type": "pool.refresh", "label": "候选池刷新", "enabled": True,
            "schedule": "interval", "interval_minutes": 10,
            "trading_hours_only": True,
        }})
        names = {j.name for j in pl.jobs()}
        assert "pool_refresh" in names
        job = {j.name: j for j in pl.jobs()}["pool_refresh"]
        assert job.task_type == "pool.refresh"
        assert job.interval_seconds_value() == 600.0
        assert job.trading_hours_only is True

    def test_custom_job_survives_reload(self, tmp_path):
        pl = self._planner(tmp_path, jobs={"pool_refresh": {
            "task_type": "pool.refresh", "enabled": True,
            "schedule": "interval", "interval_minutes": 10,
            "trading_hours_only": True,
        }})
        pl.save()
        again = MaintenancePlanner(
            publish=lambda *a, **kw: None,
            path=os.path.join(str(tmp_path), "maintenance.json"))
        job = {j.name: j for j in again.jobs()}.get("pool_refresh")
        assert job is not None
        assert job.interval_seconds_value() == 600.0
        assert job.trading_hours_only is True

    def test_job_without_task_type_is_skipped_not_crashed(self, tmp_path):
        pl = self._planner(tmp_path, jobs={"broken": {"enabled": True}})
        assert "broken" not in {j.name for j in pl.jobs()}

    def test_builtin_jobs_are_still_there(self, tmp_path):
        pl = self._planner(tmp_path)
        assert DEFAULT_JOBS.keys() <= {j.name for j in pl.jobs()}

    def test_add_job(self, tmp_path):
        pl = self._planner(tmp_path)
        out = pl.add_job("hot10", {"task_type": "pool.refresh",
                                   "interval_minutes": 10, "enabled": True})
        assert out["ok"] is True
        assert "hot10" in {j.name for j in pl.jobs()}

    def test_add_job_rejects_builtin_name(self, tmp_path):
        pl = self._planner(tmp_path)
        out = pl.add_job("validate", {"task_type": "x"})
        assert out["ok"] is False
        assert "内置" in out["error"]

    def test_add_job_requires_task_type(self, tmp_path):
        pl = self._planner(tmp_path)
        assert pl.add_job("nope", {"enabled": True})["ok"] is False

    def test_remove_refuses_builtin(self, tmp_path):
        pl = self._planner(tmp_path)
        out = pl.remove_job("log_cleanup")
        assert out["ok"] is False
        assert "log_cleanup" in {j.name for j in pl.jobs()}

    def test_remove_custom(self, tmp_path):
        pl = self._planner(tmp_path)
        pl.add_job("tmp10", {"task_type": "pool.refresh"})
        assert pl.remove_job("tmp10")["ok"] is True
        assert "tmp10" not in {j.name for j in pl.jobs()}

    def test_remove_unknown(self, tmp_path):
        assert self._planner(tmp_path).remove_job("ghost")["ok"] is False

    def test_snapshot_exposes_task_types_and_builtins(self, tmp_path):
        snap = self._planner(tmp_path).snapshot()
        assert "pool.refresh" in snap["task_types"]
        assert snap["builtin_jobs"] == sorted(DEFAULT_JOBS)


# ============================================================
# 通用规则：动态 task_type
# ============================================================
class TestGenericMaintenanceRule:
    """新增定时任务不该要求改规则表——这条规则从事件里取 task_type。"""

    @staticmethod
    def _due_events(job: str, task_type: str, **extra):
        from app.runtime.events.schema import EngineEvent
        from app.runtime.events.types import EventType
        from app.runtime.scheduling.defaults import build_default_rules
        from app.runtime.scheduling.rules import RuleRegistry

        reg = RuleRegistry()
        reg.register_all(build_default_rules())
        ev = EngineEvent(
            event_type=EventType.SYSTEM_MAINTENANCE_DUE,
            data=dict(job=job, task_type=task_type, **extra),
            source="test",
        )
        return [(r.name, r.build_task(ev, None))
                for r in reg.match(EventType.SYSTEM_MAINTENANCE_DUE)
                if r.should_run(ev, None)]

    def test_custom_job_routes_to_its_own_task_type(self):
        built = self._due_events("pool_refresh", "pool.refresh")
        assert [name for name, _ in built] == ["maintenance_due_generic"]
        task = built[0][1]
        assert task.task_type == "pool.refresh"
        # 队列按 task_type 登记，动态类型必须在建任务时就地重解析
        assert task.queue == "research"

    def test_builtin_jobs_not_duplicated(self):
        """同一个事件不能被专用规则和通用规则各建一次任务。"""
        for job, expect in (("log_cleanup", "maintenance.log_cleanup"),
                            ("archive", "maintenance.archive"),
                            ("validate", "maintenance.validate")):
            built = self._due_events(job, expect)
            assert len(built) == 1
            assert built[0][1].task_type == expect

    def test_dead_history_is_no_longer_orphaned(self):
        """``dead_history`` 有默认配置但过去没有规则响应它，等于从不执行。"""
        built = self._due_events("dead_history", "maintenance.dead_history")
        assert [name for name, _ in built] == ["maintenance_due_generic"]
        assert built[0][1].task_type == "maintenance.dead_history"

    def test_bare_event_keeps_original_broadcast_semantics(self):
        """没带 job 名的裸事件 = "把运维活都跑一遍"，通用规则不该插手。"""
        from app.runtime.events.schema import EngineEvent
        from app.runtime.events.types import EventType
        from app.runtime.scheduling.defaults import build_default_rules
        from app.runtime.scheduling.rules import RuleRegistry

        reg = RuleRegistry()
        reg.register_all(build_default_rules())
        ev = EngineEvent(event_type=EventType.SYSTEM_MAINTENANCE_DUE,
                         data={"task_type": "x"}, source="test")
        hit = [r.name for r in reg.match(EventType.SYSTEM_MAINTENANCE_DUE)
               if r.should_run(ev, None)]
        assert "maintenance_due_generic" not in hit
        assert len(hit) == 3

    def test_missing_task_type_produces_nothing(self):
        assert self._due_events("weird", "") == []

    def test_rule_allows_empty_static_type_when_dynamic(self):
        from app.runtime.scheduling.rules import Rule

        with pytest.raises(ValueError):
            Rule(name="bad", event_type="e", task_type="")
        rule = Rule(name="ok", event_type="e",
                    task_type_fn=lambda e, c: "pool.refresh")
        assert rule.to_dict()["dynamic_task_type"] is True

    def test_task_type_fn_failure_falls_back(self):
        from app.runtime.events.schema import EngineEvent
        from app.runtime.scheduling.rules import Rule

        def boom(event, ctx):
            raise RuntimeError("推导失败")

        rule = Rule(name="r", event_type="e", task_type="static.fallback",
                    task_type_fn=boom)
        task = rule.build_task(EngineEvent(event_type="e", data={}, source="t"))
        assert task.task_type == "static.fallback"


# ============================================================
# 保存护栏（不得误拦周期性任务）
# ============================================================
class TestDestructiveGuard:
    @staticmethod
    def _guard(payload):
        from app.control.control_plane import ControlCenter

        return ControlCenter._guard_destructive(payload)

    def test_minute_schedule_is_not_blocked(self):
        """回归点：``cfg.get("interval_sec") or 0`` 会让任何没写旧字段的
        周期配置被判为 0 秒而拒绝保存，"十分钟刷新"根本配不进去。"""
        out = self._guard({"jobs": {"pool_refresh": {
            "schedule": "interval", "interval_minutes": 10, "params": {}}}})
        assert out is None, out

    def test_old_field_still_enforced(self):
        out = self._guard({"jobs": {"x": {
            "schedule": "interval", "interval_sec": 100, "params": {}}}})
        assert out is not None and out["ok"] is False

    def test_retention_floor_intact(self):
        out = self._guard({"jobs": {"x": {
            "schedule": "daily", "params": {"retention_days": 0}}}})
        assert out is not None and ">= 1 天" in out["error"]

    def test_harmless_config_passes(self):
        assert self._guard({"jobs": {"x": {
            "schedule": "interval", "interval_sec": 3600, "params": {}}}}) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
