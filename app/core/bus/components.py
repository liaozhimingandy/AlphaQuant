#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : components.py
# @Description : 把文件总线接到引擎事件总线上：服务之间通过"行情/新闻"主题协作
#               发布服务把 BAR_RECEIVED / NEWS_ANALYZED 写进总线；
#               订阅服务轮询总线，把它们重新注入本地事件总线
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from twisted.internet import defer, task, threads

from app.core.bus.filebus import FileBus
from app.core.engine.components.ibase import IBaseComponent
from app.core.engine.event import StandardEvents
from app.core.market.types import Bar, NewsAnalysis, NewsItem
from app.utils.logger import logger


def bar_from_dict(data: Dict[str, Any]) -> Optional[Bar]:
    """重建 Bar。行情是跨服务传递的关键载荷，这里必须容错。"""
    if not isinstance(data, dict):
        return None
    try:
        dt = data.get("dt")
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        elif not isinstance(dt, datetime):
            dt = datetime.now()
        return Bar(
            symbol=str(data.get("symbol") or ""),
            dt=dt,
            open=float(data.get("open") or 0.0),
            high=float(data.get("high") or 0.0),
            low=float(data.get("low") or 0.0),
            close=float(data.get("close") or 0.0),
            volume=float(data.get("volume") or 0.0),
            amount=float(data.get("amount") or 0.0),
        )
    except Exception as exc:
        logger.debug(f"总线报文重建 Bar 失败: {exc}")
        return None


def news_from_dict(data: Dict[str, Any]):
    """重建 (NewsItem, NewsAnalysis|None)。"""
    if not isinstance(data, dict):
        return None, None
    try:
        raw_news = data.get("news") or {}
        pub = raw_news.get("published_at")
        if isinstance(pub, str):
            try:
                pub = datetime.fromisoformat(pub)
            except ValueError:
                pub = None
        item = NewsItem(
            title=str(raw_news.get("title") or ""),
            content=str(raw_news.get("content") or ""),
            url=str(raw_news.get("url") or ""),
            source=str(raw_news.get("source") or "bus"),
            published_at=pub,
            symbols=list(raw_news.get("symbols") or []),
            raw=dict(raw_news.get("raw") or {}),
        )
        raw_ana = data.get("analysis")
        ana = None
        if isinstance(raw_ana, dict):
            ana = NewsAnalysis(
                news_fingerprint=str(raw_ana.get("news_fingerprint") or item.fingerprint),
                symbols=list(raw_ana.get("symbols") or []),
                score=float(raw_ana.get("score") or 0.0),
                confidence=float(raw_ana.get("confidence") or 0.0),
                direction=str(raw_ana.get("direction") or "neutral"),
                reason=str(raw_ana.get("reason") or ""),
                analyzer=str(raw_ana.get("analyzer") or "bus"),
                raw=dict(raw_ana.get("raw") or {}),
            )
        return item, ana
    except Exception as exc:
        logger.debug(f"总线报文重建新闻失败: {exc}")
        return None, None


