#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：真实引擎跑真实数据，多任务 + 新闻事件触发交易。"""
from datetime import datetime

from twisted.internet import reactor

from app.core.engine.builder import build_engine
from app.core.market.types import NewsItem
from app.utils.logger import logger


def main():
    engine = build_engine(mode="BACKTEST")
    sm = engine.get_component("strategy_manager")
    news_c = engine.get_component("news_center")
    market = engine.get_component("market_center")

    print(f"\n装载任务数: {len(sm)}")
    print(f"行情中心: {market.snapshot()}")

    def bullish():
        return NewsItem(
            title="平安银行(000001)业绩预增，净利润增长80%，远超预期",
            content="公司发布业绩预告，净利润同比增长 80%，大幅超出市场预期。",
            source="smoke",
            published_at=datetime.now(),
            symbols=["000001"],
        )

    def bearish():
        return NewsItem(
            title="平安银行(000001)被立案调查，或面临重大处罚",
            content="监管层对其涉嫌违规事项立案调查，存在退市风险。",
            source="smoke",
            published_at=datetime.now(),
            symbols=["000001"],
        )

    # 按K线计数注入新闻，避免依赖墙上时钟（reactor 负载会让 callLater 漂移）
    counter = {"n": 0}
    injected = set()

    def on_bar(bar=None, **kw):
        counter["n"] += 1
        n = counter["n"]
        if n == 120 and "bullish" not in injected:
            injected.add("bullish")
            news_c.inject(bullish())
        elif n == 240 and "bearish" not in injected:
            injected.add("bearish")
            news_c.inject(bearish())

    engine.get_event_bus().subscribe("bar_received", on_bar)

    ctx = engine.start()

    print("\n=== 引擎结束 ===")
    print(f"run_id={ctx.run_id} mode={ctx.run_mode} status={ctx.engine_status}")
    print(f"\n=== 任务汇总 ===")
    print(sm.summary())
    print(f"\n=== 新闻中心 ===")
    snap = news_c.snapshot()
    print("  分析器:", snap["analyzers"], "| stats:", snap["stats"])
    print(f"\n=== 行情中心 ===")
    print(" ", market.snapshot())

    bad = [t.task_id for t in sm.runtime if t.portfolio.position.size < 0]
    print("\n净做空任务:", bad or "无 ✓")
    errs = {t.task_id: len(t.errors) for t in sm.runtime if t.errors}
    print("任务异常:", errs or "无 ✓")


if __name__ == "__main__":
    main()
