#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_kline_sources.py
# @Description : mootdx / a-stock-data 两个新源的适配器
#
#                测试原则：**不联网**。mootdx 本来就没装（可选依赖），
#                a-stock-data 走 HTTP —— 两者都用 mock 喂原始表，
#                验证的是"解析对不对、异常语义对不对"。
#
#                重点验证两件最容易错、且错了不会报错的事：
#                  1. **列顺序**。腾讯给的是 [开, 收, 高, 低]，
#                     照 OHLC 惯性取值会把收盘价当成最低价，值还在合理范围，
#                     静态检查完全看不出来；
#                  2. **可选依赖的降级语义**。没装 mootdx 必须抛
#                     DataUnavailable（源没坏，跳过即可），
#                     抛成 SourceError 会把一个"根本没装"的源反复熔断重试。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

import pandas as pd

from app.data.kline_base import DAILY_COLUMNS, normalize_daily
from app.data.sources import DataUnavailable, SourceError


class TestNormalizeDaily(unittest.TestCase):
    """归一化：列名各异 → 统一结构。"""

    def test_maps_chinese_columns(self):
        raw = pd.DataFrame({
            "日期": ["2024-01-02", "2024-01-03"],
            "开盘": [1.0, 1.1], "最高": [1.5, 1.6],
            "最低": [0.9, 0.8], "收盘": [1.2, 1.3],
            "成交量": [100, 200], "成交额": [120.0, 260.0],
        })
        out = normalize_daily(raw, "000001")
        self.assertEqual(list(out.columns), DAILY_COLUMNS)
        self.assertEqual(out.iloc[0]["date"], date(2024, 1, 2))
        self.assertEqual(out.iloc[0]["symbol"], "000001")
        self.assertEqual(out.iloc[1]["close"], 1.3)

    def test_maps_tdx_english_columns(self):
        """通达信/mootdx 的列：vol 而不是 volume。"""
        raw = pd.DataFrame({
            "datetime": ["2024-01-02", "2024-01-03"],
            "open": [1.0, 1.1], "close": [1.2, 1.3],
            "high": [1.5, 1.6], "low": [0.9, 0.8],
            "vol": [100, 200],
        })
        out = normalize_daily(raw, "600000")
        self.assertEqual(list(out["volume"]), [100.0, 200.0])
        self.assertTrue(out["amount"].isna().all())   # 无成交额列 → 留空

    def test_date_range_and_dedup(self):
        raw = pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03", "2024-01-03", "2024-01-08"],
            "open": [1, 1, 9, 1], "high": [2, 2, 9, 2],
            "low": [0.5, 0.5, 9, 0.5], "close": [1.5, 1.5, 9, 1.5],
            "volume": [1, 1, 1, 1],
        })
        out = normalize_daily(raw, "000001", start="2024-01-02", end="2024-01-05")
        self.assertEqual(len(out), 2)                   # 区间外被裁掉，重复日被去
        # 重复日保留后出现的那条（后到的更正数据优先）
        self.assertEqual(out.iloc[1]["close"], 9.0)
        self.assertEqual(out.iloc[0]["close"], 1.5)

    def test_missing_required_column_raises(self):
        raw = pd.DataFrame({"foo": [1]})
        with self.assertRaises(ValueError) as ctx:
            normalize_daily(raw, "000001")
        self.assertIn("缺少必需列", str(ctx.exception))


