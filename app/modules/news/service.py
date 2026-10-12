#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : news.py
# @Description : 资讯服务：定时采集新闻 -> 分析 -> 发布 NEWS_ANALYZED
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

重构说明
========

由原 ``NewsCenterComponent`` 提升为 **资讯服务**。

它符合服务的定义：不消费引擎内其他组件的输出，
只对外提供"新闻事件流"这一项能力（``news.feed``）。
策略组件订阅它产出的事件，但资讯服务自己不依赖任何服务。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from twisted.internet import defer, task, threads

from app.engine.capability import Cap, RestartPolicy, ServiceComponent
from app.runtime.event_bus import StandardEvents
from app.modules.news.center import NewsCenter, NewsEvent
from app.modules.news.source import create_news_source
from app.common.logging import logger


class NewsService(ServiceComponent):
    """资讯采集与分析服务。

    关键点：**抓取和分析都是阻塞的**（HTTP + 可能的 LLM 调用），
    必须 ``deferToThread``，否则一个慢 RSS 源会把 reactor 整个卡住——
    那意味着行情和交易全停，比少收几条新闻严重得多。

    **提供的能力**：``news.feed``
    """

    name = "news_service"
    provides = frozenset({Cap.NEWS_FEED})
    restart_policy = RestartPolicy.ON_ERROR

    def __init__(
        self,
        sources: Optional[List[Any]] = None,
        analyzers: Optional[List[Any]] = None,
        interval: float = 60.0,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.news_center = NewsCenter(sources=sources or [], analyzers=analyzers)
        self.interval = float(interval)
        self._loop: Optional[task.LoopingCall] = None
        self._polling = False

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        for s in cfg.get("sources") or []:
            self.news_center.sources.append(create_news_source(s))
        for a in cfg.get("analyzers") or []:
            from app.modules.news.analyzer import create_news_analyzer

            self.news_center.analyzers.append(create_news_analyzer(a))
        if cfg.get("interval"):
            self.interval = float(cfg["interval"])
        if not self.news_center.analyzers:
            from app.modules.news.analyzer import create_news_analyzer

            self.news_center.analyzers.append(create_news_analyzer("keyword"))
        logger.info(
            f"资讯服务就绪 | 源: {[s.name for s in self.news_center.sources]} "
            f"| 分析器: {[a.name for a in self.news_center.analyzers]} "
            f"| 间隔: {self.interval}s"
        )

    def on_start(self) -> defer.Deferred:
        if not self.news_center.sources:
            logger.warning("资讯服务未配置任何新闻源，跳过启动轮询")
            return defer.succeed(None)
        self._loop = task.LoopingCall(self._tick)
        self._loop.start(self.interval, now=False)
        logger.info(f"新闻轮询已启动，间隔 {self.interval}s")
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None
        return defer.succeed(None)

    # ---------------- 轮询 ----------------
    @defer.inlineCallbacks
    def _tick(self):
        if self._polling:
            # 上一轮还没回来（源太慢），跳过本轮，避免线程池被堆满
            logger.warning("上一轮新闻采集未完成，跳过本轮")
            return
        self._polling = True
        try:
            events: List[NewsEvent] = yield threads.deferToThread(
                self.news_center.poll
            )
            for evt in events:
                self._publish(evt)
            if events:
                logger.info(f"新闻本轮新增 {len(events)} 条")
        except Exception as exc:
            logger.error(f"新闻采集异常: {exc}", exc_info=True)
        finally:
            self._polling = False

    def _publish(self, evt: NewsEvent) -> None:
        symbols = evt.symbols
        ana = evt.analysis
        logger.info(
            f"新闻事件 | {symbols or '未标注'} | "
            f"{ana.direction if ana else 'N/A'} "
            f"score={ana.score if ana else '-'} "
            f"conf={ana.confidence if ana else '-'} | {evt.news.title[:40]}"
        )
        self.event_bus.publish(
            StandardEvents.NEWS_RECEIVED, news=evt.news, event=evt
        )
        self.event_bus.publish(
            StandardEvents.NEWS_ANALYZED,
            news=evt.news,
            analysis=evt.analysis,
            event=evt,
        )

    # ---------------- 外部注入 ----------------
    def inject(self, news) -> None:
        """手动注入一条新闻（运维/测试用），走与轮询完全相同的链路。"""
        if self.event_bus is None:
            raise RuntimeError("服务未初始化")
        evt = self.news_center.inject(news)
        if evt is not None:
            self._publish(evt)

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update(self.news_center.snapshot())
        return base

    def is_busy(self) -> bool:
        """轮询在跑就认为忙，避免引擎在后台服务场景误判空闲而退出。"""
        return self._loop is not None and bool(self._loop.running)


__all__ = ["NewsService"]
