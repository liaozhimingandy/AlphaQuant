#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_task_runtime.py
# @Description : 任务实例与多任务运行时的单元测试
#               核心不变量：一个引擎跑 N 个任务、状态隔离、绝不出现净做空
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from app.core.market.types import Bar, NewsAnalysis, NewsItem, Side
from app.core.task import TaskRuntime, build_task, parse_task_specs


def gen_bars(symbol: str, n: int = 150, start: float = 10.0, amp: float = 2.0) -> list[Bar]:
    base = datetime(2024, 1, 1)
    out = []
    for i in range(n):
        c = start + 0.02 * i + amp * math.sin(i / 7.0)
        out.append(Bar(symbol, base + timedelta(days=i), c - 0.1, c + 0.3, c - 0.3, c, 1000 + i, 0))
    return out


MA_SPEC = {
    "task_id": "t-ma",
    "symbol": "000001",
    "warmup_bars": 30,
    "sizer": {"type": "percent", "pct": 0.3},
    "strategy": {"type": "ma_cross", "params": {"fast": 5, "slow": 20}},
    "risk": [{"type": "cooldown", "min_bars": 1}, {"type": "cash_reserve"}],
}

NEWS_SPEC = {
    "task_id": "t-news",
    "symbol": "600000",
    "warmup_bars": 30,
    "sizer": {"type": "percent", "pct": 0.3},
    "strategy": {
        "type": "combo",
        "entry": "never",
        "exit": "never",
        "event_entry": {"factor": "news_impact", "op": "gt", "value": 0.3},
        "event_exit": {"factor": "news_impact", "op": "lt", "value": -0.3},
        "params": {"use_news_confidence_as_strength": True},
    },
    "risk": [{"type": "news_confidence", "min_confidence": 0.5}],
}


class TestQuantTask(unittest.TestCase):
    def test_warmup_suppresses_trading(self):
        t = build_task(MA_SPEC)
        t.start()
        orders = [t.on_bar(b) for b in gen_bars("000001", 20)]
        self.assertTrue(all(o is None for o in orders))
        self.assertEqual(len(t.portfolio.trades), 0)

    def test_bar_channel_trades(self):
        t = build_task(MA_SPEC)
        t.start()
        for b in gen_bars("000001", 150):
            t.on_bar(b)
            t.advance()
        self.assertGreater(len(t.portfolio.trades), 0)

    def test_never_goes_net_short(self):
        """上一轮修复的僵尸止损单就是靠这条不变量兜住的。"""
        t = build_task(MA_SPEC)
        t.start()
        for b in gen_bars("000001", 200):
            t.on_bar(b)
            t.advance()
        self.assertGreaterEqual(t.portfolio.position.size, 0)
        self.assertGreaterEqual(t.state.position_size, 0)

    def test_news_triggers_buy_and_sell(self):
        t = build_task(NEWS_SPEC)
        t.start()
        for b in gen_bars("600000", 60):
            t.on_bar(b)
            t.advance()

        bullish = NewsItem(title="重大利好 超预期", content="", symbols=["600000"], source="t")
        order = t.on_news(
            bullish,
            NewsAnalysis("f1", ["600000"], score=0.9, confidence=0.9),
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.side, Side.BUY)
        self.assertGreater(t.portfolio.position.size, 0)

        bearish = NewsItem(title="突发利空 立案", content="", symbols=["600000"], source="t")
        order = t.on_news(
            bearish,
            NewsAnalysis("f2", ["600000"], score=-0.9, confidence=0.9),
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.side, Side.SELL)
        self.assertEqual(t.portfolio.position.size, 0)

    def test_low_confidence_news_blocked(self):
        t = build_task(NEWS_SPEC)
        t.start()
        for b in gen_bars("600000", 60):
            t.on_bar(b)
            t.advance()
        order = t.on_news(
            NewsItem(title="模糊消息", symbols=["600000"], source="t"),
            NewsAnalysis("f3", ["600000"], score=0.9, confidence=0.1),
        )
        self.assertIsNone(order)

    def test_paused_task_ignores_events(self):
        t = build_task(MA_SPEC)
        t.start()
        t.pause()
        for b in gen_bars("000001", 60):
            self.assertIsNone(t.on_bar(b))
        self.assertEqual(len(t.portfolio.trades), 0)

    def test_strategy_exception_isolated(self):
        """策略抛异常要把任务标记为异常，但不能让调用方崩掉。"""
        t = build_task(MA_SPEC)
        t.start()

        def boom(*a, **kw):
            raise RuntimeError("boom")

        t.strategy.on_bar = boom  # type: ignore[method-assign]
        for b in gen_bars("000001", 40):
            self.assertIsNone(t.on_bar(b))
        self.assertGreater(len(t.errors), 0)


