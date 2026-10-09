#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_collector.py
# @Description : 行情采集服务的测试：频率解析、交易时段、任务配置、采集组件
#               采集组件会创建定时器，所以这里只验证纯逻辑 + 手动驱动单次跑，
#               不起 reactor —— 定时器并入 schedules 另行在冒烟脚本里验
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import unittest
from datetime import datetime, time as dtime
from pathlib import Path

from app.core.collect.spec import (
    DAILY_PERIOD,
    A_SHARE_SESSIONS,
    CollectJob,
    CollectorSpec,
    build_job,
    humanize_frequency,
    in_trading_session,
    is_intraday,
    is_trading_day,
    load_collector_spec,
    normalize_period,
    parse_frequency,
)
from app.core.engine.components.collector import DataCollectorComponent, parse_interval
from app.core.engine.event import EventBus, StandardEvents
from app.core.engine.settings import EngineContext, EngineStatus, RunMode
from app.test.helpers import TempWorkspaceMixin


# ===========================================================================
# 频率解析 —— 用户在这一步写错的概率最高，必须宽容但不能静默答错
# ===========================================================================
class TestParseFrequency(unittest.TestCase):
    def test_bare_number_is_seconds(self):
        self.assertEqual(parse_frequency(60), 60.0)
        self.assertEqual(parse_frequency(300.0), 300.0)

    def test_none_falls_back_to_default(self):
        self.assertEqual(parse_frequency(None), 300.0)
        self.assertEqual(parse_frequency(None, 900.0), 900.0)
        self.assertEqual(parse_frequency("  ", 120.0), 120.0)

    def test_unit_suffixes(self):
        cases = {
            "30s": 30.0, "30second": 30.0, "30seconds": 30.0,
            "5m": 300.0, "5min": 300.0, "5mins": 300.0, "5 minute": 300.0,
            "1h": 3600.0, "1hr": 3600.0, "1hour": 3600.0,
            "1d": 86400.0, "1day": 86400.0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_frequency(text), expected)

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_frequency("  15m  "), 900.0)

    def test_garbage_falls_back_not_crash(self):
        # 采集服务最忌讳"配置写错就起不来"——数据旧一点没关系，服务挂了才是事故
        self.assertEqual(parse_frequency("abc", 300.0), 300.0)
        self.assertEqual(parse_frequency("10光年", 300.0), 300.0)
        self.assertEqual(parse_frequency("m", 300.0), 300.0)

    def test_non_positive_number_falls_back(self):
        self.assertEqual(parse_frequency(0, 300.0), 300.0)
        self.assertEqual(parse_frequency(-5, 300.0), 300.0)

    def test_invalid_type_raises(self):
        # 但完全不是数字/字符串的类型要显式报错，别静默吞掉
        with self.assertRaises(ValueError):
            parse_frequency([60])

    def test_humanize_roundtrip_readable(self):
        self.assertEqual(humanize_frequency(30), "30s")
        self.assertEqual(humanize_frequency(300), "5m")
        self.assertEqual(humanize_frequency(3600), "1h")
        self.assertEqual(humanize_frequency(86400), "1d")


# ===========================================================================
# 数据粒度
# ===========================================================================
class TestPeriod(unittest.TestCase):
    def test_daily_aliases(self):
        for raw in ("1d", "d", "day", "daily", "日线", "1D"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_period(raw), DAILY_PERIOD)

    def test_minute_periods(self):
        for raw in ("1", "5", "15", "30", "60", "5min", "15m"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_period(raw), raw.rstrip("min"))

    def test_unsupported_period_raises(self):
        with self.assertRaises(ValueError):
            normalize_period("7")

    def test_is_intraday(self):
        self.assertTrue(is_intraday("5"))
        self.assertFalse(is_intraday("1d"))


