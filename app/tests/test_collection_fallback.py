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

import threading
import time
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
        import app.providers.market.service.facade as MX

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
        import app.providers.market.service.facade as MX

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
        import app.providers.market.service.facade as MX

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
        import app.providers.market.service.facade as MX

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
        import app.providers.market.service.facade as MX
        from app.providers.market.compat.registry import DataUnavailable

        def fake_call(name, *args, **kwargs):
            raise MX.MarketXError("stock_zt_pool_em 返回空数据（非交易日？接口变更？）")

        with mock.patch.object(MX, "_call", fake_call):
            with self.assertRaises(DataUnavailable):
                MX.fetch_limitup_pool()


class TestParseErrorNotNetwork(unittest.TestCase):
    """解析失败不能被当成域名不可达。"""

    def test_classify(self):
        from app.providers.market.compat.registry import (
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
        from app.providers.market.compat.registry import SourceRegistry, SourceSpec

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


class TestFlowRankKindAlias(unittest.TestCase):
    """资金榜视角参数：少写"排行"二字就会 100% 抛 ValueError。

    akshare 的 ``stock_fund_flow_individual`` 只认 ``3日排行``/``5日排行``/
    ``10日排行``，内部是等值判断 + else 落到即时分支。传 ``"3日"`` 会
    用**即时**的 URL 拉回 10 列、再按多周期的 7 个列名赋值，于是必然
    ``ValueError: Length mismatch``。曾表现为"资金榜只剩即时一个视角"，
    资金流兜底序列被压到 1 行，资金链维度整体失效。
    """

    def test_alias_normalized(self):
        from app.providers.market.service.facade import _flow_rank_kind

        self.assertEqual(_flow_rank_kind("即时"), "即时")
        self.assertEqual(_flow_rank_kind("3日"), "3日排行")
        self.assertEqual(_flow_rank_kind("5日"), "5日排行")
        self.assertEqual(_flow_rank_kind("10日"), "10日排行")
        # 已经是规范名的不能再加后缀
        self.assertEqual(_flow_rank_kind("3日排行"), "3日排行")

    def test_unknown_falls_back_to_realtime(self):
        from app.providers.market.service.facade import _flow_rank_kind

        self.assertEqual(_flow_rank_kind("垃圾输入"), "即时")
        self.assertEqual(_flow_rank_kind(None), "即时")

    def test_multi_period_columns_parsed(self):
        """多周期视角是 7 列、列名不同且**没有成交额**，必须也能解析。"""
        import pandas as pd

        from app.providers.market.service import facade as MX

        df = pd.DataFrame({
            "序号": [1],
            "股票代码": ["000001"],
            "股票简称": ["平安银行"],
            "最新价": [11.0],
            "阶段涨跌幅": ["3.20%"],
            "连续换手率": ["5.10%"],
            "资金流入净额(元)": ["1.20亿"],
        })
        with mock.patch.object(MX, "_call", lambda *a, **k: df):
            out = MX._flow_rank_akshare(rank_kind="3日")
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["symbol"], "000001")
        # 1.20亿 = 1.2e8
        self.assertAlmostEqual(out.iloc[0]["main_net"], 1.2e8, delta=1e6)
        # 多周期没有成交额列，净占比算不出来 —— 留 None，不能造假
        self.assertIsNone(out.iloc[0]["main_net_pct"])

    def test_multi_period_kind_reaches_akshare(self):
        """真正传给 akshare 的必须是带"排行"的规范名。"""
        import pandas as pd

        from app.providers.market.service import facade as MX

        df = pd.DataFrame({"股票代码": ["000001"], "名称": ["平安银行"],
                           "涨跌幅": [1.0], "净额": [1.0], "成交额": [10.0]})
        seen: dict = {}

        def fake_call(name, *a, **kw):
            seen.update(kw)
            return df

        with mock.patch.object(MX, "_call", fake_call):
            MX._flow_rank_akshare(rank_kind="5日")
        self.assertEqual(seen.get("symbol"), "5日排行")


