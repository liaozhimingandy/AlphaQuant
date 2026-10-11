#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_source_retry_and_dedup.py
# @Description : 采集链路的三条硬要求：
#
#                 1. **先重试、再换源**：一个源报错先原地重试 N 次（只重试瞬时
#                    错误），用尽了才换下一个源，而不是"失败一次就放弃"；
#                 2. **全挂要告警**：所有数据源都用尽时必须打出带各源原因的告警，
#                    不能只留一句"获取失败"让人猜；
#                 3. **已采集的不重复采**：库里已有当日数据时跳过联网请求 ——
#                    同一交易日的数据拉第二次拿到的还是同一份，只会白耗额度。
#
#                同时校验"用了哪个源"的可观测性（recent_events / used_sources）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from contextlib import contextmanager

import unittest
from datetime import date
from unittest import mock

import pandas as pd

from app.alpha.pool import sync as S
from app.datasource.service import facade as MX
from app.datasource.compat.registry import (
    REGISTRY,
    DataUnavailable,
    SourceError,
    SourceRegistry,
    is_transient,
)
from app.storage.repo import kline_repo as R


class ProxyError(Exception):  # noqa: N818 - 名字与 requests 的 ProxyError 对齐
    """瞬时网络错误（`is_transient` 按类型名判定）。"""


class SchemaChangedError(Exception):
    """确定性错误（接口改版/缺列），重试没有意义。"""


# 用一个冷僻的交易日做隔离，避免和其它用例/真实数据互相污染
DAY = date(2020, 3, 2)
DAY_STR = "20200302"


class TestRetryThenSwitch(unittest.TestCase):
    """报错 → 重试 N 次 → 仍失败才换下一个源。"""

    def setUp(self):
        self.waits: list = []
        self.reg = SourceRegistry(circuit_threshold=99, cooldown=60.0, retries=3)
        self.reg._sleep = self.waits.append  # 注入假 sleep，不用真等退避

    def _src(self, name, fn):
        self.reg.register(name, supports=["k"], fetch=fn)

    def test_retries_three_times_before_switching(self):
        calls = {"bad": 0, "good": 0}

        def flaky(symbol=None):
            calls["bad"] += 1
            raise ProxyError("被代理拦截")

        def good(symbol=None):
            calls["good"] += 1
            return pd.DataFrame({"symbol": ["600000"]})

        self._src("src_a", flaky)
        self._src("src_b", good)
        with mock.patch.object(MX.settings, "priority_list",
                               return_value=["src_a", "src_b"]):
            df = self.reg.fetch("k")

        # 1 次首次调用 + 3 次重试 = 4 次，用尽后才切到下一个源
        self.assertEqual(calls["bad"], 4, "重试次数应等于 retries(3) + 首次(1)")
        self.assertEqual(len(self.waits), 3, "每次重试前都要退避")
        self.assertEqual(calls["good"], 1, "第一个源用尽后应换第二个源")
        self.assertEqual(df.iloc[0]["symbol"], "600000")

    def test_backoff_grows_between_retries(self):
        def flaky(symbol=None):
            raise ProxyError("一直被拦")

        self._src("only", flaky)
        with self.assertRaises(SourceError):
            self.reg.fetch("k")
        self.assertEqual(len(self.waits), 3)
        self.assertGreater(self.waits[1], self.waits[0], "退避应指数增长")
        self.assertGreater(self.waits[2], self.waits[1])

    def test_deterministic_error_skips_retry(self):
        """接口改版/缺列这类确定性错误，重试多少次都一样 —— 立刻换源。"""
        calls = {"n": 0}

        def broken(symbol=None):
            calls["n"] += 1
            raise SchemaChangedError("缺少主力净流入列")

        self._src("only", broken)
        with self.assertRaises(SourceError):
            self.reg.fetch("k")
        self.assertEqual(calls["n"], 1, "确定性错误不该重试")
        self.assertEqual(self.waits, [])

    def test_data_unavailable_skips_retry_and_penalty(self):
        """源是好的、只是没这份数据 → 不重试、不熔断。"""
        calls = {"n": 0}

        def no_data(symbol=None):
            calls["n"] += 1
            raise DataUnavailable("该股停牌，无资金流")

        self._src("only", no_data)
        with self.assertRaises(SourceError):
            self.reg.fetch("k")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.waits, [])
        spec = next(s for s in self.reg.health() if s["name"] == "only")
        self.assertTrue(spec["healthy"], "数据缺失不能熔断健康的源")

    def test_success_records_which_source_was_used(self):
        self._src("src_a", lambda symbol=None: (_ for _ in ()).throw(
            ProxyError("挂了")))
        self._src("src_b", lambda symbol=None: pd.DataFrame({"symbol": ["600000"]}))
        with mock.patch.object(MX.settings, "priority_list",
                               return_value=["src_a", "src_b"]):
            self.reg.fetch("k")

        used = self.reg.used_sources()
        self.assertEqual(used.get("k"), "src_b", "要能回答'这批数据来自哪个源'")
        ok_events = [e for e in self.reg.recent_events() if e["ok"]]
        self.assertEqual(ok_events[-1]["source"], "src_b")
        self.assertGreater(ok_events[-1]["attempts"], 0)


