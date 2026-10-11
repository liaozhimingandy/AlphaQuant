#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_marketx_retry_sources.py
# @Description : 资金流"瞬时故障重试 + 多数据源兜底"的行为测试
#
#                本机真实环境里，资金流逐日接口走东财 push2his 域名，
#                常被本地代理(127.0.0.1:59258)拦成 ProxyError —— 典型表现为
#                "资金流同步有 68 只失败（已跳过）"。这曾经被误判成"API 限流"：
#                其实没有 429，是代理拦截。因此这里覆盖三条防线：
#                  1. _call 对 ProxyError/连接类瞬时错误做指数退避重试；
#                  2. akshare 主源失败后，切"全市场快照"(push2 域名)兜底并熔断；
#                  3. 配置 TUSHARE_TOKEN 时，再切 Tushare moneyflow 作独立源。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import date
from unittest import mock

import pandas as pd

from app.core.pool import sync as S
from app.data import marketx as MX
from app.data.sources import (
    REGISTRY,
    DataUnavailable,
    SourceError,
    SourceRegistry,
    SourceSpec,
    is_transient,
)


class ProxyError(Exception):  # noqa: N818 - 名字故意与 requests.exceptions.ProxyError 对齐
    """模拟 requests 的 ProxyError（`_is_retryable` 按类型名判定）。"""


class NotRetryableError(Exception):
    """非瞬时错误（如接口变更/缺列），不该重试。"""


def _raw_rank_df(code: str, main: float, amount: float) -> pd.DataFrame:
    """东财排名接口的**原始**形状：只有 净额/成交额，没有净占比列。"""
    return pd.DataFrame([
        {"序号": 1, "股票代码": int(code), "股票简称": "测试股",
         "最新价": 10.0, "涨跌幅": "1.50%", "换手率": "2.00%",
         "净额": f"{main / 1e8:.2f}亿", "成交额": f"{amount / 1e8:.2f}亿"},
    ])


def _raw_rank_df_multi(codes) -> pd.DataFrame:
    """东财排名接口的原始形状（多只票），供"整批"场景用。"""
    rows = []
    for i, code in enumerate(codes):
        rows.append({"序号": i + 1, "股票代码": int(code), "股票简称": f"测试{i}",
                     "最新价": 10.0, "涨跌幅": "1.50%", "换手率": "2.00%",
                     "净额": "1.00亿", "成交额": "10.00亿"})
    return pd.DataFrame(rows)


def _raw_daily_df(code: str) -> pd.DataFrame:
    """逐日资金流接口的原始形状。"""
    return pd.DataFrame([
        {"日期": "2026-10-09", "收盘价": 10.0, "涨跌幅": "1.50%",
         "主力净流入-净额": "1.00亿", "主力净流入-净占比": "5.00%",
         "成交额": "20.00亿", "换手率": "2.00%"},
        {"日期": "2026-10-10", "收盘价": 10.2, "涨跌幅": "2.00%",
         "主力净流入-净额": "2.00亿", "主力净流入-净占比": "8.00%",
         "成交额": "25.00亿", "换手率": "2.50%"},
    ])


class TestIsRetryable(unittest.TestCase):
    """只有"瞬时网络类"错误才值得重试。"""

    def test_proxy_error_is_retryable(self):
        self.assertTrue(MX._is_retryable(ProxyError("blocked by proxy")))

    def test_parser_error_is_not_retryable(self):
        # 接口变更/缺列是确定性错误，重试没用，必须立刻上报
        self.assertFalse(MX._is_retryable(NotRetryableError("缺少主力净流入列")))

    def test_markterxerror_wrapping_proxy_is_retryable(self):
        exc = MX.MarketXError(
            "stock_individual_fund_flow 调用失败: ProxyError: "
            "HTTPSConnectionPool(host='push2his.eastmoney.com', port=443): Max retries"
        )
        self.assertTrue(MX._is_retryable(exc))


