#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_data_fabric.py
# @Description : 数据采集层重构（Provider / Capability / Router / Canonical / Raw）
#
#                这份测试存在的理由：新架构的价值全在**语义**上 ——
#                "这个源没有这个能力"是不是真的不会被调用、
#                "akshare 的 K 线被熔断了"会不会连累它的人气榜、
#                "取到了错数据"能不能被拦下来。
#                这些都不是看代码能确认的，必须各写一条断言钉住。
#
#                原则：**全程不联网**。所有 handler 都是本地假实现；
#                每条用例都自建注册表（不碰全局 PROVIDERS），
#                避免健康分/熔断这种全局状态在用例之间互相污染。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
import unittest
from datetime import date
from typing import Optional

import pandas as pd

from app.datasource.model.enums import AdjustType, Frequency
from app.datasource.model.fundflow import FundFlowRow
from app.datasource.model.kline import KLINE_COLUMNS, Kline, KlineRequest
from app.datasource.model.rank import RankKind, RankRow
from app.datasource.provider.base import (
    DataCapability,
    DataProvider,
    ProviderError,
    ProviderErrorType,
    Route,
    as_provider_error,
    capability,
    classify,
)
from app.datasource.routing.config import ProviderConfig
from app.datasource.provider.health import CircuitBreaker, HealthBook, ProviderState
from app.datasource.routing.quality import check, check_kline, check_rank
from app.datasource.provider.registry import ProviderRegistry
from app.datasource.routing.router import DataRouter
from app.datasource.raw.raw_store import RawStore


def _good_kline(rows: int = 2) -> pd.DataFrame:
    return pd.DataFrame({
        "date": [date(2024, 1, 2 + i) for i in range(rows)],
        "symbol": ["600000"] * rows,
        "open": [10.0] * rows, "high": [10.8] * rows,
        "low": [9.5] * rows, "close": [10.5] * rows,
        "volume": [1000] * rows, "amount": [10500.0] * rows,
    })


def _good_rank(rows: int = 2) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["60000%d" % i for i in range(rows)],
        "name": ["甲", "乙"][:rows],
        "rank": list(range(1, rows + 1)),
        "pct_change": [1.0] * rows,
    })


def _registry(config: Optional[ProviderConfig] = None,
              cooldown: float = 300.0) -> ProviderRegistry:
    cfg = config or ProviderConfig(
        providers={}, routes={},
        router={"retries": 0, "backoff_base": 0.0, "circuit_threshold": 3,
                "cooldown": cooldown, "quality_check": True,
                "raw_enabled": False},
    )
    return ProviderRegistry(config=cfg)


def _router(reg: ProviderRegistry, **kw) -> DataRouter:
    r = DataRouter(registry=reg, **kw)
    r._sleep = lambda s: None          # 退避 sleep 换成空操作，测试不等
    return r


# ===========================================================================
# 一、Capability（能力）
# ===========================================================================
class TestCapability(unittest.TestCase):
    """能力是枚举：拼错在 import 时就炸，而不是半夜跑批才发现。"""

    def test_parse_legacy_kind(self):
        self.assertIs(DataCapability.parse("daily"), DataCapability.DAILY_KLINE)
        self.assertIs(DataCapability.parse("fund_flow"), DataCapability.FUND_FLOW)
        self.assertIs(DataCapability.parse("hot_rank"), DataCapability.HOT_RANK)

    def test_unknown_capability_raises(self):
        with self.assertRaises(ValueError) as ctx:
            DataCapability.parse("not_a_thing")
        self.assertIn("未知的数据能力", str(ctx.exception))

    def test_provider_declares_only_what_it_has(self):
        class P(DataProvider):
            name = "only_kline"

            @capability(DataCapability.DAILY_KLINE)
            def daily(self, symbol, **kw):
                return _good_kline()

        p = P()
        self.assertTrue(p.supports(DataCapability.DAILY_KLINE))
        self.assertFalse(p.supports(DataCapability.HOT_RANK))
        # 没声明的能力 → 路由里根本没有它，谈不上"调用后失败"
        self.assertNotIn(DataCapability.HOT_RANK, p.capabilities)

    def test_multiple_variants_of_same_capability(self):
        """同一 Provider 的多个端点要能各自注册、各自熔断。"""
        class P(DataProvider):
            name = "multi"

            @capability(DataCapability.HOT_RANK, name="multi_a")
            def a(self, top=None):
                return _good_rank()

            @capability(DataCapability.HOT_RANK, name="multi_b")
            def b(self, top=None):
                return _good_rank()

        reg = _registry()
        reg.register(P())
        self.assertEqual([r.name for r in reg.routes_for(DataCapability.HOT_RANK)],
                         ["multi_a", "multi_b"])
        # 熔断键必须互不相同
        keys = {r.key for r in reg.routes_for(DataCapability.HOT_RANK)}
        self.assertEqual(len(keys), 2)


