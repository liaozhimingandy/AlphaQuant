#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_calendar.py
# @Description : 交易日历：交易日判断、时段判断、闭市不请求
#
#                这一组测试守的是一条硬约束：
#                  **闭市时不该发数据请求**。
#                请求发出去了、限流风险占了、日志刷了一屏，
#                拿回来的还是和昨天一模一样的数据。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import date, datetime, time as dtime

from app.domain.calendar import (
    STATE_CLOSED,
    STATE_HOLIDAY,
    STATE_NAMES,
    STATE_LUNCH,
    STATE_OPEN,
    STATE_POST_CLOSE,
    STATE_PRE_OPEN,
    TradeCalendar,
)


class TestTradeCalendar(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = TradeCalendar()

    # ---------------- 交易日 ----------------
    def test_weekend_is_not_trading_day(self):
        # 2026-10-10 是周六
        self.assertFalse(self.cal.is_trading_day(date(2026, 10, 10)))
        self.assertFalse(self.cal.is_trading_day(date(2026, 10, 11)))

    def test_workday_is_trading_day(self):
        self.assertTrue(self.cal.is_trading_day(date(2026, 10, 9)))   # 周五

    def test_holiday_is_not_trading_day(self):
        """2026 年交易所公告：国庆 10/1(四)-10/7(三) 休市。

        这里五个工作日都会休 —— 只靠"是不是周末"判断会白采五天。
        """
        for d in (date(2026, 10, 1), date(2026, 10, 2),
                  date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)):
            self.assertFalse(self.cal.is_trading_day(d), f"{d} 应是休市日")
        # 10/8 起照常开市
        self.assertTrue(self.cal.is_trading_day(date(2026, 10, 8)))

    def test_spring_festival(self):
        """春节 2/16(一)-2/20(五) + 2/23(一) 休市，2/24 开市。"""
        for d in (date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
                  date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23)):
            self.assertFalse(self.cal.is_trading_day(d), f"{d} 应是休市日")
        self.assertTrue(self.cal.is_trading_day(date(2026, 2, 24)))

    def test_extra_session_overrides_weekend(self):
        """调休导致周末开市时，extra_sessions 能覆盖周末规则。"""
        cal = TradeCalendar(extra_sessions=["2026-10-10"])
        self.assertTrue(cal.is_trading_day(date(2026, 10, 10)))

    # ---------------- 时段与状态 ----------------
    def test_state_open(self):
        self.assertEqual(self.cal.state(datetime(2026, 10, 9, 10, 0)), STATE_OPEN)
        self.assertEqual(self.cal.state(datetime(2026, 10, 9, 14, 0)), STATE_OPEN)

    def test_state_lunch(self):
        self.assertEqual(self.cal.state(datetime(2026, 10, 9, 12, 0)), STATE_LUNCH)

    def test_state_post_close_then_closed(self):
        # 15:30 仍在收盘结算缓冲内（数据源还没定稿）
        self.assertEqual(self.cal.state(datetime(2026, 10, 9, 15, 30)),
                         STATE_POST_CLOSE)
        # 18:00 超出缓冲，当日已收市
        self.assertEqual(self.cal.state(datetime(2026, 10, 9, 18, 0)), STATE_CLOSED)

    def test_state_pre_open_and_early_morning(self):
        self.assertEqual(self.cal.state(datetime(2026, 10, 9, 9, 15)), STATE_PRE_OPEN)
        # 凌晨是"今天开盘、但还没到点"，**不是**非交易日。
        # 以前这里返回 HOLIDAY，于是每个交易日清晨日志都写"当前 非交易日"，
        # 看着像今天不开盘，排查时特别容易被带偏。
        self.assertEqual(self.cal.state(datetime(2026, 10, 9, 3, 0)), STATE_CLOSED)
        self.assertEqual(STATE_NAMES[STATE_CLOSED], "休市中")

    def test_state_holiday_on_non_trading_day(self):
        self.assertEqual(self.cal.state(datetime(2026, 10, 10, 10, 0)), STATE_HOLIDAY)

    def test_is_open(self):
        """is_open = 值得采数据的窗口（交易中/午休/收盘结算）。"""
        self.assertTrue(self.cal.is_open(datetime(2026, 10, 9, 10, 0)))
        self.assertTrue(self.cal.is_open(datetime(2026, 10, 9, 12, 0)))
        self.assertTrue(self.cal.is_open(datetime(2026, 10, 9, 15, 20)))
        self.assertFalse(self.cal.is_open(datetime(2026, 10, 9, 18, 0)))
        self.assertFalse(self.cal.is_open(datetime(2026, 10, 9, 3, 0)))
        # 非交易日全天都不采
        self.assertFalse(self.cal.is_open(datetime(2026, 10, 10, 10, 0)))

    # ---------------- 推算 ----------------
    def test_next_open(self):
        # 周六 → 下周一 9:30
        nxt = self.cal.next_open(datetime(2026, 10, 10, 10, 0))
        self.assertEqual(nxt, datetime(2026, 10, 12, 9, 30))
        # 周五盘前 → 当天 9:30
        nxt = self.cal.next_open(datetime(2026, 10, 9, 8, 0))
        self.assertEqual(nxt, datetime(2026, 10, 9, 9, 30))
        # 国庆假期内 → 10/8 开市
        nxt = self.cal.next_open(datetime(2026, 10, 5, 10, 0))
        self.assertEqual(nxt, datetime(2026, 10, 8, 9, 30))

    def test_last_trading_day(self):
        self.assertEqual(self.cal.last_trading_day(date(2026, 10, 10)),
                         date(2026, 10, 9))
        self.assertEqual(self.cal.last_trading_day(date(2026, 10, 12)),
                         date(2026, 10, 12))

    def test_recent_trading_days(self):
        """跨过国庆长假取最近 5 个交易日：假期里的工作日必须被跳过。"""
        days = self.cal.recent_trading_days(5, date(2026, 10, 12))
        self.assertEqual(len(days), 5)
        self.assertEqual(days, sorted(days))          # 升序，最后一个是最近的
        self.assertEqual(days[-1], date(2026, 10, 12))
        for d in days:
            self.assertTrue(self.cal.is_trading_day(d), f"{d} 不是交易日")
        # 10/1~10/7 是假期，一个都不该出现在结果里
        for d in days:
            self.assertFalse(date(2026, 10, 1) <= d <= date(2026, 10, 7),
                             f"{d} 落在国庆假期里")

    # ---------------- 持久化 ----------------
    def test_save_and_load_roundtrip(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trade_calendar.json"
            self.cal.save(str(p))
            raw = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("holidays", raw)
            got = TradeCalendar.load(str(p))
            self.assertEqual(got.holidays, self.cal.holidays)
            self.assertFalse(got.is_trading_day(date(2026, 10, 1)))

    def test_load_missing_file_falls_back_to_builtin(self):
        """文件不存在时不能报错 —— 日历缺失会导致该采的时候不采。"""
        cal = TradeCalendar.load("/nonexistent/trade_calendar.json")
        self.assertFalse(cal.is_trading_day(date(2026, 10, 1)))
        self.assertTrue(cal.is_trading_day(date(2026, 10, 9)))


class TestClosedMarketNoRequest(unittest.TestCase):
    """闭市不请求：这是本轮最核心的行为约束。"""

    def _component(self, symbols=("000001",)):
        # 日频截面原是独立的 ExtDataComponent，现在并进 CollectionService
        # 的 ``_cross`` 单元：构造参数带 ``cross_`` 前缀，方法带 ``_cross`` 中缀。
        from app.modules.collection.service import CollectionService

        # K 线单元用一个不存在的配置文件：构造期它会解析一次采集配置，
        # 不隔离的话会读到工程里的 config/collector.json，把真实标的带进用例。
        return CollectionService(
            cross_symbols=list(symbols),
            cross_enabled=True,
            kline_config_path="/nonexistent/collector.json",
        )

    def test_when_market_closed_no_fetch(self):
        from app.domain import calendar as cal_mod

        # 周六：非交易日
        closed = TradeCalendar()
        cal_mod.set_calendar(closed)
        try:
            c = self._component()
            import datetime as _dt

            saturday = _dt.datetime(2026, 10, 10, 10, 0)
            with _freeze(saturday):
                self.assertFalse(c._cross_should_fetch())
            self.assertGreater(c.stats_cross["skipped_closed"], 0)
        finally:
            cal_mod.set_calendar(TradeCalendar.load())

    def test_same_window_not_collected_twice(self):
        """同一个采集窗口一天只做一次。

        日频截面数据一天一条，在同一个窗口里反复拉就是在浪费请求额度。
        """
        from app.domain import calendar as cal_mod

        cal_mod.set_calendar(TradeCalendar())
        try:
            c = self._component()
            post = datetime(2026, 10, 9, 15, 40)          # 盘后窗口
            with _freeze(post):
                self.assertEqual(c._cross_slot(), "post")
                c._cross_slots[post.date()] = {"post"}          # 标记已完成
                self.assertIsNone(c._cross_slot())
            self.assertGreater(c.stats_cross["skipped_synced"], 0)
        finally:
            cal_mod.set_calendar(TradeCalendar.load())

    def test_pre_and_post_are_separate_windows(self):
        """盘前补齐与盘后定稿是**两个**窗口，各自采一次，互不顶替。"""
        from app.domain import calendar as cal_mod

        cal_mod.set_calendar(TradeCalendar())
        try:
            c = self._component()
            pre = datetime(2026, 10, 9, 9, 10)
            with _freeze(pre):
                self.assertEqual(c._cross_slot(), "pre")
                c._cross_slots[pre.date()] = {"pre"}
                self.assertIsNone(c._cross_slot())
            # 到了盘后，定稿那一轮仍然要做
            post = datetime(2026, 10, 9, 15, 40)
            with _freeze(post):
                self.assertEqual(c._cross_slot(), "post")
        finally:
            cal_mod.set_calendar(TradeCalendar.load())

    def test_not_collected_before_settlement(self):
        """15:10 还没到结算时间 —— 拿到的是未定稿数据，宁可不采。"""
        from app.domain import calendar as cal_mod

        cal_mod.set_calendar(TradeCalendar())
        try:
            c = self._component()
            with _freeze(datetime(2026, 10, 9, 15, 10)):
                self.assertIsNone(c._cross_slot())
            with _freeze(datetime(2026, 10, 9, 15, 35)):
                self.assertEqual(c._cross_slot(), "post")
        finally:
            cal_mod.set_calendar(TradeCalendar.load())


class _freeze:
    """把"现在"固定到指定时刻。

    只替换**被测模块里**的 date/datetime 名字（模块全局），
    不去动标准库 —— 后者影响面太大，容易把测试框架自己也冻住。

    两个都要换：``datetime.now()`` 决定时段状态，``date.today()``
    决定"今天采过没有"。只换一个会得到自相矛盾的结果。
    """

    # 采集时间窗口的判断在 schedule 模块里，也要一起冻住 ——
    # 只冻 calendar 会出现"日历说在盘后、窗口说不在"的自相矛盾
    _MODULES = ("app.domain.calendar",
                "app.modules.collection.service",
                "app.modules.collection.schedule")

    def __init__(self, when: datetime) -> None:
        self.when = when
        self._saved: list = []

    def __enter__(self):
        import importlib

        real_date = date
        real_dt = datetime
        frozen = self.when

        class FrozenDate(real_date):
            @classmethod
            def today(cls):
                return frozen.date()

        class FrozenDatetime(real_dt):
            @classmethod
            def now(cls, tz=None):
                return frozen

            @classmethod
            def today(cls):
                return frozen.date()

        for name in self._MODULES:
            mod = importlib.import_module(name)
            self._saved.append((mod,
                                getattr(mod, "date", None),
                                getattr(mod, "datetime", None)))
            mod.date = FrozenDate
            mod.datetime = FrozenDatetime
        self._frozen_date_cls = FrozenDate
        return self

    def __exit__(self, *exc):
        for mod, d, dt_ in self._saved:
            if d is not None:
                mod.date = d
            if dt_ is not None:
                mod.datetime = dt_
        self._saved.clear()
        return False


if __name__ == "__main__":
    unittest.main()