class TestExhaustedWarning(unittest.TestCase):
    """所有数据源用尽 → 必须告警，且告警里带每个源的原因。"""

    def setUp(self):
        self.reg = SourceRegistry(circuit_threshold=99, cooldown=60.0, retries=1)
        self.reg._sleep = lambda s: None

    def test_warning_logged_with_every_source_reason(self):
        self.reg.register("src_a", supports=["k"],
                          fetch=lambda symbol=None: (_ for _ in ()).throw(
                              ProxyError("push2his 被代理拦截")))
        self.reg.register("src_b", supports=["k"],
                          fetch=lambda symbol=None: (_ for _ in ()).throw(
                              SourceError("雪球返回非 JSON")))

        with mock.patch("app.datasource.compat.registry.log_throttled") as m_log:
            with self.assertRaises(SourceError) as ctx:
                self.reg.fetch("k")

        msg = str(ctx.exception)
        self.assertIn("所有数据源均失败", msg)
        self.assertIn("src_a", msg, "要说明 src_a 为什么失败")
        self.assertIn("src_b", msg, "要说明 src_b 为什么失败")

        # 告警必须真的发出去（用户要在界面/日志里看到），且是 WARNING 级
        warnings = [c for c in m_log.call_args_list if c.args and c.args[0] == "WARNING"]
        self.assertTrue(warnings, "所有源用尽时必须打 WARNING 告警")
        self.assertIn("已全部用尽", warnings[-1].args[1])

    def test_adapter_param_named_kind_raises_clear_error(self):
        """适配器参数不能叫 kind（会和 fetch 的位置参数撞名）。"""
        self.reg.register("bad", supports=["k"], fetch=lambda kind=None: None)
        with self.assertRaises(TypeError) as ctx:
            self.reg.fetch("k", kind="即时")
        self.assertIn("kind", str(ctx.exception))


@contextmanager
def _patch_source(name: str, **kw):
    """替换某条路由的实现（用于把兜底源也打挂）。

    必须打在 **Route.handler** 上而不是模块属性上：
    Provider 注册时存的是**绑定方法的引用**，之后改模块里的同名属性，
    路由里那个还是旧函数。
    """
    from app.datasource.provider import PROVIDERS

    route = PROVIDERS.route(name)
    assert route is not None, f"没有注册路由 {name}"
    with mock.patch.object(route, "handler", **kw):
        yield


