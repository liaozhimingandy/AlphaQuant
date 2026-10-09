#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_core_layers.py
# @Description : 指标 / 因子 / 规则 / 策略 的单元测试
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from app.core.factor import FactorContext, create_factor, list_factors
from app.core.indicator import create_indicator, list_indicators
from app.core.market import Bar, BarSeries
from app.core.rule import AllRule, AnyRule, NotRule, build_rule
from app.core.rule.builtin import AlwaysRule, NeverRule, ThresholdRule
from app.core.strategy import build_strategy, list_strategies
from app.core.strategy.state import DecisionState


def make_series(n: int = 120, start: float = 10.0, slope: float = 0.08) -> BarSeries:
    s = BarSeries("TEST")
    base = datetime(2024, 1, 1)
    for i in range(n):
        c = start + slope * i + 0.3 * math.sin(i / 3.0)
        s.append(Bar("TEST", base + timedelta(days=i), c - 0.1, c + 0.2, c - 0.3, c, 1000 + i, 0))
    return s


def make_ctx(n: int = 120, features=None) -> FactorContext:
    return FactorContext(
        "TEST", make_series(n), params={}, features=features or {}
    )


class TestIndicators(unittest.TestCase):
    def test_registry_not_empty(self):
        self.assertIn("sma", list_indicators())
        self.assertIn("rsi", list_indicators())

    def test_sma_matches_manual(self):
        s = make_series(30)
        val = create_indicator("sma", period=5).value(s)
        expected = sum(b.close for b in s.bars[-5:]) / 5
        self.assertAlmostEqual(val, expected, places=6)

    def test_rsi_in_bounds_on_uptrend(self):
        ctx = make_ctx(120)
        # 强上涨序列，RSI 应接近上界
        self.assertGreater(create_factor("rsi").compute(ctx), 80.0)

    def test_unknown_indicator_returns_none(self):
        s = make_series(30)
        self.assertIsNone(s and None)
        # ctx.ind 对未注册指标返回 None，而不是抛异常
        ctx = make_ctx(30)
        self.assertIsNone(ctx.ind("not_exist"))


class TestFactors(unittest.TestCase):
    def test_registry_not_empty(self):
        names = list_factors()
        for expect in ("price", "ma", "rsi", "news_impact"):
            self.assertIn(expect, names)

    def test_news_factors_read_features(self):
        ctx = make_ctx(60, features={"news_sentiment": 0.8, "news_confidence": 0.5})
        self.assertAlmostEqual(create_factor("news_sentiment").compute(ctx), 0.8)
        # impact = score * confidence
        self.assertAlmostEqual(create_factor("news_impact").compute(ctx), 0.4)

    def test_price_position_bounded(self):
        ctx = make_ctx(80)
        v = create_factor("price_position", period=20).compute(ctx)
        self.assertGreaterEqual(v, 0.0)
        self.assertLessEqual(v, 1.0)

    def test_factor_never_raises_on_short_series(self):
        ctx = make_ctx(3)
        for name in ("ma", "rsi", "momentum", "volatility", "drawdown"):
            create_factor(name).compute(ctx)  # 不应抛异常


class TestRules(unittest.TestCase):
    def test_threshold_rule(self):
        ctx = make_ctx(60, features={"news_impact": 0.9})
        self.assertTrue(ThresholdRule("news_impact", "gt", 0.5).is_satisfied(ctx))
        self.assertFalse(ThresholdRule("news_impact", "lt", 0.5).is_satisfied(ctx))

    def test_combinators(self):
        ctx = make_ctx(60)
        self.assertTrue(AllRule([AlwaysRule(), AlwaysRule()]).is_satisfied(ctx))
        self.assertFalse(AllRule([AlwaysRule(), NeverRule()]).is_satisfied(ctx))
        self.assertTrue(AnyRule([NeverRule(), AlwaysRule()]).is_satisfied(ctx))
        self.assertTrue(NotRule(NeverRule()).is_satisfied(ctx))

    def test_build_rule_from_spec(self):
        ctx = make_ctx(60, features={"news_impact": 0.9, "news_confidence": 0.8})
        spec = {
            "all": [
                {"factor": "news_impact", "op": "gt", "value": 0.3},
                {"factor": "news_confidence", "op": "gte", "value": 0.5},
            ]
        }
        self.assertTrue(build_rule(spec).is_satisfied(ctx))

    def test_build_rule_shorthands(self):
        self.assertTrue(build_rule("always").is_satisfied(make_ctx()))
        self.assertFalse(build_rule("never").is_satisfied(make_ctx()))

    def test_build_rule_rejects_garbage(self):
        with self.assertRaises(ValueError):
            build_rule({"unknown_key": 1})
        with self.assertRaises(ValueError):
            build_rule(None)


class TestStrategyBuild(unittest.TestCase):
    def test_registered_strategies(self):
        self.assertIn("combo", list_strategies())
        self.assertIn("ma_cross", list_strategies())

    def test_build_combo_with_event_rules(self):
        spec = {
            "type": "combo",
            "entry": "never",
            "exit": "never",
            "event_entry": {"factor": "news_impact", "op": "gt", "value": 0.3},
        }
        st = build_strategy(spec)
        ctx = make_ctx(60, features={"news_impact": 0.9, "news_confidence": 0.9})
        state = DecisionState(task_id="t", symbol="TEST", last_price=10.0)
        sig = st.on_event(ctx, state, event=None)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side.value, "BUY")

    def test_bar_channel_blocked_by_never(self):
        st = build_strategy({"type": "combo", "entry": "never", "exit": "never"})
        state = DecisionState(task_id="t", symbol="TEST", last_price=10.0)
        self.assertIsNone(st.on_bar(make_ctx(60), state))


if __name__ == "__main__":
    unittest.main()
