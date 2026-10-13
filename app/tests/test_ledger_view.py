#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_ledger_view.py
# @Description : 任务明细的过滤/排序/日期范围 + 内置定时任务清单
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""这一组用例钉住的是"面板上点了过滤却没反应"这个现象背后的两个真实缺陷。

**失败是个瞬时状态**
``TaskStore.mark_failure`` 在同一次调用里就把任务转成了
``retrying``（还能重试）或 ``dead_letter``（重试耗尽），
``failed`` 从来不会因为一次失败而留存下来。
于是按 ``failed`` 精确查询**永远返回空**，而按钮上写的数字来自
``stats["failed"]`` 累计值 —— "失败 12 条"和"点进去一条没有"同时成立。
这不是样式问题，是查询语义错了。

**明细是两份数据拼出来的**
``tasks()`` 与 ``history()`` 各自倒序，前端 concat 之后整体却不是有序的：
"在册的旧任务"会排在"已淘汰的新任务"前面。看着像排序失效，
实际是拼错了地方。合并、去重、排序、计数必须由同一个地方一次做完，
否则任何一种边界都能让两者对不上。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.control.control_plane import ControlCenter
from app.runtime.events.types import Queue
from app.runtime.maintenance import MaintenancePlanner
from app.runtime.tasking.models import EngineTask
from app.runtime.tasking.store import (
    LEDGER_STATUS_FILTERS,
    STATUS_GROUPS,
    TaskStore,
    parse_ledger_dt,
    status_matches,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _task(task_type: str = "collect.now", retries: int = 3) -> EngineTask:
    """造一条待执行任务。默认给重试额度，失败才会停在 retrying。"""
    task = EngineTask(task_type=task_type, params={"x": 1}, queue=Queue.MARKET)
    task.max_retries = retries
    return task


def _run(store: TaskStore, task: EngineTask) -> None:
    """入队 → 领取 → 标记成功。"""
    store.enqueue(task)
    store.mark_running(task)
    store.mark_success(task, {"ok": True})


def _fail(store: TaskStore, task: EngineTask, error: str = "boom") -> str:
    """入队 → 领取 → 失败，返回去向（``retry`` / ``dead_letter``）。"""
    store.enqueue(task)
    store.mark_running(task)
    return store.mark_failure(task, error)


class _FakeScheduler:
    """只提供 task_ledger / builtin_schedules 需要的那点东西。"""

    def __init__(self, store: TaskStore, maintenance: MaintenancePlanner = None) -> None:
        self.store = store
        self.maintenance = maintenance
        self._timers = {"maintenance_tick": (None, 30.0)}

    def timers(self):
        return [
            {"timer_id": tid, "interval": interval, "running": True}
            for tid, (_lc, interval) in self._timers.items()
        ]


def _control(scheduler, components=None) -> ControlCenter:
    ctrl = ControlCenter.__new__(ControlCenter)
    ctrl._event_scheduler = lambda: scheduler
    ctrl._audit = lambda *a, **k: None

    class _Engine:
        def get_component(self, name):
            return (components or {}).get(name)

    ctrl.engine = _Engine()
    return ctrl


# ---------------------------------------------------------------------------
# 状态组
# ---------------------------------------------------------------------------
class TestStatusGroups(unittest.TestCase):
    """面板上的一个按钮要能覆盖一组状态，因为它对应用户的一种意图。"""

    def test_failed_covers_retrying(self):
        """这是本文件的起因：失败的任务会立刻停在 retrying。"""
        self.assertIn("retrying", STATUS_GROUPS["failed"])

    def test_failed_matches_retrying_task(self):
        self.assertTrue(status_matches("retrying", "failed"))
        self.assertTrue(status_matches("failed", "failed"))

    def test_exact_status_still_works(self):
        self.assertTrue(status_matches("dead_letter", "dead_letter"))
        self.assertFalse(status_matches("success", "dead_letter"))

    def test_unknown_group_falls_back_to_exact(self):
        """没登记过的名字按精确值处理 —— 加新状态不必先来这里补名单。"""
        self.assertTrue(status_matches("success", "success"))
        self.assertFalse(status_matches("running", "queued"))

    def test_empty_means_no_filter(self):
        self.assertTrue(status_matches("anything", ""))

    def test_every_ledger_filter_is_known(self):
        """按钮清单里的每个值要么是真状态，要么是状态组 —— 不能有拼错的。"""
        known = {"pending", "queued", "running", "success", "failed",
                 "retrying", "dead_letter", "cancelled"}
        for key in LEDGER_STATUS_FILTERS:
            if key == "":
                continue
            self.assertTrue(key in known or key in STATUS_GROUPS, key)


# ---------------------------------------------------------------------------
# 明细查询
# ---------------------------------------------------------------------------
class TestLedgerRows(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TaskStore()

    # ---- 起因：失败必须查得到 ----
    def test_failed_filter_finds_retrying_task(self):
        self.assertEqual(_fail(self.store, _task()), "retry")
        rows = self.store.ledger_rows(status="failed")["rows"]
        self.assertEqual(len(rows), 1, "失败后正在重试的任务必须出现在「失败」里")
        self.assertEqual(rows[0]["status"], "retrying")

    def test_failed_and_dead_letter_do_not_overlap(self):
        """失败按钮看"还在重试"，死信按钮看"已经判死"，两者不该重合。"""
        self.assertEqual(_fail(self.store, _task()), "retry")
        self.assertEqual(_fail(self.store, _task(retries=0)), "dead_letter")

        failed = [r["task_id"] for r in self.store.ledger_rows(status="failed")["rows"]]
        dead = [r["task_id"] for r in self.store.ledger_rows(status="dead_letter")["rows"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(dead), 1)
        self.assertFalse(set(failed) & set(dead))
        # 而"没做成"这一组要把两边都含进去
        self.assertEqual(len(self.store.ledger_rows(status="unsuccessful")["rows"]), 2)

    # ---- 计数必须与明细同源 ----
    def test_status_counts_match_detail_rows(self):
        _run(self.store, _task())
        _fail(self.store, _task())
        _fail(self.store, _task(retries=0))

        result = self.store.ledger_rows()
        counts = result["status_counts"]
        for key in LEDGER_STATUS_FILTERS:
            actual = len(self.store.ledger_rows(status=key)["rows"])
            self.assertEqual(
                counts[key], actual,
                f"按钮 {key!r} 写着 {counts[key]}，点下去却是 {actual} 条",
            )

    def test_counts_narrow_down_with_other_filters(self):
        """选了类型之后，计数得跟着收窄 —— 否则又是另一种"对不上"。"""
        _run(self.store, _task("collect.now"))
        _run(self.store, _task("backtest.run"))

        scoped = self.store.ledger_rows(task_type="collect.now")
        self.assertEqual(scoped["status_counts"][""], 1)
        self.assertEqual(scoped["status_counts"]["success"], 1)

    # ---- 合并去重 ----
    def test_no_duplicate_task_ids(self):
        ok = _task()
        _run(self.store, ok)
        # 成功会同时进 _tasks 和 history，明细里只能出现一次
        rows = self.store.ledger_rows()["rows"]
        ids = [r["task_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 1)

    def test_history_only_rows_are_still_visible(self):
        """任务被 _tasks 淘汰后，只剩历史里有 —— 明细必须还看得见它。"""
        task = _task()
        _run(self.store, task)
        self.store._tasks.pop(task.task_id, None)

        rows = self.store.ledger_rows()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], task.task_id)

    # ---- 排序 ----
    def test_global_order_across_both_sources(self):
        """前端 tasks(desc)+history(desc) 拼接的并不是全局有序，这是它的回归用例。"""
        older = _task()
        _run(self.store, older)
        older.created_at = datetime(2026, 10, 13, 10, 0, 0)

        newer = _task()
        _run(self.store, newer)
        newer.created_at = datetime(2026, 10, 13, 12, 0, 0)
        self.store._tasks.pop(newer.task_id, None)   # 让它只活在历史里

        rows = self.store.ledger_rows(order="desc")["rows"]
        self.assertEqual(
            [r["task_id"] for r in rows], [newer.task_id, older.task_id],
            "倒序时最新的那条必须在最前，哪怕它来自另一份数据",
        )
        rows = self.store.ledger_rows(order="asc")["rows"]
        self.assertEqual([r["task_id"] for r in rows], [older.task_id, newer.task_id])

    def test_desc_is_the_default(self):
        for i in range(3):
            task = _task()
            _run(self.store, task)
            task.created_at = datetime(2026, 10, 13, 10, 0, i)
        stamps = [r["created_at"] for r in self.store.ledger_rows()["rows"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_sort_by_elapsed(self):
        quick = _task()
        _run(self.store, quick)
        slow = _task()
        _run(self.store, slow)
        base = datetime(2026, 10, 13, 10, 0, 0)
        quick.started_at, quick.finished_at = base, base + timedelta(seconds=1)
        slow.started_at, slow.finished_at = base, base + timedelta(seconds=30)

        rows = self.store.ledger_rows(sort="elapsed", order="desc")["rows"]
        self.assertEqual([r["task_id"] for r in rows], [slow.task_id, quick.task_id])
        rows = self.store.ledger_rows(sort="elapsed", order="asc")["rows"]
        self.assertEqual([r["task_id"] for r in rows], [quick.task_id, slow.task_id])

    # ---- 日期范围 ----
    def test_date_range_filters_by_created_at(self):
        today = _task()
        _run(self.store, today)
        today.created_at = datetime.now()

        old = _task()
        _run(self.store, old)
        old.created_at = datetime(2020, 1, 1, 8, 0, 0)

        since = parse_ledger_dt(datetime.now().strftime("%Y-%m-%d"))
        rows = self.store.ledger_rows(since=since)["rows"]
        self.assertEqual([r["task_id"] for r in rows], [today.task_id])

    def test_until_includes_the_whole_day(self):
        """只给日期时，截止必须补到当天最后一刻，否则"截止到今天"会漏掉今天。"""
        task = _task()
        _run(self.store, task)
        task.created_at = datetime(2026, 10, 13, 21, 30, 0)

        until = parse_ledger_dt("2026-10-13", end=True)
        self.assertIsNotNone(until)
        self.assertEqual(until.hour, 23)
        self.assertEqual(len(self.store.ledger_rows(until=until)["rows"]), 1)

        # 反过来：不补的话同一个任务会被排除掉
        naive = parse_ledger_dt("2026-10-13")
        self.assertEqual(len(self.store.ledger_rows(until=naive)["rows"]), 0)

    def test_reversed_range_is_swapped_not_empty(self):
        """起止填反返回空的接口，用户只会学到"这个功能坏了"。"""
        _run(self.store, _task())
        rows = self.store.ledger_rows(
            since=parse_ledger_dt("2030-01-01"), until=parse_ledger_dt("2020-01-01")
        )["rows"]
        self.assertEqual(len(rows), 1)

    def test_bad_date_is_ignored_with_a_warning(self):
        """日期格式不对最多是"没过滤"，不能让整屏变成报错。"""
        _run(self.store, _task())
        self.assertIsNone(parse_ledger_dt("not-a-date"))
        self.assertIsNone(parse_ledger_dt(""))
        self.assertEqual(len(self.store.ledger_rows(since=None)["rows"]), 1)

    # ---- 限量与总量 ----
    def test_limit_truncates_but_matched_reports_truth(self):
        for _ in range(5):
            _run(self.store, _task())
        result = self.store.ledger_rows(limit=2)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(
            result["matched"], 5, "面板要靠它提示「还有 N 条没显示」",
        )

    def test_limit_is_clamped(self):
        self.assertGreaterEqual(self.store.ledger_rows(limit=0)["limit"], 1)
        self.assertLessEqual(self.store.ledger_rows(limit=99999)["limit"], 500)


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------
class TestTaskLedgerApi(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TaskStore()
        self.ctrl = _control(_FakeScheduler(self.store))

    def _call(self, **kw):
        return self.ctrl.task_ledger(**kw)

    def test_failed_rows_match_failed_count(self):
        _fail(self.store, _task())
        _fail(self.store, _task(retries=0))
        data = self._call(status="failed")
        self.assertEqual(len(data["rows"]), data["status_counts"]["failed"])

    def test_date_range_passthrough(self):
        task = _task()
        _run(self.store, task)
        task.created_at = datetime(2020, 1, 1, 8, 0, 0)

        self.assertEqual(len(self._call(since="2020-01-01", until="2020-01-02")["rows"]), 1)
        self.assertEqual(len(self._call(since="2026-10-13")["rows"]), 0)

    def test_no_scheduler_reports_clearly(self):
        ctrl = ControlCenter.__new__(ControlCenter)
        ctrl._event_scheduler = lambda: None
        data = ctrl.task_ledger()
        self.assertFalse(data["ok"])
        self.assertIn("调度器", data["error"])

    def test_legacy_fields_still_present(self):
        """``tasks`` / ``history`` 有别的调用方在用，不能因为加了 rows 就删掉。"""
        _run(self.store, _task())
        data = self._call()
        for key in ("rows", "tasks", "history", "summary",
                    "task_types", "tracked", "history_size"):
            self.assertIn(key, data)

    def test_filters_are_echoed_back(self):
        data = self._call(status="failed", order="asc", sort="elapsed", limit=3)
        self.assertEqual(data["filters"]["status"], "failed")
        self.assertEqual(data["filters"]["order"], "asc")
        self.assertEqual(data["filters"]["sort"], "elapsed")


# ---------------------------------------------------------------------------
# 内置定时任务清单
# ---------------------------------------------------------------------------
_COLLECTION_SNAPSHOT = {
    "kline": {"enabled": True, "running": True, "buckets": [60.0, 300.0],
              "jobs": [{"symbol": "000001"}], "trading_hours_only": True,
              "stats": {"runs": 12, "errors": 1}},
    "cross": {"enabled": True, "interval": 900.0, "next_slot": "盘后",
              "stats": {"runs": 2, "errors": 0}},
    "guard": {"enabled": True, "running": True, "interval": 3600.0,
              "start_delay": 60, "auto_repair": True,
              "stats": {"runs": 1, "errors": 0}},
    "watchlist": {"enabled": True, "running": True, "total": 5,
                  "enabled_count": 4, "due_count": 1,
                  "config": {"interval": "1h", "sweep_seconds": 60.0},
                  "stats": {"runs": 3, "errors": 0}},
}


class _FakeCollection:
    def snapshot(self):
        return dict(_COLLECTION_SNAPSHOT)


def _planner(cfg: dict) -> MaintenancePlanner:
    path = os.path.join(tempfile.gettempdir(),
                        f"aq_mt_{id(cfg)}.json").replace("\\", "/")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    return MaintenancePlanner(publish=lambda *a, **k: None, path=path)


class TestBuiltinSchedules(unittest.TestCase):
    """「这台机器自己会做哪些事」必须一眼看全，缺一类就有人白等。"""

    def setUp(self) -> None:
        self.cfg = {"jobs": {
            "log_cleanup": {"task_type": "maintenance.log_cleanup", "enabled": True,
                            "schedule": "daily", "at": "03:10"},
            "pool_refresh": {"task_type": "pool.refresh", "enabled": True,
                             "schedule": "interval", "interval_minutes": 10},
        }}
        self.planner = _planner(self.cfg)

    def test_three_sources_are_all_listed(self):
        ctrl = _control(_FakeScheduler(TaskStore(), self.planner),
                        {"collection_service": _FakeCollection()})
        data = ctrl.builtin_schedules()
        groups = {i["group"] for i in data["items"]}
        self.assertEqual(groups, {"维护计划", "节拍定时器", "采集单元"})

    def test_source_and_editability_are_marked(self):
        """错标"可以改"比漏列更糟 —— 用户会改完配置等一个不会来的执行。"""
        ctrl = _control(_FakeScheduler(TaskStore(), self.planner),
                        {"collection_service": _FakeCollection()})
        by_name = {i["name"]: i for i in ctrl.builtin_schedules()["items"]}
        self.assertTrue(by_name["pool_refresh"]["editable"])
        self.assertEqual(by_name["pool_refresh"]["source"], "配置")
        self.assertFalse(by_name["maintenance_tick"]["editable"])
        self.assertEqual(by_name["maintenance_tick"]["source"], "代码")

    def test_interval_minutes_is_readable(self):
        """「每 10 分钟」要出现在面板上 —— 配置里写的是 interval_minutes。"""
        ctrl = _control(_FakeScheduler(TaskStore(), self.planner))
        item = next(i for i in ctrl.builtin_schedules()["items"]
                    if i["name"] == "pool_refresh")
        self.assertIn("10 分钟", item["cadence"])

    def test_daily_job_cadence_uses_a_full_day_boundary(self):
        """翻译出来的周期是给人看的，拼错一个字都得靠肉眼发现 —— 钉住它。"""
        ctrl = _control(_FakeScheduler(TaskStore(), self.planner))
        item = next(i for i in ctrl.builtin_schedules()["items"]
                    if i["name"] == "log_cleanup")
        self.assertEqual(item["cadence"], "每天 03:10")
        self.assertEqual(item["next_run_text"][:10], (
            (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            if datetime.now().hour >= 3 else datetime.now().strftime("%Y-%m-%d")
        ))

    def test_missing_collection_service_is_graceful(self):
        """没装配采集服务不该让整屏报错 —— 那是最常见的部署形态之一。"""
        ctrl = _control(_FakeScheduler(TaskStore(), self.planner))
        data = ctrl.builtin_schedules()
        self.assertTrue(data["ok"])
        groups = {i["group"] for i in data["items"]}
        self.assertNotIn("采集单元", groups)

    def test_engine_raising_is_graceful(self):
        ctrl = _control(_FakeScheduler(TaskStore(), self.planner))

        class _Boom:
            def get_component(self, name):
                raise RuntimeError("未装配")

        ctrl.engine = _Boom()
        self.assertTrue(ctrl.builtin_schedules()["ok"])

    def test_counters_summary(self):
        ctrl = _control(_FakeScheduler(TaskStore(), self.planner),
                        {"collection_service": _FakeCollection()})
        data = ctrl.builtin_schedules()
        self.assertEqual(data["total"], len(data["items"]))
        self.assertEqual(data["enabled"],
                         sum(1 for i in data["items"] if i["enabled"]))
        self.assertGreaterEqual(data["configurable"], 1)


class TestHttpRequestRouting(unittest.TestCase):
    """面板 HTTP 层：路径与状态值的形状。

    这里的断言保护的是 JS 与后端之间那份"没写在任何接口文档里"的约定：

    * 单任务明细在 JS 里曾被写成 ``/api/engine_tasks``，后端没这条路由 →
      每次轮询一个 404，界面上就成了一句「查询任务失败: 未知接口」，
      而任务其实已经跑完；
    * ``status`` 是**小写**的。拿它跟 ``'SUCCESS'`` 比永远不等，
      轮询只能转满超时才退出。
    """

    def setUp(self) -> None:
        self.store = TaskStore()
        self.ctrl = _control(_FakeScheduler(self.store))

        class _Res:
            """resource.py 只用得到这两个成员。"""

            def __init__(self, ctrl):
                # resource.py 里 ``c = self.component``，之后一律 ``c.control.xxx``，
                # 所以 control 必须挂在这个替身上，而不是外层。
                self.control = ctrl
                self.run_id = "test-run"
                self.store = None
                self.snapshot_base = ""
                self.audit: list = []

        from twisted.web.test.requesthelper import DummyRequest
        from app.monitoring.resource import MonitorResource

        resource = MonitorResource(_Res(self.ctrl))

        def get(path, **qs):
            segs = [p for p in path.strip("/").split("/")]
            req = DummyRequest([p.encode() for p in segs])
            req.path = ("/" + "/".join(segs)).encode()
            req.args = {k.encode(): [str(v).encode()] for k, v in qs.items()}
            raw = resource.render_GET(req)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {"__raw__": raw.decode("utf-8", "replace")}

        self.get = get

    def test_task_endpoint_is_reachable_under_both_names(self):
        task = _task()
        _run(self.store, task)
        canonical = self.get("/api/task", id=task.task_id)
        aliased = self.get("/api/engine_tasks", id=task.task_id)
        self.assertTrue(canonical["ok"])
        self.assertEqual(aliased, canonical,
                         "面板早期用过的名字必须还能用，否则轮询会一直 404")

    def test_statuses_are_lowercase(self):
        """JS 的终态集合是小写 —— 这条断言就是那份约定的另一半。"""
        task = _task()
        _run(self.store, task)
        detail = self.get("/api/task", id=task.task_id)["task"]
        self.assertEqual(detail["status"], "success")
        self.assertEqual(detail["status"], detail["status"].lower())
        rows = self.get("/api/ledger")["rows"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["status"], row["status"].lower())

    def test_date_filter_is_applied_server_side(self):
        """选了日期却一条都没有 —— 这条覆盖"日期到底生效没有"。

        后端确实会过滤，但表格曾被 uiBusy() 拦住不重绘，
        用户看到的是"筛选没效果"。这里钉住后端契约，
        前端那份约定（点了搜索才查）由 test_dashboard_js 覆盖。
        """
        task = _task()
        _run(self.store, task)
        task.created_at = datetime(2020, 1, 1, 8, 0, 0)

        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(len(self.get("/api/ledger", since="2020-01-01",
                                      until="2020-01-02")["rows"]), 1)
        # 同一天：截止日必须含当天 23:59，否则"查今天"永远查不到今天
        self.assertEqual(len(self.get("/api/ledger", since="2020-01-01",
                                      until="2020-01-01")["rows"]), 1)
        self.assertEqual(len(self.get("/api/ledger", since=today)["rows"]), 0)
        # 空结果也要给出套用的条件，便于用户判断"是没数据还是条件错了"
        empty = self.get("/api/ledger", since=today, until=today)
        self.assertEqual(empty["rows"], [])
        self.assertEqual(empty["matched"], 0)
        self.assertEqual(empty["filters"]["since"], today)

    def test_unknown_endpoint_is_still_a_404(self):
        # 别名不能扩大成"什么都接"，打错路径还要能立刻看出来
        body = self.get("/api/definitely_not_a_route")
        self.assertIn("未知接口", str(body))


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