class TestHotRankMultiSource(unittest.TestCase):
    """人气榜必须注册多个源，且主源全挂时能一路降级到兜底源。"""

    def setUp(self):
        # 熔断/健康分是**全局状态**：上一个用例把源打进冷却，
        # 下一个用例就会跳过它落到别的源上，于是"降级顺序"的断言随机失败。
        # 每个用例前重置，保证断言的是降级逻辑本身而不是历史状态。
        from app.datasource.provider import PROVIDERS

        REGISTRY.reset()
        PROVIDERS.reset()
        # 域名健康度也是全局的（net 模块级字典），而且更隐蔽：
        # 上一个用例把 push2 打挂两次，本用例里所有依赖它的源会被**直接跳过**，
        # 于是"应该降级到某源"的断言变成"全被跳过"。必须一起清。
        from app.datasource.transport import net

        net.reset()

    def test_hot_rank_has_multiple_sources(self):
        from app.datasource.provider import PROVIDERS

        names = [r.name for r in PROVIDERS.ranking("hot_rank")]
        self.assertGreaterEqual(len(names), 2,
                                f"人气榜要有兜底源，实际只有 {names}")

    def test_falls_back_when_primary_blocked(self):
        backup = pd.DataFrame([
            {"代码": "600000", "股票名称": "浦发银行", "当前排名": 1, "涨跌幅": 1.2},
        ])
        # emapp_hot 是当前人气榜的首个源（不依赖被拦的 push2），
        # 要验"主源挂了换下一个"，就得把它也打挂。
        with mock.patch.object(MX.ak, "stock_hot_rank_em",
                               side_effect=ProxyError("push2 被拦")), \
             mock.patch.object(MX.ak, "stock_hot_follow_xq",
                               return_value=backup), \
             _patch_source("emapp_hot", side_effect=ProxyError("emappdata 也挂")), \
             _patch_source("local_hot",
                           side_effect=AssertionError("不该降级到本地兜底")):
            df = MX.fetch_hot_rank(top=10)
        self.assertEqual(df.iloc[0]["symbol"], "600000")
        self.assertEqual(df.iloc[0]["name"], "浦发银行")

    def test_all_hot_sources_fail_raises_with_reasons(self):
        # 本地兜底源（local_hot）也要一起打挂 —— 否则它会顶上，
        # 这里要验的是"彻底取不到数据时的报错内容"。
        # 注意要打在**注册表里那个 spec**上：注册时存的是函数引用，
        # 改模块属性不会影响已经注册进去的源。
        with mock.patch.object(MX.ak, "stock_hot_rank_em",
                               side_effect=ProxyError("push2 被拦")), \
             mock.patch.object(MX.ak, "stock_hot_follow_xq",
                               side_effect=ProxyError("雪球也被拦")), \
             mock.patch.object(MX.ak, "stock_zh_a_spot_em",
                               side_effect=ProxyError("快照也被拦")), \
             _patch_source("emapp_hot", side_effect=ProxyError("emappdata 也挂")), \
             _patch_source("local_hot", side_effect=RuntimeError("库也是空的")):
            with self.assertRaises(MX.MarketXError) as ctx:
                MX.fetch_hot_rank()
        msg = str(ctx.exception)
        self.assertIn("所有数据源均失败", msg)
        # 每个源都要在原因里出现，否则用户无法判断该放行哪个域名
        for name in ("emapp_hot", "akshare_em", "xueqiu_hot", "akshare_spot",
                     "local_hot"):
            self.assertIn(name, msg, f"原因里应包含 {name}")
        # 报错要能直接行动：网络层失败必须附上排查提示
        self.assertIn("排查", msg)

    def test_falls_back_to_local_when_all_network_sources_fail(self):
        """外网三源全挂 → 落到本地兜底，且日志/结果能看出是兜底。"""
        fallback = pd.DataFrame([
            {"symbol": "000001", "name": "A", "rank": 1, "pct_change": 1.0},
        ])
        with mock.patch.object(MX.ak, "stock_hot_rank_em",
                               side_effect=ProxyError("push2 被拦")), \
             mock.patch.object(MX.ak, "stock_hot_follow_xq",
                               side_effect=ProxyError("雪球也被拦")), \
             mock.patch.object(MX.ak, "stock_zh_a_spot_em",
                               side_effect=ProxyError("快照也被拦")), \
             _patch_source("emapp_hot", side_effect=ProxyError("emappdata 也挂")), \
             _patch_source("local_hot", return_value=fallback):
            df = MX.fetch_hot_rank()
        self.assertEqual(len(df), 1)
        from app.datasource.compat.registry import REGISTRY

        used = [e for e in REGISTRY.recent_events(5)
                if e.get("kind") == "hot_rank" and e.get("ok")]
        self.assertTrue(used and used[-1]["source"] == "local_hot")