class TestMootdxAdapter(unittest.TestCase):
    """mootdx：可选依赖，未安装 → DataUnavailable；装了 → 解析正确。"""

    def setUp(self):
        import app.data.mootdx_source as M

        self.M = M
        M.reset_client()

    def test_missing_package_is_data_unavailable(self):
        """没装 mootdx 是"这条路径不可用"，不是"源坏了" —— 不该被熔断重试。"""
        with mock.patch.object(self.M, "_local_reader", return_value=None):
            with mock.patch.dict("sys.modules", {"mootdx": None,
                                                 "mootdx.quotes": None}):
                with self.assertRaises(DataUnavailable) as ctx:
                    self.M._mootdx_daily("600000")
        self.assertIn("mootdx", str(ctx.exception))

    def test_parses_online_bars(self):
        """线上 Quotes.bars 的返回：open/close/high/low/vol。"""
        raw = pd.DataFrame({
            "datetime": ["2024-01-02", "2024-01-03"],
            "open": [10.0, 10.2], "close": [10.5, 10.1],
            "high": [10.8, 10.4], "low": [9.9, 9.8],
            "vol": [1000, 1200],
        })
        with mock.patch.object(self.M, "_from_local", return_value=None), \
             mock.patch.object(self.M, "_from_online", return_value=raw):
            out = self.M._mootdx_daily("600000")
        self.assertEqual(len(out), 2)
        self.assertEqual(out.iloc[0]["close"], 10.5)
        self.assertEqual(out.iloc[0]["low"], 9.9)
        self.assertEqual(out.iloc[0]["symbol"], "600000")

    def test_local_reader_preferred(self):
        """配了本地通达信目录时优先读本地：不联网、不限流。"""
        raw = pd.DataFrame({
            "datetime": ["2024-01-02"], "open": [1.0], "close": [1.2],
            "high": [1.3], "low": [0.9], "vol": [10],
        })
        with mock.patch.object(self.M, "_from_local", return_value=raw) as local, \
             mock.patch.object(self.M, "_from_online",
                               side_effect=AssertionError("不该走联网")):
            out = self.M._mootdx_daily("600000")
        local.assert_called_once()
        self.assertEqual(len(out), 1)

    def test_empty_result_is_data_unavailable(self):
        raw = pd.DataFrame()
        with mock.patch.object(self.M, "_from_local", return_value=None), \
             mock.patch.object(self.M, "_from_online", return_value=raw):
            with self.assertRaises(DataUnavailable):
                self.M._mootdx_daily("600000")

    def test_connection_failure_is_source_error(self):
        """连不上通达信服务器是源级故障 → 熔断 + 降权。"""
        with mock.patch.object(self.M, "_from_local", return_value=None), \
             mock.patch.object(self.M, "_from_online",
                               side_effect=SourceError("TCP 超时")):
            with self.assertRaises(SourceError):
                self.M._mootdx_daily("600000")


class TestAStockDataAdapter(unittest.TestCase):
    """a-stock-data 行情层（腾讯 K线）。"""

    def setUp(self):
        import app.data.astockdata_source as A

        self.A = A

    #: 腾讯返回体的形状：data.<code>.qfqday = [[日期,开,收,高,低,量], ...]
    def _payload(self):
        return {
            "code": 0,
            "data": {
                "sh600000": {
                    "qfqday": [
                        ["2024-01-02", "9.00", "9.50", "9.60", "8.90", "123456"],
                        ["2024-01-03", "9.50", "9.30", "9.70", "9.20", "234567"],
                    ]
                }
            },
        }

    def test_column_order_open_close_high_low(self):
        """腾讯的顺序是 开/收/**高/低**，不是 OHLC —— 取错不会报错，只能靠断言。"""
        with mock.patch.object(self.A, "_http_get", return_value=self._payload()):
            out = self.A._astockdata_daily("600000")
        row = out.iloc[0]
        self.assertEqual(row["open"], 9.0)
        self.assertEqual(row["close"], 9.5)
        self.assertEqual(row["high"], 9.6)
        self.assertEqual(row["low"], 8.9)
        self.assertEqual(row["volume"], 123456.0)
        # 腾讯 K线不返回成交额，留空而不是编一个
        self.assertTrue(pd.isna(row["amount"]))

    def test_uses_qfq_key(self):
        with mock.patch.object(self.A, "_http_get",
                               return_value=self._payload()) as get:
            self.A._astockdata_daily("600000", adjust="qfq")
        self.assertIn("qfq", get.call_args[0][1]["param"])
        self.assertIn("sh600000", get.call_args[0][1]["param"])

    def test_market_prefix(self):
        self.assertEqual(self.A._param("600000").split(",")[0], "sh600000")
        self.assertEqual(self.A._param("000001").split(",")[0], "sz000001")

    def test_empty_series_is_data_unavailable(self):
        """新股/停牌：接口通了但序列为空 = 数据缺失，不该熔断。"""
        with mock.patch.object(self.A, "_http_get",
                               return_value={"code": 0, "data": {"sz000001": {"qfqday": []}}}):
            with self.assertRaises(DataUnavailable):
                self.A._astockdata_daily("000001")

    def test_non_json_response_is_source_error(self):
        """代理拦截页表现成"返回体不是 JSON"，必须按源故障处理（可重试/换源）。

        适配器已改为走统一传输层 ``net``，所以这里 mock 的是 ``net.request``：
        让它返回一个"内容是 HTML 拦截页"的响应，json() 必然失败，
        传输层把它翻译成 NetworkError，适配器再翻译成 SourceError。
        """
        from app.data import net

        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("not json")
        with mock.patch.object(net, "request", return_value=resp):
            with self.assertRaises(SourceError):
                self.A.fetch_tx_kline("600000")

    def test_http_get_parses_json(self):
        from app.data import net

        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"code": 0}
        with mock.patch.object(net, "request", return_value=resp):
            self.assertEqual(self.A._http_get("http://x", {"a": 1}), {"code": 0})


