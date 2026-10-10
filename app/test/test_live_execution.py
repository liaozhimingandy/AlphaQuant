#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_live_execution.py
# @Description : 实盘执行链路测试：网关 / 回报驱动的撮合 / 前置风控
#
#                这一组测试守的是"实盘上最贵的那几个 bug"：
#                  - 下单即成交的错觉（实盘是异步回报）
#                  - 回报重复推送导致重复入账
#                  - 撤单被当成同步成功
#                  - 未结订单期间重复下单堆出一堆委托
#                  - 成交回报入不了账，订单却标记成 FILLED
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest

from app.core.execution.gateway import (
    AccountSnapshot,
    FillEvent,
    GatewayError,
    GatewayOrder,
    OrderRejected,
    OrderRequest,
    SimulatedGateway,
    create_gateway,
    has_gateway,
    list_gateways,
)
from app.core.execution.live_broker import LiveBroker
from app.core.market.types import OrderStatus, Side
from app.core.portfolio.manager import Portfolio
from app.core.risk.base import RiskVerdict
from app.core.risk.builtin import (
    DailyTradeLimitRisk,
    OrderValueLimitRisk,
    PriceLimitRisk,
    SellablePositionRisk,
    TPlusOneRisk,
)
from app.core.strategy.state import DecisionState
from app.core.market.types import Signal, SignalSource


def _signal(side: Side = Side.BUY) -> Signal:
    return Signal(symbol="000001", side=side, source=SignalSource.BAR)


def _state(**kw) -> DecisionState:
    """构造决策状态。

    注意区分两类入参：``DecisionState`` 的**真实字段**（last_price /
    position_size …）走构造，其余（prev_close / bought_today …）走 meta。
    混为一谈会让风控读到默认值，测试就变成了"测了个寂寞"。
    """
    fields = set(DecisionState.__dataclass_fields__)
    attrs = {k: v for k, v in kw.items() if k in fields and k != "meta"}
    extra = {k: v for k, v in kw.items() if k not in fields or k == "meta"}
    st = DecisionState(
        task_id="t1", symbol="000001",
        position_size=attrs.pop("position_size", 1000),
        last_price=attrs.pop("last_price", 10.0),
        cash=attrs.pop("cash", 100_000.0),
        equity=attrs.pop("equity", 110_000.0),
        **attrs,
    )
    st.meta.update(extra)
    return st


def _broker(cash: float = 100_000.0, delay: int = 1, **gw_kw):
    pf = Portfolio(task_id="t1", symbol="000001", initial_cash=cash, fee_rate=0.0003)
    gw = SimulatedGateway(price_source=lambda: {"000001": 10.0},
                          fill_delay_calls=delay, initial_cash=cash, **gw_kw)
    gw.connect()
    br = LiveBroker(pf, gw, fee_rate=0.0003)
    br.set_price(10.0)
    return br, gw, pf


class TestGatewayRegistry(unittest.TestCase):
    def test_simulated_registered(self):
        self.assertTrue(has_gateway("simulated"))
        self.assertIn("simulated", list_gateways())

    def test_create(self):
        gw = create_gateway("simulated")
        self.assertEqual(gw.name, "simulated")

    def test_unknown_raises(self):
        with self.assertRaises(KeyError):
            create_gateway("no_such_broker")

    def test_order_request_serializable(self):
        r = OrderRequest(symbol="000001", side=Side.BUY, size=100, price=10.0)
        d = r.to_dict()
        self.assertEqual(d["side"], "BUY")
        self.assertEqual(d["size"], 100)

    def test_gateway_order_remaining_and_final(self):
        o = GatewayOrder(size=1000, filled_size=400)
        self.assertEqual(o.remaining, 600)
        self.assertFalse(o.is_final)
        o.status = OrderStatus.FILLED
        self.assertTrue(o.is_final)


class TestLiveBrokerSubmit(unittest.TestCase):
    def test_submit_is_not_immediate_fill(self):
        """这是实盘与回测的分水岭：submit 返回时只是 SUBMITTED。"""
        br, gw, pf = _broker(delay=1)
        o = br.create_order("000001", Side.BUY, 1000, "t1", "买", "strategy")
        br.submit(o)
        self.assertEqual(o.status, OrderStatus.SUBMITTED)
        self.assertEqual(pf.position.size, 0)
        self.assertEqual(pf.account.cash, 100_000.0)
        self.assertEqual(len(br.open_orders), 1)

    def test_zero_size_rejected(self):
        br, _, _ = _broker()
        o = br.create_order("000001", Side.BUY, 0, "t1")
        br.submit(o)
        self.assertEqual(o.status, OrderStatus.REJECTED)
        self.assertEqual(br.stats["rejected"], 1)

    def test_price_cage_rejects(self):
        br, _, _ = _broker()
        o = br.create_order("000001", Side.BUY, 100, "t1")
        o.price = 12.0            # 偏离现价 20% > 默认 2% 笼子
        br.submit(o)
        self.assertEqual(o.status, OrderStatus.REJECTED)
        self.assertIn("笼子", o.reject_reason)

    def test_max_order_value_rejects(self):
        br, _, _ = _broker()
        br.max_order_value = 5000.0
        o = br.create_order("000001", Side.BUY, 1000, "t1")   # 10 * 1000 = 10000
        br.submit(o)
        self.assertEqual(o.status, OrderStatus.REJECTED)
        self.assertIn("上限", o.reject_reason)