class TestBatchBreaker(unittest.TestCase):
    """逐票接口必须有批量熔断：源挂了就别让 60 只票各重试 3 次。"""

    def setUp(self):
        from app.providers.market.service import facade as MX

        self.MX = MX
        MX._BATCH_BREAKER.clear()

    def tearDown(self):
        self.MX._BATCH_BREAKER.clear()

    def test_opens_after_consecutive_failures(self):
        MX = self.MX
        err = ConnectionError("Connection aborted")

        def boom():
            raise err

        for _ in range(MX._BATCH_BREAKER_THRESHOLD):
            with self.assertRaises(Exception):
                MX._breaker_guard("unit-x", boom)
        self.assertIsNotNone(MX._breaker_open("unit-x"),
                             "连续失败后应熔断，否则整批票会各自重试")

    def test_tripped_call_does_not_invoke_source(self):
        """熔断期间绝不能再发请求 —— 这正是限流的放大器。"""
        MX = self.MX
        err = ConnectionError("Connection aborted")
        def boom():
            raise err

        for _ in range(MX._BATCH_BREAKER_THRESHOLD):
            with self.assertRaises(Exception):
                MX._breaker_guard("unit-y", boom)

        calls = {"n": 0}

        def counted():
            calls["n"] += 1
            raise err

        with self.assertRaises(Exception) as ctx:
            MX._breaker_guard("unit-y", counted)
        self.assertEqual(calls["n"], 0, "熔断中还发请求，等于白重试")
        self.assertIn("熔断", str(ctx.exception))

    def test_success_resets(self):
        import pandas as pd

        MX = self.MX
        err = ConnectionError("Connection aborted")
        def boom():
            raise err

        for _ in range(MX._BATCH_BREAKER_THRESHOLD - 1):
            with self.assertRaises(Exception):
                MX._breaker_guard("unit-z", boom)
        self.assertIsNone(MX._breaker_open("unit-z"))
        MX._breaker_guard("unit-z", lambda: pd.DataFrame({"a": [1]}))
        self.assertEqual(MX._BATCH_BREAKER.get("unit-z", {}).get("fails", 0), 0)

    def test_data_error_does_not_trip(self):
        """'这只票没数据'是数据问题，不该让整批票跟着跳过。"""
        MX = self.MX

        def no_data():
            raise MX.MarketXError("stock_cyq_em 返回空数据")

        for _ in range(MX._BATCH_BREAKER_THRESHOLD + 2):
            with self.assertRaises(MX.MarketXError):
                MX._breaker_guard("unit-w", no_data)
        self.assertIsNone(MX._breaker_open("unit-w"),
                          "非瞬时错误不该触发熔断")


class TestSnapshotSeriesSynthesis(unittest.TestCase):
    """快照合成序列：多周期是**累计值**，不能直接当单日净额。"""

    def test_cumulative_is_averaged_to_daily(self):
        """10 日累计 10 亿应摊成日均 1 亿，而不是某天流入 10 亿。"""
        import pandas as pd

        from app.providers.market.service import facade as MX

        frames = {}
        for kind, val in (("即时", 1e8), ("3日", 3e8), ("5日", 5e8), ("10日", 10e8)):
            frames[kind] = pd.DataFrame({
                "symbol": ["000001"], "name": ["x"], "pct_change": [1.0],
                "main_net": [val], "main_net_pct": [0.1],
            })
        with mock.patch.object(MX, "_ff_snapshot",
                               lambda k: frames.get(k, pd.DataFrame())), \
             mock.patch.object(MX, "_ff_anchor_day", lambda: date(2026, 10, 12)):
            out = MX._fund_flow_from_snapshot("000001", days=None)

        self.assertEqual(len(out), 4, "四个视角都要进序列")
        # 摊成日均后四个点应在同一量级（原值 1/3/5/10 亿 → 日均都约 1 亿）
        vals = list(out["main_net"])
        for v in vals:
            self.assertAlmostEqual(v, 1e8, delta=1e7)


