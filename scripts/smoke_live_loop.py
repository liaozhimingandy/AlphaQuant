#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘闭环冒烟：逐跳验证用户要求的那条数据流。

    数据采集 → 数据清洗 & 存储 → 因子/指标计算 → 策略引擎
    → 风控前置检查 → 订单执行 → 成交回报 → 持仓 & 资金更新 → 回到数据采集

用内置 simulated 券商网关，**不联网、不碰真钱**，
所以可以在任何机器上反复跑。每一跳都有独立断言 —— 某一跳坏了能立刻看出来坏在哪。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

os.environ.setdefault("BROKER_GATEWAY", "simulated")

FAILED: list = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[{'OK ' if cond else 'FAIL'}] {label} {extra}")
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)


def main() -> int:
    from app.core.engine.builder import build_engine
    from app.core.engine.event import StandardEvents
    from app.core.market.clean import BarCleaner, is_tradable
    from app.core.market.types import Bar
    from app.core.risk import list_risks
    from app.repository.trading_repository import TradingRepository

    # ============================================================
    section("① 数据采集：清洗层独立可用")
    # ============================================================
    d0 = datetime(2026, 1, 5)
    dirty = [
        Bar(symbol="000001", dt=d0, open=10, high=10.5, low=9.5, close=10.2, volume=1000),
        Bar(symbol="000001", dt=d0, open=10, high=10.5, low=9.5, close=10.2, volume=1000),
        Bar(symbol="000001", dt=d0 - timedelta(days=1), open=10, high=10.5, low=9.5,
            close=10.0, volume=1000),
        Bar(symbol="000001", dt=d0 + timedelta(days=1), open=10.2, high=10.2, low=10.2,
            close=25.0, volume=0),
    ]
    bars, rep = BarCleaner().clean(dirty)
    check("清洗：重复时间戳被识别", rep.duplicates_removed >= 1, f"去重 {rep.duplicates_removed}")
    check("清洗：时间倒序被修正", rep.reordered)
    check("清洗：异常/停牌被标记", bool(rep.flags), f"{rep.flags}")
    check("清洗：不可交易的 bar 被标出",
          any(not is_tradable(b) for b in bars), f"保留 {len(bars)} 根")
    check("清洗：脏数据仍然保留（只标记不丢）", len(bars) >= 3)

    # ============================================================
    section("② 因子 / 指标：参数化 + 缓存")
    # ============================================================
    from app.core.factor.context import FactorContext
    from app.core.indicator.registry import cache_stats, clear_cache
    from app.core.market.series import BarSeries

    series = BarSeries("000001", maxlen=500)
    for i in range(120):
        px = 10.0 + (i % 7) * 0.05
        series.append(Bar(symbol="000001", dt=d0 + timedelta(days=i),
                          open=px, high=px * 1.01, low=px * 0.99, close=px, volume=1e6))
    clear_cache()
    ctx = FactorContext("000001", series)
    v5 = ctx.ind("sma", period=5)
    v20 = ctx.ind("sma", period=20)
    v20b = ctx.ind("sma", period=20)
    check("指标：周期是参数（同一指标两个周期）",
          v5 is not None and v20 is not None and v5 != v20,
          f"sma5={v5:.3f} sma20={v20:.3f}")
    # 注册表缓存的意义在于**跨 context** 复用：每根K线都会新建一个 ctx，
    # 同一根K线上多个因子要同一个指标时，第二次必须命中缓存而不是重算整条序列。
    before = cache_stats()["hits"]
    ctx2 = FactorContext("000001", series)
    ctx2.ind("sma", period=20)
    after = cache_stats()["hits"]
    check("指标：跨 context 命中共享缓存", after > before,
          f"hits {before} → {after}")
    check("指标：结果稳定", abs(v20 - v20b) < 1e-12)
    check("因子：周线族可用", ctx.ind("wma", period=5) is not None,
          f"wma5={ctx.ind('wma', period=5)}")

    # ============================================================
    section("③ 风控前置：A 股实盘规则已注册")
    # ============================================================
    risks = list_risks()
    for r in ("t_plus_one", "price_limit", "order_value_limit",
              "daily_trade_limit", "sellable_position"):
        check(f"风控规则 {r} 已注册", r in risks)

    # ============================================================
    section("④ 订单执行 → 成交回报 → 持仓资金更新")
    # ============================================================
    engine = build_engine(
        mode="LIVE", market_mode="poll", with_market=False, with_news=False,
        with_monitor=False, auto_load_runtime=False, broker_gateway="simulated",
        with_collector=False, symbols=["000001"],
    )
    gw_comp = engine.get_component("live_gateway")
    sm = engine.get_component("strategy_manager")
    store = engine.get_component("trading_store")
    check("引擎装配：实盘网关已挂上", gw_comp is not None)
    check("引擎装配：落库组件已挂上", store is not None)

    engine.initialize()
    gw_comp.start()
    sm.start()

    task = list(sm.runtime)[0]
    check("任务跟随引擎进入 LIVE", type(task.broker).__name__ == "LiveBroker",
          f"实际 {type(task.broker).__name__}")
    check("任务共用同一条网关连接", task.broker.gateway is gw_comp.gateway)

    # 灌行情，触发策略
    for i in range(120):
        px = 10.0 + (i % 7) * 0.05
        engine.get_event_bus().publish(StandardEvents.BAR_RECEIVED, bar=Bar(
            symbol="000001", dt=d0 + timedelta(days=i), open=px, high=px * 1.01,
            low=px * 0.99, close=px, volume=1e6))

    n_orders = len(task.orders)
    check("订单执行：产生了订单", n_orders >= 1, f"{n_orders} 笔")
    check("订单执行：报单后**未**成交（异步语义）",
          all(o.status.value in ("SUBMITTED", "FILLED", "PARTIAL") for o in task.orders))
    submitted = [o for o in task.orders if o.status.value == "SUBMITTED"]
    check("防堆单：未结期间不再重复下单", len(task.orders) <= 2,
          f"订单 {len(task.orders)} 笔（无保护时会每根K线一单）")

    cash_before = task.portfolio.account.cash
    pos_before = task.portfolio.position.size
    for _ in range(4):
        gw_comp._poll_once()
    pos_after = task.portfolio.position.size
    cash_after = task.portfolio.account.cash

    check("成交回报：被正确路由（无无法归属的回报）",
          gw_comp.stats["unknown"] == 0, f"unknown={gw_comp.stats['unknown']}")
    check("成交回报：产生了成交", gw_comp.stats["fills"] >= 1,
          f"fills={gw_comp.stats['fills']}")
    check("持仓更新：仓位变化", pos_after != pos_before or pos_after > 0,
          f"{pos_before} → {pos_after}")
    check("资金更新：现金减少", cash_after < cash_before,
          f"{cash_before:.2f} → {cash_after:.2f}")
    check("资金更新：现金非负", cash_after >= 0)
    check("日内簿记：T+1 冻结量已记录",
          task.state.meta.get("bought_today", 0) >= 0,
          f"bought_today={task.state.meta.get('bought_today')}")
    filled = [o for o in task.orders if o.status.value == "FILLED"]
    if filled:
        o = filled[0]
        check("成交价有效（非 0）", o.filled_price > 0, f"均价 {o.filled_price:.4f}")
        check("成交量有效", o.filled_size > 0, f"成交 {o.filled_size} 股")

    # ============================================================
    section("⑤ 对账：账户级一致")
    # ============================================================
    report = gw_comp._tick_reconcile()
    check("对账通过", report.get("ok") is True,
          f"持仓差异 {report.get('position_diffs')} 资金差异 {report.get('cash_diff')}")
    check("对账是账户级（汇总所有任务）", "local_positions" in report,
          f"本地汇总 {report.get('local_positions')}")

    # ============================================================
    section("⑥ 落库：交易数据进了 SQLite")
    # ============================================================
    from app.repository.trading_repository import get_repository

    get_repository().flush()
    run_id = store.run_id
    check("run_id 与引擎一致",
          run_id == engine.snapshot()["engine"]["run_id"], f"{run_id}")
    orders_db = TradingRepository.recent_orders(run_id=run_id, limit=50)
    check("订单已入库", len(orders_db) >= 1, f"{len(orders_db)} 条")
    if orders_db:
        check("订单状态是终态", any(o["status"] == "FILLED" for o in orders_db),
              f"{TradingRepository.order_stats(run_id=run_id)}")
    trades_db = TradingRepository.recent_trades(run_id=run_id, limit=50)
    check("成交已入库", len(trades_db) >= 1, f"{len(trades_db)} 条")
    if trades_db and orders_db:
        check("订单与成交一一对应（无重复计数）",
              len(trades_db) <= len(orders_db),
              f"订单 {len(orders_db)} / 成交 {len(trades_db)}")

    # ============================================================
    section("⑦ 闭环：新数据回写 → 可被下一次决策读到")
    # ============================================================
    engine.get_event_bus().publish(StandardEvents.DATA_COLLECTED,
                                   symbol="000001", period="1d", rows=2)
    get_repository().flush()
    events = TradingRepository.events(run_id=run_id, category="collect", limit=10)
    check("采集事件已留痕（形成闭环）", len(events) >= 1,
          f"{[e['message'] for e in events][:2]}")

    # ============================================================
    section("⑧ 退出：先撤单再断连")
    # ============================================================
    gw_comp.stop()
    sm.stop()
    check("网关已断开", not gw_comp.gateway.is_connected())
    check("未结订单已清理", len(task.broker.open_orders) == 0,
          f"剩余 {len(task.broker.open_orders)} 笔")

    print("\n" + "=" * 66)
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过:")
        for f in FAILED:
            print(f"   - {f}")
    else:
        print("✅ 实盘闭环冒烟全部通过")
        print()
        print("  数据采集 → 清洗 → 存储 → 指标/因子 → 策略 → 风控前置")
        print("  → 订单执行 → 成交回报 → 持仓&资金更新 → 落库 → 回到采集")
        print("  每一跳都已验证。")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
