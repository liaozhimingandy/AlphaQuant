#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_risk_sizer.py
# @Description : 风控闸门链与仓位计算的单元测试
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest

from app.core.market.types import Account, Side, Signal, SignalSource
from app.core.portfolio import build_sizer
from app.core.risk import RiskChain, build_risk_chain, create_risk, list_risks
from app.core.strategy.state import DecisionState


def signal(side=Side.BUY, source=SignalSource.BAR, **meta) -> Signal:
    return Signal(symbol="TEST", side=side, source=source, meta=meta)


def state(**kw) -> DecisionState:
    base = dict(task_id="t", symbol="TEST", last_price=10.0, cash=100_000.0,
                equity=100_000.0, peak_equity=100_000.0)
    base.update(kw)
    return DecisionState(**base)


class TestRiskRegistry(unittest.TestCase):
    def test_builtin_registered(self):
        names = list_risks()
        for n in ("max_drawdown", "position_limit", "cash_reserve",
                  "cooldown", "news_confidence", "daily_loss"):
            self.assertIn(n, names)

    def test_unknown_risk_raises(self):
        with self.assertRaises(ValueError):
            create_risk("not_exist")


class TestRiskChain(unittest.TestCase):
    def test_blocks_on_drawdown(self):
        chain = build_risk_chain([{"type": "max_drawdown", "max_drawdown": 0.1}])
        st = state(equity=80_000.0, peak_equity=100_000.0)  # 回撤 20%
        v = chain.check(signal(), st)
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "max_drawdown")

    def test_passes_when_healthy(self):
        chain = build_risk_chain([{"type": "max_drawdown", "max_drawdown": 0.3}])
        self.assertTrue(chain.check(signal(), state()).allowed)

    def test_any_rule_blocks(self):
        chain = build_risk_chain([
            {"type": "max_drawdown", "max_drawdown": 0.5},
            {"type": "cooldown", "min_bars": 5},
        ])
        st = state(bars_since_last_trade=1)  # 冷却期内
        v = chain.check(signal(), st)
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "cooldown")

    def test_sell_not_blocked_by_drawdown(self):
        # 回撤风控只拦开仓，不能把平仓也拦死
        chain = build_risk_chain([{"type": "max_drawdown", "max_drawdown": 0.01}])
        st = state(equity=50_000.0, peak_equity=100_000.0)
        self.assertTrue(chain.check(signal(side=Side.SELL), st).allowed)

    def test_news_confidence_gate(self):
        chain = build_risk_chain([{"type": "news_confidence", "min_confidence": 0.6}])
        low = signal(source=SignalSource.NEWS, news_confidence=0.3)
        high = signal(source=SignalSource.NEWS, news_confidence=0.9)
        self.assertFalse(chain.check(low, state()).allowed)
        self.assertTrue(chain.check(high, state()).allowed)

    def test_missing_confidence_blocked(self):
        chain = build_risk_chain([{"type": "news_confidence", "min_confidence": 0.6}])
        self.assertFalse(
            chain.check(signal(source=SignalSource.NEWS), state()).allowed
        )

    def test_broken_rule_passes_and_logs(self):
        """风控自身崩溃不能把系统锁死 —— 按放行处理。"""
        class Boom:
            name = "boom"

            def check(self, *a, **kw):
                raise RuntimeError("boom")

        chain = RiskChain([Boom()])  # type: ignore[list-item]
        self.assertTrue(chain.check(signal(), state()).allowed)

    def test_cash_reserve_blocks_unaffordable(self):
        chain = build_risk_chain([{"type": "cash_reserve"}])
        st = state(last_price=100.0)
        v = chain.check(signal(meta={"planned_size": 100000}), st, account=Account(cash=1000.0))
        self.assertFalse(v.allowed)


class TestSizer(unittest.TestCase):
    def test_fixed_sizer_lot_rounding(self):
        s = build_sizer({"type": "fixed", "size": 550})
        n = s.size(Side.BUY, 10.0, Account(cash=1_000_000.0))
        self.assertEqual(n, 500)  # 向下取整到 100 股

    def test_sell_returns_position(self):
        s = build_sizer({"type": "percent", "pct": 0.5})
        n = s.size(Side.SELL, 10.0, Account(cash=100_000.0), position_size=300)
        self.assertEqual(n, 300)

    def test_percent_respects_cash(self):
        s = build_sizer({"type": "percent", "pct": 0.5})
        n = s.size(Side.BUY, 10.0, Account(cash=10_000.0))
        self.assertLessEqual(n * 10.0, 10_000.0)

    def test_scale_reduces_size(self):
        s = build_sizer({"type": "percent", "pct": 0.5})
        full = s.size(Side.BUY, 10.0, Account(cash=1_000_000.0))
        half = s.size(Side.BUY, 10.0, Account(cash=1_000_000.0), scale=0.5)
        self.assertLessEqual(half, full)

    def test_zero_price_yields_zero(self):
        s = build_sizer({"type": "percent"})
        self.assertEqual(s.size(Side.BUY, 0.0, Account(cash=100_000.0)), 0)

    def test_method_shadowing_is_rejected(self):
        """构造参数与 size() 方法同名会被提前拦掉，而不是等到下单时报诡异错误。"""
        from app.core.portfolio.sizer import IBaseSizer

        class Bad(IBaseSizer):
            name = "bad"

            def __init__(self, size=1):
                self.size = size  # 故意遮蔽

        import app.core.portfolio.sizer as sz

        sz._SIZERS["bad"] = Bad
        try:
            with self.assertRaises(TypeError):
                build_sizer({"type": "bad"})
        finally:
            sz._SIZERS.pop("bad", None)


if __name__ == "__main__":
    unittest.main()