def _daily_frame():
    return pd.DataFrame({
        "date": [date(2024, 1, 2)], "open": [1.0], "high": [1.5],
        "low": [0.9], "close": [1.2], "volume": [10], "amount": [12.0],
    })


class TestDailyRouteChain(unittest.TestCase):
    """日线走 **Provider Router**：源挂了自动换下一个。

    迁移说明：这几个用例以前操作的是旧注册表 ``REGISTRY``，
    现在改操作 ``PROVIDERS``（能力声明 + 能力级熔断 + 健康分排序）。
    断言的仍是同一件事：**取数入口不关心有几个源、谁先谁后**。
    """

    def setUp(self):
        import app.data.marketx  # noqa: F401  触发装配
        from app.data import net
        from app.data.providers import PROVIDERS

        self.PROVIDERS = PROVIDERS
        # 熔断/健康分/域名健康度都是**全局状态**，用例之间会互相污染：
        # 上一个用例把源打进冷却，下一个就会跳过它落到别的源上，
        # 于是"降级顺序"的断言随机失败。每个用例前全部重置。
        PROVIDERS.reset()
        net.reset()

    def test_daily_routes_registered(self):
        from app.data.kline_sources import daily_sources

        names = daily_sources()
        self.assertIn("baostock_daily", names)
        self.assertIn("astockdata", names)
        self.assertIn("mootdx", names)

    def test_fetch_daily_switches_route_on_failure(self):
        import app.data.kline_sources as K
        from app.data.providers.base import DataCapability, Route

        def boom(**kw):
            raise RuntimeError("主源挂了: ProxyError")

        def ok(**kw):
            return _daily_frame()

        # 只留这两个临时路由，避免真实源参与（本用例要验的是换源逻辑本身）
        saved = dict(self.PROVIDERS._routes)
        try:
            self.PROVIDERS._routes.clear()
            self.PROVIDERS.add_route(Route(
                name="t_a", provider="test", capability=DataCapability.DAILY_KLINE,
                handler=boom))
            self.PROVIDERS.add_route(Route(
                name="t_b", provider="test", capability=DataCapability.DAILY_KLINE,
                handler=ok))
            df = K.fetch_daily("600000", retries=0)
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["close"], 1.2)
        finally:
            self.PROVIDERS._routes.clear()
            self.PROVIDERS._routes.update(saved)

    def test_fetch_daily_raises_when_all_routes_fail(self):
        import app.data.kline_sources as K
        from app.data.providers import PROVIDERS
        from app.data.providers.base import DataCapability, Route

        saved = dict(PROVIDERS._routes)
        try:
            PROVIDERS._routes.clear()
            PROVIDERS.add_route(Route(
                name="t_x", provider="test", capability=DataCapability.DAILY_KLINE,
                handler=lambda **kw: (_ for _ in ()).throw(
                    RuntimeError("全部挂了")), priority=100))
            with self.assertRaises(RuntimeError) as ctx:
                K.fetch_daily("600000", retries=0)
            self.assertIn("日线获取失败", str(ctx.exception))
        finally:
            PROVIDERS._routes.clear()
            PROVIDERS._routes.update(saved)

    def test_explicit_source_bypasses_chain(self):
        """指定源排障：不走排序。"""
        import app.data.kline_sources as K
        from app.data.providers import PROVIDERS

        route = PROVIDERS.route("astockdata")
        self.assertIsNotNone(route, "astockdata 路由应已注册")
        with mock.patch.object(route, "handler", return_value=_daily_frame()):
            df = K.fetch_daily("600000", source="astockdata")
        self.assertEqual(len(df), 1)


if __name__ == "__main__":
    unittest.main()
