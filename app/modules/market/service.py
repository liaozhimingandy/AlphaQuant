#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : market.py
# @Description : 行情服务：把数据源的 DataFrame 变成一根根 Bar 推给引擎
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

重构说明
========

由原 ``MarketCenterComponent`` 提升为 **服务**。

它本来就是服务：不消费任何人的输出，只对外提供行情能力，
被策略组件与工作单元依赖。之前只是因为和采集、落库混在同一个
``component/`` 目录里，看起来像同级的东西。

提升为服务后新增了三件以前做不到的事：
  1. **能力声明** —— ``provides = {BAR_FEED, WATCHLIST}``，
     依赖方按能力找它，不再硬编码 ``get_component("market_center")``；
  2. **可单独重启** —— 行情源抽风时重启这一个服务，不用重启整台引擎；
  3. **可脱离引擎测试** —— 手工 new 一个塞进桩对象即可，
     以前要构造整台引擎才能测"回放能不能推完"。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from twisted.internet import defer, task, threads

from app.engine.capability import Cap, RestartPolicy, ServiceComponent
from app.runtime.event_bus import StandardEvents
from app.domain.clean import clean_frame, default_cleaner
from app.domain.types import Bar
from app.common.logging import logger


def df_to_bars(symbol: str, df) -> List[Bar]:
    """DataFrame -> Bar 列表。兼容 date 为索引或列两种形态。"""
    if df is None or getattr(df, "empty", True):
        return []

    frame = df.copy()
    if "date" not in frame.columns:
        frame = frame.reset_index()
    col = "date" if "date" in frame.columns else frame.columns[0]

    bars: List[Bar] = []
    for _, row in frame.iterrows():
        dt = row[col]
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt)
            except Exception:
                continue
        bars.append(
            Bar(
                symbol=symbol,
                dt=dt,
                open=float(row.get("open", 0.0)),
                high=float(row.get("high", 0.0)),
                low=float(row.get("low", 0.0)),
                close=float(row.get("close", 0.0)),
                volume=float(row.get("volume", 0.0) or 0.0),
                amount=float(row.get("amount", 0.0) or 0.0),
            )
        )
    bars.sort(key=lambda b: b.dt)
    return bars


