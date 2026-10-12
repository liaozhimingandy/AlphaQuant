#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_risk_sizer.py
# @Description : 风控闸门链与仓位计算的单元测试
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest

from app.domain.types import Account, Side, Signal, SignalSource
from app.account import build_sizer
from app.modules.risk import RiskChain, build_risk_chain, create_risk, list_risks
from app.modules.strategy.state import DecisionState


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
        from app.account.sizer import IBaseSizer

        class Bad(IBaseSizer):
            name = "bad"

            def __init__(self, size=1):
                self.size = size  # 故意遮蔽

        import app.account.sizer as sz

        sz._SIZERS["bad"] = Bad
        try:
            with self.assertRaises(TypeError):
                build_sizer({"type": "bad"})
        finally:
            sz._SIZERS.pop("bad", None)


class TestAStockHardRules(unittest.TestCase):
    """A 股硬约束必须自动装配 —— 它们是市场规则，不是策略偏好。

    背景：默认配置里只写了 cooldown/cash_reserve/position_limit/max_drawdown，
    T+1、涨跌停、可卖量一条都没有。照默认配置上实盘就是"涨停追买、
    当日买当日卖、甚至卖空"，每一笔都被柜台拒单，却照样占用报单额度。
    """

    def test_backtest_chain_is_untouched(self):
        """回测不追加：回测里 prev_close/is_same_day 常常缺失，
        多挂一层就多一处与实盘不一致的拦截口径。"""
        chain = build_risk_chain([{"type": "max_drawdown", "max_drawdown": 0.1}])
        self.assertEqual(len(chain.rules), 1)

    def test_live_chain_gets_hard_rules(self):
        chain = build_risk_chain(
            [{"type": "max_drawdown", "max_drawdown": 0.1}],
            enforce_astock=True,
        )
        names = {r.name for r in chain.rules}
        for n in ("sellable_position", "price_limit", "t_plus_one"):
            self.assertIn(n, names, f"实盘风控链缺少硬约束 {n}")

    def test_user_rule_wins_no_duplicate(self):
        """用户显式配过同名的，用他的参数，不重复挂两条。"""
        chain = build_risk_chain(
            [{"type": "price_limit", "limit_pct": 0.20}],
            enforce_astock=True,
        )
        pl = [r for r in chain.rules if r.name == "price_limit"]
        self.assertEqual(len(pl), 1)
        self.assertEqual(pl[0].limit_pct, 0.20)

    def test_short_sell_blocked_by_default_in_live(self):
        """无持仓却要卖出 —— 默认配置下这条必须被拦住。"""
        chain = build_risk_chain([], enforce_astock=True)
        st = state(position_size=0)
        v = chain.check(signal(Side.SELL), st)
        self.assertFalse(v.allowed)

    def test_limit_up_buy_blocked_in_live(self):
        chain = build_risk_chain([], enforce_astock=True)
        st = state(position_size=0, last_price=11.0,
                   meta={"prev_close": 10.0})   # +10% 涨停
        v = chain.check(signal(Side.BUY), st)
        self.assertFalse(v.allowed)
        self.assertIn("涨停", v.reason)

    def test_broken_hard_rule_does_not_break_chain(self):
        """硬约束装配失败不能让整条链建不起来（配置错也要能启动）。"""
        from unittest import mock
        from app.modules.risk import registry as R

        with mock.patch.object(R, "create_risk",
                               side_effect=ValueError("boom")):
            chain = R.build_risk_chain([], enforce_astock=True)
        self.assertEqual(len(chain.rules), 0)


class TestLiveLikeModeDetection(unittest.TestCase):
    """哪些运行模式要按真金白银的标准装配风控。"""

    def test_live_and_simulate_enforce(self):
        from app.runtime.tasking.spec import _is_live_like
        for mode in ("LIVE", "live", "SIMULATE", "simulate"):
            self.assertTrue(_is_live_like(mode), mode)

    def test_backtest_does_not(self):
        from app.runtime.tasking.spec import _is_live_like
        for mode in ("BACKTEST", "backtest", None, ""):
            self.assertFalse(_is_live_like(mode), mode)

    def test_enum_and_string_both_work(self):
        """两种 RunMode 枚举 + 纯字符串都要认得（配置里是字符串）。"""
        from app.domain.types import RunMode as DMode
        from app.engine.context import RunMode as EMode
        from app.runtime.tasking.spec import _is_live_like

        self.assertTrue(_is_live_like(DMode.LIVE))
        self.assertTrue(_is_live_like(EMode.SIMULATE))
        self.assertFalse(_is_live_like(DMode.BACKTEST))


if __name__ == "__main__":
    unittest.main()
