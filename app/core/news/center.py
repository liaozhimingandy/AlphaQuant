#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : center.py
# @Description : 新闻中心：多源采集 -> 去重 -> 分析 -> 产出事件
#               与引擎解耦：它只 produce 事件，不关心谁消费，也不直接下单
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional

from app.core.market.types import NewsAnalysis, NewsItem
from app.core.news.analyzer import IBaseNewsAnalyzer, create_news_analyzer
from app.core.news.source import IBaseNewsSource, create_news_source
from app.utils.logger import logger


@dataclass
class NewsEvent:
    """一条"已分析"的新闻，事件通道的最终载荷。"""
    news: NewsItem
    analysis: Optional[NewsAnalysis] = None
    received_at: datetime = field(default_factory=datetime.now)

    @property
    def symbols(self) -> List[str]:
        if self.analysis and self.analysis.symbols:
            return list(self.analysis.symbols)
        return list(self.news.symbols)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "news": self.news.to_dict(),
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "received_at": self.received_at.isoformat(),
        }


class NewsCenter:
    """新闻中心。

    三条硬约束：
      1. **去重必须做**：RSS 源会反复推送同一条，不除重会让策略被同一条新闻反复触发
      2. **抓取必须在线程池**：阻塞 IO 直接进 reactor 会卡死整个引擎
      3. **分析失败必须降级**：拿不到结论时给出 confidence=0 的事件，
         由下游风控拦掉，而不是整条链路静默丢掉
    """

    def __init__(
        self,
        sources: Optional[List[Any]] = None,
        analyzers: Optional[List[Any]] = None,
        dedup_size: int = 5000,
        max_per_poll: int = 50,
        on_event: Optional[Callable[[NewsEvent], None]] = None,
    ) -> None:
        self.sources: List[IBaseNewsSource] = [
            create_news_source(s) for s in (sources or [])
        ]
        self.analyzers: List[IBaseNewsAnalyzer] = [
            create_news_analyzer(a) for a in (analyzers or [])
        ] or [create_news_analyzer("keyword")]

        self._seen: Deque[str] = deque(maxlen=int(dedup_size))
        self._seen_set: set[str] = set()
        self.max_per_poll = int(max_per_poll)
        self.on_event = on_event

        self.stats: Dict[str, int] = {
            "polls": 0,
            "fetched": 0,
            "duplicates": 0,
            "analyzed": 0,
            "emitted": 0,
            "errors": 0,
        }
        self.history: Deque[NewsEvent] = deque(maxlen=200)

    # ============================================================
    # 采集（阻塞，请在线程池调用）
    # ============================================================
    def poll(self) -> List[NewsEvent]:
        """拉取所有源 -> 去重 -> 分析 -> 返回新事件。"""
        self.stats["polls"] += 1
        events: List[NewsEvent] = []

        for src in self.sources:
            try:
                items = src.fetch() or []
            except Exception as exc:
                self.stats["errors"] += 1
                logger.warning(f"新闻源 {src.name} 拉取失败: {exc}")
                continue
            self.stats["fetched"] += len(items)
            for item in items[: self.max_per_poll]:
                evt = self.ingest(item)
                if evt is not None:
                    events.append(evt)

        for evt in events:
            self._emit(evt)
        return events

    def ingest(self, news: NewsItem) -> Optional[NewsEvent]:
        """单条入库：去重 -> 分析。重复则返回 None。"""
        fp = news.fingerprint
        if fp in self._seen_set:
            self.stats["duplicates"] += 1
            return None
        self._remember(fp)

        analysis: Optional[NewsAnalysis] = None
        for ana in self.analyzers:
            try:
                analysis = ana.analyze(news)
            except Exception as exc:
                self.stats["errors"] += 1
                logger.warning(f"分析器 {ana.name} 异常: {exc}")
                continue
            if analysis is not None:
                break
        if analysis is not None:
            self.stats["analyzed"] += 1

        return NewsEvent(news=news, analysis=analysis)

    def inject(self, news: NewsItem) -> Optional[NewsEvent]:
        """手动注入一条新闻（测试/运维用），走同一条链路。"""
        evt = self.ingest(news)
        if evt is not None:
            self._emit(evt)
        return evt

    # ============================================================
    def _emit(self, evt: NewsEvent) -> None:
        self.stats["emitted"] += 1
        self.history.append(evt)
        if self.on_event is not None:
            try:
                self.on_event(evt)
            except Exception as exc:
                logger.error(f"新闻事件回调异常: {exc}", exc_info=True)

    def _remember(self, fp: str) -> None:
        self._seen.append(fp)
        self._seen_set.add(fp)
        # deque 满时会自动丢弃最老的，同步清理 set 防止内存泄漏
        if len(self._seen) < len(self._seen_set):
            self._seen_set = set(self._seen)

    # ============================================================
    def snapshot(self) -> Dict[str, Any]:
        return {
            "sources": [s.name for s in self.sources],
            "analyzers": [a.name for a in self.analyzers],
            "stats": dict(self.stats),
            "recent": [e.to_dict() for e in list(self.history)[-5:]],
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NewsCenter sources={len(self.sources)} analyzers={len(self.analyzers)}>"


__all__ = ["NewsCenter", "NewsEvent"]
