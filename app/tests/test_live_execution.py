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

from app.execution.gateway import (
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
from app.execution.live_broker import LiveBroker
from app.domain.types import OrderStatus, Side
from app.portfolio.manager import Portfolio
from app.risk.base import RiskVerdict
from app.risk.builtin import (
    DailyTradeLimitRisk,
    OrderValueLimitRisk,
    PriceLimitRisk,
    SellablePositionRisk,
    TPlusOneRisk,
)
from app.strategy.state import DecisionState
from app.domain.types import Signal, SignalSource


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
                                       __import__("app.execution.gateway",
                                                  fromlist=["PositionSnapshot"]
                                                  ).PositionSnapshot(symbol="000001"))(symbol="000001")
        gw._positions["000001"].size = 5000     # 券商侧凭空多出持仓
        report = br.reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(report["position_diffs"])


class TestGatewayCapitalSeeding(unittest.TestCase):
    """券商账本的本金必须以任务为准，且每个任务只计一次。

    踩过两次坑：接入点配置自带 initial_cash 被叠加（本金算两遍），
    和同一个任务被挂两次（又算一遍）。两次都表现为"对账不一致 → 严格模式阻断交易"。
    """

    class _FakeTask:
        """只提供 attach 真正读到的三样东西：task_id / broker.gateway / 本金。"""

        def __init__(self, task_id: str, cash: float) -> None:
            self.task_id = task_id
            self.broker = type("B", (), {"gateway": None})()
            self.portfolio = type("P", (), {
                "account": type("A", (), {"initial_cash": cash})()
            })()

    def _component(self, gateway):
        # 券商网关原是独立的 LiveGatewayComponent，现在与落库单元一起
        # 并进 TradingService（网关那一半是它的 ``gateway`` 单元）。
        from app.engine.component import TradingService

        return TradingService(gateway=gateway)

    def test_endpoint_cash_not_counted_twice(self):
        from app.execution.gateway import SimulatedGateway

        # 接入点配置里的 initial_cash 只是"单独用网关"的兜底值
        gw = SimulatedGateway(initial_cash=100_000)
        comp = self._component(gw)
        comp.attach(self._FakeTask("a", 100_000))
        comp.attach(self._FakeTask("b", 80_000))
        self.assertEqual(gw._cash, 180_000)

    def test_same_task_attached_twice_seeds_once(self):
        from app.execution.gateway import SimulatedGateway

        gw = SimulatedGateway(initial_cash=100_000)
        comp = self._component(gw)
        comp.attach(self._FakeTask("a", 100_000))
        comp.attach(self._FakeTask("a", 100_000))   # 装载 + 启动各挂一次
        self.assertEqual(gw._cash, 100_000)

    def test_non_simulated_gateway_untouched(self):
        """真实券商的资金以券商为准，绝不能被本地任务本金覆盖。"""
        from app.execution.gateway import SimulatedGateway

        gw = SimulatedGateway(initial_cash=500_000)
        gw.name = "real_broker"
        comp = self._component(gw)
        comp.attach(self._FakeTask("a", 100_000))
        self.assertEqual(gw._cash, 500_000)


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


# ===========================================================================
# 超时单回收（reaper）
#
# 守的是实盘最难排查的一类故障：**策略在出信号，但就是不下单**。
# 根因是券商回报丢失后订单永远停在 SUBMITTED，而"已有未结订单就不再下新单"
# 那道保护会让该方向的信号被永久静默丢弃 —— 日志里一条 ERROR 都没有。
# ===========================================================================
class _SilentGateway(SimulatedGateway):
    """可控的假网关：下单受理，但**永不推送回报**。

    ``query_orders`` 的返回值由测试指定，用来模拟三种真实情况：
      1. 券商那边早就是终态了（回报丢了，回查能对齐）
      2. 券商那边还挂着（正常挂单，不该被撤）
      3. 券商查无此单（报单可能根本没成功）
    """

    def __init__(self, *a, query_result=None, cancel_raises=False, **kw):
        super().__init__(*a, **kw)
        self.query_result = query_result if query_result is not None else []
        self.cancel_raises = cancel_raises
        self.cancel_calls = 0
        self.query_calls = 0

    def poll_fills(self):
        return []          # 回报永久丢失

    def poll_order_updates(self):
        return []

    def query_orders(self, only_open: bool = True):
        self.query_calls += 1
        return self.query_result

    def cancel_order(self, broker_order_id: str) -> bool:
        self.cancel_calls += 1
        if self.cancel_raises:
            raise GatewayError("撤单接口超时")
        return True


