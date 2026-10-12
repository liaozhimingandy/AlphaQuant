#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票池：人气榜/资金榜读库兜底 + 名称列 + 默认来源口径。

覆盖本轮修复：
  * 人气榜(hot)/资金榜(flow) 候选池从「纯实时、拉不到就 0 条且不落库」改成
    「先读库、库空且指定日则联网补拉、再不行用实时兜底」，并落库；
  * 评分列表新增「名称」列：代码↔名称对照表 (stock_info) + scores() 回填；
  * 读取列表时来源为空（前端"（默认）"）回退到配置默认来源，修复
    「刷新说 11 只、表格却 15 只」的来源口径错位。
"""
from datetime import date
from unittest import mock
import unittest

import pandas as pd

from app.monitoring.server import MonitorWebComponent
from app.modules.universe.pool import pool as pool_mod
from app.modules.universe.pool.pool import StockPool
from app.data.engine import SessionLocal
from app.data.schema import StockFlowRank, StockHotRank, StockInfo, StockScore
from app.data.repo import kline_repo as R


DAY = "2026-10-09"


class TestRepositoryHotFlow(unittest.TestCase):
    def tearDown(self):
        with SessionLocal() as s:
            s.query(StockHotRank).filter(StockHotRank.date == date(2026, 10, 9)).delete()
            s.query(StockFlowRank).filter(StockFlowRank.date == date(2026, 10, 9)).delete()
            s.query(StockInfo).filter(StockInfo.symbol.in_(["600000", "000001", "600042"])).delete()
            s.commit()

    def test_hot_pool_reads_db(self):
        df = pd.DataFrame([
            {"date": DAY, "symbol": "600000", "name": "浦发银行", "rank": 1, "pct_change": 1.2},
            {"date": DAY, "symbol": "000001", "name": "平安银行", "rank": 2, "pct_change": -0.5},
        ])
        R.save_hot_rank(df)
        rows = R.hot_pool(DAY)
        self.assertEqual([r["symbol"] for r in rows], ["600000", "000001"])
        self.assertEqual(rows[0]["name"], "浦发银行")

    def test_flow_pool_orders_by_main_net(self):
        df = pd.DataFrame([
            {"date": DAY, "symbol": "600000", "name": "浦发银行", "pct_change": 1.2, "main_net": 100.0, "main_net_pct": 5.0},
            {"date": DAY, "symbol": "000001", "name": "平安银行", "pct_change": -0.5, "main_net": 300.0, "main_net_pct": 8.0},
        ])
        R.save_flow_rank(df)
        rows = R.flow_pool(DAY)
        # 资金流按主力净流入降序：平安银行(300) 应在浦发(100) 之前
        self.assertEqual([r["symbol"] for r in rows], ["000001", "600000"])

    def test_stock_info_resolve(self):
        R.save_stock_info([
            {"symbol": "600000", "name": "浦发银行"},
            {"symbol": "000001", "name": "平安银行"},
        ])
        names = R.resolve_names(["600000", "000001", "600042"])
        self.assertEqual(names.get("600000"), "浦发银行")
        self.assertNotIn("600042", names)  # 查不到的代码不在返回里（前端显示"—"即未知）
        self.assertGreater(R.stock_info_count(), 0)


class TestScoresNameEnrichment(unittest.TestCase):
    def tearDown(self):
        with SessionLocal() as s:
            s.query(StockScore).filter(StockScore.date == date(2026, 10, 9)).delete()
            s.query(StockInfo).filter(StockInfo.symbol.in_(["600000", "600042"])).delete()
            s.commit()

    def test_scores_attaches_name(self):
        R.save_stock_info([{"symbol": "600000", "name": "浦发银行"}])
        with SessionLocal() as s:
            s.add(StockScore(date=date(2026, 10, 9), symbol="600000",
                             total_score=80.0, rank=1, grade="A", source="limitup"))
            s.add(StockScore(date=date(2026, 10, 9), symbol="600042",
                             total_score=50.0, rank=2, grade="C", source="limitup"))
            s.commit()
        rows = R.scores(day=DAY)
        by_sym = {r["symbol"]: r for r in rows}
        self.assertEqual(by_sym["600000"]["name"], "浦发银行")
        self.assertEqual(by_sym["600042"]["name"], "")  # 600042 无名称 → 未知


class TestCandidatesHotFlow(unittest.TestCase):
    """候选池 hot/flow 必须「先读库、库空补拉、再实时兜底」，而不是永远实时。"""

    def test_hot_uses_db_when_present(self):
        pool = StockPool()
        with mock.patch.object(R, "hot_pool", return_value=[{"symbol": "600000", "name": "浦发银行"}]) as m_hot, \
             mock.patch.object(pool, "_live_symbols", return_value=["LIVE"]) as m_live:
            syms, info = pool.candidates("hot", DAY)
        self.assertEqual(syms, ["600000"])
        m_hot.assert_called()
        m_live.assert_not_called()  # 库里有就不该再走实时

    def test_hot_falls_back_to_live_when_db_empty(self):
        pool = StockPool()
        # latest_pool = "库里最近一天已有的榜单"，这里要隔离它才能测到实时兜底
        with mock.patch.object(R, "hot_pool", return_value=[]), \
             mock.patch.object(R, "latest_pool", return_value=[]), \
             mock.patch.object(pool, "_live_symbols", return_value=["LIVE1", "LIVE2"]) as m_live:
            syms, info = pool.candidates("hot", DAY)
        self.assertEqual(syms, ["LIVE1", "LIVE2"])
        self.assertEqual(m_live.call_args[0][0], "hot")

    def test_hot_reuses_last_collected_day_before_network(self):
        """指定日没采过时，应先复用库里最近一天的榜单，而不是急着联网。"""
        pool = StockPool()
        with mock.patch.object(R, "hot_pool", return_value=[]), \
             mock.patch.object(R, "latest_pool",
                               return_value=[{"symbol": "600000", "date": "2026-10-08"}]), \
             mock.patch("app.modules.universe.pool.sync.sync_hot_rank") as m_sync, \
             mock.patch.object(pool, "_live_symbols", return_value=["LIVE"]) as m_live:
            syms, info = pool.candidates("hot", DAY)
        self.assertEqual(syms, ["600000"])
        m_sync.assert_not_called()   # 库里有历史数据 → 不联网
        m_live.assert_not_called()
        self.assertIn("2026-10-08", info.get("note") or "", "要说明复用的是哪天的数据")

    def test_hot_repulls_when_db_empty_then_sync(self):
        pool = StockPool()
        # 第一次读库空、同步后第二次读库返回数据：验证"库空→补拉→再读"链路
        calls = {"n": 0}

        def fake_hot(day, limit=500):
            calls["n"] += 1
            return [] if calls["n"] == 1 else [{"symbol": "600000", "name": "浦发银行"}]

        with mock.patch.object(R, "hot_pool", side_effect=fake_hot), \
             mock.patch.object(R, "latest_pool", return_value=[]), \
             mock.patch("app.modules.universe.pool.sync.sync_hot_rank", return_value={"ok": 1, "failed": []}) as m_sync, \
             mock.patch.object(pool, "_live_symbols", return_value=["LIVE"]) as m_live:
            syms, info = pool.candidates("hot", DAY)
        self.assertEqual(syms, ["600000"])
        m_sync.assert_called_once()       # 库空触发了联网补拉
        m_live.assert_not_called()        # 补拉成功后不必再走实时


class TestPoolTopSourceDefault(unittest.TestCase):
    """读取列表时来源为空 → 回退配置默认来源，使读取与刷新口径一致。"""

    def setUp(self):
        # 造两条 limitup + 三条其它来源，落在同一评分日
        with SessionLocal() as s:
            for sym, src in (("600000", "limitup"), ("000001", "limitup"),
                             ("600100", "other"), ("600200", "other"), ("600300", "other")):
                s.add(StockScore(date=date(2026, 10, 9), symbol=sym,
                                 total_score=70.0, rank=1, grade="B", source=src))
            s.commit()

    def tearDown(self):
        with SessionLocal() as s:
            s.query(StockScore).filter(StockScore.date == date(2026, 10, 9)).delete()
            s.commit()

    def test_empty_source_falls_back_to_config_default(self):
        # 配置默认来源 = limitup：空来源读取应只返回 limitup 的 2 条
        stub_cfg = mock.MagicMock()
        stub_cfg.source = "limitup"
        with mock.patch("app.modules.universe.pool.PoolConfig") as m_cfg:
            m_cfg.load.return_value = stub_cfg
            rows = MonitorWebComponent().pool_top(limit=100, source="", day=DAY)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["source"] == "limitup" for r in rows))

    def test_explicit_source_not_overridden(self):
        stub_cfg = mock.MagicMock()
        stub_cfg.source = "limitup"
        with mock.patch("app.modules.universe.pool.PoolConfig") as m_cfg:
            m_cfg.load.return_value = stub_cfg
            # 显式传 other：即便默认是 limitup，也应返回 other 的 3 条
            rows = MonitorWebComponent().pool_top(limit=100, source="other", day=DAY)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["source"] == "other" for r in rows))


if __name__ == "__main__":
    unittest.main()