# ===========================================================================
# 二、错误分类
# ===========================================================================
class TestErrorClassification(unittest.TestCase):
    """不同失败 = 不同处理。分类错了，后面全错。"""

    def _t(self, exc, expect):
        self.assertIs(classify(exc), expect, f"{exc!r} 应判为 {expect}")

    def test_network_errors(self):
        self._t(RuntimeError("ProxyError: HTTPSConnectionPool ..."),
                ProviderErrorType.NETWORK_ERROR)
        self._t(RuntimeError("Connection reset by peer"),
                ProviderErrorType.NETWORK_ERROR)

    def test_proxy_interception_page_is_network(self):
        """代理拦截页 → 响应是 HTML → JSONDecodeError。

        这是"响应不是我们想要的样子"，重试大概率能拿到正常响应，
        判成 PARSE_ERROR（不重试）就会白白丢掉一个其实还能用的源。
        """
        import json

        try:
            json.loads("<html>拦截</html>")
        except Exception as exc:
            self.assertIs(classify(exc), ProviderErrorType.NETWORK_ERROR)

    def test_timeout_and_rate_limit(self):
        self._t(TimeoutError("read timed out"), ProviderErrorType.TIMEOUT)
        self._t(RuntimeError("访问频率超限，请稍后再试"),
                ProviderErrorType.RATE_LIMIT)

    def test_blocked_and_auth(self):
        self._t(RuntimeError("403 Forbidden"), ProviderErrorType.BLOCKED)
        self._t(RuntimeError("TUSHARE token 无效"), ProviderErrorType.AUTH_ERROR)

    def test_no_data_is_not_a_fault(self):
        from app.datasource.compat.registry import DataUnavailable

        self._t(DataUnavailable("停牌，未返回数据"), ProviderErrorType.NO_DATA)
        self.assertNotIn(ProviderErrorType.NO_DATA,
                         __import__("app.datasource.provider.base",
                                    fromlist=["PENALIZE"]).PENALIZE)

    def test_parse_error_not_retried(self):
        self._t(ValueError("缺少必需列 ['close']"), ProviderErrorType.PARSE_ERROR)

    def test_provider_error_keeps_its_type(self):
        err = ProviderError("x", type=ProviderErrorType.BLOCKED)
        self.assertIs(classify(err), ProviderErrorType.BLOCKED)
        self.assertFalse(err.retryable)
        self.assertTrue(err.penalize)

    def test_as_provider_error_wraps(self):
        err = as_provider_error(RuntimeError("ProxyError"), provider="p")
        self.assertEqual(err.provider, "p")
        self.assertEqual(err.type, ProviderErrorType.NETWORK_ERROR)
        self.assertTrue(err.retryable)