class TestCallRetry(unittest.TestCase):
    def setUp(self):
        self.retry_patch = mock.patch.object(MX.settings, "DATA_FETCH_MAX_RETRY", 2)
        self.retry_patch.start()
        self.sleep_patch = mock.patch.object(MX.time, "sleep")
        self.sleep = self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.retry_patch.stop()

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ProxyError("间歇代理拦截")
            return _raw_daily_df("600000")

        with mock.patch.object(MX.ak, "stock_individual_fund_flow", side_effect=flaky):
            df = MX._call("stock_individual_fund_flow", stock="600000",
                          market="sh", limit_key="fundflow")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(df), 2)

    def test_gives_up_after_max_retry(self):
        calls = {"n": 0}

        def always_fail(**kwargs):
            calls["n"] += 1
            raise ProxyError("一直被拦")

        with mock.patch.object(MX.ak, "stock_individual_fund_flow", side_effect=always_fail):
            with self.assertRaises(MX.MarketXError) as ctx:
                MX._call("stock_individual_fund_flow", stock="600000",
                         market="sh", limit_key="fundflow")
        # 最多 MAX_RETRY+1 次调用
        self.assertEqual(calls["n"], 3)
        self.assertIn("ProxyError", str(ctx.exception))

    def test_non_retryable_not_retried(self):
        calls = {"n": 0}

        def deterministic(**kwargs):
            calls["n"] += 1
            raise NotRetryableError("接口已变更")

        with mock.patch.object(MX.ak, "stock_individual_fund_flow", side_effect=deterministic):
            with self.assertRaises(MX.MarketXError):
                MX._call("stock_individual_fund_flow", stock="600000",
                         market="sh", limit_key="fundflow")
        self.assertEqual(calls["n"], 1)

    def test_empty_result_not_retried(self):
        calls = {"n": 0}

        def empty(**kwargs):
            calls["n"] += 1
            return pd.DataFrame()

        with mock.patch.object(MX.ak, "stock_individual_fund_flow", side_effect=empty):
            with self.assertRaises(MX.MarketXError) as ctx:
                MX._call("stock_individual_fund_flow", stock="600000",
                         market="sh", limit_key="fundflow")
        self.assertEqual(calls["n"], 1)
        self.assertIn("返回空数据", str(ctx.exception))

    def test_backoff_grows(self):
        def always_fail(**kwargs):
            raise ProxyError("一直被拦")

        # 关掉限速的 sleep，否则退避 sleep 会和 _throttle 的 sleep 混在一起
        with mock.patch.object(MX, "_throttle"), \
             mock.patch.object(MX.ak, "stock_individual_fund_flow", side_effect=always_fail):
            with self.assertRaises(MX.MarketXError):
                MX._call("stock_individual_fund_flow", stock="600000",
                         market="sh", limit_key="fundflow")
        waits = [c.args[0] for c in self.sleep.call_args_list if c.args]
        self.assertEqual(len(waits), 2, f"应退避 2 次(MAX_RETRY)，实际 {waits}")
        self.assertGreater(waits[1], waits[0], "退避间隔应指数增长")


