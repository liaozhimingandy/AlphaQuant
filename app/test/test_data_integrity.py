#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_data_integrity.py
# @Description : 数据完整性校验 + data_guard 组件
#
#                这组测试守的是一句话：**库里少一天数据，系统不许沉默**。
#
#                校验器最容易写错的三处，这里都覆盖了：
#                  1. 右端点取"今天"而不是"最近交易日" → 周末凭空造出假缺口；
#                  2. 停牌票的长期缺失被当成漏采 → 每轮去联网补，白打数据源；
#                  3. 补回来的数据没按缺失日过滤 → 把区间外的旧行重复写一遍。
#
#                另一个重点是"坏数据"：high<low / 价格非正这类行
#                不会体现在覆盖度统计里，只能逐行查。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.market.calendar import TradeCalendar
from app.db.database import apply_sqlite_pragmas
from app.db.models import Base
from app.repository import stock_repository as stock_repo
from app.test.helpers import reap_async


def _recent_days(cal: TradeCalendar, n: int = 6) -> list[date]:
    """最近 n 个交易日（升序）。

    校验窗口是**锚到最近交易日**的（不会把周末当成缺口），
    所以测试数据必须落在窗口内 —— 用 2024 年的固定日期会全部落在窗口外，
    断言自然全是 0，而那种"测试通过"毫无意义。
    """
    days = cal.recent_trading_days(n)
    return sorted(days)


def _seed(db, symbol: str, days: list[date], close: float = 10.0) -> None:
    rows = [{
        "symbol": symbol, "date": d,
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": 1000.0, "amount": close * 1000,
    } for d in days]
    stock_repo.StockRepository.batch_upsert(db, rows, commit=False)


class _DbMixin:
    """临时 SQLite（生产同款 PRAGMA），并把仓储的 SessionLocal 指过去。"""

    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.engine = apply_sqlite_pragmas(
            create_engine(f"sqlite:///{Path(self._td.name) / 't.db'}")
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False,
                                    autoflush=False)
        self._orig_session = stock_repo.SessionLocal
        stock_repo.SessionLocal = self.Session

    def tearDown(self):
        stock_repo.SessionLocal = self._orig_session
        self.engine.dispose()
        import tempfile

        reap_async(Path(self._td.name))
        self._td.cleanup()


class TestGapDetection(_DbMixin, unittest.TestCase):
    """缺哪几天，能不能算对。"""

    def _checker(self, **kw):
        from app.core.data.integrity import DataIntegrityChecker

        return DataIntegrityChecker(db=None, **kw)

    def test_detects_missing_trading_days(self):
        """中间缺了一天 → 必须被找出来（这是本模块存在的全部理由）。"""
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 6)
        hole = days[2]

        with stock_repo.session_scope(None) as db:
            _seed(db, "000001", [d for d in days if d != hole])

        checker = self._checker(calendar=cal, lookback_days=30, auto_repair=False)
        rep = checker.check_symbol("000001")

        self.assertIn(hole, rep.missing)
        self.assertNotIn(days[0], rep.missing)
        self.assertEqual(len(rep.missing), 1)

    def test_weekend_is_not_a_gap(self):
        """周末/节假日不算缺口 —— 否则每周一都会凭空报两天缺失。"""
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 5)
        with stock_repo.session_scope(None) as db:
            _seed(db, "000001", days)
        checker = self._checker(calendar=cal, lookback_days=10, auto_repair=False)
        rep = checker.check_symbol("000001")
        for d in rep.missing:
            self.assertTrue(cal.is_trading_day(d), f"{d} 不是交易日却算成了缺口")

    def test_window_end_is_last_trading_day_not_today(self):
        """右端点锚到最近交易日 —— 否则周末/节假日会凭空造出一堆假缺口。"""
        cal = TradeCalendar(source="test")
        checker = self._checker(calendar=cal, lookback_days=10)
        start, end = checker.window({"start": "2020-01-01", "end": "2020-01-01"})
        self.assertEqual(end, cal.last_trading_day())
        self.assertTrue(cal.is_trading_day(end))

    def test_suspended_stock_is_not_treated_as_gap(self):
        """长期停牌：缺失占比过高 → 判定停牌，不补。

        否则每轮体检都会去联网补一只停牌票，白打数据源还容易被限流。
        """
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 10)
        with stock_repo.session_scope(None) as db:
            _seed(db, "600002", [days[0]])     # 只采到最早的一天

        checker = self._checker(calendar=cal, lookback_days=30, auto_repair=False)
        rep = checker.check_symbol("600002")
        self.assertTrue(rep.suspended)
        self.assertEqual(rep.repaired, 0)

    def test_no_data_symbol_reports_error(self):
        checker = self._checker(lookback_days=10, auto_repair=False)
        rep = checker.check_symbol("999999")
        self.assertIn("无任何日线数据", rep.error)

    def test_complete_data_is_ok(self):
        """数据完整时必须是 ok —— 别把正常情况报成问题。"""
        cal = TradeCalendar(source="test")
        end = cal.last_trading_day()
        days = [end - timedelta(days=i) for i in range(40)]
        days = [d for d in days if cal.is_trading_day(d)][:10]

        with stock_repo.session_scope(None) as db:
            _seed(db, "000001", days)

        checker = self._checker(calendar=cal, lookback_days=10, auto_repair=False)
        rep = checker.check_symbol("000001")
        self.assertEqual(rep.missing, [])
        self.assertTrue(rep.ok)