class TestNoRecollect(unittest.TestCase):
    """库里已有当日数据 → 跳过重复采集（force=True 除外）。"""

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        from app.storage.engine import SessionLocal
        from app.storage.schema import ChipDistribution, FundFlow, StockHotRank

        syms = ["600901", "600902"]
        with SessionLocal() as s:
            for m in (StockHotRank, FundFlow, ChipDistribution):
                s.query(m).filter(m.symbol.in_(syms)).delete(
                    synchronize_session=False)
            s.query(StockHotRank).filter(StockHotRank.date == DAY).delete(
                synchronize_session=False)
            s.commit()

    def test_pool_count_reports_existing_rows(self):
        self.assertEqual(R.pool_count("hot", DAY), 0)
        R.save_hot_rank(pd.DataFrame([
            {"date": DAY, "symbol": "600901", "name": "甲", "rank": 1},
            {"date": DAY, "symbol": "600902", "name": "乙", "rank": 2},
        ]))
        self.assertEqual(R.pool_count("hot", DAY), 2)
        self.assertEqual(R.latest_pool("hot", DAY)[0]["symbol"], "600901")

    def test_hot_sync_skips_when_already_collected(self):
        R.save_hot_rank(pd.DataFrame([
            {"date": DAY, "symbol": "600901", "name": "甲", "rank": 1},
        ]))
        with mock.patch.object(MX, "fetch_hot_rank",
                               side_effect=AssertionError("不该联网")):
            res = S.sync_hot_rank(DAY_STR)
        self.assertTrue(res.get("skipped"), "库里已有就该跳过")
        self.assertEqual(res["ok"], 0)

    def test_hot_sync_force_recollects(self):
        R.save_hot_rank(pd.DataFrame([
            {"date": DAY, "symbol": "600901", "name": "甲", "rank": 1},
        ]))
        with mock.patch.object(MX, "fetch_hot_rank",
                               return_value=pd.DataFrame([
                                   {"symbol": "600902", "name": "乙", "rank": 1},
                               ])) as m_fetch:
            res = S.sync_hot_rank(DAY_STR, force=True)
        m_fetch.assert_called_once()
        self.assertEqual(res["ok"], 1, "force=True 应真的重新采集")
        self.assertNotIn("skipped", res)

    def test_fund_flow_skips_symbols_already_in_db(self):
        R.save_fund_flow(pd.DataFrame([
            {"symbol": "600901", "date": DAY, "main_net": 1e8, "main_net_pct": 0.1},
        ]))
        with mock.patch.object(MX, "fetch_fund_flow",
                               side_effect=AssertionError("不该联网")):
            res = S.sync_fund_flow(["600901"], day=DAY)
        self.assertEqual(res.get("skipped"), 1, "已有当日数据应跳过")
        self.assertEqual(res["ok"], 0)
        self.assertEqual(res["failed"], [])

    def test_fund_flow_fetches_when_missing(self):
        with mock.patch.object(MX, "fetch_fund_flow",
                               return_value=pd.DataFrame([
                                   {"symbol": "600902", "date": DAY,
                                    "main_net": 2e8, "main_net_pct": 0.2},
                               ])) as m_fetch:
            res = S.sync_fund_flow(["600902"], day=DAY)
        m_fetch.assert_called_once()
        self.assertEqual(res["ok"], 1)

    def test_latest_dates_batch_lookup(self):
        R.save_fund_flow(pd.DataFrame([
            {"symbol": "600901", "date": DAY, "main_net": 1e8},
            {"symbol": "600902", "date": DAY, "main_net": 2e8},
        ]))
        got = R.latest_dates("fund_flow", ["600901", "600902", "NOTEXIST"])
        self.assertEqual(got.get("600901"), DAY.isoformat())
        self.assertNotIn("NOTEXIST", got, "查不到的不出现在结果里")


if __name__ == "__main__":
    unittest.main()