# ===========================================================================
# 三、熔断器（能力级）
# ===========================================================================
class TestCircuitBreaker(unittest.TestCase):

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(threshold=3, cooldown=60)
        now = time.monotonic()
        cb.finish_failure(now)
        cb.finish_failure(now)
        self.assertEqual(cb.state, ProviderState.DEGRADED)
        cb.finish_failure(now)
        self.assertEqual(cb.state, ProviderState.OPEN)
        self.assertFalse(cb.allows(now))

    def test_half_open_after_cooldown_allows_single_probe(self):
        cb = CircuitBreaker(threshold=1, cooldown=10)
        now = time.monotonic()
        cb.finish_failure(now)
        self.assertEqual(cb.state, ProviderState.OPEN)
        # 冷却结束 → 半开，**只放一个探测**
        later = now + 11
        self.assertEqual(cb.state, ProviderState.OPEN)
        self.assertTrue(cb.begin(later))
        self.assertEqual(cb.state, ProviderState.HALF_OPEN)
        self.assertFalse(cb.begin(later), "半开期间只能有一个探测请求")

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(threshold=1, cooldown=10)
        now = time.monotonic()
        cb.finish_failure(now)
        later = now + 11
        cb.begin(later)
        cb.finish_failure(later, "探测也失败")
        self.assertEqual(cb.state, ProviderState.OPEN)
        self.assertGreater(cb.remaining(later), 0)

    def test_success_closes(self):
        cb = CircuitBreaker(threshold=3, cooldown=60)
        now = time.monotonic()
        cb.finish_failure(now)
        cb.finish_success(now)
        self.assertEqual(cb.state, ProviderState.HEALTHY)
        self.assertEqual(cb.consecutive_fail, 0)

    def test_capability_level_isolation(self):
        """akshare 的 K线被熔断，它的人气榜必须照常用 —— 这是能力级熔断的全部意义。"""
        book = HealthBook(threshold=2, cooldown=60)
        kline = ("akshare", "daily_kline", "akshare_daily")
        hot = ("akshare", "hot_rank", "akshare_em")
        now = time.monotonic()
        for _ in range(2):
            book.record(kline, False, error="被限流")
        self.assertEqual(book.breaker(kline).state, ProviderState.OPEN)
        self.assertFalse(book.breaker(kline).allows(now))
        # 同一个 provider，不同能力 —— 不受影响
        self.assertTrue(book.breaker(hot).allows(now))
        self.assertEqual(book.breaker(hot).state, ProviderState.HEALTHY)

    def test_no_data_is_neutral(self):
        """没这只票的数据不能算源失败，否则好源会被误杀。"""
        book = HealthBook(threshold=2)
        key = ("mootdx", "daily_kline", "mootdx")
        for _ in range(5):
            book.record(key, None, error="停牌")
        st = book.stats(key)
        self.assertEqual(st.fail, 0)
        self.assertEqual(st.neutral, 5)
        self.assertEqual(st.success_rate, 1.0)
        self.assertEqual(book.breaker(key).state, ProviderState.HEALTHY)


# ===========================================================================
# 四、路由决策
# ===========================================================================
class _Flaky(DataProvider):
    """测试用 Provider：两个能力，成败由外部注入。"""

    name = "flaky"

    def __init__(self):
        self.kline_fn = lambda **kw: _good_kline()
        self.hot_fn = lambda **kw: _good_rank()

    @capability(DataCapability.DAILY_KLINE, name="flaky_daily")
    def daily(self, symbol, start=None, end=None, adjust="qfq"):
        return self.kline_fn(symbol=symbol, start=start, end=end, adjust=adjust)

    @capability(DataCapability.HOT_RANK, name="flaky_hot")
    def hot(self, top=None):
        return self.hot_fn(top=top)