class TestAdaptiveThrottle(unittest.TestCase):
    """撞到限流后必须放慢，否则一輪 60 只票会被持续打掉。"""

    def setUp(self):
        from app.providers.market.service import facade as MX

        self.MX = MX
        MX._THROTTLE_GAP.clear()

    def tearDown(self):
        self.MX._THROTTLE_GAP.clear()

    def test_penalize_then_relax(self):
        MX = self.MX
        self.assertEqual(MX._throttle_gap("unit-t"),
                         float(__import__("app.common.config",
                                          fromlist=["settings"]).settings
                               .DATA_FETCH_MIN_INTERVAL))
        MX._throttle_penalize("unit-t")
        first = MX._throttle_gap("unit-t")
        self.assertGreater(first, 0.3)
        MX._throttle_penalize("unit-t")
        self.assertGreater(MX._throttle_gap("unit-t"), first)
        MX._throttle_relax("unit-t")
        self.assertLess(MX._throttle_gap("unit-t"),
                        MX._throttle_gap("unit-t") * 2 + 1)

    def test_capped(self):
        MX = self.MX
        for _ in range(20):
            MX._throttle_penalize("unit-c")
        self.assertLessEqual(MX._throttle_gap("unit-c"), MX._THROTTLE_GAP_MAX,
                             "退避要封顶，否则一次限流能把采集拖到不可用")

    def test_success_relaxes(self):
        """成功后要回落 —— 否则一次抖动会让后续采集长期变慢。"""
        import pandas as pd

        MX = self.MX
        MX._THROTTLE_GAP["unit-r"] = 4.0
        with mock.patch.object(MX, "_require_akshare", lambda: None), \
             mock.patch.object(MX.ak, "fake_ok",
                               lambda *a, **k: pd.DataFrame({"a": [1]}),
                               create=True):
            MX._call("fake_ok", limit_key="unit-r", retry=0)
        self.assertLess(MX._throttle_gap("unit-r"), 4.0)

    def test_concurrent_calls_are_serialized_not_batched(self):
        """并发下限速必须仍然生效 —— 这是限速最容易失效的地方。

        实现若退化成「读上次时间 → 各自 sleep」，N 个线程会读到
        同一个 ``last``、算出同样的等待，睡醒后**同一瞬间一起打出去**，
        限速形同虚设，源端该 RST 还是 RST。

        正确的做法是"预约时刻"：每个线程领到不同的发车时刻，排成一列。
        这里断言：8 个并发线程的实际发车时刻两两间隔 >= gap。
        """
        import threading

        MX = self.MX
        key = "unit-concurrent"
        MX._THROTTLE_GAP[key] = 0.2      # 200ms，够小不至于拖慢测试
        fired: list = []
        lock = threading.Lock()

        def worker():
            MX._throttle(key)
            with lock:
                fired.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        fired.sort()

        gaps = [b - a for a, b in zip(fired, fired[1:])]
        self.assertEqual(len(fired), 8)
        # 留一点浮点/调度余量，但不允许出现"几乎同时发车"
        self.assertTrue(all(g >= 0.2 * 0.8 for g in gaps),
                        f"并发下限速被绕过，实际间隔: {[round(g, 3) for g in gaps]}")

    def test_breaker_counts_are_not_lost_under_concurrency(self):
        """熔断计数并发下不能丢。``fails += 1`` 是读改写，无锁会丢计数，
        表现是"明明失败了很多次，熔断却迟迟不触发"。"""
        import threading

        MX = self.MX
        key = "unit-breaker-concurrent"
        MX._BATCH_BREAKER.pop(key, None)

        err = ConnectionError("Connection aborted")

        def fail():
            def boom():
                raise err
            try:
                MX._breaker_guard(key, boom)
            except Exception:
                pass

        # 阈值抬到并发数：只要**丢一次**计数就打不开熔断，
        # 用例就能抓到。（用默认阈值 3 的话，10 次并发里丢几次也照样能开，
        # 验不出问题。）
        with mock.patch.object(MX, "_BATCH_BREAKER_THRESHOLD", 10):
            threads = [threading.Thread(target=fail) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        st = MX._BATCH_BREAKER.get(key) or {}
        self.assertGreaterEqual(int(st.get("fails") or 0), 10,
                                f"并发下丢了熔断计数: {st}")
        self.assertIsNotNone(MX._breaker_open(key), "达到阈值就该断开")
        MX._BATCH_BREAKER.pop(key, None)


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
