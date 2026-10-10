#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金流/筹码/题材因子 + 多维度打分 + 股票池 的单元测试。

数据全部用合成数据写库（日期固定在 1990 年，避免和真实数据混淆），
**不依赖网络** —— 打分层与数据获取层本来就该分开验证。
"""
from __future__ import annotations

import unittest
from datetime import date

from app.core.config import settings
from app.db.database import Base, engine
from app.repository import marketx_repository as R
from app.test.helpers import TempWorkspaceMixin

#: 用一个不可能与真实数据冲突的日期
D0 = date(1990, 1, 10)


def _flows(sym: str, nets, closes, pcts) -> list:
    """造资金流历史。**最后一天必须是 D0** —— 否则评分会被记到别的日期上。"""
    from datetime import timedelta

    rows = []
    n = len(nets)
    for i, (net, close, pct) in enumerate(zip(nets, closes, pcts)):
        amount = 1e9
        rows.append({
            "symbol": sym, "date": D0 - timedelta(days=n - 1 - i),
            "close": close, "pct_change": pct, "main_net": net,
            "main_net_pct": net / amount, "amount": amount, "turnover": 6.0,
        })
    return rows


def _seed_strong(sym: str = "900001") -> None:
    R.save_fund_flow(_flows(sym, [1e8, 1.5e8, 2e8, 3e8, 6e8],
                            [10.0, 10.2, 10.5, 10.9, 11.5],
                            [1.0, 2.0, 3.0, 4.0, 5.0]))
    R.save_chip([{"symbol": sym, "date": D0, "profit_ratio": 0.55, "avg_cost": 9.8,
                  "concentration_90": 0.08, "peak_price": 10.6}])
    R.save_stock_themes([{"symbol": sym, "board": "人工智能", "kind": "concept",
                          "source": "test", "date": D0}])
    R.save_limitup([{"date": D0, "symbol": sym, "name": "强", "continuous": 2,
                     "seal_amount": 4e8, "broken_times": 0, "industry": "人工智能"}])


def _seed_weak(sym: str = "900002") -> None:
    R.save_fund_flow(_flows(sym, [-2e8, -1e8, -1.5e8, -2e8, -3e8],
                            [20.0, 19.5, 19.0, 18.5, 18.0],
                            [-2.0, -2.5, -2.0, -2.0, -3.0]))
    R.save_chip([{"symbol": sym, "date": D0, "profit_ratio": 0.05, "avg_cost": 22.0,
                  "concentration_90": 0.40, "peak_price": 21.0}])
    R.save_stock_themes([{"symbol": sym, "board": "传统制造", "kind": "concept",
                          "source": "test", "date": D0}])


def _seed_themes() -> None:
    R.save_themes([
        {"date": D0, "board": "人工智能", "kind": "concept", "pct_change": 5.2,
         "main_net": 8e8, "rank": 2, "leading_stock": "强"},
        {"date": D0, "board": "传统制造", "kind": "concept", "pct_change": -2.1,
         "main_net": -3e8, "rank": 180},
    ])


class RepoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import app.db.models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        _seed_themes()
        _seed_strong()
        _seed_weak()


class TestBand(unittest.TestCase):
    def test_below_and_above(self):
        from app.core.score.engine import band

        pts = [(0.0, 20.0), (0.10, 80.0)]
        self.assertEqual(band(-1.0, pts), 20.0)
        self.assertEqual(band(1.0, pts), 80.0)

    def test_linear_interpolation(self):
        from app.core.score.engine import band

        pts = [(0.0, 0.0), (10.0, 100.0)]
        self.assertAlmostEqual(band(5.0, pts), 50.0)
        self.assertAlmostEqual(band(2.5, pts), 25.0)

    def test_none_returns_default(self):
        from app.core.score.engine import band

        self.assertIsNone(band(None, [(0.0, 1.0), (1.0, 2.0)]))
        self.assertEqual(band(None, [(0.0, 1.0), (1.0, 2.0)], default=42.0), 42.0)

    def test_nan_is_none(self):
        from app.core.score.engine import band

        self.assertIsNone(band(float("nan"), [(0.0, 1.0), (1.0, 2.0)]))


class TestDerivedMetrics(unittest.TestCase):
    """连续天数 / 加速度 / 脉冲 —— 这三条才是"大资金在快速进场"的判据。"""

    def test_net_days(self):
        """从**最后一天往前**数。今天流出就是 0 天，不管之前流了多少天。"""
        from app.core.data.features import _net_days

        self.assertEqual(_net_days([{"main_net": 1}, {"main_net": 2}]), 2)
        self.assertEqual(_net_days([{"main_net": 1}, {"main_net": -1}]), 0)
        # 前天流出、昨天和今天流入 → 2 天（今天流出就归零，不管之前多少天）
        self.assertEqual(_net_days([{"main_net": -1}, {"main_net": 3}, {"main_net": 5}]), 2)
        self.assertEqual(_net_days([{"main_net": None}]), 0)

    def test_accel(self):
        from app.core.data.features import _accel

        flows = [{"main_net": v} for v in (1.0, 1.0, 1.0, 3.0, 3.0, 3.0)]
        self.assertAlmostEqual(_accel(flows, window=3), 3.0)

    def test_accel_invalid_when_base_negative(self):
        """前段是净流出时"加速度"没有意义，必须返回 None 而不是编一个数。"""
        from app.core.data.features import _accel

        flows = [{"main_net": v} for v in (-1.0, -1.0, -1.0, 3.0, 3.0, 3.0)]
        self.assertIsNone(_accel(flows, window=3))

    def test_pulse_uses_absolute_baseline(self):
        """用绝对值做分母：否则历史均值接近 0 时比值会爆炸成假信号。"""
        from app.core.data.features import _pulse

        flows = [{"main_net": v} for v in (-2.0, -2.0, -2.0, 6.0)]
        self.assertAlmostEqual(_pulse(flows), 3.0)

    def test_insufficient_history_is_none(self):
        from app.core.data.features import _accel, _pulse

        self.assertIsNone(_accel([{"main_net": 1.0}]))
        self.assertIsNone(_pulse([]))


class TestFeatureProvider(RepoTestCase):
    def test_fund_flow_loaded(self):
        from app.core.data.features import FeatureProvider

        f = FeatureProvider(ttl=0).get("900001", D0)
        self.assertIsNotNone(f.main_net)
        self.assertGreater(f.net_days, 0)
        self.assertIsNotNone(f.pulse)
        self.assertTrue(f.available["capital"])

    def test_chip_and_theme_loaded(self):
        from app.core.data.features import FeatureProvider

        f = FeatureProvider(ttl=0).get("900001", D0)
        self.assertAlmostEqual(f.profit_ratio, 0.55)
        self.assertIn("人工智能", f.themes)
        self.assertEqual(f.theme_rank, 2)
        self.assertTrue(f.is_limitup)
        self.assertEqual(f.continuous, 2)

    def test_missing_symbol_is_all_unavailable(self):
        from app.core.data.features import FeatureProvider

        f = FeatureProvider(ttl=0).get("999999", D0)
        self.assertFalse(f.available.get("capital", False))
        self.assertFalse(f.available.get("chip", False))


class TestScoreEngine(RepoTestCase):
    def _engine(self):
        from app.core.data.features import FeatureProvider
        from app.core.score.engine import ScoreConfig, ScoreEngine

        return ScoreEngine(config=ScoreConfig.load(),
                           provider=FeatureProvider(ttl=0))

    def test_strong_beats_weak(self):
        eng = self._engine()
        a = eng.score("900001", D0)
        b = eng.score("900002", D0)
        self.assertIsNotNone(a.total)
        self.assertIsNotNone(b.total)
        self.assertGreater(a.total, b.total + 20,
                           "强势股应该显著高于弱势股，否则维度没起作用")

    def test_weights_redistributed_when_dim_missing(self):
        """涨停维度不可用时，它的权重必须分给其它维度，而不是按 0 分算。"""
        eng = self._engine()
        r = eng.score("900002", D0)     # 900002 没有涨停数据
        self.assertFalse(r.available["limitup"])
        self.assertNotIn("limitup", r.weights)
        self.assertAlmostEqual(sum(r.weights.values()), 1.0, places=3)
        self.assertGreater(r.weights["capital"], 0.30,
                           "资金维度应拿到原本属于涨停的权重")

    def test_refuses_when_too_few_dims(self):
        """可用维度不足 2 个时**必须拒绝给分**。

        只有资金流一个维度就给 90 分，是最容易骗到人的假信号。
        """
        from app.core.data.features import FeatureProvider, StockFeatures
        from app.core.score.engine import ScoreConfig, ScoreEngine

        cfg = ScoreConfig.load()
        cfg.min_dims = 2
        eng = ScoreEngine(config=cfg, provider=FeatureProvider(ttl=0))
        feat = StockFeatures(symbol="999999")
        feat.main_net = 1e8
        feat.main_net_pct = 0.2
        feat.available["capital"] = True
        r = eng.score_features(feat)
        self.assertIsNone(r.total)
        self.assertEqual(r.grade, "N/A")
        self.assertTrue(any("可用维度不足" in w for w in r.reasons))

    def test_risk_penalty_applied(self):
        """获利盘极高要扣分 —— 人人浮盈意味着随时可能兑现。"""
        from app.core.data.features import FeatureProvider, StockFeatures
        from app.core.score.engine import ScoreConfig, ScoreEngine

        eng = ScoreEngine(config=ScoreConfig.load(), provider=FeatureProvider(ttl=0))
        base = StockFeatures(symbol="X")
        base.main_net, base.main_net_pct, base.pct_change = 1e8, 0.2, 2.0
        base.profit_ratio = 0.55
        base.turnover = 6.0
        base.available.update({"capital": True, "chip": True, "technical": True})
        clean = eng.score_features(base)

        hot = StockFeatures(symbol="X")
        hot.__dict__.update(base.__dict__)
        hot.profit_ratio = 0.99          # 几乎全员浮盈
        penalized = eng.score_features(hot)
        self.assertLess(penalized.total, clean.total)
        self.assertTrue(any("获利盘" in n for n, _ in penalized.penalties))

    def test_score_many_ranks(self):
        eng = self._engine()
        res = eng.score_many(["900001", "900002"], D0)
        self.assertEqual(res[0].symbol, "900001")
        self.assertEqual(res[0].rank, 1)
        self.assertEqual(res[1].rank, 2)


class TestScoreStrategy(unittest.TestCase):
    def _strategy(self, **kw):
        from app.core.strategy import create_strategy

        return create_strategy("score", **kw)

    def _ctx(self, score):
        from app.core.factor.context import FactorContext
        from app.core.market.series import BarSeries

        return FactorContext("000001", BarSeries("000001"),
                             features={"composite_score": score})

    def _state(self, has_position=False):
        from app.core.strategy.state import DecisionState

        return DecisionState(task_id="t", symbol="000001", has_position=has_position)

    def test_buy_above_entry(self):
        s = self._strategy(entry_score=70)
        sig = s.decide(self._ctx(85.0), self._state())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side.value, "BUY")

    def test_no_buy_below_entry(self):
        s = self._strategy(entry_score=70)
        self.assertIsNone(s.decide(self._ctx(60.0), self._state()))

    def test_sell_below_exit(self):
        s = self._strategy(entry_score=70, exit_score=50)
        sig = s.decide(self._ctx(30.0), self._state(has_position=True))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side.value, "SELL")

    def test_hold_in_between(self):
        """评分在中间地带时不动手 —— 小幅波动就交易会把手续费吃光。"""
        s = self._strategy(entry_score=70, exit_score=50)
        self.assertIsNone(s.decide(self._ctx(62.0), self._state(has_position=True)))

    def test_missing_score_does_nothing(self):
        """拿不到评分时不动手。凭空假设一个分数去交易，比不交易危险得多。"""
        from app.core.factor.context import FactorContext
        from app.core.market.series import BarSeries

        s = self._strategy()
        ctx = FactorContext("000001", BarSeries("000001"), features={})
        self.assertIsNone(s.decide(ctx, self._state()))
        self.assertIsNone(s.decide(ctx, self._state(has_position=True)))

    def test_rejects_inverted_thresholds(self):
        """出口高于入口会造成"刚买就卖"的抖动，必须在构造时拒绝。"""
        from app.core.strategy.score_strategy import ScoreStrategy

        with self.assertRaises(ValueError):
            ScoreStrategy(entry_score=50, exit_score=80)


class TestSyncDegradation(unittest.TestCase):
    """同步失败必须**说清楚且不中断整批**。"""

    def test_single_failure_does_not_crash(self):
        import app.core.pool.sync as S
        from app.data import marketx as MX

        original = MX.fetch_fund_flow

        def boom(symbol, days=None):
            raise MX.MarketXError(f"模拟失败: {symbol}")

        MX.fetch_fund_flow = boom
        try:
            r = S.sync_fund_flow(["900001", "900002"])
        finally:
            MX.fetch_fund_flow = original
        self.assertEqual(r["ok"], 0)
        self.assertEqual(len(r["failed"]), 2)
        self.assertIn("模拟失败", r["failed"][0][1])

    def test_partial_failure_reports_each(self):
        import app.core.pool.sync as S
        from app.data import marketx as MX

        original = MX.fetch_chip
        calls = []

        def flaky(symbol, adjust=""):
            calls.append(symbol)
            if symbol == "900002":
                raise MX.MarketXError("网络抖动")
            import pandas as pd

            return pd.DataFrame([{"date": D0, "获利比例": 0.5}])

        MX.fetch_chip = flaky
        try:
            r = S.sync_chip(["900001", "900002"])
        finally:
            MX.fetch_chip = original
        self.assertEqual(r["ok"], 1)
        self.assertEqual(len(r["failed"]), 1)
        self.assertEqual(r["failed"][0][0], "900002")


class TestStockPool(RepoTestCase):
    def test_refresh_with_explicit_symbols(self):
        """直接指定标的（不走候选池）—— 离线也能验证整条链路。"""
        from app.core.pool import StockPool
        from app.core.data.features import FeatureProvider
        from app.core.score.engine import ScoreConfig, ScoreEngine

        pool = StockPool(provider=FeatureProvider(ttl=0),
                         engine=ScoreEngine(config=ScoreConfig.load(),
                                            provider=FeatureProvider(ttl=0)))
        rep = pool.refresh(symbols=["900001", "900002"], day=D0,
                           do_sync=False, save=True)
        self.assertEqual(rep["count"], 2)
        self.assertEqual(rep["results"][0]["symbol"], "900001")

    def test_saved_scores_readable(self):
        from app.core.pool import StockPool

        rows = StockPool.top(day=D0, limit=10)
        syms = {r["symbol"] for r in rows}
        self.assertIn("900001", syms)

    def test_saved_score_exposes_total_alias(self):
        """从库里读出来的分数必须带 `total`。

        库列名是 total_score，但打分引擎和面板/CLI 用的键是 total。
        以前只给了前者，结果 `pool top` 和面板表格里总分全是 0/空 ——
        明细里有分、列表里没分，最难查的那种 bug。
        """
        from app.core.pool import StockPool

        rows = StockPool.top(day=D0, limit=10)
        row = next(r for r in rows if r["symbol"] == "900001")
        self.assertIsNotNone(row.get("total"))
        self.assertGreater(row["total"], 50.0)
        self.assertEqual(row["total"], row["total_score"])

    def test_unknown_source_reports_error(self):
        from app.core.pool import StockPool

        rep = StockPool().refresh(source="no_such_source", do_sync=False)
        self.assertIn("error", rep)

    def test_empty_candidates_reports_error(self):
        """候选池拉失败时**不能**静默退化成全市场 —— 那会把几十秒变成几十分钟。"""
        from app.core.pool import StockPool

        syms, info = StockPool().candidates("limitup", date(1899, 1, 1))
        self.assertEqual(syms, [])


class TestFlowFactors(unittest.TestCase):
    """因子在数据缺失时必须返回 None（而不是 0）。"""

    def _ctx(self, features):
        from app.core.factor.context import FactorContext
        from app.core.market.series import BarSeries

        return FactorContext("000001", BarSeries("000001"), features=features)

    def test_missing_feature_is_none(self):
        from app.core.factor import create_factor

        for name in ("main_net_pct", "capital_accel", "capital_pulse",
                     "net_inflow_days", "chip_profit_ratio",
                     "chip_concentration", "chip_peak_gap", "composite_score"):
            f = create_factor(name)
            self.assertIsNone(f.compute(self._ctx({})), name)

    def test_present_feature_returned(self):
        from app.core.factor import create_factor

        f = create_factor("main_net_pct")
        self.assertAlmostEqual(f.compute(self._ctx({"main_net_pct": 0.12})), 0.12)

    def test_limitup_effect_combines_fields(self):
        """涨停效应不是原始字段，而是连板+封板-炸板的合成质量分。"""
        from app.core.factor import create_factor

        f = create_factor("limitup_effect")
        v = f.compute(self._ctx({"is_limitup": True, "limitup_continuous": 2,
                                 "seal_amount": 2e8, "broken_times": 0}))
        self.assertIsNotNone(v)
        self.assertGreater(v, 3.0)   # 1 + 2连板 + 封板加成

    def test_limitup_effect_none_when_not_limitup(self):
        from app.core.factor import create_factor

        f = create_factor("limitup_effect")
        self.assertIsNone(f.compute(self._ctx({"is_limitup": False})))


class TestFundamentalScore(unittest.TestCase):
    """基本面综合分：估值 30% + 成长 40% + 质量 30%，缺项重新加权。"""

    def test_quality_beats_junk(self):
        from app.core.data.features import fundamental_score

        good = fundamental_score({
            "pe": 12, "pb": 2.0, "roe": 22, "gross_margin": 45,
            "revenue_yoy": 30, "profit_yoy": 35, "debt_ratio": 35,
        })
        junk = fundamental_score({
            "pe": -5, "pb": 8, "roe": -2, "gross_margin": 8,
            "revenue_yoy": -25, "profit_yoy": -40, "debt_ratio": 88,
        })
        self.assertIsNotNone(good)
        self.assertGreater(good, 80)
        self.assertLess(junk, 20)

    def test_missing_items_reweighted(self):
        """只有估值和 ROE 也能给分（按可用项重新加权），不会被判死。"""
        from app.core.data.features import fundamental_score

        v = fundamental_score({"pe": 25, "roe": 18})
        self.assertIsNotNone(v)
        self.assertGreater(v, 60)

    def test_empty_is_none(self):
        """没数据返回 None，不是 0 —— 0 会被当成"基本面极差"。"""
        from app.core.data.features import fundamental_score

        self.assertIsNone(fundamental_score({}))

    def test_high_debt_penalised(self):
        from app.core.data.features import fundamental_score

        low = fundamental_score({"roe": 20, "gross_margin": 40, "debt_ratio": 30})
        high = fundamental_score({"roe": 20, "gross_margin": 40, "debt_ratio": 90})
        self.assertLess(high, low)

    def test_factor_and_feature_share_one_implementation(self):
        """因子与特征注入必须算出同一个数 —— 两份实现迟早会漂移。"""
        from app.core.data.features import fundamental_score
        from app.core.factor import create_factor
        from app.core.factor.context import FactorContext
        from app.core.market.series import BarSeries

        fields = {"pe": 15, "pb": 3, "roe": 16, "revenue_yoy": 20,
                  "profit_yoy": 25, "debt_ratio": 50}
        ctx = FactorContext("000001", BarSeries("000001"), features=dict(fields))
        f = create_factor("fundamental_score")
        self.assertAlmostEqual(f.compute(ctx), fundamental_score(fields), places=2)


class TestFundamentalFactors(unittest.TestCase):
    def _ctx(self, features):
        from app.core.factor.context import FactorContext
        from app.core.market.series import BarSeries

        return FactorContext("000001", BarSeries("000001"), features=features)

    def test_raw_factors(self):
        from app.core.factor import create_factor

        data = {"pe": 18.0, "pb": 2.5, "roe": 15.0, "revenue_yoy": 12.0,
                "profit_yoy": 20.0, "debt_ratio": 45.0, "dividend_yield": 2.0,
                "market_cap": 3.2e11}
        for name, expect in (("pe", 18.0), ("pb", 2.5), ("roe", 15.0),
                             ("revenue_yoy", 12.0), ("profit_yoy", 20.0),
                             ("debt_ratio", 45.0), ("dividend_yield", 2.0),
                             ("market_cap", 3.2e11)):
            got = create_factor(name).compute(self._ctx(data))
            self.assertAlmostEqual(got, expect, msg=name)

    def test_missing_is_none(self):
        from app.core.factor import create_factor

        for name in ("pe", "pb", "roe", "revenue_yoy", "profit_yoy",
                     "debt_ratio", "dividend_yield", "market_cap"):
            self.assertIsNone(create_factor(name).compute(self._ctx({})), name)


class TestMultiDimRisk(unittest.TestCase):
    """实盘多维度闸门：买入要过综合评分与基本面排雷。"""

    def _signal(self, side):
        from app.core.market.types import Signal, SignalSource

        return Signal(symbol="000001", side=side, source=SignalSource.BAR)

    def _state(self):
        from app.core.strategy.state import DecisionState

        return DecisionState()

    def _ctx(self, features):
        from app.core.factor.context import FactorContext
        from app.core.market.series import BarSeries

        return FactorContext("000001", BarSeries("000001"), features=features)

    def test_min_score_blocks_low(self):
        from app.core.market.types import Side
        from app.core.risk.builtin import MinScoreRisk

        r = MinScoreRisk(min_score=60.0)
        v = r.check(self._signal(Side.BUY), self._state(),
                    ctx=self._ctx({"composite_score": 42.0}))
        self.assertFalse(v.allowed)

    def test_min_score_passes_high(self):
        from app.core.market.types import Side
        from app.core.risk.builtin import MinScoreRisk

        r = MinScoreRisk(min_score=60.0)
        v = r.check(self._signal(Side.BUY), self._state(),
                    ctx=self._ctx({"composite_score": 78.0}))
        self.assertTrue(v.allowed)

    def test_min_score_never_blocks_sell(self):
        """卖出不能被评分挡住 —— 评分掉下来正是该卖的理由。"""
        from app.core.market.types import Side
        from app.core.risk.builtin import MinScoreRisk

        r = MinScoreRisk(min_score=90.0)
        v = r.check(self._signal(Side.SELL), self._state(),
                    ctx=self._ctx({"composite_score": 10.0}))
        self.assertTrue(v.allowed)

    def test_min_score_unknown_passes_with_reason(self):
        """没数据时默认放行但要说明 —— 一律拒绝会让策略莫名其妙一整天不下单。"""
        from app.core.market.types import Side
        from app.core.risk.builtin import MinScoreRisk

        r = MinScoreRisk(min_score=60.0)
        v = r.check(self._signal(Side.BUY), self._state(), ctx=self._ctx({}))
        self.assertTrue(v.allowed)
        self.assertIn("放行", str(v.reason))

    def test_fundamental_rejects_loss_making(self):
        from app.core.market.types import Side
        from app.core.risk.builtin import FundamentalRisk

        r = FundamentalRisk(reject_loss=True)
        v = r.check(self._signal(Side.BUY), self._state(),
                    ctx=self._ctx({"pe": -8.0}))
        self.assertFalse(v.allowed)

    def test_fundamental_debt_limit(self):
        from app.core.market.types import Side
        from app.core.risk.builtin import FundamentalRisk

        r = FundamentalRisk(max_debt_ratio=70.0)
        v = r.check(self._signal(Side.BUY), self._state(),
                    ctx=self._ctx({"pe": 20.0, "debt_ratio": 88.0}))
        self.assertFalse(v.allowed)

    def test_fundamental_passes_clean(self):
        from app.core.market.types import Side
        from app.core.risk.builtin import FundamentalRisk

        r = FundamentalRisk(reject_loss=True, max_debt_ratio=70.0,
                            min_fundamental_score=50.0)
        v = r.check(self._signal(Side.BUY), self._state(),
                    ctx=self._ctx({"pe": 15.0, "roe": 18.0, "debt_ratio": 40.0,
                                   "fundamental_score": 72.0}))
        self.assertTrue(v.allowed)


if __name__ == "__main__":
    unittest.main()