class TestRouter(unittest.TestCase):

    def _reg(self):
        reg = _registry()
        reg.register(_Flaky())
        return reg

    def test_capability_mismatch_not_called(self):
        """没有 Provider 声明该能力 → NOT_SUPPORTED，而不是"调用后失败"。"""
        reg = self._reg()
        calls = []
        reg.add_route(Route(name="x", provider="x",
                            capability=DataCapability.FINANCIAL_INDICATOR,
                            handler=lambda **kw: calls.append(1)))
        router = _router(reg)
        with self.assertRaises(ProviderError) as ctx:
            router.fetch(DataCapability.NEWS)
        self.assertEqual(ctx.exception.type, ProviderErrorType.NOT_SUPPORTED)
        # 能力不匹配的那条压根不该被调用
        self.assertEqual(calls, [])

    def test_switches_to_next_route_on_failure(self):
        reg = self._reg()
        p = reg.provider("flaky")
        p.kline_fn = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("ProxyError 被拦"))
        good = _good_kline(3)
        reg.add_route(Route(name="backup", provider="backup",
                            capability=DataCapability.DAILY_KLINE,
                            handler=lambda **kw: good, priority=10))
        # 备份路由排在后面：把它写进配置顺序的末尾即可
        reg.config.routes["daily_kline"] = ["flaky_daily", "backup"]
        router = _router(reg)
        result = router.fetch(DataCapability.DAILY_KLINE, symbol="600000")
        self.assertEqual(result.route, "backup")
        self.assertEqual(len(result.data), 3)
        self.assertEqual(len(result.attempts), 2)

    def test_quality_error_goes_to_next_route(self):
        """取到了但自相矛盾 → 按质量问题处理，换下一个源。"""
        reg = self._reg()
        bad = _good_kline(1)
        bad.loc[0, "high"] = 1.0          # high < close，明显错数据
        p = reg.provider("flaky")
        p.kline_fn = lambda **kw: bad
        reg.add_route(Route(name="backup", provider="backup",
                            capability=DataCapability.DAILY_KLINE,
                            handler=lambda **kw: _good_kline(2)))
        reg.config.routes["daily_kline"] = ["flaky_daily", "backup"]
        router = _router(reg)
        result = router.fetch(DataCapability.DAILY_KLINE, symbol="600000")
        self.assertEqual(result.route, "backup")
        types = [a.error_type for a in result.attempts]
        self.assertIn(ProviderErrorType.QUALITY_ERROR.value, types)

    def test_fallback_only_after_all_normal_routes_fail(self):
        """兜底源绝不能和正常源一起竞争 —— 否则会变成"有网也用旧数据"。"""
        reg = self._reg()
        p = reg.provider("flaky")
        p.hot_fn = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("ProxyError 被拦"))
        reg.add_route(Route(name="local_hot", provider="local",
                            capability=DataCapability.HOT_RANK,
                            handler=lambda **kw: _good_rank(1), fallback=True))
        reg.config.routes["hot_rank"] = ["flaky_hot"]
        router = _router(reg)
        result = router.fetch(DataCapability.HOT_RANK, top=5)
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.route, "local_hot")

    def test_params_filtered_by_handler_signature(self):
        """路由按适配器签名裁剪参数：多传的参数不能把调用搞崩。"""
        reg = self._reg()
        seen = {}

        def only_top(top=None):
            seen["top"] = top
            return _good_rank()

        reg.add_route(Route(name="sig", provider="sig",
                            capability=DataCapability.HOT_RANK,
                            handler=only_top, priority=100))
        reg.config.routes["hot_rank"] = ["sig"]
        router = _router(reg)
        # 路由手里有 trade_date（给别的源用的），但这条只认 top
        router.fetch(DataCapability.HOT_RANK, top=7, trade_date="2024-01-02")
        self.assertEqual(seen.get("top"), 7)

    def test_explicit_source(self):
        reg = self._reg()
        router = _router(reg)
        result = router.fetch(DataCapability.HOT_RANK, top=3, source="flaky_hot")
        self.assertEqual(result.route, "flaky_hot")
        with self.assertRaises(ProviderError) as ctx:
            router.fetch(DataCapability.HOT_RANK, source="nope")
        self.assertEqual(ctx.exception.type, ProviderErrorType.INVALID_REQUEST)

    def test_success_is_not_punished_by_latency(self):
        """刚成功的源不能被"没验证过"的源挤下去。

        这条是踩过的坑：如果按"距上次成功"算新鲜度，
        一个一直好好干活的源会随时间自己掉分，
        于是配置里的首选源用着用着就换人了 —— 而没人动过配置。
        """
        reg = self._reg()
        slow = _good_kline(2)
        reg.provider("flaky").kline_fn = lambda **kw: slow
        reg.add_route(Route(name="backup", provider="backup",
                            capability=DataCapability.DAILY_KLINE,
                            handler=lambda **kw: _good_kline()))
        reg.config.routes["daily_kline"] = ["flaky_daily", "backup"]
        router = _router(reg)
        router.fetch(DataCapability.DAILY_KLINE, symbol="600000")
        # 让"上次成功"是 40 分钟前：旧口径下这里会掉到 backup 后面
        key = ("flaky", "daily_kline", "flaky_daily")
        reg.health.stats(key).last_ok_at -= 2400
        order = [r.name for r in reg.ranking(DataCapability.DAILY_KLINE)]
        self.assertEqual(order[0], "flaky_daily")

    def test_failed_route_drops_behind(self):
        reg = self._reg()
        reg.provider("flaky").kline_fn = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("ProxyError 被拦"))
        reg.add_route(Route(name="backup", provider="backup",
                            capability=DataCapability.DAILY_KLINE,
                            handler=lambda **kw: _good_kline()))
        reg.config.routes["daily_kline"] = ["flaky_daily", "backup"]
        router = _router(reg)
        router.fetch(DataCapability.DAILY_KLINE, symbol="600000")
        st = reg.health.stats(("flaky", "daily_kline", "flaky_daily"))
        self.assertEqual(st.ok, 0)
        self.assertGreater(st.fail, 0)
        self.assertLess(st.score(), 60)

    def test_no_data_does_not_penalize(self):
        reg = self._reg()
        p = reg.provider("flaky")
        from app.datasource.compat.registry import DataUnavailable

        def no_data(**kw):
            raise DataUnavailable("停牌")

        p.kline_fn = no_data
        router = _router(reg)
        with self.assertRaises(ProviderError):
            router.fetch(DataCapability.DAILY_KLINE, symbol="600000")
        key = ("flaky", "daily_kline", "flaky_daily")
        self.assertEqual(reg.health.stats(key).fail, 0)
        self.assertEqual(reg.health.breaker(key).state, ProviderState.HEALTHY)

    def test_ranking_drops_unhealthy_route(self):
        """连续失败 → 健康分下降 → 排到后面（下一个成功会再涨回来）。"""
        reg = self._reg()
        p = reg.provider("flaky")
        p.kline_fn = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("ProxyError 被拦"))
        reg.add_route(Route(name="backup", provider="backup",
                            capability=DataCapability.DAILY_KLINE,
                            handler=lambda **kw: _good_kline()))
        reg.config.routes["daily_kline"] = ["flaky_daily", "backup"]
        router = _router(reg)
        router.fetch(DataCapability.DAILY_KLINE, symbol="600000")
        order = [r.name for r in reg.ranking(DataCapability.DAILY_KLINE)]
        self.assertEqual(order[0], "backup")


