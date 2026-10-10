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
      4. **撮合可换但不影响本类**：模拟撮合 submit 即成交；实盘撮合 submit
         只是报单，成交要靠 ``on_broker_update`` 回调。
         两个路径的差别被 ``_after_order`` / ``_on_broker_update`` 吸收掉了。
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
        broker: Any = None,
        gateway: Any = None,
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
        # 撮合实现由外部注入（回测/模拟给 SimulatedBroker，实盘给 LiveBroker）。
        # 没注入时按运行模式自建，保证单独 new 一个 QuantTask 也能跑。
        #
        # **实盘必须共用同一条网关连接**：券商通常不允许同一账号开多条长连接，
        # 每个任务各连一次会连接数爆炸、甚至被柜台踢下线。
        # 所以这里优先用外部注入的 gateway，只在单机调试时才自己建。
        if broker is not None:
            self.broker = broker
        elif run_mode == RunMode.LIVE:
            from app.core.config import settings
            from app.core.execution.gateway import (
                create_gateway,
                has_gateway,
                list_gateways,
            )
            from app.core.execution.live_broker import LiveBroker

            gw = gateway
            if gw is None:
                if not settings.BROKER_GATEWAY or not has_gateway(settings.BROKER_GATEWAY):
                    # 实盘却没配网关：宁可起不来，也不要"悄悄用模拟撮合假装在实盘"
                    raise RuntimeError(
                        f"LIVE 模式必须配置 BROKER_GATEWAY（当前={settings.BROKER_GATEWAY!r}）"
                        f" | 可用网关: {list_gateways()}"
                    )
                gw = create_gateway(settings.BROKER_GATEWAY)
            self.broker = LiveBroker(
                self.portfolio,
                gw,
                fee_rate=float(commission),
                max_order_value=settings.LIVE_MAX_ORDER_VALUE,
                price_limit_pct=settings.LIVE_PRICE_LIMIT_PCT,
                order_timeout=settings.LIVE_ORDER_TIMEOUT,
            )
        else:
            self.broker = SimulatedBroker(
                self.portfolio, slippage=float(slippage), fee_rate=float(commission)
            )

        # 实盘：成交回报到达时走这个回调（模拟撮合下它不会被触发）
        if hasattr(self.broker, "on_order_update"):
            self.broker.on_order_update = self._on_broker_update

        #: 订单出口。由 TaskRuntime 在 add() 时注入，让回报驱动的订单变化
        #: 也能进入事件总线（返回值机制只覆盖 on_bar/on_news 的调用栈内）。
        self._order_sink: Optional[Any] = None
        #: 已入账的订单（实盘同一订单会有多次状态变化，不能重复 append）
        self._booked_orders: Dict[str, Order] = {}
        #: 每个订单已计入日内统计的累计成交量（避免部分成交时重复计数）
        self._counted_fills: Dict[str, int] = {}

        # ---- 日内簿记：给实盘风控规则（T+1 / 单日笔数 / 涨跌停）供数 ----
        self._trading_day: Optional[Any] = None
        self._bought_today: int = 0     # 当日买入量（T+1 冻结）
        self._trades_today: int = 0     # 当日成交笔数
        self._prev_close: float = 0.0   # 上一根K线收盘（涨跌停判定基准）
        self._is_same_day: bool = False

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
    # 订单出口
    # ============================================================
    def set_order_sink(self, sink: Any) -> None:
        self._order_sink = sink

    def _emit_order(self, order: Order) -> None:
        """把订单变化推出去（事件总线 / 落库）。失败不能影响交易。"""
        if self._order_sink is None:
            return
        try:
            self._order_sink(order)
        except Exception as exc:
            logger.debug(f"[{self.task_id}] 订单事件发布失败（忽略）: {exc}")

    def _on_broker_update(self, order: Order) -> None:
        """券商回报驱动的订单状态变化。

        实盘里这是持仓/资金更新之后最重要的一件事：
        **计数器必须在这里重置，而不是在下单时**。
        下单≠持股，把"持股超时"从报单时刻开始算，会把还没成交的时间也算进去。
        """
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            if order.status == OrderStatus.FILLED:
                self._bars_since_trade = 0
                if order.side == Side.BUY or self.portfolio.position.size == 0:
                    self._hold_bars = 0
            self._count_fill(order)
            self._sync_state(order.created_at)
        self._book_order(order)
        self._emit_order(order)

    def _book_order(self, order: Order) -> None:
        """把订单登记进 task.orders，同一订单只保留一条（状态取最新）。"""
        existing = self._booked_orders.get(order.order_id)
        if existing is None:
            self._booked_orders[order.order_id] = order
            self.orders.append(order)
        elif existing is not order:
            # 外部换了对象：用新对象替换旧位置，保持顺序不变
            try:
                idx = self.orders.index(existing)
                self.orders[idx] = order
            except ValueError:
                self.orders.append(order)
            self._booked_orders[order.order_id] = order

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

        # 日内簿记必须在 append 之前取：prev_close 是"上一根"的收盘，
        # append 之后再去读就成了当前这根自己的价。
        self._roll_day(bar)
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

    def _roll_day(self, bar: Bar) -> None:
        """跨日时重置日内计数（T+1 解冻、单日成交笔数归零）。

        这段逻辑看着琐碎，但它是 T+1 风控能否成立的前提：
        ``bought_today`` 没归零，第二天所有卖出都会被误判为"当日买入不可卖"，
        策略就永远只能买不能卖。
        """
        dt = getattr(bar, "dt", None)
        day = dt.date() if dt is not None else None
        prev = self.series.last
        prev_dt = getattr(prev, "dt", None) if prev is not None else None

        self._is_same_day = bool(
            day is not None and prev_dt is not None and day == prev_dt.date()
        )
        self._prev_close = float(prev.close) if prev is not None else 0.0

        if day is not None and day != self._trading_day:
            self._trading_day = day
            self._bought_today = 0
            self._trades_today = 0

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
            self._book_order(order)
            self._emit_order(order)
            return None

        # 2) 仓位计算
        price = self.state.last_price
        if price <= 0:
            return None

        # 2.5) 已有未结订单时不再重复下单。
        #      回测里订单即时成交，这个判断永远为假、零影响；
        #      实盘里订单会挂一会儿，没有这道保护策略会每根K线都发一单，
        #      13 根K线就是 13 笔委托 —— 全部成交后会建出远超计划的仓位，
        #      而且资金占用完全不可控。这类事故在实盘上很常见。
        if self._has_open_order(signal.side):
            logger.debug(
                f"[{self.task_id}] 已有未结 {signal.side.value} 订单，跳过本次信号"
            )
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

        # 3) 撮合。返回时**未必已成交**（实盘只是报单成功）
        order = self.broker.create_order(
            self.symbol,
            signal.side,
            size,
            self.task_id,
            signal.reason,
            signal.source.value,
        )
        order = self.broker.submit(order)
        self._book_order(order)
        self.strategy.on_order(order)
        self._emit_order(order)

        # 4) 同步成交（回测/模拟）与异步成交（实盘）在这里分叉
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            # 模拟撮合：submit 返回即成交，立即记账
            self._on_filled_sync(order)
        elif order.status == OrderStatus.SUBMITTED:
            logger.debug(
                f"[{self.task_id}] 已报单待回报 | {order.order_id} "
                f"{order.symbol} {order.side.value} {order.size}"
            )
        self._sync_state(dt)
        return order

    def _on_filled_sync(self, order: Order) -> None:
        """同步成交（回测/模拟）后的簿记。

        实盘的对应逻辑在 :meth:`_on_broker_update` —— 两边都要维护同样的
        计数器语义，否则同一个策略在回测和实盘上的行为会不一致。
        """
        self._bars_since_trade = 0
        if order.side == Side.BUY or self.portfolio.position.size == 0:
            self._hold_bars = 0
        self._count_fill(order)

    def _count_fill(self, order: Order) -> None:
        """把订单的成交增量计入日内统计。

        用**累计量的差值**而不是"成交一次加一次"：部分成交会分多次到达，
        直接累加会把同一笔成交量重复计数，T+1 冻结量就会虚高。
        """
        cum = int(order.filled_size or 0)
        prev = int(self._counted_fills.get(order.order_id, 0))
        delta = cum - prev
        if delta <= 0:
            return
        self._counted_fills[order.order_id] = cum
        self._trades_today += 1
        if order.side == Side.BUY:
            self._bought_today += delta

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
        # 日内状态交给风控规则（T+1 / 涨跌停 / 单日笔数 / 可卖量）
        self.state.meta.update({
            "bought_today": self._bought_today,
            "trades_today": self._trades_today,
            "prev_close": self._prev_close,
            "is_same_day": self._is_same_day,
            "trading_day": self._trading_day.isoformat() if self._trading_day else None,
            "open_orders": self._open_order_count(),
            "pending_size": self._pending_size(),
            "pending_sell": self._pending_sell_size(),
        })

    def _open_order_count(self) -> int:
        try:
            return len(getattr(self.broker, "open_orders", []) or [])
        except Exception:
            return 0

    def _has_open_order(self, side: Optional[Side] = None) -> bool:
        """是否已有同方向的未结订单（实盘防堆单用）。"""
        for o in self._booked_orders.values():
            if o.status not in (OrderStatus.PENDING, OrderStatus.SUBMITTED,
                                OrderStatus.PARTIAL):
                continue
            if side is None or o.side == side:
                return True
        return False

    def cancel_open_orders(self, side: Optional[Side] = None) -> int:
        """撤销未结订单。风控熔断 / 引擎停止 / 手动干预时用。"""
        n = 0
        for o in list(self._booked_orders.values()):
            if o.status not in (OrderStatus.PENDING, OrderStatus.SUBMITTED,
                                OrderStatus.PARTIAL):
                continue
            if side is not None and o.side != side:
                continue
            try:
                if self.broker.cancel(o.order_id):
                    n += 1
            except Exception as exc:
                logger.debug(f"[{self.task_id}] 撤单失败 {o.order_id}: {exc}")
        return n

    def _pending_size(self) -> int:
        return sum(
            max(0, int(o.size) - int(o.filled_size))
            for o in self._booked_orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)
        )

    def _pending_sell_size(self) -> int:
        """已挂出但未成交的卖单量。这笔量不能再被第二个卖单占用。"""
        return sum(
            max(0, int(o.size) - int(o.filled_size))
            for o in self._booked_orders.values()
            if o.side == Side.SELL
            and o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)
        )

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
        # 实盘：强制离场单也挂着没成交时不要继续发。
        # 否则每根K线下一单，成交后会把仓位打到负数（或制造一堆废单）。
        if self._has_open_order(Side.SELL):
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
        self._book_order(order)
        self._emit_order(order)
        logger.warning(f"[{self.task_id}] 强制离场 | {signal.reason}")
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
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
        open_orders = 0
        try:
            open_orders = len(getattr(self.broker, "open_orders", []) or [])
        except Exception:
            open_orders = 0
        snap.update(
            {
                "task_id": self.task_id,
                "status": self.status.value,
                "strategy": self.strategy.name,
                "run_mode": getattr(self.run_mode, "value", str(self.run_mode)),
                "bar_count": self.bar_count,
                "hold_bars": self._hold_bars,
                "bars_since_trade": self._bars_since_trade,
                "risk_rules": [r.name for r in self.risk_chain.rules],
                "error_count": len(self.errors),
                "news_count": len(self.recent_news),
                # 实盘看板最关心的一个数：还有多少单挂在外面没成交
                "open_orders": open_orders,
                "pending_size": sum(
                    max(0, int(o.size) - int(o.filled_size))
                    for o in self._booked_orders.values()
                    if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED,
                                    OrderStatus.PARTIAL)
                ),            }
        )
        return snap

    def __repr__(self) -> str:  # pragma: no cover
        return f"<QuantTask {self.task_id} {self.symbol} {self.strategy.name} {self.status.value}>"