class TestStaleOrderReaper(unittest.TestCase):
    def _stuck_broker(self, **gw_kw):
        """造一个"单已报出、回报永不到"的 Broker。"""
        pf = Portfolio(task_id="t1", symbol="000001",
                       initial_cash=100_000.0, fee_rate=0.0003)
        gw = _SilentGateway(price_source=lambda: {"000001": 10.0},
                            initial_cash=100_000.0, **gw_kw)
        gw.connect()
        br = LiveBroker(pf, gw, fee_rate=0.0003, order_timeout=1.0)
        br.set_price(10.0)
        order = br.create_order("000001", Side.BUY, 100, task_id="t1")
        br.submit(order)
        return br, gw, order

    def test_not_stale_before_timeout(self):
        """没超时就不该动它 —— 限价单挂一会儿是常态，乱撤会撤掉能成交的单。"""
        br, gw, order = self._stuck_broker()
        self.assertEqual(len(br.sweep_stale_orders()), 0)
        self.assertTrue(order.status == OrderStatus.SUBMITTED)
        self.assertEqual(gw.query_calls, 0)

    def test_recovered_by_broker_query(self):
        """多数"卡单"只是回报丢了：回查能对齐就不撤单、不误伤。

        这是最重要的一条路径 —— 它把"真故障"和"正常挂单"区分开了。
        """
        br, gw, order = self._stuck_broker()
        # 回查结果必须用**真实券商单号**，否则匹配不上（回查按 broker_order_id 找）
        bid = br._broker_ids[order.order_id]
        filled = GatewayOrder(
            broker_order_id=bid, client_order_id=order.order_id, symbol="000001",
            side=Side.BUY, size=100, price=10.0, status=OrderStatus.FILLED,
        )
        gw.query_result = [filled]

        handled = br.sweep_stale_orders(now=_later(order.created_at, 5))
        self.assertEqual(len(handled), 1)
        self.assertTrue(order.is_final)
        self.assertEqual(br.stats["stale_recovered"], 1)
        self.assertEqual(gw.cancel_calls, 0)     # 对齐了就不需要撤单

    def test_still_open_at_broker_is_not_cancelled_early(self):
        """券商确认还挂着 → 还没到强制撤单时长就不该撤。"""
        br, gw, order = self._stuck_broker()
        open_gw = GatewayOrder(
            broker_order_id=br._broker_ids[order.order_id],
            client_order_id=order.order_id, symbol="000001",
            side=Side.BUY, size=100, price=10.0, status=OrderStatus.SUBMITTED,
        )
        gw.query_result = [open_gw]
        br.sweep_stale_orders(now=_later(order.created_at, 5))
        self.assertEqual(gw.cancel_calls, 0)

    def test_forced_close_after_cancel_without_feedback(self):
        """撤单请求发出后仍无回报 → 强制收尾，绝不让任务永久停摆。"""
        br, gw, order = self._stuck_broker()
        # 1) 超过撤单阈值 -> 发撤单请求
        br.sweep_stale_orders(now=_later(order.created_at, 11))
        self.assertEqual(gw.cancel_calls, 1)
        self.assertFalse(order.is_final)
        # 2) 撤单后等够时间仍无回报 -> 强制置终态
        br.sweep_stale_orders(now=_later(order.created_at, 14))
        self.assertTrue(order.is_final)
        self.assertEqual(br.stats["stale_timeout"], 1)
        self.assertTrue(order.meta.get("stale"))

    def test_force_close_when_broker_never_heard_of_it(self):
        """券商查无此单（报单可能没成功）：回查次数用尽后收尾。"""
        br, gw, order = self._stuck_broker()   # query_result 为空
        for _ in range(br.STALE_MAX_TRIES):
            br.sweep_stale_orders(now=_later(order.created_at, 3))
        self.assertTrue(order.is_final, "回查用尽后必须收尾，否则永久卡单")
        self.assertEqual(br.stats["stale_timeout"], 1)

    def test_cancel_retries_then_recovers_by_query(self):
        """撤单接口报错，但回查发现单子已终态 —— 按回查结果收尾，不算失败。"""
        br, gw, order = self._stuck_broker(cancel_raises=True)
        done = GatewayOrder(
            broker_order_id=br._broker_ids[order.order_id],
            client_order_id=order.order_id, symbol="000001",
            side=Side.BUY, size=100, price=10.0, status=OrderStatus.CANCELLED,
        )
        gw.query_result = [done]
        self.assertTrue(br.cancel(order.order_id))
        self.assertEqual(order.status, OrderStatus.CANCELLED)


def _later(t, seconds: float):
    """把订单创建时间往后推 N 秒 —— 模拟"这笔单已经挂了很久"。"""
    from datetime import timedelta
    return t + timedelta(seconds=seconds)


if __name__ == "__main__":
    unittest.main()