class TestTaskRuntime(unittest.TestCase):
    def _runtime(self) -> TaskRuntime:
        rt = TaskRuntime()
        rt.add_from_config([MA_SPEC, NEWS_SPEC])
        rt.start_all()
        return rt

    def test_multiple_tasks_isolated(self):
        rt = self._runtime()
        self.assertEqual(len(rt), 2)
        a, b = rt.get("t-ma"), rt.get("t-news")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertIsNot(a.series, b.series)
        self.assertIsNot(a.portfolio, b.portfolio)

    def test_bar_dispatch_by_symbol(self):
        rt = self._runtime()
        bars1 = gen_bars("000001", 120)
        bars2 = gen_bars("600000", 120)
        for i in range(120):
            rt.on_bar(bars1[i])
            rt.on_bar(bars2[i])
        self.assertEqual(rt.get("t-ma").bar_count, 120)
        self.assertEqual(rt.get("t-news").bar_count, 120)

    def test_news_routed_to_matching_task_only(self):
        rt = self._runtime()
        for i in range(60):
            rt.on_bar(gen_bars("000001", 60)[i])
            rt.on_bar(gen_bars("600000", 60)[i])

        orders = rt.on_news(
            NewsItem(title="只影响 600000 的利好", symbols=["600000"], source="t"),
            NewsAnalysis("f", ["600000"], score=0.9, confidence=0.9),
        )
        self.assertTrue(all(o.task_id == "t-news" for o in orders))

    def test_unannotated_news_broadcasts(self):
        rt = self._runtime()
        for i in range(60):
            rt.on_bar(gen_bars("000001", 60)[i])
            rt.on_bar(gen_bars("600000", 60)[i])
        # 无标注标的的宏观新闻广播给所有任务
        rt.on_news(
            NewsItem(title="全面降准", symbols=[], source="t"),
            NewsAnalysis("f", [], score=0.9, confidence=0.9),
        )
        self.assertTrue(all(len(t.recent_news) >= 1 for t in rt))

    def test_duplicate_task_id_rejected(self):
        rt = TaskRuntime()
        rt.add_from_spec(MA_SPEC)
        with self.assertRaises(ValueError):
            rt.add_from_spec(MA_SPEC)

    def test_remove_stops_task(self):
        rt = self._runtime()
        self.assertTrue(rt.remove("t-ma"))
        self.assertIsNone(rt.get("t-ma"))
        self.assertFalse(rt.remove("t-ma"))

    def test_disabled_specs_skipped(self):
        specs = parse_task_specs([
            dict(MA_SPEC, task_id="a", enabled=True),
            dict(MA_SPEC, task_id="b", enabled=False),
        ])
        self.assertEqual([s.task_id for s in specs], ["a"])

    def test_bad_spec_rejected(self):
        with self.assertRaises(ValueError):
            parse_task_specs([{"strategy": "ma_cross"}])  # 缺 symbol
        with self.assertRaises(ValueError):
            parse_task_specs([{"symbol": "000001"}])  # 缺 strategy

    def test_summary_contains_all_tasks(self):
        rt = self._runtime()
        text = rt.summary()
        for tid in ("t-ma", "t-news"):
            self.assertIn(tid, text)


if __name__ == "__main__":
    unittest.main()