# ===========================================================================
# 五、质量校验
# ===========================================================================
class TestQuality(unittest.TestCase):

    def test_good_kline_passes(self):
        rep = check_kline(_good_kline())
        self.assertTrue(rep.ok, rep.summary())

    def test_high_lower_than_close_fails(self):
        df = _good_kline(1)
        df.loc[0, "high"] = 1.0
        rep = check_kline(df)
        self.assertFalse(rep.ok)
        self.assertTrue(any("high" in i for i in rep.issues))

    def test_negative_volume_fails(self):
        df = _good_kline(1)
        df.loc[0, "volume"] = -1
        self.assertFalse(check_kline(df).ok)

    def test_duplicate_date_fails(self):
        df = _good_kline(2)
        df.loc[1, "date"] = df.loc[0, "date"]
        self.assertFalse(check_kline(df).ok)

    def test_empty_is_failed(self):
        self.assertFalse(check_kline(pd.DataFrame()).ok)

    def test_rank_requires_symbol_only(self):
        """资金榜没有名次列是正常的（顺序即名次），不能算质量问题。"""
        df = pd.DataFrame({"symbol": ["600000"], "main_net": [1.0]})
        self.assertTrue(check_rank(df).ok)

    def test_rank_rejects_empty_symbol(self):
        df = pd.DataFrame({"symbol": [""], "rank": [1]})
        self.assertFalse(check_rank(df).ok)

    def test_check_dispatch_by_capability(self):
        self.assertTrue(check(DataCapability.DAILY_KLINE, _good_kline()).ok)
        self.assertTrue(check(DataCapability.HOT_RANK, _good_rank()).ok)


