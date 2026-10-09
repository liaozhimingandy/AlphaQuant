#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : task.py
# @Description : 量化任务实例：一个「标的 + 策略 + 风控 + 账本」的独立运行单元
#               多个 Task 互不共享状态，由 TaskRuntime 统一驱动
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.execution.broker import SimulatedBroker
from app.core.factor.context import FactorContext
from app.core.market.series import BarSeries
from app.core.market.types import (
    Account,
    Bar,
    NewsAnalysis,
    NewsItem,
    Order,
    OrderStatus,
    RunMode,
    Side,
    Signal,
    SignalSource,
    TaskState,
)
from app.core.portfolio.manager import Portfolio
from app.core.portfolio.sizer import build_sizer
from app.core.risk.registry import RiskChain
from app.core.strategy.base import IBaseStrategy
from app.core.strategy.state import DecisionState
from app.utils.logger import logger


class QuantTask:
    """一个独立的量化交易任务。

    双通道输入，同一条处理链路::

        on_bar(bar)      ──┐
                           ├─> 策略产出 Signal ─> 风控闸门 ─> 仓位计算 ─> 撮合 ─> 记账
        on_event(news)   ──┘

    关键设计：
      1. **状态私有**：每个任务有自己的 BarSeries / Portfolio / 策略实例，
         一个任务的异常不会污染其它任务
      2. **行情不足时静默跳过**：均线类策略在预热期必然算不出信号，
         这是正常的，不该刷日志
      3. **信号必须过风控**：任何跳过风控的路径都是 bug，所以这里只有一条出口
    """

    def __init__(
        self,
        task_id: str,
        symbol: str,
        strategy: IBaseStrategy,
        risk_chain: Optional[RiskChain] = None,
        initial_cash: float = 100_000.0,
        commission: float = 0.0003,
        slippage: float = 0.0005,
        sizer: Any = None,
        warmup_bars: int = 60,
        max_hold_bars: int = 0,
        run_mode: RunMode = RunMode.SIMULATE,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.task_id = task_id
        self.symbol = symbol
        self.strategy = strategy
        self.risk_chain = risk_chain or RiskChain()
        self.run_mode = run_mode
        self.warmup_bars = int(warmup_bars)
        self.max_hold_bars = int(max_hold_bars)
        self.meta: Dict[str, Any] = meta or {}

        self.series = BarSeries(symbol)
        self.news_features: Dict[str, Any] = {}
        self.recent_news: List[NewsAnalysis] = []

        self.portfolio = Portfolio(
            task_id=task_id,
            symbol=symbol,
            initial_cash=float(initial_cash),
            fee_rate=float(commission),
            sizer=build_sizer(sizer),
        )
        self.broker = SimulatedBroker(
            self.portfolio, slippage=float(slippage), fee_rate=float(commission)
        )

        self.state = DecisionState(
            task_id=task_id,
            symbol=symbol,
            cash=self.portfolio.account.cash,
            equity=self.portfolio.equity,
            peak_equity=self.portfolio.peak_equity,
        )

        self.status: TaskState = TaskState.CREATED
        self.bar_count: int = 0
        self._bars_since_trade: int = 0
        self._hold_bars: int = 0
        self.orders: List[Order] = []
        self.errors: List[str] = []

        strategy.on_init(task_id=task_id, symbol=symbol)

    # ============================================================
    # 生命周期
    # ============================================================
    def start(self) -> None:
        self.status = TaskState.RUNNING
        self.strategy.on_start()
        logger.info(f"[{self.task_id}] 任务启动 | {self.symbol} | 策略: {self.strategy.name}")

    def stop(self) -> None:
        self.status = TaskState.STOPPED
        self.strategy.on_stop()
        logger.info(f"[{self.task_id}] 任务停止 | 终值: {self.portfolio.equity:.2f}")

    def pause(self) -> None:
        if self.status == TaskState.RUNNING:
            self.status = TaskState.PAUSED

    def resume(self) -> None:
        if self.status == TaskState.PAUSED:
            self.status = TaskState.RUNNING

    # ============================================================
    # 通道一：行情
    # ============================================================
    def on_bar(self, bar: Bar) -> Optional[Order]:
        """收到一根K线。返回成交的订单（未触发则 None）。"""
        if self.status != TaskState.RUNNING:
            return None

        self.series.append(bar)
        self.bar_count += 1
        self.broker.set_price(bar.close)
        self._sync_state(bar.dt)

        # 预热期：数据不足时不决策，但也不报错
        if len(self.series) < self.warmup_bars:
            self.portfolio.mark(bar.dt)
            return None

        # 持股超时强制离场（不依赖策略自觉）
        forced = self._check_max_hold(bar)
        if forced is not None:
            return forced

        ctx = self._build_ctx(bar.dt)
        try:
            signal = self.strategy.on_bar(ctx, self.state.copy())
        except Exception as exc:
            msg = f"[{self.task_id}] 策略 on_bar 异常: {exc}"
            logger.error(msg, exc_info=True)
            self.errors.append(msg)
            return None

        return self._process_signal(signal, ctx, bar.dt) if signal else None

    # ============================================================
    # 通道二：事件（新闻 / 大模型）
    # ============================================================
    def on_news(self, news: NewsItem, analysis: Optional[NewsAnalysis] = None) -> Optional[Order]:
        """收到一条新闻。若带分析结果则直接用于决策，否则置中性特征。"""
        if self.status != TaskState.RUNNING:
            return None

        self.news_features["news_sentiment"] = float(analysis.score) if analysis else 0.0
        self.news_features["news_confidence"] = float(analysis.confidence) if analysis else 0.0
        self.news_features["news_impact"] = (
            float(analysis.score) * float(analysis.confidence) if analysis else 0.0
        )
        self.news_features["news_count"] = float(len(self.recent_news) + 1)
        if analysis:
            self.recent_news.append(analysis)
            # 只保留最近的，避免长跑内存膨胀
            self.recent_news = self.recent_news[-50:]

        # 行情不足时新闻也无法决策（需要价格算仓位）
        last = self.series.last
        if last is None or len(self.series) < self.warmup_bars:
            logger.debug(f"[{self.task_id}] 行情不足，新闻事件暂不触发交易")
            return None

        ctx = self._build_ctx(last.dt)
        try:
            signal = self.strategy.on_event(ctx, self.state.copy(), event=news)
        except Exception as exc:
            msg = f"[{self.task_id}] 策略 on_event 异常: {exc}"
            logger.error(msg, exc_info=True)
            self.errors.append(msg)
            return None

        return self._process_signal(signal, ctx, last.dt) if signal else None

    # ============================================================
    # 统一出口：Signal -> 风控 -> 仓位 -> 撮合
    # ============================================================
    def _process_signal(
        self, signal: Signal, ctx: FactorContext, dt: Optional[datetime]
    ) -> Optional[Order]:
        signal.task_id = self.task_id
        signal.symbol = self.symbol
        signal.ts = dt

        # 1) 风控闸门
        verdict = self.risk_chain.check(
            signal, self.state, ctx=ctx, account=self.portfolio.account
        )
        if not verdict.allowed:
            logger.info(f"[{self.task_id}] 信号被风控否决 | {verdict.rule}: {verdict.reason}")
            order = self.broker.create_order(
                self.symbol, signal.side, 0, self.task_id, signal.reason, signal.source.value
            )
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"{verdict.rule}: {verdict.reason}"
            self.orders.append(order)
            return None

        # 2) 仓位计算
        price = self.state.last_price
        if price <= 0:
            return None
        size = self.portfolio.plan_size(
            side=signal.side,
            price=price,
            strength=signal.strength,
            scale=verdict.scale,
        )
        if size <= 0:
            logger.debug(f"[{self.task_id}] 计算出的下单数量为 0，跳过")
            return None

        # 3) 撮合
        order = self.broker.create_order(
            self.symbol,
            signal.side,
            size,
            self.task_id,
            signal.reason,
            signal.source.value,
        )
        order = self.broker.submit(order)
        self.orders.append(order)
        self.strategy.on_order(order)

        if order.status == OrderStatus.FILLED:
            self._bars_since_trade = 0
            if signal.side == Side.BUY:
                self._hold_bars = 0
            elif self.portfolio.position.size == 0:
                self._hold_bars = 0
        self._sync_state(dt)
        return order

    # ============================================================
    # 内部：状态同步与强制离场
    # ============================================================
    def _sync_state(self, dt: Optional[datetime]) -> None:
        p = self.portfolio
        self.state.last_price = p.position.last_price
        self.state.cash = p.account.cash
        self.state.equity = p.equity
        self.state.peak_equity = max(p.peak_equity, p.equity)
        self.state.has_position = p.position.size > 0
        self.state.position_size = p.position.size
        self.state.avg_price = p.position.avg_price
        self.state.holding_bars = self._hold_bars
        self.state.bars_since_last_trade = self._bars_since_trade

    def _build_ctx(self, dt: Optional[datetime]) -> FactorContext:
        return FactorContext(
            symbol=self.symbol,
            series=self.series,
            params=dict(self.strategy.params),
            features=dict(self.news_features),
            now=dt,
        )

    def _check_max_hold(self, bar: Bar) -> Optional[Order]:
        """持股超过上限强制卖出。风控只能否决，不能下单，所以强制平仓放在这里。"""
        if self.max_hold_bars <= 0:
            return None
        if not self.state.has_position:
            return None
        if self._hold_bars < self.max_hold_bars:
            return None

        signal = Signal(
            symbol=self.symbol,
            side=Side.SELL,
            source=SignalSource.TIMER,
            reason=f"持股 {self._hold_bars} 根K线，触发强制离场",
            strength=1.0,
            task_id=self.task_id,
            ts=bar.dt,
        )
        # 强制离场**绕过风控**（否则冷却期之类的规则会把平仓卡死）
        size = self.portfolio.position.size
        if size <= 0:
            return None
        order = self.broker.create_order(
            self.symbol, Side.SELL, size, self.task_id, signal.reason, "force_exit"
        )
        order = self.broker.submit(order)
        self.orders.append(order)
        logger.warning(f"[{self.task_id}] 强制离场 | {signal.reason}")
        self._bars_since_trade = 0
        self._hold_bars = 0
        return order

    def advance(self) -> None:
        """推进内部计数器：每根K线处理完后调用一次。"""
        self._bars_since_trade += 1
        if self.portfolio.position.size > 0:
            self._hold_bars += 1

    # ============================================================
    # 输出
    # ============================================================
    def snapshot(self) -> Dict[str, Any]:
        snap = self.portfolio.snapshot()
        snap.update(
            {
                "task_id": self.task_id,
                "status": self.status.value,
                "strategy": self.strategy.name,
                "bar_count": self.bar_count,
                "hold_bars": self._hold_bars,
                "bars_since_trade": self._bars_since_trade,
                "risk_rules": [r.name for r in self.risk_chain.rules],
                "error_count": len(self.errors),
                "news_count": len(self.recent_news),
            }
        )
        return snap

    def __repr__(self) -> str:  # pragma: no cover
        return f"<QuantTask {self.task_id} {self.symbol} {self.strategy.name} {self.status.value}>"