class TestAnomalyDetection(_DbMixin, unittest.TestCase):
    """坏数据（不是缺数据）：high<low、价格非正。"""

    def test_high_lower_than_low(self):
        cal = TradeCalendar(source="test")
        day = _recent_days(cal, 3)[-1]
        with stock_repo.session_scope(None) as db:
            stock_repo.StockRepository.batch_upsert(db, [{
                "symbol": "000001", "date": day,
                "open": 10.0, "high": 9.0, "low": 11.0, "close": 10.0,
                "volume": 1.0, "amount": 10.0,
            }], commit=False)

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=7,
                                       auto_repair=False)
        rep = checker.check_symbol("000001")
        self.assertEqual(len(rep.anomalies), 1)
        self.assertIn("最高价 < 最低价", rep.anomalies[0]["reason"])
        self.assertFalse(rep.ok)

    def test_non_positive_close(self):
        cal = TradeCalendar(source="test")
        day = _recent_days(cal, 3)[-1]
        with stock_repo.session_scope(None) as db:
            stock_repo.StockRepository.batch_upsert(db, [{
                "symbol": "000002", "date": day,
                "open": -1.0, "high": -1.0, "low": -2.0, "close": 0.0,
                "volume": 1.0, "amount": 1.0,
            }], commit=False)

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=7,
                                       auto_repair=False)
        rep = checker.check_symbol("000002")
        self.assertTrue(any("非正" in a["reason"] for a in rep.anomalies))


class TestAutoRepair(_DbMixin, unittest.TestCase):
    """缺口自动补全。"""

    def test_repair_fills_missing_days(self):
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 4)
        hole = days[1]
        with stock_repo.session_scope(None) as db:
            _seed(db, "000001", [d for d in days if d != hole])

        # 数据源返回完整区间（含缺失的那天）
        fetched = pd.DataFrame([{
            "date": d, "symbol": "000001", "open": 10.0, "high": 10.1,
            "low": 9.9, "close": 10.05, "volume": 100.0, "amount": 1005.0,
        } for d in days])

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=30,
                                       auto_repair=True)
        with mock.patch("app.data.kline_sources.fetch_daily",
                        return_value=fetched):
            rep = checker.check_symbol("000001")

        self.assertGreaterEqual(rep.repaired, 1)
        self.assertNotIn(hole, rep.missing)

        with stock_repo.session_scope(None) as db:
            cov = stock_repo.StockRepository.get_coverage(db, "000001")
        self.assertEqual(cov["rows"], len(days))

    def test_repair_only_writes_missing_days(self):
        """补全必须按缺失日过滤 —— 不能把区间外的旧行重复写一遍。"""
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 4)
        with stock_repo.session_scope(None) as db:
            _seed(db, "000003", [days[0]])

        outside = days[0] - timedelta(days=400)
        fetched = pd.DataFrame([{
            "date": d, "symbol": "000003", "open": 10.0, "high": 10.1,
            "low": 9.9, "close": 10.05, "volume": 100.0, "amount": 1005.0,
        } for d in (outside, days[1])])

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=30,
                                       auto_repair=True)
        with mock.patch("app.data.kline_sources.fetch_daily",
                        return_value=fetched):
            checker.check_symbol("000003")

        with stock_repo.session_scope(None) as db:
            have = {r[0] for r in db.query(
                stock_repo.StockDaily.date).filter(
                    stock_repo.StockDaily.symbol == "000003").all()}
        self.assertNotIn(outside, have)     # 区间外的不该被写进去
        self.assertIn(days[1], have)

    def test_repair_failure_is_recorded_not_raised(self):
        """补全失败要记在报告里，不能让整轮体检挂掉。"""
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 4)
        with stock_repo.session_scope(None) as db:
            _seed(db, "000004", [days[0]])

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=30,
                                       auto_repair=True)
        with mock.patch("app.data.kline_sources.fetch_daily",
                        side_effect=RuntimeError("所有源都挂了")):
            rep = checker.check_symbol("000004")
        self.assertIn("补全失败", rep.error)
        self.assertEqual(rep.repaired, 0)

    def test_no_repair_when_disabled(self):
        """auto_repair=False = 只体检不联网。"""
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 4)
        with stock_repo.session_scope(None) as db:
            _seed(db, "000005", [days[0]])

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=7,
                                       auto_repair=False)
        with mock.patch("app.data.kline_sources.fetch_daily",
                        side_effect=AssertionError("不该联网")):
            rep = checker.check_symbol("000005")
        self.assertEqual(rep.repaired, 0)
        self.assertTrue(rep.missing)