class MarketService(ServiceComponent):
    """行情服务。

    - ``replay`` 模式：把历史数据按 interval 逐根推送，推完自动停。
      用于离线验证与回测式演示——不联网也能把整条链路跑通
    - ``poll``  模式：每个周期抓一次最新行情，推最后一根。
      用于模拟盘/实盘

    两种模式发布的事件完全一样（BAR_RECEIVED），下游无需区分。

    **提供的能力**：``market.bar_feed`` / ``market.watchlist`` / ``market.quote``
    """

    name = "market_service"
    provides = frozenset({Cap.BAR_FEED, Cap.WATCHLIST, Cap.SNAPSHOT_QUOTE})
    restart_policy = RestartPolicy.ON_ERROR

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        mode: str = "replay",
        interval: float = 0.05,
        start: str = "",
        end: str = "",
        data_source: str = "db",
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.symbols: List[str] = [str(s) for s in (symbols or [])]
        self.mode = str(mode or "replay")
        self.interval = float(interval)
        # 注意：不能存成 self.start / self.end —— 会遮蔽基类的 start() 方法
        self.start_date = str(start)
        self.end_date = str(end)
        self.data_source = str(data_source or "db")

        self._queues: Dict[str, Deque[Bar]] = {}
        self._loop: Optional[task.LoopingCall] = None
        self._finished = False
        # 是否只在交易时段推进（poll 模式）。replay 是历史回放，与
        # "今天是不是交易日"无关，不能受它约束。
        self.gate_trading_day = mode == "poll"
        # 已推送 bar 的日期，用于同 bar 去重：盘后数据定稿后每个周期都
        # 会取到同一根，重复推送会让策略被同一根 bar 反复驱动。
        self._last_bar_date: Dict[str, Any] = {}
        self.stats: Dict[str, int] = {
            "bars": 0, "polls": 0, "errors": 0,
            "skipped_closed": 0, "dup_bars": 0,
        }

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        self.symbols = [str(s) for s in (cfg.get("symbols") or self.symbols)]
        self.mode = str(cfg.get("mode") or self.mode)
        self.interval = float(cfg.get("interval") or self.interval)
        self.start_date = str(cfg.get("start") or self.start_date)
        self.end_date = str(cfg.get("end") or self.end_date)
        self.data_source = str(cfg.get("data_source") or self.data_source)
        # mode 可能被配置覆盖，闸门要跟着重算（不能只在 __init__ 里算一次）
        self.gate_trading_day = (
            self.mode == "poll" and bool(cfg.get("gate_trading_day", True))
        )

        if self.mode == "replay":
            self._load_history()
        logger.info(
            f"行情服务就绪 | 标的: {self.symbols} | 模式: {self.mode} "
            f"| 间隔: {self.interval}s"
        )

    def _load_history(self) -> None:
        if not self.start_date or not self.end_date:
            logger.warning("replay 模式缺少 start/end，跳过加载")
            return
        for sym in self.symbols:
            self._load_symbol_history(sym)

    def _load_symbol_history(self, symbol: str) -> int:
        """加载单个标的的历史K线到回放队列。返回载入根数。"""
        if not self.start_date or not self.end_date:
            return 0
        from app.data.service import MarketDataService
        from app.domain.symbol import normalize_symbol

        try:
            df = MarketDataService.load(
                symbol, self.start_date, self.end_date, source=self.data_source
            )
            # 清洗：数据源给的东西不等于能喂给策略的东西。
            # 重复行、时间倒序、OHLC 自相矛盾、停牌/涨跌停标记都要在这里处理完，
            # 否则脏数据会一路流到策略里，让它在错误的价格上做决定。
            df, report = clean_frame(df, symbol)
            if report.dirty:
                self.stats["cleaned"] = self.stats.get("cleaned", 0) + 1
                logger.warning(f"  {symbol} 历史数据清洗: {report.summary()}")
            bars = df_to_bars(normalize_symbol(symbol), df)
        except Exception as exc:
            self.stats["errors"] += 1
            logger.error(f"加载 {symbol} 历史数据失败: {exc}")
            bars = []
        self._queues[symbol] = deque(bars)
        logger.info(f"  {symbol}: 载入 {len(bars)} 根K线")
        return len(bars)

    # ---------------- 动态订阅 ----------------
    def watch(self, symbol: str) -> bool:
        """运行时新增一个订阅标的（面板/CLI 新增任务时调用）。

        返回 True 表示这是一次新增；False 表示已在订阅列表中。
        replay 模式下需要同时把该标的的历史补进回放队列，
        否则新任务会一直"行情不足"。
        """
        symbol = str(symbol or "").strip()
        if not symbol:
            return False
        if symbol in self.symbols:
            return False
        self.symbols.append(symbol)
        # 初始化之前不要在这里加载历史——on_initialize 会统一遍历 self.symbols 加载，
        # 否则同一标的会被读两次数据（慢了一倍，还容易踩到数据源限流）
        if self.mode == "replay" and self.context is not None:
            self._load_symbol_history(symbol)
        logger.info(f"行情服务新增订阅: {symbol} | 当前标的: {self.symbols}")
        return True

    def unwatch(self, symbol: str) -> bool:
        """取消订阅。不会清空已有队列，避免误伤仍在使用该标的的任务。"""
        symbol = str(symbol or "").strip()
        if symbol not in self.symbols:
            return False
        self.symbols.remove(symbol)
        self._queues.pop(symbol, None)
        logger.info(f"行情服务取消订阅: {symbol}")
        return True

    def on_start(self) -> defer.Deferred:
        if self.mode not in ("replay", "poll"):
            # 行情由外部（如文件总线）供给时，本服务不产生任何推送
            logger.info(f"行情模式 {self.mode!r}：本地行情源关闭，交由外部供给")
            return defer.succeed(None)
        if not self.symbols:
            logger.warning("行情服务未配置标的，跳过启动")
            return defer.succeed(None)
        self._loop = task.LoopingCall(self._tick)
        self._loop.start(self.interval, now=False)
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None
        return defer.succeed(None)

    # ---------------- 推送 ----------------
    @defer.inlineCallbacks
    def _tick(self):
        try:
            if self.mode == "replay":
                yield self._tick_replay()
            else:
                yield self._tick_poll()
        except Exception as exc:
            self.stats["errors"] += 1
            logger.error(f"行情推送异常: {exc}", exc_info=True)

    def _tick_replay(self):
        """每个 symbol 各推一根，全部推完则停表。"""
        pushed = 0
        for sym, q in self._queues.items():
            if q:
                self._emit(q.popleft())
                pushed += 1
        if pushed == 0:
            if not self._finished:
                self._finished = True
                logger.info("历史行情回放完毕，行情服务停止推送")
                if self._loop is not None and self._loop.running:
                    self._loop.stop()
        return defer.succeed(None)

    @defer.inlineCallbacks
    def _tick_poll(self):
        """抓取最新行情（阻塞 IO，走线程池）。

        只在交易时段推进。非交易日库里本来就没有当日 bar，硬查会抛
        ``DataFetchError`` 并每周期每标的刷一条告警——既污染日志，
        又白搭一次 DB 往返（标的一多就很可观）。
        """
        if self.gate_trading_day and not self._in_session():
            self.stats["skipped_closed"] += 1
            return
        self.stats["polls"] += 1
        for sym in self.symbols:
            try:
                bar = yield threads.deferToThread(self._fetch_latest, sym)
            except Exception as exc:
                self.stats["errors"] += 1
                logger.warning(f"抓取 {sym} 最新行情失败: {exc}")
                continue
            if bar is not None:
                self._emit(bar, dedupe=True)

    def _in_session(self) -> bool:
        """当前是否处在交易时段（仅 poll 模式关心）。

        日历自身出故障时**放行**而不是掐死行情——日历是优化项，
        不该成为新的单点故障；真出问题下游会照常报出来。
        """
        try:
            from app.domain.calendar import get_calendar

            cal = get_calendar()
            return bool(cal.is_trading_day()) and bool(cal.is_open())
        except Exception:
            return True

    def _fetch_latest(self, symbol: str) -> Optional[Bar]:
        from app.data.service import MarketDataService
        from app.domain.symbol import normalize_symbol

        end = self.end_date or datetime.now().strftime("%Y-%m-%d")
        start = self.start_date or end
        df = MarketDataService.load(symbol, start, end, source=self.data_source)
        bars = df_to_bars(normalize_symbol(symbol), df)
        return bars[-1] if bars else None

    def _emit(self, bar: Bar, dedupe: bool = False) -> None:
        """推一根行情给策略。

        :param dedupe: 同一标的同一日期只推一次。轮询模式下日线当日定稿后
            每个周期都会取到同一根 bar，不去重会让策略被同一根反复驱动
            （表现为重复信号、模拟盘重复成交、实盘重复报单）。
            手动 :meth:`push` 不去重 —— 那是人主动触发的。
        """
        if dedupe:
            key = str(getattr(bar, "symbol", "") or "")
            day = getattr(bar, "date", None)
            if day is not None and self._last_bar_date.get(key) == day:
                self.stats["dup_bars"] += 1
                return
            if key:
                self._last_bar_date[key] = day
        self.stats["bars"] += 1
        self.event_bus.publish(StandardEvents.BAR_RECEIVED, bar=bar)

    # ---------------- 能力出口 ----------------
    def latest_bar(self, symbol: str) -> Optional[Bar]:
        """``market.quote`` 能力：取一只票的最新一根。（阻塞 IO，走线程池）。"""
        from app.domain.symbol import normalize_symbol

        return self._fetch_latest(normalize_symbol(str(symbol or "")))

    def is_busy(self) -> bool:
        """供引擎空闲看门狗判断：轮询还在跑就说明还有行情要推。

        replay 模式推完会自动停表 → 引擎随即可空闲退出；
        poll 模式常驻不停 → 引擎也就不会误判为空闲而退出。
        """
        return self._loop is not None and bool(self._loop.running)

    def push(self, bar: Bar) -> None:
        """外部直接推一根行情（测试/手动触发用）。"""
        if self.event_bus is None:
            raise RuntimeError("服务未初始化")
        self._emit(bar)

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update({
            "symbols": list(self.symbols),
            "mode": self.mode,
            "remaining": {s: len(q) for s, q in self._queues.items()},
            "finished": self._finished,
            "stats": dict(self.stats),
        })
        return base


__all__ = ["MarketService", "df_to_bars"]
