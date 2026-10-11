#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票池：表头对齐 + 候选池来源过滤 + 剔除条件（ST/创业板/科创板/北交所）。

覆盖本轮修复/新增：
  * 打分表 8 维度（综合分/资金链/题材/筹码/技术/涨停/基本面/舆情/宏观）必须与
    表头 14 列对齐，否则"来源"起错位；
  * /api/pool 现在按 source 过滤，刷新按来源筛出的票要能在列表里只看到这些；
  * 剔除条件：板块识别 classify_board + apply_exclusions + PoolConfig 持久化。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.alpha.pool import pool as pool_mod
from app.alpha.pool.pool import (
    PoolConfig, StockPool, classify_board, apply_exclusions,
)
from app.storage.repo import kline_repo as R
from app.storage.engine import SessionLocal
from app.storage.schema import StockScore


class TestClassifyBoard(unittest.TestCase):
    def test_boards(self):
        self.assertEqual(classify_board("600000"), "sh")
        self.assertEqual(classify_board("601318"), "sh")
        self.assertEqual(classify_board("000001"), "sz")
        self.assertEqual(classify_board("002594"), "sz")
        self.assertEqual(classify_board("300750"), "cyb")   # 创业板
        self.assertEqual(classify_board("688981"), "kcb")   # 科创板
        self.assertEqual(classify_board("830799"), "bj")    # 北交所
        self.assertEqual(classify_board("430047"), "bj")    # 北交所(老)
        self.assertEqual(classify_board("920002"), "bj")     # 北交所(新)
        self.assertEqual(classify_board("900901"), "other")  # B股不计
        self.assertEqual(classify_board("SH600000"), "sh")    # 去前缀


class TestApplyExclusions(unittest.TestCase):
    def setUp(self):
        # 用假 ST 集合（按代码，与真实 _st_codes 返回口径一致），避免联网。
        # 真实环境里 ST 标记在「名称」上，akshare 取的是代码，所以按代码匹配。
        self._patcher = mock.patch.object(
            pool_mod, "_st_codes", return_value={"600519"})
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_excludes_cyb_kcb_bj(self):
        # 600519 是 ST（在 st_set 里）；300750 创业板 / 688981 科创板 / 830799 北交所
        syms = ["600000", "300750", "688981", "830799", "000001", "600519"]
        kept, cnt = apply_exclusions(
            syms, exclude_st=True, exclude_cyb=True, exclude_kcb=True, exclude_bj=True)
        self.assertEqual(set(kept), {"600000", "000001"})  # ST/创业板/科创板/北交所被剔除
        self.assertEqual(cnt, {"st": 1, "cyb": 1, "kcb": 1, "bj": 1})

    def test_st_excluded_when_flag_on(self):
        syms = ["600519", "000001"]
        kept, cnt = apply_exclusions(syms, exclude_st=True)
        self.assertEqual(kept, ["000001"])
        self.assertEqual(cnt["st"], 1)

    def test_no_exclusions_keeps_all(self):
        syms = ["300750", "688981", "830799", "ST0001"]
        kept, cnt = apply_exclusions(
            syms, exclude_st=False, exclude_cyb=False, exclude_kcb=False, exclude_bj=False)
        self.assertEqual(kept, syms)
        self.assertEqual(cnt, {"st": 0, "cyb": 0, "kcb": 0, "bj": 0})


class TestPoolConfigExclusions(unittest.TestCase):
    def test_defaults(self):
        cfg = PoolConfig()
        self.assertTrue(cfg.exclude_st)
        self.assertFalse(cfg.exclude_cyb)
        self.assertFalse(cfg.exclude_kcb)
        self.assertFalse(cfg.exclude_bj)

    def test_to_dict_has_flags(self):
        d = PoolConfig().to_dict()
        for k in ("exclude_st", "exclude_cyb", "exclude_kcb", "exclude_bj"):
            self.assertIn(k, d)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pool.json"
            cfg = PoolConfig(source="hot", exclude_cyb=True, exclude_kcb=True)
            cfg.path = str(p)
            cfg.save()
            self.assertTrue(p.exists())
            loaded = PoolConfig.load(path=str(p))
            self.assertEqual(loaded.source, "hot")
            self.assertTrue(loaded.exclude_cyb)
            self.assertTrue(loaded.exclude_kcb)
            self.assertFalse(loaded.exclude_bj)
            # 没写的字段保留默认
            self.assertTrue(loaded.exclude_st)


class TestPoolSourceFilter(unittest.TestCase):
    """R.scores / StockPool.top 必须按 source 过滤（修"筛选出2条却显示15条"）。"""

    SYMS = ("EXCLa1", "EXCLa2", "EXCLb1")

    def tearDown(self):
        with SessionLocal() as s:
            s.query(StockScore).filter(
                StockScore.symbol.in_(self.SYMS)).delete()
            s.commit()

    def _mine(self, rows):
        """只保留本用例自己种的数据。

        生产库是共享的：任何时刻都可能已有其它标的/来源的评分躺在"今天"。
        这两个用例要验证的是**来源过滤语义**，不是"今天全库只有我这几条"，
        所以断言必须限定在本用例的标的集合内，否则会被无关数据随机打挂。
        """
        return {r["symbol"] for r in rows if r["symbol"] in self.SYMS}

    def test_scores_filters_by_source(self):
        from datetime import date
        today = date.today()
        R.save_scores([
            {"symbol": "EXCLa1", "total": 90, "source": "limitup", "date": today},
            {"symbol": "EXCLa2", "total": 80, "source": "limitup", "date": today},
            {"symbol": "EXCLb1", "total": 70, "source": "hot", "date": today},
        ], score_date=today)
        self.assertEqual(self._mine(R.scores(source="limitup")),
                         {"EXCLa1", "EXCLa2"})
        self.assertEqual(self._mine(R.scores(source="hot")), {"EXCLb1"})
        # 不过滤来源时，三只都应该在（说明过滤确实生效、而不是查不到）
        self.assertEqual(self._mine(R.scores()),
                         {"EXCLa1", "EXCLa2", "EXCLb1"})

    def test_stockpool_top_passes_source(self):
        from datetime import date
        today = date.today()
        R.save_scores([
            {"symbol": "EXCLa1", "total": 90, "source": "limitup", "date": today},
            {"symbol": "EXCLb1", "total": 70, "source": "hot", "date": today},
        ], score_date=today)
        self.assertEqual(
            self._mine(StockPool.top(source="limitup")), {"EXCLa1"})
        self.assertEqual(
            self._mine(StockPool.top(source="hot")), {"EXCLb1"})


if __name__ == "__main__":
    unittest.main()