class TestFillFlow(unittest.TestCase):
    def test_fill_updates_portfolio(self):
        br, gw, pf = _broker()
        o = br.create_order("000001", Side.BUY, 1000, "t1", "买", "strategy")
        br.submit(o)
        for f in gw.poll_fills():
            br.on_fill(f)
        self.assertEqual(o.status, OrderStatus.FILLED)
        self.assertEqual(pf.position.size, 1000)
        self.assertLess(pf.account.cash, 100_000.0)
        self.assertEqual(len(br.open_orders), 0)

    def test_duplicate_fill_is_idempotent(self):
        """券商回报可能重复推送。重复入账会让持仓凭空翻倍。"""
        br, gw, pf = _broker()
        o = br.create_order("000001", Side.BUY, 1000, "t1")
        br.submit(o)
        fills = gw.poll_fills()
        self.assertEqual(len(fills), 1)
        br.on_fill(fills[0])
        size_after_first = pf.position.size
        br.on_fill(fills[0])       # 再来一次
        self.assertEqual(pf.position.size, size_after_first)
        self.assertEqual(br.stats["filled"], 1)

    def test_unknown_order_fill_ignored(self):
        br, _, pf = _broker()
        r = br.on_fill(FillEvent(
            fill_id="x", client_order_id="never-seen", broker_order_id="SIMX",
            symbol="000001", side=Side.BUY, size=100, price=10.0, filled_size=100,
        ))
        self.assertIsNone(r)
        self.assertEqual(pf.position.size, 0)

    def test_zero_price_fill_not_marked_filled(self):
        """账本拒绝入账时订单**不能**被标成 FILLED。
        否则订单显示已成交、持仓却是空的，上层所有判断都建立在错前提上。"""
        br, _, pf = _broker()
        o = br.create_order("000001", Side.BUY, 1000, "t1")
        br.submit(o)
        r = br.on_fill(FillEvent(
            fill_id="z", client_order_id=o.order_id, broker_order_id="SIMZ",
            symbol="000001", side=Side.BUY, size=1000, price=0.0, filled_size=1000,
        ))
        self.assertEqual(r.status, OrderStatus.SUBMITTED)   # 没有被标记成交
        self.assertEqual(pf.position.size, 0)

    def test_oversell_truncated_to_holdings(self):
        br, gw, pf = _broker()
        o = br.create_order("000001", Side.BUY, 1000, "t1")
        br.submit(o)
        for f in gw.poll_fills():
            br.on_fill(f)
        o2 = br.create_order("000001", Side.SELL, 99999, "t1")
        br.submit(o2)
        for f in gw.poll_fills():
            br.on_fill(f)
        self.assertEqual(pf.position.size, 0)
        self.assertGreaterEqual(pf.account.cash, 0)

    def test_order_update_callback_fires(self):
        br, gw, pf = _broker()
        seen = []
        br.on_order_update = lambda o: seen.append(o.status.value)
        o = br.create_order("000001", Side.BUY, 1000, "t1")
        br.submit(o)
        for f in gw.poll_fills():
            br.on_fill(f)
        self.assertIn("SUBMITTED", seen)
        self.assertIn("FILLED", seen)

    def test_avg_price_across_partial_fills(self):
        """两笔成交后均价必须是成交量加权，不能是"最后一次的价格"。"""
        br, _, _ = _broker()
        o = br.create_order("000001", Side.BUY, 1000, "t1")
        br.submit(o)
        br.on_fill(FillEvent(fill_id="f1", client_order_id=o.order_id,
                             symbol="000001", side=Side.BUY, size=400,
                             price=10.0, filled_size=400))
        self.assertEqual(o.status, OrderStatus.PARTIAL)
        br.on_fill(FillEvent(fill_id="f2", client_order_id=o.order_id,
                             symbol="000001", side=Side.BUY, size=600,
                             price=11.0, filled_size=1000))
        self.assertEqual(o.status, OrderStatus.FILLED)
        self.assertAlmostEqual(o.filled_price, (10.0 * 400 + 11.0 * 600) / 1000, places=6)


class TestCancel(unittest.TestCase):
    def test_cancel_is_async(self):
        """撤单返回 True 只代表"券商受理了"，状态要等回报才变。"""
        br, gw, pf = _broker(delay=99)
        o = br.create_order("000001", Side.BUY, 500, "t1")
        br.submit(o)
        self.assertTrue(br.cancel(o.order_id))
        self.assertEqual(o.status, OrderStatus.SUBMITTED)   # 还没变
        for u in gw.poll_order_updates():
            br.apply_order_update(u)
        self.assertEqual(o.status, OrderStatus.CANCELLED)
        self.assertEqual(len(br.open_orders), 0)

    def test_cancel_final_order_returns_false(self):
        br, gw, pf = _broker()
        o = br.create_order("000001", Side.BUY, 500, "t1")
        br.submit(o)
        for f in gw.poll_fills():
            br.on_fill(f)
        self.assertFalse(br.cancel(o.order_id))

    def test_cancel_all(self):
        br, gw, _ = _broker(delay=99)
        for _ in range(3):
            o = br.create_order("000001", Side.BUY, 100, "t1")
            br.submit(o)
        self.assertEqual(br.cancel_all(), 3)
        for u in gw.poll_order_updates():
            br.apply_order_update(u)
        self.assertEqual(len(br.open_orders), 0)