class TestFundFlowSnapshotFallback(unittest.TestCase):
    """主源(东财 push2his)被拦 → 全市场快照(push2)兜底 + 熔断。"""

    def setUp(self):
        MX._FF_PRIMARY_DEAD_UNTIL = 0
        MX._FF_SNAPSHOT_CACHE.clear()
        # priority_list() 是**类方法读类属性**，patch 实例属性无效，
        # 所以直接替换这个方法本身来隔离数据源优先级。
        self.prio = mock.patch.object(MX.settings, "priority_list",
                                      return_value=["akshare"])
        self.prio.start()

    def tearDown(self):
        self.prio.stop()
        MX._FF_PRIMARY_DEAD_UNTIL = 0
        MX._FF_SNAPSHOT_CACHE.clear()

    def test_primary_success_path(self):
        with mock.patch.object(MX.ak, "stock_individual_fund_flow",
                               return_value=_raw_daily_df("600000")):
            df = MX.fetch_fund_flow("600000", days=5)
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[-1]["main_net"], 2e8)
        self.assertGreater(df.iloc[-1]["main_net_pct"], 0)

    def test_falls_back_to_snapshot_when_primary_blocked(self):
        rank = _raw_rank_df("600000", main=-4.6e7, amount=9.1e8)
        # 兜底消费的是 fetch_fund_flow_rank 归一化后的结果，
        # 所以这里 mock 底层 akshare 接口，让真实的解析逻辑跑一遍
        with mock.patch.object(MX.ak, "stock_individual_fund_flow",
                               side_effect=ProxyError("push2his 被代理拦截")), \
             mock.patch.object(MX.ak, "stock_fund_flow_individual", return_value=rank):
            df = MX.fetch_fund_flow("600000", days=5)
        self.assertFalse(df.empty)
        last = df.iloc[-1]
        self.assertAlmostEqual(last["main_net"], -4.6e7, delta=1e5)
        # 占比 = 净额 / 成交额，是资金链维度的主输入，必须有值
        self.assertIsNotNone(last["main_net_pct"])
        self.assertAlmostEqual(last["main_net_pct"], -4.6e7 / 9.1e8, places=5)

    def test_circuit_breaker_skips_primary_for_rest_of_batch(self):
        # 快照要包含整批涉及的票，否则第二只会因查不到而失败
        rank = _raw_rank_df_multi(["600000", "000001"])
        primary = mock.Mock(side_effect=ProxyError("push2his 被代理拦截"))
        with mock.patch.object(MX.ak, "stock_individual_fund_flow", primary), \
             mock.patch.object(MX.ak, "stock_fund_flow_individual", return_value=rank):
            MX.fetch_fund_flow("600000")
            # 熔断已触发
            self.assertGreater(MX._FF_PRIMARY_DEAD_UNTIL, time.monotonic())
            before = primary.call_count
            # 同批次第二只：不再打被拦的主源，直接走快照
            MX.fetch_fund_flow("000001")
            self.assertEqual(primary.call_count, before)

    def test_rank_computes_pct_when_source_has_no_ratio_column(self):
        # 东财排名接口只有 净额/成交额，没有净占比列 —— 必须自己算，
        # 否则 main_net_pct 整列为 None，资金链维度直接失效。
        raw = _raw_rank_df("600000", main=2.0e8, amount=1.0e9)
        with mock.patch.object(MX.ak, "stock_fund_flow_individual", return_value=raw):
            out = MX.fetch_fund_flow_rank(kind="即时")
        self.assertEqual(len(out), 1)
        self.assertIsNotNone(out.iloc[0]["main_net_pct"])
        self.assertAlmostEqual(out.iloc[0]["main_net_pct"], 0.2, places=5)

    def test_all_sources_fail_raises(self):
        with mock.patch.object(MX.settings, "priority_list", return_value=["akshare"]), \
             mock.patch.object(MX.ak, "stock_individual_fund_flow",
                               side_effect=ProxyError("主源被拦")), \
             mock.patch.object(MX, "fetch_fund_flow_rank",
                               side_effect=MX.MarketXError("快照也失败")):
            with self.assertRaises(MX.MarketXError) as ctx:
                MX.fetch_fund_flow("600000")
        # 注册表统一报"所有数据源均失败"，并把每个源的原因都带上
        msg = str(ctx.exception)
        self.assertIn("所有数据源均失败", msg)
        self.assertIn("akshare", msg, "要说明 akshare 源为什么失败")
        self.assertIn("tushare", msg, "要说明 tushare 源为什么被跳过")

    def test_snapshot_dates_anchor_to_trading_day(self):
        """兜底合成序列的日期必须锚到交易日，不能在周末造出非交易日数据行。

        直接用 date.today() 会让"周日刷新"在库里多出周日的资金流，
        把"最新评分日"顶成周日，列表和刷新口径全乱。
        """
        anchor = MX._ff_anchor_day()
        self.assertLess(anchor.weekday(), 5, f"锚点不能是周末: {anchor}")
        if date.today().weekday() >= 5:
            self.assertNotEqual(anchor, date.today(),
                                "周末刷新时兜底日期不能是今天")

    def test_snapshot_returns_anchored_dates(self):
        rank = _raw_rank_df("600000", main=1.0e8, amount=1.0e9)
        MX._FF_PRIMARY_DEAD_UNTIL = 0
        MX._FF_SNAPSHOT_CACHE.clear()
        with mock.patch.object(MX.settings, "priority_list", return_value=["akshare"]), \
             mock.patch.object(MX.ak, "stock_individual_fund_flow",
                               side_effect=ProxyError("主源被拦")), \
             mock.patch.object(MX.ak, "stock_fund_flow_individual", return_value=rank):
            df = MX.fetch_fund_flow("600000", days=10)
        for d in df["date"]:
            self.assertLess(d.weekday(), 5, f"兜底生成了周末数据行: {d}")

    def test_baostock_priority_is_skipped_gracefully(self):
        # 默认优先级 "baostock,akshare"：baostock 无资金流能力，应自动跳过
        with mock.patch.object(MX.settings, "priority_list",
                               return_value=["baostock", "akshare"]), \
             mock.patch.object(MX.ak, "stock_individual_fund_flow",
                               return_value=_raw_daily_df("600000")):
            df = MX.fetch_fund_flow("600000")
        self.assertEqual(len(df), 2)


