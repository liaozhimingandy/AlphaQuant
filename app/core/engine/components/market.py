#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : market.py
# @Description : 行情中心组件：把数据源的 DataFrame 变成一根根 Bar 推给引擎
#               支持 replay（回放历史）与 poll（定时抓最新）两种模式
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Deque, Dict, List, Optional
from collections import deque

from twisted.internet import defer, task, threads

from app.core.engine.components.ibase import IBaseComponent
from app.core.engine.event import StandardEvents
from app.core.market.types import Bar
from app.utils.logger import logger


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


class MarketCenterComponent(IBaseComponent):
    """行情中心。

    - ``replay`` 模式：把历史数据按 interval 逐根推送，推完自动停。
      用于离线验证与回测式演示——不联网也能把整条链路跑通
    - ``poll``  模式：每个周期抓一次最新行情，推最后一根。
      用于模拟盘/实盘

    两种模式发布的事件完全一样（BAR_RECEIVED），下游无需区分。
    """

    name = "market_center"

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
        self.stats: Dict[str, int] = {"bars": 0, "polls": 0, "errors": 0}

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        self.symbols = [str(s) for s in (cfg.get("symbols") or self.symbols)]
        self.mode = str(cfg.get("mode") or self.mode)
        self.interval = float(cfg.get("interval") or self.interval)
        self.start_date = str(cfg.get("start") or self.start_date)
        self.end_date = str(cfg.get("end") or self.end_date)
        self.data_source = str(cfg.get("data_source") or self.data_source)

        if self.mode == "replay":
            self._load_history()
        logger.info(
            f"行情中心就绪 | 标的: {self.symbols} | 模式: {self.mode} "
            f"| 间隔: {self.interval}s"
        )

    def _load_history(self) -> None:
        if not self.start_date or not self.end_date:
            logger.warning("replay 模式缺少 start/end，跳过加载")
            return
        from app.data.service import MarketDataService
        from app.data.datasource import normalize_symbol

        for sym in self.symbols:
            try:
                df = MarketDataService.load(sym, self.start_date, self.end_date, source=self.data_source)
                bars = df_to_bars(normalize_symbol(sym), df)
            except Exception as exc:
                self.stats["errors"] += 1
                logger.error(f"加载 {sym} 历史数据失败: {exc}")
                bars = []
            self._queues[sym] = deque(bars)
            logger.info(f"  {sym}: 载入 {len(bars)} 根K线")

    def on_start(self) -> defer.Deferred:
        if not self.symbols:
            logger.warning("行情中心未配置标的，跳过启动")
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
                logger.info("历史行情回放完毕，行情中心停止推送")
                if self._loop is not None and self._loop.running:
                    self._loop.stop()
        return defer.succeed(None)

    @defer.inlineCallbacks
    def _tick_poll(self):
        """抓取最新行情（阻塞 IO，走线程池）。"""
        self.stats["polls"] += 1
        for sym in self.symbols:
            try:
                bar = yield threads.deferToThread(self._fetch_latest, sym)
            except Exception as exc:
                self.stats["errors"] += 1
                logger.warning(f"抓取 {sym} 最新行情失败: {exc}")
                continue
            if bar is not None:
                self._emit(bar)

    def _fetch_latest(self, symbol: str) -> Optional[Bar]:
        from app.data.service import MarketDataService
        from app.data.datasource import normalize_symbol

        end = self.end_date or datetime.now().strftime("%Y-%m-%d")
        start = self.start_date or end
        df = MarketDataService.load(symbol, start, end, source=self.data_source)
        bars = df_to_bars(normalize_symbol(symbol), df)
        return bars[-1] if bars else None

    def _emit(self, bar: Bar) -> None:
        self.stats["bars"] += 1
        self.event_bus.publish(StandardEvents.BAR_RECEIVED, bar=bar)

    # ---------------- 引擎协作 ----------------
    def is_busy(self) -> bool:
        """供引擎空闲看门狗判断：轮询还在跑就说明还有行情要推。

        replay 模式推完会自动停表 → 引擎随即可空闲退出；
        poll 模式常驻不停 → 引擎也就不会误判为空闲而退出。
        """
        return self._loop is not None and bool(self._loop.running)

    def push(self, bar: Bar) -> None:
        """外部直接推一根行情（测试/手动触发用）。"""
        if self.event_bus is None:
            raise RuntimeError("组件未初始化")
        self._emit(bar)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "mode": self.mode,
            "remaining": {s: len(q) for s, q in self._queues.items()},
            "finished": self._finished,
            "stats": dict(self.stats),
        }


__all__ = ["MarketCenterComponent", "df_to_bars"]