class TestScan(_DbMixin, unittest.TestCase):
    """批量扫描与汇总。"""

    def test_scan_report(self):
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 5)

        with stock_repo.session_scope(None) as db:
            _seed(db, "000001", days)
            _seed(db, "000002", days[:-1])     # 少一天

        from app.core.data.integrity import DataIntegrityChecker, summarize

        checker = DataIntegrityChecker(calendar=cal, lookback_days=10,
                                       auto_repair=False, max_symbols=10)
        report = checker.scan()
        self.assertEqual(report.checked, 2)
        self.assertEqual(report.missing_total, 1)
        self.assertIn("缺口 1 天", summarize(report))
        self.assertIn("daily", report.tables)

    def test_scan_respects_max_symbols(self):
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 3)
        with stock_repo.session_scope(None) as db:
            for sym in ("000001", "000002", "000003"):
                _seed(db, sym, days)

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=3,
                                       auto_repair=False, max_symbols=2)
        report = checker.scan()
        self.assertEqual(report.checked, 2)

    def test_scan_with_explicit_symbols(self):
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 3)
        with stock_repo.session_scope(None) as db:
            _seed(db, "000001", days)
            _seed(db, "000002", days)

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=3,
                                       auto_repair=False)
        report = checker.scan(["000002"])
        self.assertEqual([s.symbol for s in report.symbols], ["000002"])

    def test_max_repair_limits_requests(self):
        """单次补采有上限：宁可下一轮再补，也不要启动瞬间打出几百个请求。"""
        cal = TradeCalendar(source="test")
        days = _recent_days(cal, 6)

        with stock_repo.session_scope(None) as db:
            for sym in ("000001", "000002", "000003"):
                _seed(db, sym, days[:-1])     # 每只都缺一天

        fetched = pd.DataFrame([{
            "date": days[-1], "symbol": "000001", "open": 1.0, "high": 1.1,
            "low": 0.9, "close": 1.0, "volume": 1.0, "amount": 1.0,
        }])

        from app.core.data.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(calendar=cal, lookback_days=10,
                                       auto_repair=True, max_repair=1)
        with mock.patch("app.data.kline_sources.fetch_daily",
                        return_value=fetched) as fn:
            checker.scan()
        self.assertEqual(fn.call_count, 1)


