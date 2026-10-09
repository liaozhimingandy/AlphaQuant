#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_news.py
# @Description : 新闻源解析 / 去重 / 分析器降级的单元测试
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest

from app.core.market.types import NewsItem
from app.core.news import (
    KeywordAnalyzer,
    LLMAnalyzer,
    MockNewsSource,
    NewsCenter,
)
from app.core.news.source import extract_symbols, parse_feed, parse_published

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>平安银行(000001)业绩预增</title>
    <description>净利润&lt;p&gt;增长&lt;/p&gt;80%</description>
    <link>http://example.com/a</link>
    <pubDate>Mon, 01 Jan 2024 10:00:00 +0800</pubDate>
  </item>
  <item><title>无日期条目</title><description>x</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>某公司被立案调查</title>
    <summary>重大违规</summary>
    <link href="http://example.com/b"/>
    <updated>2024-01-02T10:00:00Z</updated>
  </entry>
</feed>"""


class TestFeedParsing(unittest.TestCase):
    def test_rss_parsed(self):
        items = parse_feed(RSS.encode("utf-8"), "test")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "http://example.com/a")

    def test_atom_parsed(self):
        items = parse_feed(ATOM.encode("utf-8"), "test")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "http://example.com/b")

    def test_html_stripped_from_content(self):
        items = parse_feed(RSS.encode("utf-8"), "test")
        self.assertNotIn("<p>", items[0].content)

    def test_missing_date_is_none(self):
        items = parse_feed(RSS.encode("utf-8"), "test")
        self.assertIsNone(items[1].published_at)

    def test_pubdate_parsed(self):
        items = parse_feed(RSS.encode("utf-8"), "test")
        self.assertEqual(items[0].published_at.year, 2024)

    def test_garbage_time_returns_none(self):
        self.assertIsNone(parse_published("不是时间"))
        self.assertIsNone(parse_published(""))


class TestSymbolExtraction(unittest.TestCase):
    def test_bare_code(self):
        self.assertEqual(extract_symbols("000001 上涨"), ["000001"])

    def test_prefixed_code(self):
        self.assertEqual(extract_symbols("sh600000 大涨"), ["600000"])

    def test_keyword_map(self):
        self.assertEqual(
            extract_symbols("平安银行涨停", {"平安银行": "000001"}), ["000001"]
        )

    def test_dedup(self):
        self.assertEqual(extract_symbols("000001 和 000001"), ["000001"])


class TestKeywordAnalyzer(unittest.TestCase):
    def setUp(self):
        self.ana = KeywordAnalyzer()

    def test_bullish(self):
        r = self.ana.analyze(NewsItem(title="业绩预增，超预期", source="t"))
        self.assertEqual(r.direction, "bullish")
        self.assertGreater(r.score, 0)

    def test_bearish(self):
        r = self.ana.analyze(NewsItem(title="被立案调查，或退市", source="t"))
        self.assertEqual(r.direction, "bearish")
        self.assertLess(r.score, 0)

    def test_neutral(self):
        r = self.ana.analyze(NewsItem(title="今天天气不错", source="t"))
        self.assertEqual(r.direction, "neutral")
        self.assertEqual(r.confidence, 0.0)

    def test_confidence_grows_with_hits(self):
        one = self.ana.analyze(NewsItem(title="利好", source="t"))
        many = self.ana.analyze(NewsItem(title="利好 业绩预增 超预期 涨停", source="t"))
        self.assertGreater(many.confidence, one.confidence)


class TestLLMAnalyzerFallback(unittest.TestCase):
    def test_disabled_without_key(self):
        llm = LLMAnalyzer(api_key="")
        self.assertFalse(llm.available)

    def test_falls_back_to_keyword(self):
        llm = LLMAnalyzer(api_key="", fallback=KeywordAnalyzer())
        r = llm.analyze(NewsItem(title="业绩预增", source="t"))
        self.assertIsNotNone(r)
        self.assertEqual(r.analyzer, "keyword")

    def test_json_extraction_tolerates_fences(self):
        parsed = LLMAnalyzer._parse_json('```json\n{"score": 0.5, "confidence": 0.8}\n```')
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed["score"], 0.5)

    def test_json_extraction_from_prose(self):
        parsed = LLMAnalyzer._parse_json('我认为：{"score": -0.4, "confidence": 0.6} 就这样')
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed["score"], -0.4)

    def test_garbage_returns_none(self):
        self.assertIsNone(LLMAnalyzer._parse_json("完全没有 JSON"))


class TestNewsCenter(unittest.TestCase):
    def _item(self, title="测试新闻", symbol="000001") -> NewsItem:
        return NewsItem(title=title, content=title, source="t", symbols=[symbol])

    def test_dedup(self):
        src = MockNewsSource([self._item()])
        nc = NewsCenter(sources=[src], analyzers=["keyword"])
        self.assertEqual(len(nc.poll()), 1)
        self.assertEqual(len(nc.poll()), 0)  # 第二轮全重复

    def test_inject_twice_dedups(self):
        nc = NewsCenter(sources=[], analyzers=["keyword"])
        self.assertIsNotNone(nc.inject(self._item()))
        self.assertIsNone(nc.inject(self._item()))

    def test_on_event_callback(self):
        seen = []
        nc = NewsCenter(sources=[], analyzers=["keyword"], on_event=seen.append)
        nc.inject(self._item())
        self.assertEqual(len(seen), 1)

    def test_source_failure_isolated(self):
        class Boom(MockNewsSource):
            name = "boom"

            def fetch(self):
                raise RuntimeError("boom")

        nc = NewsCenter(sources=[Boom(), MockNewsSource([self._item()])])
        events = nc.poll()
        self.assertEqual(len(events), 1)  # 坏源不影响好源
        self.assertEqual(nc.stats["errors"], 1)

    def test_analyzer_failure_isolated(self):
        class Boom(KeywordAnalyzer):
            name = "boom"

            def analyze(self, news):
                raise RuntimeError("boom")

        nc = NewsCenter(sources=[MockNewsSource([self._item()])], analyzers=[Boom()])
        evt = nc.poll()[0]
        self.assertIsNone(evt.analysis)  # 拿不到结论也要产出事件，由下游风控拦
        self.assertEqual(nc.stats["errors"], 1)

    def test_stats_tracked(self):
        nc = NewsCenter(sources=[MockNewsSource([self._item()])], analyzers=["keyword"])
        nc.poll()
        self.assertEqual(nc.stats["fetched"], 1)
        self.assertEqual(nc.stats["emitted"], 1)


if __name__ == "__main__":
    unittest.main()