class TestReconcile(unittest.TestCase):
    def test_consistent_books(self):
        br, gw, pf = _broker()
        o = br.create_order("000001", Side.BUY, 1000, "t1")
        br.submit(o)
        for f in gw.poll_fills():
            br.on_fill(f)
        report = br.reconcile()
        self.assertTrue(report["ok"], report)

    def test_detects_position_mismatch(self):
        """有人在同一账户手工下单时，只有对账能发现。"""
        br, gw, pf = _broker()
        gw._positions["000001"] = type(gw.query_positions().get("000001") or
                                       __import__("app.core.execution.gateway",
                                                  fromlist=["PositionSnapshot"]
                                                  ).PositionSnapshot(symbol="000001"))(symbol="000001")
        gw._positions["000001"].size = 5000     # 券商侧凭空多出持仓
        report = br.reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(report["position_diffs"])


class TestLiveRiskRules(unittest.TestCase):
    """A 股实盘特有的前置风控。回测里无所谓，一上实盘就致命。"""

    def test_price_limit_blocks_buy_at_limit_up(self):
        r = PriceLimitRisk(limit_pct=0.10)
        st = _state(last_price=11.0, prev_close=10.0, is_same_day=False)
        v = r.check(_signal(Side.BUY), st)
        self.assertFalse(v.allowed)
        self.assertIn("涨停", v.reason)

    def test_price_limit_blocks_sell_at_limit_down(self):
        r = PriceLimitRisk(limit_pct=0.10)
        st = _state(last_price=9.0, prev_close=10.0)
        self.assertFalse(r.check(_signal(Side.SELL), st).allowed)

    def test_price_limit_allows_normal_move(self):
        r = PriceLimitRisk(limit_pct=0.10)
        st = _state(last_price=10.3, prev_close=10.0)
        self.assertTrue(r.check(_signal(Side.BUY), st).allowed)

    def test_price_limit_passes_without_prev_close(self):
        """拿不到昨收时放行 —— 风控不该因为缺数据把系统锁死。"""
        r = PriceLimitRisk()
        self.assertTrue(r.check(_signal(Side.BUY), _state(prev_close=0)).allowed)

    def test_t_plus_one_blocks_same_day_sell(self):
        r = TPlusOneRisk()
        st = _state(position_size=1000, bought_today=1000, is_same_day=True)
        v = r.check(_signal(Side.SELL), st)
        self.assertFalse(v.allowed)
        self.assertIn("T+1", v.reason)

    def test_t_plus_one_partial_sellable(self):
        r = TPlusOneRisk()
        st = _state(position_size=1000, bought_today=400, is_same_day=True)
        self.assertTrue(r.check(_signal(Side.SELL), st).allowed)

    def test_t_plus_one_ignores_buy(self):
        r = TPlusOneRisk()
        st = _state(bought_today=1000, is_same_day=True)
        self.assertTrue(r.check(_signal(Side.BUY), st).allowed)

    def test_t_plus_one_not_applied_on_daily_bars(self):
        """日线推进时一根K线就是一个交易日，不存在"同日"。
        这条规则必须自然失效，而不是把当天所有卖出都拦掉。"""
        r = TPlusOneRisk()
        st = _state(position_size=1000, bought_today=1000, is_same_day=False)
        self.assertTrue(r.check(_signal(Side.SELL), st).allowed)

    def test_order_value_limit(self):
        r = OrderValueLimitRisk(max_value=10_000.0)
        st = _state(cash=1_000_000.0)
        self.assertFalse(r.check(_signal(Side.BUY), st).allowed)
        st2 = _state(cash=8_000.0)
        self.assertTrue(r.check(_signal(Side.BUY), st2).allowed)

    def test_daily_trade_limit(self):
        r = DailyTradeLimitRisk(max_trades=3)
        self.assertTrue(r.check(_signal(), _state(trades_today=2)).allowed)
        self.assertFalse(r.check(_signal(), _state(trades_today=3)).allowed)

    def test_sellable_position(self):
        r = SellablePositionRisk()
        st = _state(position_size=1000, bought_today=600, pending_sell=400)
        v = r.check(_signal(Side.SELL), st)
        self.assertFalse(v.allowed)
        self.assertIn("无可卖数量", v.reason)

    def test_sellable_position_no_holdings(self):
        r = SellablePositionRisk()
        self.assertFalse(r.check(_signal(Side.SELL), _state(position_size=0)).allowed)


if __name__ == "__main__":
    unittest.main()
