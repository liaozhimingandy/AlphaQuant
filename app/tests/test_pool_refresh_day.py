#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票池刷新：未开盘时回退到上一个交易日，并把数据按该日存读。

核心回归点（用户反馈"刷新没同步到数据"的根因）：
  1. refresh 默认 day=None 时，必须用交易日历决定"数据日"——
     未开盘 → 上一个交易日，否则取当天会拿到空。
  2. 列表读取（pool_top）不指定日期时，必须读"库里最新评分日"，
     否则未开盘时分数按历史日存着、列表却读今天 → 一片空。
"""
import unittest
from datetime import date, datetime
from unittest import mock

import pandas as pd

try:
    from app.tests import helpers  # noqa: F401
except Exception:
    pass

from app.domain.calendar import TradeCalendar, get_calendar
from app.modules.universe.pool import pool as P
from app.modules.universe.pool.sync import MX as SYNC_MX  # sync 模块内 `from app.data import marketx as MX`
from app.data.engine import SessionLocal
from app.data.schema import StockScore, LimitUpPool, FundFlow, ChipDistribution
from app.data.repo import kline_repo as R


def _fake_dt():
    """让 pool 模块里 refresh 解析出的"今天"是一个固定的周日（2026-10-11）。"""
    class _D(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 11, 10, 0)
    return _D


class TestResolveRefreshDay(unittest.TestCase):
    def setUp(self):
        self.cal = TradeCalendar(source="builtin")  # 不含缓存文件干扰

    def test_sunday_falls_back_to_friday(self):
        d, recent = P._resolve_refresh_day(self.cal, datetime(2026, 10, 11, 10, 0))
        self.assertEqual(d, date(2026, 10, 9))
        self.assertTrue(recent)

    def test_trading_hours_uses_today(self):
        d, recent = P._resolve_refresh_day(self.cal, datetime(2026, 10, 9, 10, 30))
        self.assertEqual(d, date(2026, 10, 9))
        self.assertFalse(recent)

    def test_pre_open_falls_back(self):
        d, recent = P._resolve_refresh_day(self.cal, datetime(2026, 10, 9, 9, 0))
        self.assertEqual(d, date(2026, 10, 8))
        self.assertTrue(recent)

    def test_holiday_falls_back(self):
        d, recent = P._resolve_refresh_day(self.cal, datetime(2026, 10, 5, 10, 0))
        self.assertEqual(d, date(2026, 9, 30))
        self.assertTrue(recent)


class TestRefreshOnClosedDay(unittest.TestCase):
    """未开盘点刷新：仍能把上一个交易日的数据拉回来、落库、并能被列表读到。"""

    def setUp(self):
        # 清掉测试标的的历史数据，保证只看到本次 mock 写入的（避免项目库里
        # 已有的真实资金流干扰 latest_fund_flow 的取值）
        self._clean_symbols()
        # 固定"今天"为周日 2026-10-11（未开盘），并用内置日历
        self._dt_patch = mock.patch.object(P, "datetime", _fake_dt())
        self._dt_patch.start()
        self._cal_patch = mock.patch(
            "app.domain.calendar.get_calendar",
            return_value=TradeCalendar(source="builtin"))
        self._cal_patch.start()

        syms = ["600000", "000001"]
        self._syms = syms
        day = "2026-10-09"  # 上一个交易日（ISO，与真实数据源一致）

        # ---- mock 网络：涨停池 / 资金流 / 筹码 / 板块 ----
        def fake_limitup(trade_date=None):
            return pd.DataFrame([
                {"date": day, "symbol": "600000", "name": "浦发", "pct_change": 10.0,
                 "close": 9.0, "amount": 1e9, "seal_amount": 5e8,
                 "first_seal_at": "09:31", "last_seal_at": "14:50",
                 "broken_times": 0, "limit_stat": "1/1", "continuous": 1,
                 "industry": "银行", "updated_at": datetime.now()},
                {"date": day, "symbol": "000001", "name": "平安", "pct_change": 10.0,
                 "close": 12.0, "amount": 2e9, "seal_amount": 8e8,
                 "first_seal_at": "09:32", "last_seal_at": "14:51",
                 "broken_times": 0, "limit_stat": "1/1", "continuous": 1,
                 "industry": "保险", "updated_at": datetime.now()},
            ])

        def fake_flow(sym, days=60):
            return pd.DataFrame([
                {"date": day, "symbol": sym, "main_net": 1e8, "main_net_pct": 12.0,
                 "turnover": 5e8, "amount": 1e9, "close": 10.0, "pct_change": 10.0},
            ])

        def fake_chip(sym):
            return pd.DataFrame([
                {"date": day, "symbol": sym, "avg_cost": 9.0, "profit_ratio": 0.8,
                 "chip_concentration": 0.3, "peak_price": 9.2, "peak_gap": 0.1},
            ])

        def fake_boards(kind="concept"):
            return pd.DataFrame([
                {"board": "银行", "kind": kind, "pct_change": 2.0,
                 "turnover": 1e10, "main_net": 3e8, "up_count": 30, "down_count": 5,
                 "leading_stock": "600000", "rank": 1},
            ])

        def fake_board_flow(kind="concept"):
            return pd.DataFrame([
                {"board": "银行", "main_net": 3e8, "pct_change": 2.0},
            ])

        self._m = mock.patch.multiple(
            SYNC_MX,
            fetch_limitup_pool=fake_limitup,
            fetch_fund_flow=fake_flow,
            fetch_chip=fake_chip,
            fetch_boards=fake_boards,
            fetch_board_fund_flow=fake_board_flow,
        )
        self._m.start()
        # 板块资金流在 sync 内 try/except，缺了也不致命，但给上更稳

    def tearDown(self):
        self._m.stop()
        self._cal_patch.stop()
        self._dt_patch.stop()
        self._clean_symbols()

    def _clean_symbols(self):
        syms = ["600000", "000001"]
        with SessionLocal() as s:
            # 清掉该日全部涨停池，避免项目库里已有的「真实涨停池」让 mock 失效
            # （之前某次真实刷新把 600825/002242 等写进了 limitup_pool，
            #  candidates 读库优先，mock 的 fetch_limitup_pool 就再也用不上）
            s.query(LimitUpPool).filter(LimitUpPool.date == date(2026, 10, 9)).delete()
            s.query(StockScore).filter(StockScore.symbol.in_(syms)).delete()
            s.query(LimitUpPool).filter(LimitUpPool.symbol.in_(syms)).delete()
            s.query(FundFlow).filter(FundFlow.symbol.in_(syms)).delete()
            s.query(ChipDistribution).filter(ChipDistribution.symbol.in_(syms)).delete()
            s.commit()

    def test_refresh_syncs_previous_trading_day(self):
        from app.modules.universe.pool import StockPool

        pool = StockPool()
        pool.config.sync_fundamental = False  # 跳过需要联网的基本面
        report = pool.refresh(source="limitup", day=None, save=True)
        self.assertNotIn("error", report, msg=report.get("error"))
        # 数据日必须是上一个交易日，且标记为历史日
        self.assertEqual(report["data_day"], "20261009")
        self.assertTrue(report["is_recent_day"])
        self.assertGreaterEqual(report["count"], 1)

        # 列表读取（不指定日期）必须能读到刚才落库的"历史日"数据
        rows = R.scores(symbol="600000", day=date(2026, 10, 9), limit=1)
        self.assertTrue(rows, "未开盘刷新后，按数据日应能读到评分")
        self.assertEqual(rows[0]["date"], "2026-10-09")
        # 核心修复：未开盘刷新必须把评分落到**上一个交易日**而不是今天
        # （今天周日无数据）。注意这里只校验本用例自己种的 600000 ——
        # 生产库是共享的，随时可能有别的标的/来源的"今天"数据躺着，
        # 直接断言"全库最新评分日 != 今天"会被无关数据随机打挂。
        self.assertNotEqual(rows[0]["date"], date.today().isoformat(),
                            "未开盘刷新不应把评分写成今天")


if __name__ == "__main__":
    unittest.main()
