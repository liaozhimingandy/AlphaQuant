#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_portfolio_service.py
# @Description : 组合服务：账户级聚合 + 跨任务额度预留
#
#               这里钉住的是两类很容易被"看起来对"的实现蒙混过去的语义：
#                 1. 全账户回撤**不能**由各任务的回撤推出来；
#                 2. 额度预留必须有 TTL，否则一笔丢失的回报会永久吃掉额度。
#               第 2 条如果不测，出问题时表现为"跑着跑着就不下单了"，
#               而日志里没有任何错误 —— 这类故障只能靠测试提前钉住。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
import unittest
from typing import Any, Dict
from unittest import mock

from app.domain.types import Order, OrderStatus, Side, Signal, SignalSource
from app.modules.portfolio.service import PortfolioService
from app.runtime.tasking.task import QuantTask
from app.account.portfolio import Portfolio


# ===========================================================
# 桩：一个只放任务的运行时
# ===========================================================
class _RuntimeStub:
    def __init__(self, portfolios) -> None:
        self.tasks: Dict[str, Any] = {}
        for pf in portfolios:
            self.tasks[pf.task_id] = type("T", (), {"portfolio": pf})()


def _pf(task_id: str, symbol: str, cash: float, size: int = 0,
        avg: float = 0.0, last: float = 0.0) -> Portfolio:
    p = Portfolio(task_id=task_id, symbol=symbol, initial_cash=cash)
    p.account.cash = float(cash)
    p.position.size = int(size)
    p.position.avg_price = float(avg)
    p.position.last_price = float(last)
    return p


def _svc(portfolios=(), **kw) -> PortfolioService:
    s = PortfolioService(**kw)
    s.attach_runtime(lambda: _RuntimeStub(list(portfolios)))
    return s


# ===========================================================
# 账户聚合
# ===========================================================
class TestAccountAggregation(unittest.TestCase):
    """账户口径：多个任务的账本要能合成一张表。"""

    def test_cash_and_equity_summed(self):
        s = _svc([_pf("a", "000001", 60_000.0), _pf("b", "600000", 40_000.0)])
        agg = s.aggregate(force=True)
        self.assertEqual(agg["task_count"], 2)
        self.assertAlmostEqual(agg["cash"], 100_000.0, places=2)
        self.assertAlmostEqual(agg["equity"], 100_000.0, places=2)

    def test_same_symbol_merged_with_weighted_avg_cost(self):
        """两只任务各持同一只票，账户视图要合成一条，成本按股数加权。"""
        s = _svc([
            _pf("a", "000001", 0.0, size=100, avg=10.0, last=11.0),
            _pf("b", "000001", 0.0, size=300, avg=12.0, last=11.0),
        ])
        agg = s.aggregate(force=True)
        self.assertEqual(len(agg["positions"]), 1)      # 合成一条
        p = agg["positions"][0]
        self.assertEqual(p["size"], 400)
        self.assertAlmostEqual(p["avg_price"], 11.5, places=3)   # (100*10+300*12)/400
        self.assertAlmostEqual(p["market_value"], 4400.0, places=2)
        self.assertEqual(sorted(p["tasks"]), ["a", "b"])

    def test_zero_position_not_listed_as_holding(self):
        """零仓位不是持仓。留着它们会让面板出现一堆 size=0 的行。"""
        s = _svc([_pf("a", "000001", 50_000.0, size=0)])
        self.assertEqual(s.aggregate(force=True)["positions"], [])

    def test_broken_task_does_not_break_account_view(self):
        """单个任务账本坏了不能拖垮整个账户视图。"""
        s = _svc([_pf("a", "000001", 10_000.0)])
        bad = type("Bad", (), {})()          # 没有 account / position
        bad.task_id = "broken"
        rt = _RuntimeStub([_pf("a", "000001", 10_000.0)])
        rt.tasks["broken"] = type("T", (), {"portfolio": bad})()
        s2 = PortfolioService()
        s2.attach_runtime(lambda: rt)
        agg = s2.aggregate(force=True)
        self.assertEqual(agg["task_count"], 1)          # 坏的被跳过，好的还在
        self.assertAlmostEqual(agg["cash"], 10_000.0, places=2)

    def test_aggregation_is_cached_until_dirty(self):
        """惰性重算：一根 bar 内被读 N 次只算一次，否则回测会被拖垮。"""
        s = _svc([_pf("a", "000001", 10_000.0)])
        s.aggregate(force=True)
        n = s.stats["aggregations"]
        for _ in range(5):
            s.aggregate()
        self.assertEqual(s.stats["aggregations"], n)
        s.mark_dirty()
        s.aggregate()
        self.assertEqual(s.stats["aggregations"], n + 1)


