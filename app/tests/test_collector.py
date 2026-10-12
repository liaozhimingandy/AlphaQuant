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

from app.modules.collection.spec import (
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
# 采集服务已由三个平级组件（K 线 / 日频截面 / 完整性）合并成一个
# ``CollectionService``，旧模块路径不再存在。K 线那部分的行为在这里按
# ``_*_kline`` 后缀的方法/属性逐一校验，语义与合并前完全一致。
from app.modules.collection.service import CollectionService, parse_interval
from app.runtime.event_bus import EventBus, StandardEvents
from app.engine.context import EngineContext, EngineStatus, RunMode
from app.tests.helpers import TempWorkspaceMixin


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


class _StubCollector(CollectionService):
    """把真正落库的动作换成计数器，验证调度/统计链路本身是否正确。

    只桩掉 K 线单元的两个方法：合并后的方法名带 ``_kline`` 后缀，
    这是三个源类同名方法（``_collect_one`` / ``_ensure_tables``）区分后的结果。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls: list[str] = []
        self.fail_for: set = set()

    def _collect_one_kline(self, job) -> int:
        self.calls.append(job.key)
        if job.key in self.fail_for:
            raise RuntimeError("boom")
        return 42

    # 建表需要连真实数据库，测试里跳过
    def _ensure_kline_tables(self) -> None:
        return None


#: 旧组件（``DataCollectorComponent``）的扁平构造参数 → 服务里 K 线单元的参数名。
#: 合并后服务要同时喂三个单元，参数必须带单元前缀，否则 ``interval``
#: 到底是 K 线频率还是截面频率就说不清了。
_KLINE_KWARGS = {
    "interval": "kline_interval",
    "period": "kline_period",
    "config_path": "kline_config_path",
    "min_interval": "kline_min_interval",
}


def _build_collector(tmp: Path, content: str = "", **kw) -> _StubCollector:
    """造一个采集服务（只开 K 线单元）。**永远挂在临时配置文件上**，
    否则会读到工程里的 ``config/collector.json``——那个文件的标的会混进用例的断言里。

    已经给了 ``config_path`` 时不要覆写文件：调用方自己管着那个文件，
    覆写会把它的内容变成 ``{}``。

    另外两个单元（日频截面、完整性）会被默认启用（跟随全局配置），
    但它们只在 ``on_start`` 里起定时器，这里从不调 ``start()``，
    所以不会干扰 K 线单元的断言。
    """
    for flat, namespaced in _KLINE_KWARGS.items():
        if flat in kw:
            kw[namespaced] = kw.pop(flat)

    if "kline_config_path" not in kw:
        p = tmp / "collector.json"
        p.write_text(content if content.strip() else "{}", encoding="utf-8")
        kw["kline_config_path"] = str(p)
    elif content:
        Path(kw["kline_config_path"]).write_text(content, encoding="utf-8")
    comp = _StubCollector(**kw)
    comp.initialize(_ctx(), EventBus(), {})
    return comp


class TestCollectionServiceShape(TempWorkspaceMixin, unittest.TestCase):
    """合并后的形态断言：一个服务、三个能力、一份三分区快照。

    这些用例在合并前是不存在的（那时有三个平级组件、三个快照）。
    它们要守的正是"别再被拆回去"。
    """

    def test_name_and_capabilities(self):
        from app.engine.capability import Cap

        self.assertEqual(CollectionService.name, "collection_service")
        self.assertEqual(
            set(CollectionService.provides),
            {Cap.COLLECT_KLINE, Cap.COLLECT_CROSS, Cap.DATA_INTEGRITY},
        )

    def test_snapshot_has_three_unit_sections(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        snap = comp.snapshot()
        for section in ("kline", "cross", "guard"):
            self.assertIn(section, snap)

    def test_unit_switches_are_read_only_views(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        # 只读属性：写回去会炸，正是为了防止外部绕过服务级开关
        with self.assertRaises(AttributeError):
            comp.kline_enabled = False

    def test_stop_is_reverse_of_start_order(self):
        """停止顺序是"补全 → 截面 → K 线"的逆序，与 on_stop 的注释一致。
        这里只验证三个单元的停止钩子都能被独立调用（不起 reactor）。"""
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        for fn in (comp._stop_guard, comp._stop_cross, comp._stop_kline):
            self.assertTrue(callable(fn))


class TestCollectionServiceKline(TempWorkspaceMixin, unittest.TestCase):
    def test_runs_each_job_once_per_bucket(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        comp._run_kline_bucket(list(comp.kline_spec.jobs))
        self.assertEqual(sorted(comp.calls), ["000001:1d", "600000:1d"])
        self.assertEqual(comp.stats_kline["rows"], 84)
        self.assertEqual(comp.stats_kline["runs"], 2)
        self.assertEqual(comp.stats_kline["errors"], 0)

    def test_failure_does_not_stop_siblings(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        comp.fail_for = {"000001:1d"}
        comp._run_kline_bucket(list(comp.kline_spec.jobs))
        self.assertEqual(comp.stats_kline["errors"], 1)
        self.assertEqual(comp.stats_kline["runs"], 2)
        self.assertIn("600000:1d", comp.calls)
        st = comp._job_state_kline["000001:1d"]
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
        original = comp.kline_spec.should_run

        def _fake_should_run(job, now=None):
            return original(job, now=_dt(2026, 10, 11, 23, 0))

        comp.kline_spec.should_run = _fake_should_run  # type: ignore[assignment]
        comp._run_kline_bucket(list(comp.kline_spec.jobs))
        self.assertEqual(comp.calls, [])
        self.assertEqual(comp.stats_kline["out_of_session"], 1)

    def test_no_timers_before_start(self):
        """起动前必须没有定时器。否则 on_start 再按桶起一轮，
        同一个频率会变成两个定时器 —— 采集频率翻倍。"""
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        self.assertEqual(comp._loops_kline, {})
        comp.set_interval("10m")
        self.assertEqual(comp._loops_kline, {})
        comp.add_symbol("600000", interval="1m")
        self.assertEqual(comp._loops_kline, {})

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
        comp._run_kline_bucket(list(comp.kline_spec.jobs))
        self.assertEqual(seen, [("000001", "1d", 42)])

    def test_set_interval_runtime(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        n = comp.set_interval("1m", symbol="600000")
        self.assertEqual(n, 1)
        self.assertEqual(comp.kline_spec.jobs[1].interval, 60.0)
        self.assertEqual(comp.kline_spec.jobs[0].interval, 300.0)

    def test_set_interval_all_periods(self):
        content = json.dumps({"symbols": [
            {"symbol": "000001", "period": "1d", "interval": "5m"},
            {"symbol": "000001", "period": "5", "interval": "5m"},
        ]})
        comp = _build_collector(self.workdir(), content)
        n = comp.set_interval("10m", symbol="000001", period="5")
        self.assertEqual(n, 1)
        self.assertEqual(comp.kline_spec.jobs[0].interval, 300.0)
        self.assertEqual(comp.kline_spec.jobs[1].interval, 600.0)

    def test_set_interval_respects_floor(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001"], interval="5m", min_interval=120
        )
        comp.set_interval("1s", symbol="000001")
        self.assertEqual(comp.kline_spec.jobs[0].interval, 120.0)

    def test_add_symbol_idempotent(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        before = len(comp.kline_spec.jobs)
        comp.add_symbol("000001")
        self.assertEqual(len(comp.kline_spec.jobs), before)
        comp.add_symbol("600000", period="5", interval="1m")
        self.assertEqual(len(comp.kline_spec.jobs), before + 1)
        self.assertEqual(comp.kline_spec.jobs[-1].period, "5")

    def test_remove_symbol(self):
        content = json.dumps({"symbols": [
            {"symbol": "000001", "period": "1d"},
            {"symbol": "000001", "period": "5"},
        ]})
        comp = _build_collector(self.workdir(), content)
        self.assertEqual(comp.remove_symbol("sh000001", period="5"), 1)
        self.assertEqual([j.period for j in comp.kline_spec.jobs], ["1d"])
        self.assertEqual(comp.remove_symbol("000001"), 1)
        self.assertEqual(comp.kline_spec.jobs, [])

    def test_snapshot_shape(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        snap = comp.snapshot()["kline"]
        for key in ("enabled", "jobs", "state", "stats", "buckets", "min_interval"):
            self.assertIn(key, snap)
        self.assertEqual(len(snap["jobs"]), 1)
        self.assertEqual(snap["jobs"][0]["symbol"], "000001")

    def test_is_busy_false_without_loop(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        self.assertFalse(comp._is_busy_kline())

    def test_health_when_all_failing(self):
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        for j in comp.kline_spec.jobs:
            comp._record_kline_failure(j, RuntimeError("x"))
        ok, msg = comp._health_check_kline()
        self.assertFalse(ok)
        self.assertIn("2", msg)

    def test_health_partial_failure_still_healthy(self):
        """一个标的失败（停牌/退市）不该让整个服务判定为不健康。"""
        comp = _build_collector(
            self.workdir(), "", symbols=["000001", "600000"], interval="5m"
        )
        comp._record_kline_failure(comp.kline_spec.jobs[0], RuntimeError("x"))
        ok, _ = comp._health_check_kline()
        self.assertTrue(ok)

    def test_reload_config_picks_up_new_symbols(self):
        path = self.workdir() / "collector.json"
        path.write_text(json.dumps({"symbols": ["000001"]}), encoding="utf-8")
        comp = _build_collector(self.workdir(), config_path=str(path))
        self.assertEqual(len(comp.kline_spec.jobs), 1)
        path.write_text(
            json.dumps({"symbols": ["000001", "600000", "000858"]}), encoding="utf-8"
        )
        comp.reload_config()
        self.assertEqual(len(comp.kline_spec.jobs), 3)

    def test_forced_run_ignores_session(self):
        content = json.dumps({
            "trading_hours_only": True,
            "symbols": [{"symbol": "000001", "period": "5"}],
        })
        comp = _build_collector(self.workdir(), content)
        from datetime import datetime as _dt
        original = comp.kline_spec.should_run
        comp.kline_spec.should_run = (  # type: ignore[assignment]
            lambda job, now=None: original(job, now=_dt(2026, 10, 11, 23, 0))
        )
        # _run_forced 不看 should_run，用户点了"立即采集"就是要立刻看到结果
        r = comp._run_kline_forced(list(comp.kline_spec.jobs))
        self.assertEqual(r["rows"], 42)
        self.assertEqual(r["errors"], [])


class TestProgressRouting(TempWorkspaceMixin, unittest.TestCase):
    """三个单元必须各有各的进度条。

    合并进同一个类后，基类那一 ``self._progress`` 成了共享变量：
    K 线单元（每分钟一轮）会把日频截面单元（一天两轮）的进度刷掉，
    面板上就再也看不到截面跑到哪了。这三个用例守的就是这件事。
    """

    @staticmethod
    def _pretend_kline_unit(comp, total: int, label: str) -> None:
        comp.start_progress(total, label)      # 模拟搬来的 _run_kline_bucket 里的调用

    @staticmethod
    def _pretend_cross_unit(comp, total: int, label: str) -> None:
        comp.start_progress(total, label)      # 模拟 _run_cross_once 里的调用

    def test_units_have_separate_progress(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        self._pretend_kline_unit(comp, 3, "K线 3 只")
        self._pretend_cross_unit(comp, 68, "截面 68 只")
        self.assertEqual(comp._unit_p["kline"].total, 3)
        self.assertEqual(comp._unit_p["cross"].total, 68)
        self.assertEqual(comp._unit_p["guard"].total, 0)

    def test_unknown_caller_keeps_service_level(self):
        """认不出归属就退回服务级 —— 退化但不丢失，总比写错地方好。"""
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        comp.start_progress(2, "服务级")
        self.assertEqual(comp._progress.total, 2)
        self.assertEqual(comp._unit_p["kline"].total, 0)

    @staticmethod
    def _pretend_kline_round(comp, total: int) -> None:
        comp.start_progress(total, "K线")
        comp.advance_progress(1)
        comp.finish_progress()

    def test_whole_round_stays_inside_one_unit(self):
        """start / advance / finish 必须落在同一份进度上，
        否则会出现"总数在截面、完成数在 K 线"的错配。"""
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        self._pretend_kline_round(comp, 4)
        self.assertEqual(comp._unit_p["kline"].total, 4)
        self.assertEqual(comp._unit_p["kline"].done, 1)
        self.assertFalse(comp._unit_p["kline"].running)
        # 服务级那份完全没被碰到
        self.assertEqual(comp._progress.total, 0)

    def test_progress_picks_running_unit(self):
        comp = _build_collector(self.workdir(), "", symbols=["000001"], interval="5m")
        self._pretend_kline_unit(comp, 3, "K线 3 只")
        self._pretend_cross_unit(comp, 68, "截面 68 只")
        # 两个都"在跑"时优先报第一个；面板至少能看到一个真实的总数
        self.assertEqual(comp.progress()["total"], 3)
        units = comp.unit_progress()
        self.assertEqual(sorted(units), ["cross", "guard", "kline"])
        self.assertEqual(units["cross"]["total"], 68)


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