class BusPublisherComponent(IBaseComponent):
    """把本服务的行情/新闻发布到文件总线。"""

    name = "bus_publisher"

    def __init__(
        self,
        bus_dir: Optional[str] = None,
        node_id: str = "",
        publish_bars: bool = True,
        publish_news: bool = True,
        bar_topic: str = "bar",
        news_topic: str = "news",
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.bus_dir = bus_dir
        self.node_id = node_id or "publisher"
        self.publish_bars = bool(publish_bars)
        self.publish_news = bool(publish_news)
        self.bar_topic = bar_topic
        self.news_topic = news_topic
        self.bus: Optional[FileBus] = None
        self.stats: Dict[str, int] = {"bars": 0, "news": 0, "errors": 0}

    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        self.bus_dir = cfg.get("bus_dir") or self.bus_dir
        self.node_id = str(cfg.get("node_id") or self.node_id)
        self.publish_bars = bool(cfg.get("publish_bars", self.publish_bars))
        self.publish_news = bool(cfg.get("publish_news", self.publish_news))
        self.bar_topic = str(cfg.get("bar_topic") or self.bar_topic)
        self.news_topic = str(cfg.get("news_topic") or self.news_topic)

        self.bus = FileBus(base_dir=self.bus_dir, node_id=self.node_id)
        if self.publish_bars:
            self.subscribe_event(StandardEvents.BAR_RECEIVED, self._on_bar)
        if self.publish_news:
            self.subscribe_event(StandardEvents.NEWS_ANALYZED, self._on_news)
        logger.info(
            f"总线发布者就绪 | node={self.node_id} | "
            f"行情={'开' if self.publish_bars else '关'} 新闻={'开' if self.publish_news else '关'}"
        )

    def on_start(self) -> defer.Deferred:
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        return defer.succeed(None)

    def _on_bar(self, bar: Any = None, origin: Any = None, **kwargs: Any) -> None:
        if bar is None or self.bus is None:
            return
        # 关键：来自总线的行情不能再次发布，否则两个服务之间会互相转发，无限循环
        if isinstance(origin, str) and origin.startswith("bus:"):
            return
        try:
            self.bus.publish(self.bar_topic, bar.to_dict(), kind="bar")
            self.stats["bars"] += 1
        except Exception as exc:
            self.stats["errors"] += 1
            logger.debug(f"行情发布到总线失败: {exc}")

    def _on_news(self, news: Any = None, analysis: Any = None, origin: Any = None, **kwargs: Any) -> None:
        if news is None or self.bus is None:
            return
        if isinstance(origin, str) and origin.startswith("bus:"):
            return
        try:
            self.bus.publish(
                self.news_topic,
                {
                    "news": news.to_dict(),
                    "analysis": analysis.to_dict() if analysis is not None else None,
                },
                kind="news",
            )
            self.stats["news"] += 1
        except Exception as exc:
            self.stats["errors"] += 1
            logger.debug(f"新闻发布到总线失败: {exc}")

    def is_busy(self) -> bool:
        return False

    def snapshot(self) -> Dict[str, Any]:
        data = self.bus.snapshot() if self.bus else {}
        data["stats"] = dict(self.stats)
        return data


class BusSubscriberComponent(IBaseComponent):
    """轮询文件总线，把别的服务产生的行情/新闻注入本服务事件总线。

    这是"多个服务互相配合"的落点：策略服务自己不需要连行情源，
    订阅行情服务发布过来的 bar 就能驱动全部策略。
    """

    name = "bus_subscriber"

    def __init__(
        self,
        bus_dir: Optional[str] = None,
        node_id: str = "",
        subscribe: Optional[List[str]] = None,
        interval: float = 1.0,
        batch: int = 200,
        seek_end_on_start: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.bus_dir = bus_dir
        self.node_id = node_id or "subscriber"
        self.subscribe = list(subscribe or ["bar", "news"])
        self.interval = float(interval)
        self.batch = int(batch)
        self.seek_end_on_start = bool(seek_end_on_start)
        self.bus: Optional[FileBus] = None
        self._loop: Optional[task.LoopingCall] = None
        self.stats: Dict[str, int] = {"rounds": 0, "bars": 0, "news": 0, "errors": 0}

    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        self.bus_dir = cfg.get("bus_dir") or self.bus_dir
        self.node_id = str(cfg.get("node_id") or self.node_id)
        self.subscribe = list(cfg.get("subscribe") or self.subscribe)
        self.interval = float(cfg.get("interval") or self.interval)
        self.batch = int(cfg.get("batch") or self.batch)
        if "seek_end_on_start" in cfg:
            self.seek_end_on_start = bool(cfg["seek_end_on_start"])

        self.bus = FileBus(base_dir=self.bus_dir, node_id=self.node_id)
        logger.info(f"总线订阅者就绪 | node={self.node_id} | 主题={self.subscribe}")

    def on_start(self) -> defer.Deferred:
        if self.bus is None or not self.subscribe:
            return defer.succeed(None)
        if self.seek_end_on_start:
            for t in self.subscribe:
                self.bus.seek_end(t)
        self._loop = task.LoopingCall(self._tick)
        self._loop.start(self.interval, now=False)
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None
        return defer.succeed(None)

    @defer.inlineCallbacks
    def _tick(self):
        """读总线 → 注入本地事件总线。IO 走线程池，不在 reactor 上读文件。"""
        if self.bus is None:
            return
        try:
            records: List[Dict[str, Any]] = yield threads.deferToThread(self._drain)
            for rec in records:
                self._inject(rec)
            self.stats["rounds"] += 1
        except Exception as exc:
            self.stats["errors"] += 1
            logger.error(f"总线订阅轮询异常: {exc}", exc_info=True)

    def _drain(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for topic in self.subscribe:
            try:
                out.extend(self.bus.poll(topic, limit=self.batch))
            except Exception as exc:
                logger.debug(f"总线主题 {topic} 读取失败: {exc}")
        return out

    def _inject(self, rec: Dict[str, Any]) -> None:
        kind = str(rec.get("kind") or rec.get("topic") or "")
        payload = rec.get("payload") or {}
        node = rec.get("node")
        if kind == "bar":
            bar = bar_from_dict(payload)
            if bar is None:
                return
            self.stats["bars"] += 1
            self.event_bus.publish(
                StandardEvents.BAR_RECEIVED, bar=bar, origin=f"bus:{node}"
            )
        elif kind == "news":
            item, ana = news_from_dict(payload)
            if item is None:
                return
            self.stats["news"] += 1
            self.event_bus.publish(StandardEvents.NEWS_RECEIVED, news=item)
            self.event_bus.publish(
                StandardEvents.NEWS_ANALYZED, news=item, analysis=ana, origin=f"bus:{node}"
            )

    def is_busy(self) -> bool:
        return False

    def snapshot(self) -> Dict[str, Any]:
        data = self.bus.snapshot() if self.bus else {}
        data["config"] = {
            "node_id": self.node_id,
            "subscribe": list(self.subscribe),
            "interval": self.interval,
        }
        data["stats"] = dict(self.stats)
        return data


__all__ = [
    "BusPublisherComponent",
    "BusSubscriberComponent",
    "bar_from_dict",
    "news_from_dict",
]
