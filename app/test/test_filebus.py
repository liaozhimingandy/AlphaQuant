#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_filebus.py
# @Description : 文件消息总线单元测试
#               核心不变量：不丢消息、不重复消费、不自我循环、进度可持久化
#
#               注意：总线语义是"节点默认不消费自己发布的消息"（防自激循环），
#               所以测试里发布方与订阅方必须是**不同 node_id**。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from app.core.bus.components import bar_from_dict, news_from_dict
from app.core.bus.filebus import FileBus
from app.core.market.types import Bar, NewsAnalysis, NewsItem
from app.test.helpers import TempWorkspaceMixin

PUB = "publisher"
SUB = "subscriber"


class TestFileBus(TempWorkspaceMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.dir = self.workdir()
        self.pub = FileBus(self.dir, node_id=PUB)
        self.sub = FileBus(self.dir, node_id=SUB)

    # ---------------- 基础收发 ----------------
    def test_publish_poll_roundtrip(self):
        self.pub.publish("bar", {"symbol": "000001", "close": 10.5})
        self.pub.publish("bar", {"symbol": "000001", "close": 10.6})

        recs = self.sub.poll("bar")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["payload"]["close"], 10.5)
        self.assertEqual(recs[1]["payload"]["close"], 10.6)
        self.assertEqual(recs[0]["node"], PUB)
        self.assertEqual(recs[0]["kind"], "bar")
        self.assertIn("ts", recs[0])

    def test_own_messages_skipped_by_default(self):
        """防自激循环：节点不该消费自己写进总线的东西。"""
        self.pub.publish("bar", {"i": 1})
        self.assertEqual(self.pub.poll("bar"), [])
        self.assertEqual(len(self.sub.poll("bar")), 1)

    def test_include_own_messages(self):
        self.pub.publish("bar", {"i": 1})
        self.assertEqual(len(self.pub.poll("bar", include_own=True)), 1)

    def test_poll_is_incremental(self):
        self.pub.publish("bar", {"i": 1})
        self.assertEqual(len(self.sub.poll("bar")), 1)
        self.assertEqual(self.sub.poll("bar"), [])
        self.pub.publish("bar", {"i": 2})
        recs = self.sub.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs], [2])

    def test_two_subscribers_independent(self):
        """多个策略服务各自有独立进度，互不影响。"""
        sub2 = FileBus(self.dir, node_id="subscriber-2")
        self.pub.publish("bar", {"i": 1})
        self.assertEqual(len(self.sub.poll("bar")), 1)
        self.assertEqual(len(sub2.poll("bar")), 1)  # 另一个订阅者仍能看到

    # ---------------- 进度 ----------------
    def test_offset_persists_across_instances(self):
        self.pub.publish("bar", {"i": 1})
        self.pub.publish("bar", {"i": 2})

        first = FileBus(self.dir, node_id=SUB)
        self.assertEqual(len(first.poll("bar", limit=1)), 1)

        # 新实例（模拟进程重启）从上次位置继续，不重复第 1 条
        restarted = FileBus(self.dir, node_id=SUB)
        recs = restarted.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs], [2])

    def test_limit_keeps_remainder(self):
        for i in range(5):
            self.pub.publish("bar", {"i": i})
        first = self.sub.poll("bar", limit=2)
        second = self.sub.poll("bar", limit=2)
        third = self.sub.poll("bar", limit=2)
        fourth = self.sub.poll("bar", limit=2)
        self.assertEqual([r["payload"]["i"] for r in first], [0, 1])
        self.assertEqual([r["payload"]["i"] for r in second], [2, 3])
        self.assertEqual([r["payload"]["i"] for r in third], [4])
        self.assertEqual(fourth, [])

    def test_tail_does_not_advance_offset(self):
        for i in range(3):
            self.pub.publish("bar", {"i": i})
        tail = self.sub.tail("bar", 2)
        self.assertEqual(len(tail), 2)
        self.assertEqual(tail[0]["payload"]["i"], 2)  # newest_first
        # tail 之后 poll 仍能拿到全部消息
        self.assertEqual(len(self.sub.poll("bar")), 3)

    def test_reset_offset_replays(self):
        self.pub.publish("bar", {"i": 1})
        self.assertEqual(len(self.sub.poll("bar")), 1)
        self.sub.reset_offset("bar")
        self.assertEqual(len(self.sub.poll("bar")), 1)

    def test_seek_end_skips_backlog(self):
        for i in range(5):
            self.pub.publish("bar", {"i": i})
        self.sub.seek_end("bar")
        self.assertEqual(self.sub.poll("bar"), [])
        self.pub.publish("bar", {"i": 99})
        recs = self.sub.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs], [99])

    def test_offset_reset_when_file_truncated(self):
        self.pub.publish("bar", {"i": 1})
        self.sub.poll("bar")
        # 模拟轮转：文件被清空
        (self.dir / "bar.jsonl").write_text("", encoding="utf-8")
        self.pub.publish("bar", {"i": 2})
        recs = self.sub.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs], [2])

    def test_offset_reset_when_rewritten_to_similar_size(self):
        """回归：文件被清空后又被写回**差不多长**的内容时，offset 仍然必须失效。

        这是纯 byte offset 分不清的场景——新文件已经长到 ≥ 原 offset，
        靠"offset > size"根本发现不了，之前会静默读到错位/空的数据。
        现在靠头部指纹识别"这已经是另一个文件了"。
        """
        for i in range(3):
            self.pub.publish("bar", {"i": i})
        self.sub.poll("bar")
        old_size = (self.dir / "bar.jsonl").stat().st_size

        (self.dir / "bar.jsonl").write_text("", encoding="utf-8")
        for i in range(3):
            self.pub.publish("bar", {"i": 100 + i})
        new_size = (self.dir / "bar.jsonl").stat().st_size
        self.assertGreaterEqual(new_size, old_size)  # 确保走的是指纹分支，不是 size 分支

        recs = self.sub.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs], [100, 101, 102])

    def test_append_does_not_trip_anchor(self):
        """正常追加不能误伤：头部没变，指纹一样，进度照常前进。"""
        self.pub.publish("bar", {"i": 1})
        self.assertEqual(len(self.sub.poll("bar")), 1)
        self.pub.publish("bar", {"i": 2})
        recs = self.sub.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs], [2])

    # ---------------- 健壮性 ----------------
    def test_topic_sanitized(self):
        self.pub.publish("a/b:c", {"x": 1})
        self.assertIn("a_b_c", self.pub.topics())

    def test_missing_topic_returns_empty(self):
        self.assertEqual(self.sub.poll("nope"), [])

    def test_malformed_line_skipped(self):
        self.pub.publish("bar", {"i": 1})
        with (self.dir / "bar.jsonl").open("a", encoding="utf-8") as f:
            f.write("this is not json\n")
        recs = self.sub.poll("bar")
        self.assertEqual(len(recs), 1)
        self.assertGreaterEqual(self.sub.stats["errors"], 1)

    def test_partial_line_not_consumed(self):
        """写到一半的行（无换行结尾）必须留给下次，不能当完整消息解析。"""
        self.pub.publish("bar", {"i": 1})
        with (self.dir / "bar.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"seq": 999, "node": "x", "kind": "bar", "payload": {"i": 2}')  # 缺 }
        recs = self.sub.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs], [1])
        # 补全后半行，下次应能读到
        with (self.dir / "bar.jsonl").open("a", encoding="utf-8") as f:
            f.write("}\n")
        recs2 = self.sub.poll("bar")
        self.assertEqual([r["payload"]["i"] for r in recs2], [2])

    def test_enums_serialized_as_values(self):
        """回归：str 子类枚举必须转成字符串值。"""
        from app.core.market.types import Side

        rec = self.pub.publish("x", {"side": Side.BUY})
        self.assertEqual(rec["payload"]["side"], "BUY")
        self.assertNotIn("Side.", str(rec["payload"]))

    def test_datetime_serialized(self):
        rec = self.pub.publish("x", {"dt": datetime(2024, 1, 2, 3, 4, 5)})
        self.assertEqual(rec["payload"]["dt"], "2024-01-02T03:04:05")

    def test_snapshot_reports_topics(self):
        self.pub.publish("bar", {"i": 1})
        self.sub.poll("bar")
        snap = self.pub.snapshot()
        self.assertIn("bar", snap["topics"])
        self.assertEqual(snap["node"], PUB)

    # ---------------- 载荷重建 ----------------
    def test_bar_payload_roundtrip(self):
        bar = Bar("000001", datetime(2024, 1, 2), 10, 11, 9, 10.5, 100, 0)
        self.pub.publish("bar", bar.to_dict(), kind="bar")
        rec = self.sub.poll("bar")[0]
        rebuilt = bar_from_dict(rec["payload"])
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.symbol, "000001")
        self.assertAlmostEqual(rebuilt.close, 10.5)
        self.assertAlmostEqual(rebuilt.high, 11.0)
        self.assertEqual(rebuilt.dt, bar.dt)

    def test_news_payload_roundtrip(self):
        item = NewsItem(title="利好", symbols=["000001"], source="t")
        ana = NewsAnalysis("fp", ["000001"], score=0.8, confidence=0.9, direction="bullish")
        self.pub.publish("news", {"news": item.to_dict(), "analysis": ana.to_dict()}, kind="news")
        rec = self.sub.poll("news")[0]
        re_item, re_ana = news_from_dict(rec["payload"])
        self.assertIsNotNone(re_item)
        self.assertIsNotNone(re_ana)
        self.assertEqual(re_item.title, "利好")
        self.assertAlmostEqual(re_ana.score, 0.8)
        self.assertAlmostEqual(re_ana.confidence, 0.9)
        self.assertEqual(re_ana.direction, "bullish")

    def test_news_payload_without_analysis(self):
        item = NewsItem(title="中性消息", symbols=["000001"], source="t")
        self.pub.publish("news", {"news": item.to_dict(), "analysis": None}, kind="news")
        rec = self.sub.poll("news")[0]
        re_item, re_ana = news_from_dict(rec["payload"])
        self.assertIsNotNone(re_item)
        self.assertIsNone(re_ana)

    def test_bad_payload_returns_none(self):
        self.assertIsNone(bar_from_dict("not a dict"))
        self.assertEqual(news_from_dict("not a dict"), (None, None))


if __name__ == "__main__":
    unittest.main()