class TestTushareSource(unittest.TestCase):
    """Tushare moneyflow 作为不依赖东财的独立备选源。"""

    def test_to_tushare_code(self):
        self.assertEqual(MX._to_tushare_code("600000"), "600000.SH")
        self.assertEqual(MX._to_tushare_code("000001"), "000001.SZ")
        self.assertEqual(MX._to_tushare_code("300750"), "300750.SZ")
        self.assertEqual(MX._to_tushare_code("600000.SH"), "600000.SH")

    def test_skipped_without_token(self):
        # 没配 token 属于"源没启用"，不是源坏了 —— 不该被熔断，
        # 所以用 DataUnavailable 而不是 SourceError。
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TUSHARE_TOKEN", None)
            with self.assertRaises(DataUnavailable) as ctx:
                MX._fund_flow_tushare("600000", 10)
        self.assertIn("TUSHARE_TOKEN", str(ctx.exception))

    def test_maps_moneyflow_when_configured(self):
        fake = mock.MagicMock()
        fake.pro_api.return_value.moneyflow.return_value = pd.DataFrame([
            {"ts_code": "600000.SH", "trade_date": "20261009",
             "buy_sm_amount": 100.0, "sell_sm_amount": 50.0,
             "buy_md_amount": 100.0, "sell_md_amount": 50.0,
             "buy_lg_amount": 100.0, "sell_lg_amount": 50.0,
             "buy_elg_amount": 100.0, "sell_elg_amount": 50.0,
             "net_mf_amount": 200.0},
        ])
        with mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "fake-token"}), \
             mock.patch.dict(sys.modules, {"tushare": fake}):
            df = MX._fund_flow_tushare("600000", 10)
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["main_net"], 200.0)
        # 成交额 = 四档买卖额之和 = 600
        self.assertEqual(row["amount"], 600.0)
        self.assertAlmostEqual(row["main_net_pct"], 200.0 / 600.0, places=5)
        self.assertEqual(row["large_net"], 50.0)  # 100 - 50

    def test_used_as_fallback_when_akshare_dead(self):
        fake = mock.MagicMock()
        fake.pro_api.return_value.moneyflow.return_value = pd.DataFrame([
            {"ts_code": "600000.SH", "trade_date": "20261009",
             "buy_elg_amount": 100.0, "sell_elg_amount": 50.0,
             "net_mf_amount": 50.0},
        ])
        MX._FF_PRIMARY_DEAD_UNTIL = 0
        MX._FF_SNAPSHOT_CACHE.clear()
        with mock.patch.object(MX.settings, "priority_list",
                               return_value=["akshare", "tushare"]), \
             mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "fake-token"}), \
             mock.patch.dict(sys.modules, {"tushare": fake}), \
             mock.patch.object(MX.ak, "stock_individual_fund_flow",
                               side_effect=ProxyError("主源被拦")), \
             mock.patch.object(MX.ak, "stock_fund_flow_individual",
                               side_effect=MX.MarketXError("快照失败")):
            df = MX.fetch_fund_flow("600000")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["main_net"], 50.0)
        MX._FF_PRIMARY_DEAD_UNTIL = 0
        MX._FF_SNAPSHOT_CACHE.clear()