class TestAccountDrawdown(unittest.TestCase):
    """全账户回撤 —— 最容易做错的一处。

    直觉是"取各任务回撤的最大值/平均值"，两者都是错的：
    任务 A 亏 10%、任务 B 赚 10%，各任务回撤都是 10%，账户却可能没回撤。
    """

    def test_drawdown_uses_aggregated_equity_peak(self):
        s = _svc([_pf("a", "000001", 60_000.0), _pf("b", "600000", 40_000.0)])
        self.assertAlmostEqual(s.aggregate(force=True)["equity"], 100_000.0, places=2)

        # 账户从 10 万跌到 9 万：回撤 10%
        for pf in s._iter_portfolios():
            pf.account.cash *= 0.9
        s.mark_dirty()
        agg = s.aggregate(force=True)
        self.assertAlmostEqual(agg["equity"], 90_000.0, places=2)
        self.assertAlmostEqual(agg["drawdown"], 0.1, places=3)
        self.assertAlmostEqual(agg["peak_equity"], 100_000.0, places=2)

    def test_each_task_down_but_account_not(self):
        """任务 A 亏、任务 B 赚 —— 各任务都有回撤，账户没有。

        这个用例专门用来打"取各任务回撤最大值"的错误实现：
        那种实现在这里会报 10%，而正确答案是 0。
        """
        a = _pf("a", "000001", 50_000.0)
        b = _pf("b", "600000", 50_000.0)
        s = _svc([a, b])
        s.aggregate(force=True)                  # 峰值 = 10 万

        a.account.cash = 45_000.0                # A 亏 10%
        b.account.cash = 55_000.0                # B 赚 10%
        s.mark_dirty()
        agg = s.aggregate(force=True)

        self.assertGreater(a.drawdown, 0.0)      # 单任务口径确实有回撤
        self.assertAlmostEqual(agg["equity"], 100_000.0, places=2)
        self.assertAlmostEqual(agg["drawdown"], 0.0, places=4)

    def test_peak_never_goes_down(self):
        s = _svc([_pf("a", "000001", 100_000.0)])
        s.aggregate(force=True)
        for pf in s._iter_portfolios():
            pf.account.cash = 80_000.0
        s.mark_dirty()
        s.aggregate(force=True)
        for pf in s._iter_portfolios():
            pf.account.cash = 90_000.0           # 涨回来但没创新高，回撤仍在
        s.mark_dirty()
        agg = s.aggregate(force=True)
        self.assertAlmostEqual(agg["peak_equity"], 100_000.0, places=2)
        self.assertAlmostEqual(agg["drawdown"], 0.1, places=3)


