#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_collection_fallback.py
# @Description : 采集链路"降级与适配"回归测试
#
#                开盘日采不到数据时，最容易被误判成"日历错了"或"网络坏了"。
#                这一组用例钉住那次排查的真实结论：**代码适配跟不上数据源变化**
#                才是主因，日历判断本身是对的。被测的四条：
#
#                1. 分钟线不能只有东财一个源 —— 它一挂，分钟线整条链路陪葬；
#                2. 估值不能写死一个 akshare 接口名 —— 接口被移除后 69 只票全废；
#                3. 涨停池当日为空要回退上一交易日 —— 早盘本来就没有当日数据；
#                4. 解析失败 ≠ 域名不可达 —— 混为一谈会把排查方向带成"查代理"。
#
#                全部用桩，不依赖网络。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from app.domain.calendar import STATE_CLOSED, STATE_NAMES, TradeCalendar
from app.domain.symbol import BaostockDataSource, DataFetchError, DataSourceFactory


class _FakeSource:
    """假数据源：用来测"主源挂了会不会降级到下一个"。"""

    def __init__(self, name: str, minute: bool = True, result=None, error=None):
        self.name = name
        self.supports_minute = minute
        self._result = result
        self._error = error
        self.calls: list = []

    def fetch_minute_data(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def _minute_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "dt": pd.to_datetime([f"2026-10-09 14:5{i}:00" for i in range(n)]),
        "symbol": ["000858"] * n,
        "open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
        "close": [1.0] * n, "volume": [1] * n, "amount": [1.0] * n,
    })


class TestMinuteLineFallback(unittest.TestCase):
    """分钟线：东财不能是唯一退路。"""

    def test_baostock_supports_minute(self):
        self.assertTrue(BaostockDataSource.supports_minute)

    def test_baostock_rejects_1min(self):
        """1 分钟线 baostock 没有，必须明确报错让上层换源，不能返回空。"""
        import app.domain.symbol as sym

        src = BaostockDataSource()
        with mock.patch.object(sym, "_lazy_baostock"):
            with self.assertRaises(DataFetchError) as ctx:
                src.fetch_minute_data("000858", period="1")
        self.assertIn("不支持", str(ctx.exception))

    def test_falls_back_when_primary_fails(self):
        """主源炸了 → 自动降级到下一个支持分钟线的源。"""
        broken = _FakeSource("akshare", error=ConnectionError("RemoteDisconnected"))
        good = _FakeSource("baostock", result=_minute_df())
        mapping = {"akshare": broken, "baostock": good}
        with mock.patch.object(DataSourceFactory, "get",
                               classmethod(lambda cls, name: mapping[name])):
            df = DataSourceFactory.get_minute_data("000858", period="5",
                                                   priority=["akshare", "baostock"])
        self.assertEqual(len(df), 3)
        self.assertEqual(len(broken.calls), 1)
        self.assertEqual(len(good.calls), 1)

    def test_error_message_is_single_line(self):
        """失败原因必须出现在第一行 —— 换行版在日志里永远看不到真因。"""
        only = _FakeSource("akshare", error=ConnectionError("连接被重置"))
        with mock.patch.object(DataSourceFactory, "get",
                               classmethod(lambda cls, n: only)):
            with self.assertRaises(DataFetchError) as ctx:
                DataSourceFactory.get_minute_data("000858", period="1",
                                                  priority=["akshare"])
        msg = str(ctx.exception)
        self.assertNotIn("\n", msg, "多行错误消息在日志里会被截断成第一行")
        self.assertIn("连接被重置", msg)