class TestSourceRegistry(unittest.TestCase):
    """数据源注册表：注册 / 排序 / 自动故障切换 / 熔断。"""

    def setUp(self):
        self.reg = SourceRegistry(circuit_threshold=2, cooldown=60.0)

    def _src(self, name, supports, fn):
        self.reg.register(name, supports=supports, fetch=fn)

    def test_register_as_decorator_and_direct(self):
        @self.reg.register("deco", supports=["chip"])
        def _f(symbol, days=None):
            return pd.DataFrame({"symbol": [symbol]})

        self.reg.register("direct", supports=["chip"], fetch=lambda symbol, days=None: None)
        self.assertIn("deco", [s.name for s in self.reg.sources_for("chip")])
        self.assertIn("direct", [s.name for s in self.reg.sources_for("chip")])
        self.assertEqual(self.reg.sources_for("fund_flow"), [])  # 不支持的类型

    def test_priority_order_follows_config(self):
        for n in ["s1", "s2", "s3"]:
            self._src(n, ["k"], lambda symbol=None: pd.DataFrame({"a": [1]}))
        with mock.patch.object(MX.settings, "priority_list", return_value=["s3", "s1"]):
            order = [s.name for s in self.reg.sources_for("k")]
        # 配置里出现的按配置顺序在前，未出现的(s2)排后面兜底
        self.assertEqual(order, ["s3", "s1", "s2"])

    def test_falls_to_next_source_on_failure(self):
        calls = []

        def bad(symbol, days=None):
            calls.append("bad")
            raise SourceError("第一个源挂了")

        def good(symbol, days=None):
            calls.append("good")
            return pd.DataFrame({"symbol": [symbol]})

        self._src("src_a", ["k"], bad)
        self._src("src_b", ["k"], good)
        with mock.patch.object(MX.settings, "priority_list", return_value=["src_a", "src_b"]):
            df = self.reg.fetch("k", symbol="600000")
        self.assertEqual(calls, ["bad", "good"], "第一个源失败后应自动换下一个")
        self.assertEqual(df.iloc[0]["symbol"], "600000")

    def test_skips_disabled_and_unsupported(self):
        calls = []
        self.reg.register("off", supports=["k"], enabled=False,
                          fetch=lambda symbol=None: calls.append("off"))
        self._src("other", ["not_k"], lambda symbol=None: calls.append("other"))
        self._src("yes", ["k"], lambda symbol=None: pd.DataFrame({"a": [1]}))
        self.reg.fetch("k")
        self.assertNotIn("off", calls)
        self.assertNotIn("other", calls)

    def test_circuit_breaks_source_after_repeated_failures(self):
        """熔断：连续失败到阈值后进入冷却，冷却期内根本不再试它。

        这里用 ``weight_decay=1.0``（关闭降权）隔离出**熔断本身**的行为：
        否则失败源会先被降权排到后面，轮不到它累积连续失败次数 ——
        那是降权的效果，由 test_source_weights.py 单独覆盖。
        """
        reg = SourceRegistry(circuit_threshold=2, cooldown=60.0,
                             weight_decay=1.0)
        n = {"c": 0}

        def bad(symbol=None):
            n["c"] += 1
            raise SourceError("一直挂")

        reg.register("dead", supports=["k"], fetch=bad)
        reg.register("alive", supports=["k"],
                     fetch=lambda symbol=None: pd.DataFrame({"a": [1]}))
        with mock.patch.object(MX.settings, "priority_list",
                               return_value=["dead", "alive"]):
            for _ in range(3):
                reg.fetch("k")
        # 阈值=2：第 3 次起 dead 应被熔断，不再调用
        self.assertEqual(n["c"], 2)
        self.assertFalse(reg.is_healthy("dead"))
        with mock.patch.object(MX.settings, "priority_list",
                               return_value=["dead", "alive"]):
            reg.fetch("k")
        self.assertEqual(n["c"], 2, "熔断期内不应再调用该源")

    def test_failed_source_is_deprioritized(self):
        """降权：失败一次后它就排到后面 —— 熔断之外少撞一次墙。"""
        calls: list = []

        def bad(symbol=None):
            calls.append("bad")
            raise SourceError("挂")

        def good(symbol=None):
            calls.append("good")
            return pd.DataFrame({"a": [1]})

        self._src("first", ["k"], bad)
        self._src("second", ["k"], good)
        with mock.patch.object(MX.settings, "priority_list",
                               return_value=["first", "second"]):
            self.reg.fetch("k")            # first 失败 → 降权 → second 成功
            self.reg.fetch("k")            # 这一轮应直接走 second
        self.assertEqual(calls.count("bad"), 1)
        self.assertEqual(calls.count("good"), 2)
        self.assertEqual([s.name for s in self.reg.sources_for("k")],
                         ["second", "first"])

    def test_data_unavailable_does_not_break_source(self):
        """"这只票没数据"不能把健康的源熔断掉。"""
        n = {"c": 0}

        def no_data(symbol=None):
            n["c"] += 1
            raise DataUnavailable("停牌，无数据")

        self._src("picky", ["k"], no_data)
        self._src("ok", ["k"], lambda symbol=None: pd.DataFrame({"a": [1]}))
        with mock.patch.object(MX.settings, "priority_list", return_value=["picky", "ok"]):
            for _ in range(5):
                self.reg.fetch("k")
        self.assertEqual(n["c"], 5, "数据缺失不该熔断，每次仍应尝试")
        self.assertTrue(self.reg.is_healthy("picky"))

    def test_all_sources_fail_raises_with_reasons(self):
        self._src("a", ["k"], lambda symbol=None: (_ for _ in ()).throw(SourceError("代理拦截")))
        self._src("b", ["k"], lambda symbol=None: (_ for _ in ()).throw(DataUnavailable("无数据")))
        with mock.patch.object(MX.settings, "priority_list", return_value=["a", "b"]):
            with self.assertRaises(SourceError) as ctx:
                self.reg.fetch("k")
        msg = str(ctx.exception)
        self.assertIn("代理拦截", msg)
        self.assertIn("无数据", msg)

    def test_success_resets_failure_counter(self):
        spec = SourceSpec(name="x", fetch=lambda **kw: pd.DataFrame({"a": [1]}), supports={"k"})
        self.reg._specs["x"] = spec
        self.reg._order.append("x")
        self.reg._penalize(spec, "boom")
        self.assertEqual(spec.fail_count, 1)
        with mock.patch.object(MX.settings, "priority_list", return_value=["x"]):
            self.reg.fetch("k")
        self.assertEqual(spec.fail_count, 0)

    def test_health_reflects_state(self):
        self._src("h", ["k", "j"], lambda symbol=None: pd.DataFrame({"a": [1]}))
        with mock.patch.object(MX.settings, "priority_list", return_value=["h"]):
            self.reg.fetch("k")
        h = self.reg.health()[0]
        self.assertEqual(h["name"], "h")
        self.assertEqual(h["supports"], ["j", "k"])
        self.assertTrue(h["healthy"])
        self.assertEqual(h["fail_count"], 0)

    def test_reset_clears_circuit(self):
        spec = SourceSpec(name="z", fetch=lambda **kw: None, supports={"k"})
        self.reg._specs["z"] = spec
        self.reg._order.append("z")
        self.reg._penalize(spec, "boom")
        self.reg._penalize(spec, "boom")
        self.assertFalse(self.reg.is_healthy("z"))
        self.reg.reset()
        self.assertTrue(self.reg.is_healthy("z"))

    def test_builtin_sources_registered(self):
        """marketx import 后，内置源必须已经在全局注册表里。"""
        names = {s["name"] for s in REGISTRY.health()}
        self.assertIn("akshare", names)
        self.assertIn("tushare", names)
        self.assertIn("fund_flow", REGISTRY.health()[0]["supports"])


class TestSyncFundFlowLogging(unittest.TestCase):
    """失败日志必须带上原因样例，否则用户只能看到"N 只失败"。"""

    def test_warning_includes_reason_sample(self):
        with mock.patch.object(MX, "fetch_fund_flow",
                               side_effect=MX.MarketXError(
                                   "stock_individual_fund_flow 调用失败: "
                                   "ProxyError: HTTPSConnectionPool(host='push2his.eastmoney.com')")):
            with mock.patch.object(S, "log_throttled") as m_log:
                res = S.sync_fund_flow(["600000"])
        self.assertEqual(res["ok"], 0)
        self.assertEqual(len(res["failed"]), 1)
        m_log.assert_called()
        msg = m_log.call_args[0][1]
        self.assertIn("600000", msg, "日志要带出失败标的代码")
        self.assertIn("ProxyError", msg, "日志要带出真实原因(代理拦截，不是限流)")


if __name__ == "__main__":
    unittest.main()