# ===========================================================
# 额度预留
# ===========================================================
class TestQuotaReservation(unittest.TestCase):

    def test_reserve_and_release(self):
        s = _svc(budget=10_000.0)
        rid = s.reserve("t1", "000001", 4_000.0, order_id="o1")
        self.assertTrue(rid)
        self.assertAlmostEqual(s.reserved, 4_000.0, places=2)
        self.assertAlmostEqual(s.available, 6_000.0, places=2)
        self.assertTrue(s.release_order("o1"))
        self.assertAlmostEqual(s.reserved, 0.0, places=2)

    def test_reserve_rejected_when_over_budget(self):
        """返回 None 而不是抛异常：额度不够是业务结果，不是错误。"""
        s = _svc(budget=10_000.0)
        self.assertTrue(s.reserve("t1", "000001", 6_000.0))
        self.assertIsNone(s.reserve("t2", "600000", 6_000.0))
        self.assertEqual(s.stats["rejects"], 1)
        self.assertAlmostEqual(s.reserved, 6_000.0, places=2)   # 失败的不占位

    def test_release_is_idempotent(self):
        """重复释放不能把额度还多。

        同一笔订单的回报可能重复到达（成交回报 + 主动查询各来一次），
        这里漏了的话预留机制形同虚设。
        """
        s = _svc(budget=10_000.0)
        rid = s.reserve("t1", "000001", 4_000.0, order_id="o1")
        self.assertTrue(s.release(rid))
        self.assertFalse(s.release(rid))          # 第二次：没有东西可放
        self.assertEqual(s.stats["releases"], 1)
        self.assertEqual(s.stats["leaked"], 1)    # 这个计数不为 0 说明调用方有问题
        self.assertAlmostEqual(s.reserved, 0.0, places=2)

    def test_zero_amount_is_noop_not_failure(self):
        """零额度的预留：既不用占位，也不该算失败。"""
        s = _svc(budget=0.0)
        self.assertEqual(s.reserve("t1", "000001", 0.0), "")
        self.assertEqual(s.stats["rejects"], 0)
        self.assertAlmostEqual(s.reserved, 0.0, places=2)

    def test_ttl_sweeps_stale_reservation(self):
        """TTL 是"回报永远不来"时唯一的兜底。

        没有它，一笔丢失的回报会永久占住额度，症状是系统跑着跑着就
        再也不下单，而日志里没有任何错误。
        """
        s = _svc(budget=10_000.0, reserve_ttl=0.01)
        s.reserve("t1", "000001", 9_000.0, order_id="o_lost")
        time.sleep(0.05)
        # 读 reserved 本身就会清理过期项 —— 过期的预留不该再算"占用"。
        # 清理只在 reserve 里顺带做是不够的：见下一个用例。
        self.assertAlmostEqual(s.reserved, 0.0, places=2)
        self.assertEqual(s.stats["expiries"], 1)
        self.assertAlmostEqual(s.available, 10_000.0, places=2)

    def test_last_reservation_expiry_restores_idle(self):
        """**最后一笔**预留过期后若再没有新买单，它不能永久留在字典里。

        清理只在 ``reserve`` 里做的话，最后一笔过期项没人再触发清理：
        ``available`` 被永久低估、``is_busy()`` 永远为真，
        引擎再也不判定空闲 —— 而日志里没有任何错误。
        这个用例钉住"读路径也要清理"。
        """
        s = _svc(budget=10_000.0, reserve_ttl=0.01)
        s.reserve("t1", "000001", 9_000.0, order_id="o_lost")
        self.assertTrue(s.is_busy())
        time.sleep(0.05)
        # 期间没有任何新的 reserve 调用
        self.assertFalse(s.is_busy())
        self.assertEqual(s.stats["expiries"], 1)
        self.assertAlmostEqual(s.available, 10_000.0, places=2)

    def test_release_by_task_and_release_all(self):
        s = _svc(budget=100_000.0)
        s.reserve("t1", "000001", 1_000.0)
        s.reserve("t1", "000002", 2_000.0)
        s.reserve("t2", "600000", 3_000.0)
        self.assertEqual(s.release_task("t1"), 2)
        self.assertAlmostEqual(s.reserved, 3_000.0, places=2)
        self.assertEqual(s.release_all(), 1)
        self.assertAlmostEqual(s.reserved, 0.0, places=2)

    def test_bind_order_late(self):
        """订单号要等 create_order 之后才有，所以绑定是后置的。"""
        s = _svc(budget=10_000.0)
        rid = s.reserve("t1", "000001", 1_000.0)
        self.assertFalse(s.release_order("o1"))          # 还没绑
        s.bind_order(rid, "o1")
        self.assertTrue(s.release_order("o1"))           # 绑上后能按订单释放

    def test_no_budget_falls_back_to_account_cash(self):
        """不配 budget 时自动用聚合账户的可用现金 —— 底线一行配置都不写也成立。"""
        s = _svc([_pf("a", "000001", 50_000.0)])
        self.assertIsNone(s.budget)
        self.assertAlmostEqual(s._effective_budget(), 50_000.0, places=2)
        self.assertTrue(s.reserve("t1", "000001", 40_000.0))
        self.assertIsNone(s.reserve("t2", "600000", 40_000.0))

    def test_invisible_account_means_unlimited(self):
        """聚合不到任何任务时不限制 —— 看不见账户的时候不该瞎拦。"""
        s = _svc([])
        self.assertIsNone(s._effective_budget())
        self.assertTrue(s.reserve("t1", "000001", 1e9))