class TestValuationAdaptation(unittest.TestCase):
    """估值：akshare 移除了乐咕接口，必须能自动换百度。"""

    def test_falls_back_to_baidu_when_legacy_api_missing(self):
        import app.datasource.service.facade as MX

        calls: list = []

        def fake_call(name, *args, **kwargs):
            calls.append((name, kwargs.get("indicator")))
            if name == MX._LEGACY_VALUATION_API:
                raise MX.MarketXError("akshare 没有接口 stock_a_indicator_lg")
            # 百度：返回 date/value 两列
            value = {"市盈率(TTM)": 5.18, "市净率": 0.48, "总市值": 2249.15}[
                kwargs.get("indicator")]
            return pd.DataFrame({
                "date": [date(2026, 10, 10), date(2026, 10, 11)],
                "value": [value - 0.1, value],
            })

        with mock.patch.object(MX, "_call", fake_call), \
             mock.patch.object(MX, "ak", SimpleNamespace()):   # 没有乐咕接口
            got = MX.fetch_valuation("000001")

        self.assertEqual(got["pe"], 5.18)
        self.assertEqual(got["pb"], 0.48)
        # 百度单位是亿元，库里统一存元
        self.assertAlmostEqual(got["market_cap"], 2249.15 * 1e8, places=0)
        self.assertEqual(got["date"], date(2026, 10, 11))
        self.assertNotIn(MX._LEGACY_VALUATION_API, [c[0] for c in calls])

    def test_uses_legacy_api_when_available(self):
        import app.datasource.service.facade as MX

        legacy = pd.DataFrame({
            "trade_date": [date(2026, 10, 11)],
            "pe": [5.2], "pb": [0.5], "ps_ttm": [1.1], "dv_ratio": [3.2],
            "total_mv": [22491500.0],      # 万元
            "circ_mv": [22490000.0],
        })

        def fake_call(name, *args, **kwargs):
            if name == MX._LEGACY_VALUATION_API:
                return legacy
            raise AssertionError("有老接口时不该再走百度")

        with mock.patch.object(MX, "_call", fake_call), \
             mock.patch.object(MX, "ak",
                               SimpleNamespace(**{MX._LEGACY_VALUATION_API: lambda *a, **k: legacy})):
            got = MX.fetch_valuation("000001")

        self.assertEqual(got["pe"], 5.2)
        self.assertEqual(got["ps"], 1.1)
        self.assertAlmostEqual(got["market_cap"], 22491500.0 * 1e4, places=0)

    def test_partial_baidu_failure_still_returns(self):
        """单个指标拿不到不该让整个估值维度全空。"""
        import app.datasource.service.facade as MX

        def fake_call(name, *args, **kwargs):
            if kwargs.get("indicator") == "市净率":
                raise MX.MarketXError("接口抽风")
            return pd.DataFrame({"date": [date(2026, 10, 11)], "value": [5.18]})

        with mock.patch.object(MX, "_call", fake_call), \
             mock.patch.object(MX, "ak", SimpleNamespace()):
            got = MX.fetch_valuation("000001")
        self.assertEqual(got["pe"], 5.18)
        self.assertIsNone(got["pb"])