class TestDataGuardComponent(unittest.TestCase):
    """组件生命周期：不阻塞启动、可手动触发、健康检查。"""

    def _comp(self, **kw):
        from app.core.engine.components.dataguard import DataGuardComponent

        return DataGuardComponent(**kw)

    def test_disabled_component_does_not_start_timer(self):
        comp = self._comp(enabled=False, disabled_reason="回测模式")
        comp.component_config = {}
        comp.on_initialize()
        with mock.patch.object(comp, "_run", side_effect=AssertionError("不该跑")):
            comp.on_start()
        self.assertIsNone(comp._loop)
        self.assertIn("回测模式", comp.disabled_reason)

    def test_manual_check_returns_report(self):
        """check_now 走线程池（不阻塞 reactor），返回 Deferred[dict]。

        测试里没有运行中的 reactor，所以把 deferToThread 换成同步执行 ——
        我们要验证的是"报告内容对不对"，不是 Twisted 的线程池。
        """
        from twisted.internet import defer

        import app.core.engine.components.dataguard as G

        comp = self._comp(enabled=True, lookback_days=3, auto_repair=False)
        fake_report = mock.MagicMock()
        fake_report.to_dict.return_value = {"checked": 1, "missing_total": 0}
        fake_report.missing_total = 0
        fake_report.repaired_total = 0
        fake_report.anomaly_total = 0
        fake_report.failed = []

        with mock.patch("app.core.data.integrity.DataIntegrityChecker.scan",
                        return_value=fake_report), \
             mock.patch.object(G.threads, "deferToThread",
                               side_effect=lambda fn: defer.succeed(fn())):
            box = {}
            comp.check_now(["000001"]).addCallback(box.update)
        self.assertEqual(box.get("checked"), 1)
        self.assertEqual(comp.stats["runs"], 1)

    def test_snapshot_shape(self):
        comp = self._comp(enabled=True)
        snap = comp.snapshot()
        for key in ("enabled", "auto_repair", "interval", "stats", "report"):
            self.assertIn(key, snap)
        self.assertTrue(snap["assembled"])

    def test_health_check_before_first_run(self):
        comp = self._comp(enabled=True)
        ok, detail = comp.health_check()
        self.assertTrue(ok)
        self.assertIn("尚未体检", detail)

    def test_health_check_reports_gaps(self):
        from app.core.data.integrity import IntegrityReport

        comp = self._comp(enabled=True)
        comp._report = IntegrityReport(missing_total=3, checked=2)
        ok, detail = comp.health_check()
        self.assertFalse(ok)
        self.assertIn("3", detail)

    def test_is_busy_flag(self):
        comp = self._comp(enabled=True)
        self.assertFalse(comp.is_busy())
        comp._busy = True
        self.assertTrue(comp.is_busy())

    def test_start_schedules_first_check_instead_of_blocking(self):
        """首检必须延后调度，不能在 on_start 里同步跑（会拖慢启动）。"""
        comp = self._comp(enabled=True, start_delay=20.0, interval=3600.0)
        comp.component_config = {}
        comp.on_initialize()
        with mock.patch("twisted.internet.reactor.callLater") as later, \
             mock.patch.object(comp, "_tick",
                               side_effect=AssertionError("不该同步跑")):
            comp.on_start()
        delays = [c[0][0] for c in later.call_args_list]
        self.assertIn(20.0, delays)


class TestBuilderAssembly(unittest.TestCase):
    """引擎装配：数据校验服务要在，且回测模式默认停用。"""

    def _build(self, **kw):
        from app.core.engine.builder import build_engine

        base = dict(
            mode=kw.pop("mode", "SIMULATE"),
            with_monitor=False,
            with_store=False,
            with_news=False,
            with_collector=False,
            with_ext_data=False,
            with_market=False,
            auto_load_runtime=False,
        )
        base.update(kw)
        return build_engine(**base)

    def test_guard_registered(self):
        engine = self._build()
        comp = engine.get_component("data_guard")
        self.assertIsNotNone(comp, "引擎必须装配数据校验服务")
        self.assertTrue(comp.enabled)

    def test_guard_registered_after_collector(self):
        """顺序即启动顺序：采集先把数据喂进库，校验才不会误判成缺口。"""
        engine = self._build(with_collector=True)
        names = engine.component_names()
        if "data_collector" in names:
            self.assertLess(names.index("data_collector"),
                            names.index("data_guard"))

    def test_backtest_mode_disables_guard(self):
        engine = self._build(mode="BACKTEST")
        comp = engine.get_component("data_guard")
        self.assertIsNotNone(comp)
        self.assertFalse(comp.enabled)
        self.assertIn("回测", comp.disabled_reason)

    def test_explicit_switch(self):
        engine = self._build(mode="BACKTEST", with_data_guard=True)
        self.assertTrue(engine.get_component("data_guard").enabled)

    def test_guard_snapshot_in_engine_snapshot(self):
        engine = self._build()
        snap = engine.snapshot()
        entry = snap["components"]["data_guard"]
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["snapshot"]["assembled"])
        self.assertIn("auto_repair", entry["snapshot"])


if __name__ == "__main__":
    unittest.main()