# ===========================================================
# 预留串进下单链路
# ===========================================================
def _task(task_id: str = "t1", symbol: str = "000001",
          cash: float = 1_000_000.0) -> QuantTask:
    """造一个能跑 _process_signal 的最小任务。

    用 ``__new__`` 跳过真实的 __init__：那条路要装策略、建账本、连网关，
    跟本文件要验的"额度预留"没有关系，只会把用例拖慢、也更容易被无关
    改动打断。
    """
    t = QuantTask.__new__(QuantTask)
    t.task_id = task_id
    t.symbol = symbol
    t.quota = None
    t.global_gate = None
    t._reservations = {}
    t.portfolio = Portfolio(task_id=task_id, symbol=symbol, initial_cash=cash)
    # 真实的 percent sizer 会按账户全额算出上百万元的委托，那不是本文件
    # 要验的东西，只会让每个用例都得先去凑一个巨大的 budget。
    # 固定成 100 股 @10 元 ≈ 1000 元，留出数量级空间给额度断言。
    t.portfolio.plan_size = lambda **kw: 100
    t.strategy = mock.MagicMock()
    t.broker = mock.MagicMock()
    t._order_sink = None
    t._booked_orders = {}
    t.orders = []
    t._bars_since_trade = 0
    t._hold_bars = 0
    t._counted_fills = {}
    t.errors = []

    class _State:
        last_price = 10.0
    t.state = _State()

    t.risk_chain = mock.MagicMock()
    t.risk_chain.check.return_value = mock.MagicMock(allowed=True, scale=1.0)
    return t


def _signal(side: Side = Side.BUY) -> Signal:
    return Signal(symbol="000001", side=side, source=SignalSource.BAR,
                  reason="test", strength=1.0)


def _open_order(t: QuantTask, status: OrderStatus, size: int = 100) -> Order:
    o = Order(order_id="o1", task_id=t.task_id, symbol=t.symbol,
              side=Side.BUY, size=size, status=status)
    return o


class TestQuotaInOrderPath(unittest.TestCase):
    """额度预留必须真的拦在下单之前。"""

    def _run(self, t: QuantTask, status: OrderStatus, side: Side = Side.BUY):
        t.broker.create_order.return_value = _open_order(t, status)
        t.broker.submit.side_effect = lambda o: o
        with mock.patch.object(t, "_has_open_order", return_value=False), \
             mock.patch.object(t, "_book_order"), \
             mock.patch.object(t, "_emit_order"), \
             mock.patch.object(t, "_sync_state"), \
             mock.patch.object(t, "_on_filled_sync"):
            return t._process_signal(_signal(side), None, None)

    def test_buy_reserves_then_releases_on_fill(self):
        """成交即终态：钱已经变成持仓记进账本，不再是"在途占用"。"""
        pf = _svc(budget=100_000.0)
        t = _task()
        t.quota = pf
        self._run(t, OrderStatus.FILLED)
        self.assertEqual(pf.stats["reserves"], 1)
        self.assertEqual(pf.stats["releases"], 1)
        self.assertAlmostEqual(pf.reserved, 0.0, places=2)

    def test_submitted_stays_reserved(self):
        """SUBMITTED 是**在途**：这时还额度等于没有预留。"""
        pf = _svc(budget=100_000.0)
        t = _task()
        t.quota = pf
        self._run(t, OrderStatus.SUBMITTED)
        self.assertEqual(pf.stats["releases"], 0)
        self.assertGreater(pf.reserved, 0.0)

    def test_partial_stays_reserved(self):
        """部分成交：还有一部分挂在市场上，那部分资金仍在途。"""
        pf = _svc(budget=100_000.0)
        t = _task()
        t.quota = pf
        self._run(t, OrderStatus.PARTIAL)
        self.assertEqual(pf.stats["releases"], 0)
        self.assertGreater(pf.reserved, 0.0)

    def test_rejected_releases_immediately(self):
        pf = _svc(budget=100_000.0)
        t = _task()
        t.quota = pf
        self._run(t, OrderStatus.REJECTED)
        self.assertEqual(pf.stats["releases"], 1)
        self.assertAlmostEqual(pf.reserved, 0.0, places=2)

    def test_sell_does_not_reserve(self):
        """卖出不占额度：持仓是 per-task 独立账本，不存在跨任务卖超。"""
        pf = _svc(budget=100_000.0)
        t = _task()
        t.quota = pf
        t.portfolio.position.size = 1000          # 有仓才能卖
        self._run(t, OrderStatus.FILLED, side=Side.SELL)
        self.assertEqual(pf.stats["reserves"], 0)
        self.assertAlmostEqual(pf.reserved, 0.0, places=2)

    def test_quota_exhausted_rejects_signal(self):
        """额度不够 → 走标准否决流程并留痕，而不是悄悄不下单。"""
        pf = _svc(budget=1_000.0)
        t = _task()
        t.quota = pf
        t.portfolio.plan_size = lambda **kw: 100   # 100 股 × 10 元 = 1000，超预算
        rejected = []
        with mock.patch.object(t, "_reject",
                               side_effect=lambda s, rule, reason: rejected.append((rule, reason))), \
             mock.patch.object(t, "_has_open_order", return_value=False):
            t._process_signal(_signal(), None, None)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0][0], "quota_exhausted")
        self.assertEqual(pf.stats["reserves"], 0)

    def test_second_task_blocked_by_first_in_flight(self):
        """本轮要解决的核心场景：两个任务共享一个账户，在途单要互相看见。

        任务 A 报了一单还在市场里挂着，任务 B 再想下一单同样大小的，
        必须被挡住 —— 否则账户一共 10 万却发出了两笔 6 万。
        """
        pf = _svc(budget=100_000.0)
        a, b = _task("a"), _task("b")
        a.quota = pf
        b.quota = pf

        # A：报单成功但还在途（SUBMITTED），额度继续占着
        self._run(a, OrderStatus.SUBMITTED)
        in_flight = pf.reserved
        self.assertGreater(in_flight, 0.0)

        # B：想把剩下的额度全吃掉还不够 —— 必须被拒，且一笔都不许占上
        b.portfolio.plan_size = lambda **kw: int(pf.available / 10) + 1000
        rejected = []
        with mock.patch.object(b, "_reject",
                               side_effect=lambda s, rule, reason: rejected.append(rule)):
            self._run(b, OrderStatus.SUBMITTED)
        self.assertEqual(rejected, ["quota_exhausted"])
        self.assertAlmostEqual(pf.reserved, in_flight, places=2)

    def test_broker_update_releases(self):
        """券商回报到终态时释放（成交回报只带订单号，不带预留号）。"""
        pf = _svc(budget=100_000.0)
        t = _task()
        t.quota = pf
        rid = pf.reserve("t1", "000001", 5_000.0, order_id="o1")
        t._reservations["o1"] = rid
        with mock.patch.object(t, "_book_order"), \
             mock.patch.object(t, "_emit_order"), \
             mock.patch.object(t, "_sync_state"), \
             mock.patch.object(t, "_count_fill"):
            t._on_broker_update(_open_order(t, OrderStatus.FILLED))
        self.assertEqual(pf.stats["releases"], 1)
        self.assertAlmostEqual(pf.reserved, 0.0, places=2)

    def test_no_quota_means_no_change(self):
        """没挂组合服务时（单任务回测的常态）一切照旧。"""
        t = _task()
        t.quota = None
        order = self._run(t, OrderStatus.FILLED)
        self.assertIsNotNone(order)