# ===========================================================================
# 交易时段
# ===========================================================================
class TestTradingWindow(unittest.TestCase):
    def test_weekend_is_not_trading_day(self):
        sat = datetime(2026, 10, 10)   # 周六
        sun = datetime(2026, 10, 11)   # 周日
        self.assertFalse(is_trading_day(sat))
        self.assertFalse(is_trading_day(sun))

    def test_weekday_is_trading_day(self):
        fri = datetime(2026, 10, 9)
        self.assertTrue(is_trading_day(fri))

    def test_custom_holiday_excluded(self):
        dt = datetime(2026, 10, 9)
        self.assertTrue(is_trading_day(dt))
        self.assertFalse(is_trading_day(dt, holidays=["2026-10-09"]))

    def test_session_boundaries(self):
        d = datetime(2026, 10, 9)
        cases = {
            dtime(9, 30): True, dtime(10, 15): True, dtime(11, 30): True,
            dtime(13, 0): True, dtime(14, 59): True, dtime(15, 0): True,
            dtime(8, 0): False, dtime(20, 0): False,
        }
        for t, expected in cases.items():
            with self.subTest(t=t):
                self.assertEqual(
                    in_trading_session(datetime.combine(d, t), buffer_minutes=0),
                    expected,
                )

    def test_buffer_extends_window(self):
        """缓冲区的意义：收盘后数据源还要一会儿才结算，
        没有缓冲就会系统性漏掉当天最后一根。"""
        d = datetime(2026, 10, 9)
        just_after = datetime.combine(d, dtime(15, 10))
        self.assertFalse(in_trading_session(just_after, buffer_minutes=0))
        self.assertTrue(in_trading_session(just_after, buffer_minutes=30))

        before_open = datetime.combine(d, dtime(9, 10))
        self.assertFalse(in_trading_session(before_open, buffer_minutes=0))
        self.assertTrue(in_trading_session(before_open, buffer_minutes=30))

    def test_lunch_break_is_outside_strict_session(self):
        """午休 A 股是停牌的，所以 11:30~13:00 不在任何一段连续竞价里。"""
        d = datetime(2026, 10, 9)
        lunch = datetime.combine(d, dtime(12, 0))
        self.assertFalse(in_trading_session(lunch, buffer_minutes=0))

    def test_sessions_const_shape(self):
        self.assertEqual(A_SHARE_SESSIONS[0][0], dtime(9, 30))
        self.assertEqual(A_SHARE_SESSIONS[1][1], dtime(15, 0))


# ===========================================================================
# 任务配置
# ===========================================================================
class TestJob(unittest.TestCase):
    def test_symbol_normalized(self):
        # 用户从行情软件复制过来常带前缀
        j = CollectJob(symbol="sh600000")
        self.assertEqual(j.symbol, "600000")

    def test_key_is_symbol_period(self):
        self.assertEqual(CollectJob(symbol="000001", period="5").key, "000001:5")

    def test_period_normalized_in_post_init(self):
        self.assertEqual(CollectJob(symbol="000001", period="5min").period, "5")

    def test_to_dict_has_human_interval(self):
        d = CollectJob(symbol="000001", interval="5m").to_dict()
        self.assertEqual(d["interval"], 300.0)
        self.assertEqual(d["interval_human"], "5m")

    def test_build_job_from_bare_string(self):
        j = build_job("000001", {"interval": "10m", "period": "1d"})
        self.assertIsNotNone(j)
        self.assertEqual(j.symbol, "000001")
        self.assertEqual(j.interval, 600.0)

    def test_build_job_from_dict_uses_code_alias(self):
        j = build_job({"code": "600519", "interval": "1m"}, {})
        self.assertIsNotNone(j)
        self.assertEqual(j.symbol, "600519")
        self.assertEqual(j.interval, 60.0)

    def test_build_job_entry_without_symbol_is_dropped(self):
        self.assertIsNone(build_job({"interval": "1m"}, {}))

    def test_build_job_bad_type_is_dropped(self):
        self.assertIsNone(build_job(12345, {}))

    def test_build_job_unknown_field_is_dropped(self):
        # dataclass 不吃陌生字段，过滤掉比抛异常友好
        j = build_job({"symbol": "000001", "nonsense": 1}, {})
        self.assertIsNotNone(j)