# ===========================================================================
# 六、Canonical Model（标准数据模型）
# ===========================================================================
class TestCanonicalModel(unittest.TestCase):

    def test_kline_roundtrip(self):
        df = _good_kline(2)
        rows = Kline.from_frame(df, source="t", provider="p")
        self.assertEqual(len(rows), 2)
        back = Kline.to_frame(rows)
        self.assertEqual(list(back.columns), KLINE_COLUMNS)
        self.assertEqual(back.iloc[0]["close"], 10.5)

    def test_kline_lineage(self):
        rows = Kline.from_frame(_good_kline(1), source="akshare_daily",
                                provider="akshare", raw_id="r-1")
        self.assertEqual(rows[0].source, "akshare_daily")
        self.assertEqual(rows[0].provider, "akshare")
        self.assertEqual(rows[0].raw_id, "r-1")
        self.assertIsInstance(rows[0].frequency, Frequency)
        self.assertIsInstance(rows[0].adjust, AdjustType)

    def test_kline_missing_column_raises(self):
        df = _good_kline(1).drop(columns=["close"])
        with self.assertRaises(ValueError):
            Kline.from_frame(df)

    def test_rank_row_keeps_extras(self):
        df = pd.DataFrame([{"symbol": "600000", "name": "甲", "rank": 1,
                            "pct_change": 1.0, "main_net": 2.0e8}])
        row = RankRow.from_frame(df, kind=RankKind.FLOW)[0]
        self.assertEqual(row.rank, 1)
        self.assertEqual(row.extras["main_net"], 2.0e8)

    def test_rank_derives_rank_when_missing(self):
        df = pd.DataFrame([{"symbol": "600000"}, {"symbol": "000001"}])
        rows = RankRow.from_frame(df)
        self.assertEqual([r.rank for r in rows], [1, 2])

    def test_fund_flow_row(self):
        df = pd.DataFrame([{"symbol": "600000", "date": date(2024, 1, 2),
                            "main_net": 1.0, "main_net_pct": 0.1}])
        row = FundFlowRow.from_frame(df)[0]
        self.assertEqual(row.main_net, 1.0)
        self.assertFalse(row.tiers_available)   # 五档缺失要能被识别

    def test_kline_request_params(self):
        req = KlineRequest(symbol="600519", start="2025-01-01", end="2025-06-01")
        params = req.to_params()
        self.assertEqual(params["symbol"], "600519")
        self.assertEqual(params["adjust"], "qfq")
        self.assertEqual(params["frequency"], "1d")


# ===========================================================================
# 七、Raw / Bronze 层
# ===========================================================================
class TestRawStore(unittest.TestCase):

    def test_save_and_load_and_recent(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = RawStore(root=str(Path(tmp) / "raw"), max_rows=1)
            raw_id = store.save(provider="akshare", capability="daily_kline",
                                route="akshare_daily", params={"symbol": "600000"},
                                frame=_good_kline(3))
            self.assertTrue(raw_id)
            rec = store.load(raw_id)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["rows"], 3)          # 总行数
            self.assertEqual(rec["stored_rows"], 1)   # 只存了样本
            self.assertTrue(rec["truncated"])
            self.assertEqual(rec["provider"], "akshare")
            recent = store.recent(5)
            self.assertEqual(len(recent), 1)
            self.assertNotIn("records", recent[0])    # 列表不返回明细，防内存炸

    def test_disabled_store_returns_empty(self):
        store = RawStore(root="/tmp/never", enabled=False)
        self.assertEqual(store.save(provider="p", capability="c", frame=None), "")


# ===========================================================================
# 八、配置
# ===========================================================================
class TestProviderConfig(unittest.TestCase):

    def test_defaults_when_file_missing(self):
        cfg = ProviderConfig.load("config/__definitely_not_exists__.json")
        self.assertIn("daily_kline", cfg.routes)
        self.assertTrue(cfg.enabled("baostock"))
        self.assertEqual(cfg.router_value("retries", 3), 3)

    def test_real_config_file(self):
        """工程里那份 providers.json 必须是可用的（否则上线才发现顺序不对）。"""
        cfg = ProviderConfig.load()
        self.assertIn("baostock_daily", cfg.route_order("daily_kline"))
        self.assertIn("emapp_hot", cfg.route_order("hot_rank"))
        # 兜底源必须排在最后 —— 由 routes 顺序 + fallback 标志共同保证
        self.assertNotIn("local_hot", cfg.route_order("hot_rank"))

    def test_disabled_provider(self):
        cfg = ProviderConfig(providers={"baostock": {"enabled": False}},
                             routes={}, router={})
        self.assertFalse(cfg.enabled("baostock"))
        self.assertTrue(cfg.enabled("akshare"))   # 没写 = 启用


if __name__ == "__main__":
    unittest.main()
