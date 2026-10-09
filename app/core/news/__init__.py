#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 新闻层：采集 -> 去重 -> 分析 -> 事件
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.core.news.analyzer import (
    IBaseNewsAnalyzer,
    KeywordAnalyzer,
    LLMAnalyzer,
    create_news_analyzer,
    register_news_analyzer,
)
from app.core.news.center import NewsCenter, NewsEvent
from app.core.news.source import (
    IBaseNewsSource,
    MockNewsSource,
    RssNewsSource,
    create_news_source,
    register_news_source,
)

__all__ = [
    "NewsCenter",
    "NewsEvent",
    "IBaseNewsSource",
    "RssNewsSource",
    "MockNewsSource",
    "create_news_source",
    "register_news_source",
    "IBaseNewsAnalyzer",
    "KeywordAnalyzer",
    "LLMAnalyzer",
    "create_news_analyzer",
    "register_news_analyzer",
]