class TestShouldRun(unittest.TestCase):
    def test_disabled_spec_blocks_all(self):
        spec = CollectorSpec(enabled=False, jobs=[CollectJob("000001")])
        self.assertFalse(spec.should_run(spec.jobs[0]))

    def test_disabled_job_blocked(self):
        job = CollectJob("000001", enabled=False)
        self.assertFalse(CollectorSpec(jobs=[job]).should_run(job))

    def test_daily_runs_anytime_by_default(self):
        """日线默认不限时段：一天只变一次，夜间也能补到收盘价。
        若也限交易时段，周末两天的数据窗口就会被错过。"""
        spec = CollectorSpec(jobs=[CollectJob("000001", period="1d")])
        sunday_night = datetime(2026, 10, 11, 23, 30)
        self.assertTrue(spec.should_run(spec.jobs[0], now=sunday_night))

    def test_intraday_respects_session_when_auto(self):
        spec = CollectorSpec(jobs=[CollectJob("000001", period="5")])
        job = spec.jobs[0]
        self.assertTrue(spec.should_run(job, now=datetime(2026, 10, 9, 10, 30)))
        self.assertFalse(spec.should_run(job, now=datetime(2026, 10, 11, 10, 30)))

    def test_explicit_override(self):
        spec = CollectorSpec(
            trading_hours_only=True,
            jobs=[CollectJob("000001", period="1d")],
        )
        self.assertFalse(spec.should_run(spec.jobs[0], now=datetime(2026, 10, 11, 23, 0)))

        spec.jobs = [CollectJob("000001", period="5")]
        spec.trading_hours_only = False
        self.assertTrue(spec.should_run(spec.jobs[0], now=datetime(2026, 10, 11, 23, 0)))


# ===========================================================================
# 配置加载
# ===========================================================================
class TestLoadSpec(TempWorkspaceMixin, unittest.TestCase):
    def _write(self, obj) -> str:
        p = self.workdir() / "collector.json"
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_missing_file_still_builds(self):
        spec = load_collector_spec(path=str(self.workdir() / "nope.json"))
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.jobs, [])

    def test_broken_json_falls_back_to_defaults(self):
        p = self.workdir() / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        spec = load_collector_spec(path=str(p))
        self.assertEqual(spec.jobs, [])
        self.assertTrue(spec.enabled)

    def test_file_symbols_and_cli_symbols_are_merged(self):
        """文件里配的是长期策略（含每个标的自己的频率），显式传入的是"这批也要采"。
        两边合并，谁也不被静默丢弃。"""
        p = self._write({"symbols": ["000001"]})
        spec = load_collector_spec(path=p, symbols=["600000"])
        self.assertEqual([j.symbol for j in spec.jobs], ["000001", "600000"])

    def test_duplicate_symbol_period_not_duplicated(self):
        """同一标的同一粒度重复出现在两处只保留一份——否则会被采集两遍，
        白白翻倍消耗数据源配额。"""
        p = self._write({"symbols": [{"symbol": "000001", "interval": "1m"}]})
        spec = load_collector_spec(path=p, symbols=["000001"])
        self.assertEqual(len(spec.jobs), 1)
        self.assertEqual(spec.jobs[0].interval, 60.0)  # 文件的定制频率没被冲掉

    def test_cli_symbols_used_when_file_has_none(self):
        p = self._write({"interval": "10m"})
        spec = load_collector_spec(path=p, symbols=["600000", "000001"])
        self.assertEqual([j.symbol for j in spec.jobs], ["600000", "000001"])
        self.assertEqual(spec.jobs[0].interval, 600.0)

    def test_per_job_interval_overrides_global(self):
        p = self._write({
            "interval": "30m",
            "symbols": ["000001", {"symbol": "600000", "interval": "1m"}],
        })
        spec = load_collector_spec(path=p)
        self.assertEqual(spec.jobs[0].interval, 1800.0)
        self.assertEqual(spec.jobs[1].interval, 60.0)

    def test_min_interval_clamps(self):
        p = self._write({
            "min_interval": 60,
            "symbols": [{"symbol": "000001", "interval": "1s"}],
        })
        spec = load_collector_spec(path=p)
        self.assertEqual(spec.jobs[0].interval, 60.0)

    def test_disabled_via_file(self):
        p = self._write({"enabled": False, "symbols": ["000001"]})
        self.assertFalse(load_collector_spec(path=p).enabled)

    def test_enabled_flag_overrides_file(self):
        p = self._write({"enabled": False, "symbols": ["000001"]})
        self.assertTrue(load_collector_spec(path=p, enabled=True).enabled)

    def test_holidays_carried(self):
        p = self._write({"holidays": ["2026-10-09"], "symbols": ["000001"]})
        spec = load_collector_spec(path=p)
        self.assertEqual(spec.holidays, ["2026-10-09"])
        self.assertFalse(is_trading_day(datetime(2026, 10, 9), spec.holidays))