class TestLimitupPoolFallback(unittest.TestCase):
    """涨停池：早盘当日数据还没生成，要回退上一交易日。"""

    def test_falls_back_to_previous_trading_day(self):
        """今天早盘还没有当日数据 → 自动取上一个交易日的。"""
        import app.datasource.service.facade as MX

        tried: list = []

        def fake_call(name, *args, **kwargs):
            day = kwargs.get("date")
            tried.append(day)
            if day == "20261012":                     # 今天：早盘无数据
                raise MX.MarketXError("stock_zt_pool_em 返回空数据")
            return pd.DataFrame({
                "代码": ["600812"], "名称": ["华北制药"], "涨跌幅": [10.0],
                "最新价": [8.8], "成交额": [1.0], "封板资金": [1.0],
                "首次封板时间": ["09:31:00"], "最后封板时间": ["09:31:00"],
                "炸板次数": [0], "涨停统计": ["1/1"], "连板数": [3],
                "所属行业": ["医药"],
            })

        class _FixedCal:
            """固定日历：让"今天"不随运行日期漂移。"""

            @staticmethod
            def recent_trading_days(n: int = 20, until=None):
                return [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)]

        with mock.patch.object(MX, "_call", fake_call), \
             mock.patch("app.domain.calendar.get_calendar", lambda: _FixedCal()):
            df = MX.fetch_limitup_pool()

        # 最近的先试，今天没有就退到上一个交易日
        self.assertEqual(tried, ["20261012", "20261009"])
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["date"], date(2026, 10, 9))
        self.assertEqual(df.iloc[0]["continuous"], 3)

    def test_all_days_empty_raises_data_unavailable(self):
        """连续 3 个交易日都没有 → 才算真的没数据（调用方按"暂无"跳过）。"""
        import app.datasource.service.facade as MX
        from app.datasource.compat.registry import DataUnavailable

        def fake_call(name, *args, **kwargs):
            raise MX.MarketXError("stock_zt_pool_em 返回空数据（非交易日？接口变更？）")

        with mock.patch.object(MX, "_call", fake_call):
            with self.assertRaises(DataUnavailable):
                MX.fetch_limitup_pool()


class TestParseErrorNotNetwork(unittest.TestCase):
    """解析失败不能被当成域名不可达。"""

    def test_classify(self):
        from app.datasource.compat.registry import (
            is_network_error_text,
            is_parse_error,
            is_parse_error_text,
        )

        parse_exc = ValueError(
            "Length mismatch: Expected axis has 13 elements, new values have 7")
        net_exc = ConnectionError("Connection aborted")

        self.assertTrue(is_parse_error(parse_exc))
        self.assertFalse(is_parse_error(net_exc))
        self.assertTrue(is_parse_error_text("[akshare_flow] ValueError: Length mismatch"))
        self.assertTrue(is_network_error_text("ProxyError ..."))
        self.assertFalse(is_parse_error_text("Connection aborted"))

    def test_penalize_skips_host_mark_on_parse_error(self):
        """解析失败时不回写域名健康度 —— 否则"接口改版"被记成"域名挂了"。"""
        from app.datasource.compat.registry import SourceRegistry, SourceSpec

        reg = SourceRegistry(circuit_threshold=99, cooldown=1.0, persist_path=None)
        spec = SourceSpec(name="akshare_flow", fetch=lambda *a, **k: pd.DataFrame(),
                          supports={"flow_rank"}, hosts=["push2.eastmoney.com"])
        marked: list = []
        with mock.patch.object(reg, "_mark_hosts",
                               lambda s, ok, why="": marked.append((s.name, ok))):
            reg._penalize(spec, "ValueError: Length mismatch",
                          exc=ValueError("Length mismatch: Expected axis has 13, got 7"))
        self.assertEqual(marked, [], "解析失败不该标记域名失败")

        with mock.patch.object(reg, "_mark_hosts",
                               lambda s, ok, why="": marked.append((s.name, ok))):
            reg._penalize(spec, "ConnectionError", exc=ConnectionError("aborted"))
        self.assertEqual(len(marked), 1, "网络失败仍要标记域名")


class TestCalendarStateNaming(unittest.TestCase):
    """交易日清晨不能显示成"非交易日"。"""

    def test_early_morning_is_not_holiday(self):
        cal = TradeCalendar()
        # 2026-10-09 是周五（交易日），凌晨 3 点：今天开盘，只是还没到点
        self.assertTrue(cal.is_trading_day(date(2026, 10, 9)))
        self.assertEqual(cal.state(datetime(2026, 10, 9, 3, 0)), STATE_CLOSED)
        self.assertEqual(STATE_NAMES[STATE_CLOSED], "休市中")

    def test_real_holiday_still_holiday(self):
        cal = TradeCalendar()
        self.assertEqual(cal.state(datetime(2026, 10, 10, 10, 0)), "holiday")


if __name__ == "__main__":
    unittest.main()
