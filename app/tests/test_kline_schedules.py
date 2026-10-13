#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_kline_schedules.py
# @Description : 标的主表 + K线三班定时 + 内置任务计划
#
#                这些测试**全部离线**：数据源用注入的假 fetcher 顶替，
#                不联网、不依赖日历之外的外部状态。
#                这不是洁癖 —— 上市日期这种"同步了才知道"的能力，
#                如果只能在联网环境下被测，那么它的回归测试就等于没有。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.data.engine import apply_sqlite_pragmas
from app.data.schema import Base


class _DbMixin:
    """临时 SQLite（生产同款 PRAGMA）+ 替换仓储的 SessionLocal。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.engine = apply_sqlite_pragmas(
            create_engine(f"sqlite:///{Path(self._td.name) / 't.db'}")
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False,
                                    autoflush=False)
        self._patches: List[Any] = []

    def patch(self, module, name, value):
        old = getattr(module, name)
        setattr(module, name, value)
        self._patches.append((module, name, old))

    def tearDown(self):
        for mod, name, old in reversed(self._patches):
            setattr(mod, name, old)
        self._patches.clear()
        try:
            self.engine.dispose()
        except Exception:
            pass
        self._td.cleanup()


# =============================================================================
# 标的主表仓储
# =============================================================================
class TestStockInfoRepo(_DbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import app.data.repo.stock_info_repo as P

        self.P = P
        self.patch(P, "SessionLocal", self.Session)

    def test_save_normalizes_symbol(self):
        n = self.P.save_profiles([{"symbol": "sh.600000", "name": "浦发银行"}])
        self.assertEqual(n, 1)
        self.assertIsNotNone(self.P.get("600000"))

    def test_none_does_not_overwrite_known_values(self):
        """第二条只带名称时，不能把刚同步好的上市日期擦掉。"""
        self.P.save_profiles([{"symbol": "600000", "name": "浦发",
                               "list_date": "1999-11-10", "source": "sse"}])
        self.P.save_profiles([{"symbol": "600000", "name": "浦发银行"}])
        row = self.P.get("600000")
        self.assertEqual(row["name"], "浦发银行")
        self.assertEqual(row["list_date"], "1999-11-10")

    def test_filter_listed_buckets(self):
        self.P.save_profiles([
            {"symbol": "600000", "name": "已上市", "list_date": "1999-11-10"},
            {"symbol": "600042", "name": "未上市", "list_date": "2099-01-01"},
            {"symbol": "999999", "name": "已退市", "delisted": True},
            {"symbol": "000001", "name": "日期未知"},
        ])
        keep, buckets = self.P.filter_listed(
            ["600000", "600042", "999999", "000001", "888888"])
        self.assertIn("600000", keep)
        self.assertNotIn("600042", keep)
        self.assertNotIn("999999", keep)
        self.assertEqual(buckets["unlisted"], ["600042"])
        self.assertEqual(buckets["delisted"], ["999999"])
        # 主表里查无此人 与 缺上市日期 都归到 unknown，且**都放行**
        self.assertEqual(sorted(buckets["unknown"]), ["000001", "888888"])
        self.assertIn("000001", keep)
        self.assertIn("888888", keep)

    def test_missing_list_date_excludes_delisted(self):
        self.P.save_profiles([
            {"symbol": "000001", "name": "缺日期"},
            {"symbol": "999999", "name": "退市", "delisted": True},
        ])
        self.assertEqual(self.P.missing_list_date(), ["000001"])

    def test_migrate_adds_columns_on_legacy_table(self):
        """老库（只有 3 列）必须能被自动补上新列，而不是报 no such column。"""
        eng = apply_sqlite_pragmas(
            create_engine(f"sqlite:///{Path(self._td.name) / 'legacy.db'}"))
        with eng.begin() as c:
            c.execute(text(
                "CREATE TABLE stock_info (symbol VARCHAR(20) PRIMARY KEY,"
                " name VARCHAR(64), updated_at DATETIME)"))
            c.execute(text("INSERT INTO stock_info(symbol,name) VALUES('600000','浦发')"))
        old_session = sessionmaker(bind=eng, autocommit=False, autoflush=False)
        self.patch(self.P, "SessionLocal", old_session)
        self.P.ensure_schema(force=True)
        self.P.save_profiles([{"symbol": "600000", "list_date": "1999-11-10"}])
        row = self.P.get("600000")
        self.assertEqual(row["list_date"], "1999-11-10")
        self.assertEqual(row["name"], "浦发")   # 老数据没丢
        eng.dispose()

    def test_summary_coverage(self):
        self.P.save_profiles([
            {"symbol": "600000", "list_date": "1999-11-10"},
            {"symbol": "000001"},
        ])
        s = self.P.summary()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["missing_list_date"], 1)
        self.assertAlmostEqual(s["coverage"], 0.5)


# =============================================================================
# 标的主表同步器
# =============================================================================
def _fake_fetcher(mapping: Dict[str, Any], fail: Optional[set] = None):
    """按函数名返回预制 DataFrame；未登记的名字按 fail 处理。"""
    fail = fail or set()

    def _fn(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in fail:
            raise RuntimeError(f"{name} 接口不可用")
        if name not in mapping:
            raise KeyError(name)
        return mapping[name]

    return _fn


class TestStockProfileSync(_DbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import app.data.repo.stock_info_repo as P
        import app.modules.universe.profile as PR

        self.P = P
        self.PR = PR
        self.patch(P, "SessionLocal", self.Session)

    def test_sync_writes_list_date_and_board(self):
        fetch = _fake_fetcher({
            "stock_zh_a_spot_em": pd.DataFrame([
                {"代码": "600000", "名称": "浦发银行"},
                {"代码": "000001", "名称": "平安银行"},
            ]),
            "stock_info_sh_name_code": pd.DataFrame([
                {"证券代码": "600000", "证券简称": "浦发银行", "上市日期": "1999-11-10"},
            ]),
            "stock_info_sz_name_code": pd.DataFrame([
                {"证券代码": "000001", "证券简称": "平安银行", "上市日期": "1991-04-03"},
            ]),
            "stock_info_bj_name_code": pd.DataFrame([
                {"证券代码": "830799", "证券简称": "艾融软件", "上市日期": "2020-07-27"},
            ]),
            "stock_individual_info_em": pd.DataFrame([
                {"item": "上市时间", "value": "2001-08-27"},
                {"item": "行业", "value": "银行"},
            ]),
        })
        # 沪市接口按参数分两次调用（主板 / 科创板），两次都返回同一张表即可
        rep = self.PR.StockProfileSync(fetcher=fetch, individual_limit=5).sync()
        self.assertTrue(rep["ok"])
        self.assertGreaterEqual(int(rep["written"]), 3)

        row = self.P.get("600000")
        self.assertEqual(row["list_date"], "1999-11-10")
        self.assertEqual(row["board"], "sh")
        self.assertEqual(row["market"], "sh")
        self.assertEqual(self.P.get("000001")["board"], "sz")
        self.assertEqual(self.P.get("830799")["board"], "bj")

    def test_one_exchange_failure_does_not_roll_back_others(self):
        fetch = _fake_fetcher(
            {
                "stock_zh_a_spot_em": pd.DataFrame([{"代码": "600000", "名称": "浦发"}]),
                "stock_info_sh_name_code": pd.DataFrame([
                    {"证券代码": "600000", "证券简称": "浦发", "上市日期": "1999-11-10"}]),
                "stock_info_sz_name_code": pd.DataFrame([
                    {"证券代码": "000001", "证券简称": "平安", "上市日期": "1991-04-03"}]),
                "stock_info_bj_name_code": pd.DataFrame([
                    {"证券代码": "830799", "证券简称": "艾融", "上市日期": "2020-07-27"}]),
                "stock_individual_info_em": pd.DataFrame([{"item": "上市时间", "value": "x"}]),
            },
            fail={"stock_info_sz_name_code"},   # 深交所挂了
        )
        rep = self.PR.StockProfileSync(fetcher=fetch, individual_limit=0).sync()
        self.assertTrue(rep["ok"])
        self.assertEqual(self.P.get("600000")["list_date"], "1999-11-10")
        self.assertIsNone(self.P.get("000001"))
        self.assertTrue(any("szse" in str(f.get("source")) for f in rep["failed"]))

    def test_skip_when_already_complete(self):
        fetch = _fake_fetcher({
            "stock_zh_a_spot_em": pd.DataFrame([{"代码": "600000", "名称": "浦发"}]),
            "stock_info_sh_name_code": pd.DataFrame([
                {"证券代码": "600000", "证券简称": "浦发", "上市日期": "1999-11-10"}]),
            "stock_info_sz_name_code": pd.DataFrame(),
            "stock_info_bj_name_code": pd.DataFrame(),
        })
        self.PR.StockProfileSync(fetcher=fetch, individual_limit=0).sync()
        rep2 = self.PR.StockProfileSync(fetcher=fetch, individual_limit=0).sync()
        self.assertTrue(rep2["skipped"])
        self.assertEqual(int(rep2["written"]), 0)

    def test_individual_step_fills_remaining(self):
        fetch = _fake_fetcher({
            "stock_zh_a_spot_em": pd.DataFrame([{"代码": "600519", "名称": "贵州茅台"}]),
            "stock_info_sh_name_code": pd.DataFrame(),
            "stock_info_sz_name_code": pd.DataFrame(),
            "stock_info_bj_name_code": pd.DataFrame(),
            "stock_individual_info_em": pd.DataFrame([
                {"item": "股票简称", "value": "贵州茅台"},
                {"item": "上市时间", "value": "2001-08-27"},
                {"item": "行业", "value": "酿酒行业"},
                {"item": "总股本", "value": "1256197000"},
            ]),
        })
        rep = self.PR.StockProfileSync(fetcher=fetch, individual_limit=10).sync()
        row = self.P.get("600519")
        self.assertEqual(row["list_date"], "2001-08-27")
        self.assertEqual(row["industry"], "酿酒行业")
        self.assertTrue(rep["ok"])


# =============================================================================
# K 线三班定时
# =============================================================================
class _FakePool:
    def __init__(self, symbols):
        self.config = type("C", (), {"source": "flow"})()
        self._symbols = list(symbols)

    def candidates(self, source=None, day=None):
        return list(self._symbols), {"source": source or "flow"}


class TestPoolKline(_DbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import app.data.repo.kline_repo as KR
        import app.data.repo.stock_info_repo as P
        import app.data.service as DS
        import app.modules.collection.pool_kline as K
        import app.modules.universe.pool as UP

        self.P, self.K, self.DS, self.UP = P, K, DS, UP
        self.patch(P, "SessionLocal", self.Session)
        # 日线覆盖率查询也在读库，不指向临时库就会读到开发库
        self.patch(KR, "SessionLocal", self.Session)
        self.calls: List[tuple] = []

        def _fake_collect(symbol, lookback_days=30, start_date="", end_date=None,
                          adjust="qfq", source=None, db=None):
            self.calls.append((symbol, lookback_days))
            return {"symbol": symbol, "rows": 5, "new_rows": 1,
                    "start": "2026-01-01", "end": "2026-10-13"}

        self.patch(DS.MarketDataService, "collect_incremental",
                   staticmethod(_fake_collect))
        class _Pool(_FakePool):
            def __init__(self):
                super().__init__(["600000", "600042", "999999", "000001"])

        self.patch(UP, "StockPool", _Pool)
        self.P.save_profiles([
            {"symbol": "600000", "list_date": "1999-11-10"},
            {"symbol": "600042", "list_date": "2099-01-01"},   # 未上市
            {"symbol": "999999", "delisted": True},            # 已退市
            {"symbol": "000001"},                              # 日期未知 → 放行
        ])

    def test_intraday_skips_unlisted_and_delisted(self):
        rep = self.K.refresh_intraday(source="flow", limit=10,
                                      require_trading=False)
        self.assertEqual(rep["mode"], "intraday")
        self.assertEqual(rep["requested"], 4)
        self.assertEqual(rep["skipped"]["unlisted"], ["600042"])
        self.assertEqual(rep["skipped"]["delisted"], ["999999"])
        self.assertEqual(sorted(rep["symbols"]), ["000001", "600000"])
        self.assertTrue(rep["ok"])

    def test_intraday_raises_nothing_when_outside_session(self):
        """休市不是失败：它不该把任务送进死信队列。"""
        rep = self.K.refresh_intraday(source="flow", require_trading=True)
        if rep["skipped_reason"]:
            self.assertEqual(rep["collected"], 0)
            self.assertEqual(rep["failed"], [])

    def test_post_close_only_collects_missing_day(self):
        rep = self.K.sync_post_close(day="2026-10-13")
        self.assertEqual(rep["target_day"], "2026-10-13")
        self.assertEqual(rep["skipped"]["unlisted"], ["600042"])
        self.assertEqual(rep["skipped"]["delisted"], ["999999"])
        self.assertEqual(sorted(rep["symbols"]), ["000001", "600000"])
        # 库里还没有任何日线，所以两只都该补
        self.assertEqual(rep["collected"], 2)

    def test_post_close_skips_when_already_up_to_date(self):
        from app.data.repo.stock_repo import StockRepository

        df = pd.DataFrame([{
            "date": "2026-10-13", "symbol": "600000", "open": 1, "high": 1,
            "low": 1, "close": 1, "volume": 1, "amount": 1,
        }])
        with self.Session() as s:
            StockRepository.save_df(s, df, commit=True)
        rep = self.K.sync_post_close(day="2026-10-13")
        self.assertIn("600000", rep["skipped"]["up_to_date"])
        self.assertEqual(rep["symbols"], ["000001"])

    def test_gap_scan_uses_integrity_checker(self):
        import app.modules.collection.integrity as INT

        class _Checker:
            def __init__(self, **kw):
                self.kw = kw
                self.scan_args = None

            def scan(self, symbols=None, on_progress=None):
                self.scan_args = list(symbols or [])
                return type("R", (), {
                    "checked": len(self.scan_args),
                    "missing_total": 3, "repaired_total": 2,
                    "anomaly_total": 0, "failed": [],
                    "to_dict": lambda self: {"checked": len(self.scan_args)},
                })()

        holder = {}

        def _factory(**kw):
            c = _Checker(**kw)
            holder["c"] = c
            return c

        self.patch(INT, "DataIntegrityChecker", _factory)
        rep = self.K.scan_gaps(source="flow", limit=10)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["checked"], 2)          # 未上市/退市被跳过
        self.assertEqual(rep["missing_total"], 3)
        self.assertEqual(rep["repaired"], 2)
        self.assertEqual(sorted(holder["c"].scan_args), ["000001", "600000"])

    def test_explicit_symbols_win_over_pool(self):
        rep = self.K.refresh_intraday(symbols=["600000"], require_trading=False)
        self.assertEqual(rep["symbols"], ["600000"])
        self.assertEqual(rep["source_info"]["source"], "explicit")


# =============================================================================
# 内置定时任务与处理器
# =============================================================================
class TestBuiltinSchedules(unittest.TestCase):
    def test_new_jobs_are_registered(self):
        from app.runtime.maintenance import DEFAULT_JOBS

        for name in ("kline_intraday", "kline_post_close", "kline_gap_scan",
                     "stock_profile", "account_settle"):
            self.assertIn(name, DEFAULT_JOBS)
            self.assertTrue(DEFAULT_JOBS[name].enabled)

    def test_planner_lists_them_with_next_run(self):
        from app.runtime.maintenance import MaintenancePlanner

        snap = MaintenancePlanner().snapshot()
        names = {j["name"] for j in snap["jobs"]}
        for name in ("kline_intraday", "kline_post_close", "kline_gap_scan",
                     "stock_profile", "account_settle"):
            self.assertIn(name, names)

    def test_kline_intraday_is_trading_hours_only(self):
        from app.runtime.maintenance import DEFAULT_JOBS

        self.assertTrue(DEFAULT_JOBS["kline_intraday"].trading_hours_only)
        self.assertGreaterEqual(int(DEFAULT_JOBS["kline_intraday"].interval_minutes or 0), 1)


class TestNewHandlers(unittest.TestCase):
    def test_handlers_registered(self):
        from app.runtime.workers.handlers import DEFAULT_HANDLERS

        for t in ("market.kline.intraday", "market.kline.post_close",
                  "market.kline.gap_scan", "stock.profile.sync", "account.settle"):
            self.assertIn(t, DEFAULT_HANDLERS)
            self.assertIn("description", DEFAULT_HANDLERS[t])

    def test_task_types_have_queue_and_pool(self):
        import itertools

        from app.runtime.events.types import Pool, TASK_QUEUE, TaskType

        for t in (TaskType.MARKET_KLINE_INTRADAY, TaskType.MARKET_KLINE_POST_CLOSE,
                  TaskType.MARKET_KLINE_GAP_SCAN, TaskType.STOCK_PROFILE_SYNC,
                  TaskType.ACCOUNT_SETTLE):
            self.assertIn(t, TASK_QUEUE)
        covered = set(itertools.chain.from_iterable(
            Pool.DEFAULT_CAPABILITIES.values()))
        for t in (TaskType.MARKET_KLINE_INTRADAY, TaskType.MARKET_KLINE_POST_CLOSE,
                  TaskType.MARKET_KLINE_GAP_SCAN, TaskType.STOCK_PROFILE_SYNC,
                  TaskType.ACCOUNT_SETTLE):
            self.assertIn(t, covered)

    def test_all_failed_raises_but_nothing_to_do_does_not(self):
        from app.runtime.workers.handlers import _raise_if_all_failed

        with self.assertRaises(RuntimeError):
            _raise_if_all_failed({"collected": 0, "requested": 3,
                                  "failed": [{"symbol": "600000", "error": "网络"}]},
                                 "盘中K线刷新")
        # 全部无事可做 / 部分成功 —— 都不该抛
        _raise_if_all_failed({"collected": 0, "requested": 0, "failed": []}, "x")
        _raise_if_all_failed({"collected": 2, "requested": 3,
                              "failed": [{"symbol": "1", "error": "e"}]}, "x")


# =============================================================================
# 交易时段风控规则
# =============================================================================
class TestTradingSessionRisk(unittest.TestCase):
    def _rule(self, **kw):
        from app.modules.risk import create_risk

        return create_risk("trading_session", **kw)

    def test_blocks_when_closed(self):
        import app.domain.calendar as CAL

        original = CAL.get_calendar
        CAL.get_calendar = lambda: type("C", (), {"state": lambda self, dt=None: "closed"})()
        try:
            verdict = self._rule().check(None, None)
        finally:
            CAL.get_calendar = original
        self.assertFalse(verdict.allowed)

    def test_allows_during_session(self):
        import app.domain.calendar as CAL

        original = CAL.get_calendar
        CAL.get_calendar = lambda: type("C", (), {"state": lambda self, dt=None: "open"})()
        try:
            verdict = self._rule().check(None, None)
        finally:
            CAL.get_calendar = original
        self.assertTrue(verdict.allowed)

    def test_opt_out(self):
        verdict = self._rule(allow_outside_hours=True).check(None, None)
        self.assertTrue(verdict.allowed)


if __name__ == "__main__":
    unittest.main()