# ===========================================================================
# 采集组件
# ===========================================================================
def _ctx() -> EngineContext:
    return EngineContext(
        run_id="collect-test",
        run_mode=RunMode.SIMULATE,
        engine_status=EngineStatus.RUNNING,
    )


class _StubCollector(DataCollectorComponent):
    """把真正落库的动作换成计数器，验证调度/统计链路本身是否正确。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls: list[str] = []
        self.fail_for: set = set()

    def _collect_one(self, job) -> int:
        self.calls.append(job.key)
        if job.key in self.fail_for:
            raise RuntimeError("boom")
        return 42

    # 建表需要连真实数据库，测试里跳过
    def _ensure_tables(self) -> None:
        return None


def _build_collector(tmp: Path, content: str = "", **kw) -> _StubCollector:
    """造一个采集组件。**永远挂在临时配置文件上**，否则会读到工程里的
    ``config/collector.json``——那个文件的标的会混进用例的断言里。

    已经给了 ``config_path`` 时不要覆写文件：调用方自己管着那个文件，
    覆写会把它的内容变成 ``{}``。
    """
    if "config_path" not in kw:
        p = tmp / "collector.json"
        p.write_text(content if content.strip() else "{}", encoding="utf-8")
        kw["config_path"] = str(p)
    elif content:
        Path(kw["config_path"]).write_text(content, encoding="utf-8")
    comp = _StubCollector(**kw)
    comp.initialize(_ctx(), EventBus(), {})
    return comp


class TestCollectorComponent(TempWorkspaceMixin, unittest.TestCase):
    def test_runs_each_job_once_per_bucket(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        comp._run_bucket(list(comp.spec.jobs))
        self.assertEqual(sorted(comp.calls), ["000001:1d", "600000:1d"])
        self.assertEqual(comp.stats["rows"], 84)
        self.assertEqual(comp.stats["runs"], 2)
        self.assertEqual(comp.stats["errors"], 0)

    def test_failure_does_not_stop_siblings(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        comp.fail_for = {"000001:1d"}
        comp._run_bucket(list(comp.spec.jobs))
        self.assertEqual(comp.stats["errors"], 1)
        self.assertEqual(comp.stats["runs"], 2)
        self.assertIn("600000:1d", comp.calls)
        st = comp._job_state["000001:1d"]
        self.assertEqual(st["errors"], 1)
        self.assertIn("boom", st["last_error"])

    def test_out_of_session_jobs_skipped(self):
        content = json.dumps({
            "trading_hours_only": True,
            "symbols": [{"symbol": "000001", "period": "5"}],
        })
        comp = _build_collector(self.workdir(), content)
        # 周日 23:00，肯定不在交易时段
        from datetime import datetime as _dt
        original = comp.spec.should_run

        def _fake_should_run(job, now=None):
            return original(job, now=_dt(2026, 10, 11, 23, 0))

        comp.spec.should_run = _fake_should_run  # type: ignore[assignment]
        comp._run_bucket(list(comp.spec.jobs))
        self.assertEqual(comp.calls, [])
        self.assertEqual(comp.stats["out_of_session"], 1)

    def test_no_timers_before_start(self):
        """起动前必须没有定时器。否则 on_start 再按桶起一轮，
        同一个频率会变成两个定时器 —— 采集频率翻倍。"""
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        self.assertEqual(comp._loops, {})
        comp.set_interval("10m")
        self.assertEqual(comp._loops, {})
        comp.add_symbol("600000", interval="1m")
        self.assertEqual(comp._loops, {})

    def test_collected_event_published(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001"], interval="5m"
        )
        bus = comp.event_bus
        seen: list = []
        bus.subscribe(
            StandardEvents.DATA_COLLECTED,
            lambda symbol=None, period=None, rows=None, **kw: seen.append(
                (symbol, period, rows)
            ),
        )
        comp._run_bucket(list(comp.spec.jobs))
        self.assertEqual(seen, [("000001", "1d", 42)])

    def test_set_interval_runtime(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        n = comp.set_interval("1m", symbol="600000")
        self.assertEqual(n, 1)
        self.assertEqual(comp.spec.jobs[1].interval, 60.0)
        self.assertEqual(comp.spec.jobs[0].interval, 300.0)

    def test_set_interval_all_periods(self):
        content = json.dumps({"symbols": [
            {"symbol": "000001", "period": "1d", "interval": "5m"},
            {"symbol": "000001", "period": "5", "interval": "5m"},
        ]})
        comp = _build_collector(self.workdir(), content)
        n = comp.set_interval("10m", symbol="000001", period="5")
        self.assertEqual(n, 1)
        self.assertEqual(comp.spec.jobs[0].interval, 300.0)
        self.assertEqual(comp.spec.jobs[1].interval, 600.0)

    def test_set_interval_respects_floor(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001"], interval="5m", min_interval=120
        )
        comp.set_interval("1s", symbol="000001")
        self.assertEqual(comp.spec.jobs[0].interval, 120.0)

    def test_add_symbol_idempotent(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        before = len(comp.spec.jobs)
        comp.add_symbol("000001")
        self.assertEqual(len(comp.spec.jobs), before)
        comp.add_symbol("600000", period="5", interval="1m")
        self.assertEqual(len(comp.spec.jobs), before + 1)
        self.assertEqual(comp.spec.jobs[-1].period, "5")

    def test_remove_symbol(self):
        content = json.dumps({"symbols": [
            {"symbol": "000001", "period": "1d"},
            {"symbol": "000001", "period": "5"},
        ]})
        comp = _build_collector(self.workdir(), content)
        self.assertEqual(comp.remove_symbol("sh000001", period="5"), 1)
        self.assertEqual([j.period for j in comp.spec.jobs], ["1d"])
        self.assertEqual(comp.remove_symbol("000001"), 1)
        self.assertEqual(comp.spec.jobs, [])

    def test_snapshot_shape(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        snap = comp.snapshot()
        for key in ("enabled", "jobs", "state", "stats", "buckets", "min_interval"):
            self.assertIn(key, snap)
        self.assertEqual(len(snap["jobs"]), 1)
        self.assertEqual(snap["jobs"][0]["symbol"], "000001")

    def test_is_busy_false_without_loop(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        self.assertFalse(comp.is_busy())

    def test_health_when_all_failing(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        for j in comp.spec.jobs:
            comp._record_failure(j, RuntimeError("x"))
        ok, msg = comp.health_check()
        self.assertFalse(ok)
        self.assertIn("2", msg)

    def test_health_partial_failure_still_healthy(self):
        """一个标的失败（停牌/退市）不该让整个服务判定为不健康。"""
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        comp._record_failure(comp.spec.jobs[0], RuntimeError("x"))
        ok, _ = comp.health_check()
        self.assertTrue(ok)

    def test_reload_config_picks_up_new_symbols(self):
        path = self.workdir() / "collector.json"
        path.write_text(json.dumps({"symbols": ["000001"]}), encoding="utf-8")
        comp = _build_collector(self.workdir(), config_path=str(path))
        self.assertEqual(len(comp.spec.jobs), 1)
        path.write_text(
            json.dumps({"symbols": ["000001", "600000", "000858"]}), encoding="utf-8"
        )
        comp.reload_config()
        self.assertEqual(len(comp.spec.jobs), 3)

    def test_forced_run_ignores_session(self):
        content = json.dumps({
            "trading_hours_only": True,
            "symbols": [{"symbol": "000001", "period": "5"}],
        })
        comp = _build_collector(self.workdir(), content)
        from datetime import datetime as _dt
        original = comp.spec.should_run
        comp.spec.should_run = (  # type: ignore[assignment]
            lambda job, now=None: original(job, now=_dt(2026, 10, 11, 23, 0))
        )
        # _run_forced 不看 should_run，用户点了"立即采集"就是要立刻看到结果
        r = comp._run_forced(list(comp.spec.jobs))
        self.assertEqual(r["rows"], 42)
        self.assertEqual(r["errors"], [])


class TestParseInterval(unittest.TestCase):
    def test_clamps_to_floor(self):
        self.assertEqual(parse_interval("1s", 60.0), 60.0)
        self.assertEqual(parse_interval(1, 60.0), 60.0)

    def test_keeps_valid(self):
        self.assertEqual(parse_interval("5m", 15.0), 300.0)

    def test_none_uses_floor(self):
        self.assertEqual(parse_interval(None, 15.0), 15.0)


if __name__ == "__main__":
    unittest.main()