class TestHealthCheckContract(unittest.TestCase):
    """``health_check`` 的契约是 ``(bool, str)`` —— **不是 dict**。

    踩过的坑：``PortfolioService`` 曾返回 dict，而它内部又调
    ``super().health_check()``（基类返回元组），于是 ``h["budget"] = ...``
    抛 ``TypeError: 'tuple' object does not support item assignment``。
    服务注册表把这个异常吞成一行"health_check 异常"，面板上看不到
    任何额度信息，而日志里也没有 ERROR —— 极难定位。
    """

    def test_returns_two_tuple(self):
        s = _svc([_pf("a", "000001", 50_000.0)])
        res = s.health_check()
        self.assertIsInstance(res, tuple, "必须是元组，不是 dict")
        self.assertEqual(len(res), 2)
        self.assertIsInstance(res[0], bool)
        self.assertIsInstance(res[1], str)

    def test_detail_carries_quota_info(self):
        """说明文本要能看出额度占用，否则这条健康检查没有诊断价值。"""
        s = _svc([_pf("a", "000001", 50_000.0)], budget=10_000.0)
        s.reserve("t1", "000001", 3_000.0)
        _, detail = s.health_check()
        # 不断言 ok：服务没启动，基类按状态判定为不健康，这是对的
        self.assertIn("预算", detail)
        self.assertIn("3000", detail)

    def test_super_result_is_tuple(self):
        """钉住基类返回值类型 —— 这是当初踩坑的直接原因。"""
        from app.modules.portfolio.service import PortfolioService

        base = super(PortfolioService, _svc([_pf("a", "000001", 1_000.0)])
                     ).health_check()
        self.assertIsInstance(base, tuple)


if __name__ == "__main__":
    unittest.main()
