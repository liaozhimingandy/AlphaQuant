#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_data_categories.py
# @Description : 多类别数据：通用因子值表 / 舆情 / 宏观 / 市场宽度 / 策略库
#
#                这一组守的是"数据维度能不能扩展"：
#                  新增一类数据应该是**加一个函数**，
#                  而不是建表 + 写仓储 + 写查询 + 改特征层。
#                如果这套测试过了但加类别仍需改四处，那设计就失败了。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta

from app.common.config import settings
from app.tests.helpers import TempWorkspaceMixin


class RepoTestCase(TempWorkspaceMixin, unittest.TestCase):
    isolated_settings = ("RUNTIME_DIR", "SNAPSHOT_DIR")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import app.storage.schema  # noqa: F401

        from app.storage.engine import Base, engine

        Base.metadata.create_all(bind=engine)


class TestFactorValueStore(RepoTestCase):
    """通用因子值表：新增类别不改表结构。"""

    SYM = "910001"

    def setUp(self) -> None:
        from app.storage.repo import kline_repo as R

        self.R = R
        self.day = date(2026, 3, 10)

    def test_save_and_read(self):
        n = self.R.save_factor_values([
            {"symbol": self.SYM, "date": self.day, "category": "lhb",
             "name": "lhb_net_buy", "value": 1.5e8},
            {"symbol": self.SYM, "date": self.day, "category": "margin",
             "name": "margin_balance", "value": 2.0e9},
        ])
        self.assertEqual(n, 2)
        got = self.R.factor_values(self.SYM, self.day)
        self.assertAlmostEqual(got["lhb_net_buy"], 1.5e8)
        self.assertAlmostEqual(got["margin_balance"], 2.0e9)

    def test_idempotent_upsert(self):
        """同一天重复采集必须覆盖，而不是追加 —— 否则回看会得到矛盾的行。"""
        row = {"symbol": self.SYM, "date": self.day, "category": "lhb",
               "name": "lhb_net_buy", "value": 1.0}
        self.R.save_factor_values(row)
        row["value"] = 9.9
        self.R.save_factor_values(row)
        self.assertAlmostEqual(
            self.R.factor_values(self.SYM, self.day)["lhb_net_buy"], 9.9)

    def test_reads_latest_not_exact_day(self):
        """股东户数这类更新很慢，按天精确匹配会永远查不到。"""
        self.R.save_factor_values([
            {"symbol": self.SYM, "date": self.day - timedelta(days=40),
             "category": "holders", "name": "holder_count", "value": 50000},
        ])
        got = self.R.factor_values(self.SYM, self.day)
        self.assertAlmostEqual(got["holder_count"], 50000.0)

    def test_filter_by_category(self):
        # 换一个专属标的：同一张表是多个用例共享的，
        # 用同一个 symbol 会读到上一个用例写进去的行
        sym = "910001_filter"
        self.R.save_factor_values([
            {"symbol": sym, "date": self.day, "category": "lhb",
             "name": "a", "value": 1},
            {"symbol": sym, "date": self.day, "category": "margin",
             "name": "b", "value": 2},
        ])
        self.assertEqual(set(self.R.factor_values(sym, self.day, "lhb")), {"a"})
        self.assertEqual(set(self.R.factor_values(sym, self.day, "margin")), {"b"})

    def test_categories_listing(self):
        """界面要能列出"现在库里有什么数据"。"""
        self.R.save_factor_values([
            {"symbol": self.SYM, "date": self.day, "category": "forecast",
             "name": "forecast_yoy", "value": 30},
        ])
        cats = {(c["category"], c["name"]) for c in self.R.factor_categories()}
        self.assertIn(("forecast", "forecast_yoy"), cats)

    def test_extra_json_for_non_numeric(self):
        self.R.save_factor_values([
            {"symbol": self.SYM, "date": self.day, "category": "forecast",
             "name": "forecast_yoy", "value": 45,
             "extra": {"type": "预增"}},
        ])
        self.assertEqual(self.R.factor_values(self.SYM, self.day)["forecast_yoy"],
                         45.0)

    def test_invalid_rows_skipped(self):
        n = self.R.save_factor_values([
            {"date": self.day, "category": "lhb", "name": "x"},   # 缺 symbol
            {"symbol": self.SYM, "category": "lhb", "name": "y"},  # 缺 date
        ])
        self.assertEqual(n, 0)


class TestNewsStore(RepoTestCase):
    SYM = "910002"

    def test_dedup_and_stats(self):
        from app.storage.repo import kline_repo as R

        day = date(2026, 3, 10)
        R.save_news([
            {"symbol": self.SYM, "date": day, "title": "业绩大增",
             "kind": "news", "sentiment": 0.8, "heat": 100},
            {"symbol": self.SYM, "date": day, "title": "业绩大增",
             "kind": "news", "sentiment": 0.8, "heat": 100},   # 重复
            {"symbol": self.SYM, "date": day, "title": "被问询",
             "kind": "notice", "sentiment": -0.6},
        ])
        st = R.news_stats(self.SYM, day, days=10)
        self.assertEqual(st["count"], 2)            # 重复那条被合并
        self.assertAlmostEqual(st["avg_sentiment"], 0.1, places=5)
        self.assertAlmostEqual(st["positive_ratio"], 0.5)

    def test_sentiment_keywords(self):
        """情感规则是粗糙的离线兜底 —— 只要求方向正确，不要求精确。"""
        from app.datasource.service.facade import _sentiment

        self.assertGreater(_sentiment("公司业绩超预期，中标大单"), 0)
        self.assertLess(_sentiment("涉嫌违规被立案调查，业绩大幅下滑"), 0)
        self.assertEqual(_sentiment("今天天气不错"), 0.0)


class TestMacroAndBreadth(RepoTestCase):
    def test_macro_save_and_latest(self):
        from app.storage.repo import kline_repo as R

        # 显式传日期：库里可能已采到更新的真实宏观数据，
        # 不指定日期会读到那一行，测试就变成"看运气"
        day = date(2026, 2, 28)
        R.save_macro([
            {"date": day, "name": "pmi", "value": 49.4, "period": "month"},
            {"date": date(2026, 1, 31), "name": "pmi", "value": 49.1,
             "period": "month"},
        ])
        self.assertAlmostEqual(R.latest_macro(day)["pmi"], 49.4)

    def test_breadth_save_and_latest(self):
        from app.storage.repo import kline_repo as R

        day = date(2026, 3, 9)
        R.save_breadth([
            {"date": day, "up_count": 3000, "down_count": 1500,
             "limit_up_count": 60, "sentiment": 55.0},
        ])
        b = R.latest_breadth(day)
        self.assertEqual(b["up_count"], 3000)
        self.assertAlmostEqual(b["sentiment"], 55.0)

    def test_breadth_idempotent(self):
        """同一天重复采集必须覆盖 —— 否则"今天情绪是多少"会有两个答案。"""
        from app.storage.repo import kline_repo as R

        d = date(2026, 3, 9)
        R.save_breadth([{"date": d, "up_count": 100, "down_count": 50}])
        R.save_breadth([{"date": d, "up_count": 3000, "down_count": 1500}])
        self.assertEqual(R.latest_breadth(d)["up_count"], 3000)


class TestFeatureAssembly(RepoTestCase):
    """新类别的数据要真的流到特征里，而不是躺在表里。"""

    SYM = "910003"

    def setUp(self) -> None:
        from app.storage.repo import kline_repo as R

        self.day = date(2026, 3, 10)
        R.save_factor_values([
            {"symbol": self.SYM, "date": self.day, "category": "lhb",
             "name": "lhb_net_buy", "value": 1.2e8},
            {"symbol": self.SYM, "date": self.day, "category": "margin",
             "name": "margin_balance", "value": 3.4e9},
            {"symbol": self.SYM, "date": self.day, "category": "holders",
             "name": "holder_change_pct", "value": -8.5},
        ], category="misc")
        R.save_news([
            {"symbol": self.SYM, "date": self.day, "title": "业绩超预期",
             "kind": "news", "sentiment": 0.7},
        ])
        R.save_macro([{"date": self.day, "name": "pmi", "value": 51.2}])
        R.save_breadth([{"date": self.day, "up_count": 2600,
                         "down_count": 1800, "sentiment": 30.0}])

    def test_features_filled(self):
        from app.feature.features import get_provider

        f = get_provider(ttl=0).get(self.SYM, self.day)
        self.assertAlmostEqual(f.lhb_net_buy, 1.2e8)
        self.assertAlmostEqual(f.margin_balance, 3.4e9)
        self.assertAlmostEqual(f.holder_change_pct, -8.5)
        self.assertAlmostEqual(f.news_sentiment, 0.7)
        self.assertAlmostEqual(f.macro_pmi, 51.2)
        self.assertAlmostEqual(f.breadth_sentiment, 30.0)

    def test_available_flags(self):
        from app.feature.features import get_provider

        f = get_provider(ttl=0).get(self.SYM, self.day)
        self.assertTrue(f.available["sentiment"])
        self.assertTrue(f.available["macro"])

    def test_market_context_cached(self):
        """宏观对所有票同值，不该每只票都查一次库。"""
        from app.feature.features import FeatureProvider

        p = FeatureProvider(ttl=0)
        p._market_context(self.day)
        first = getattr(p, "_market_cache", None)
        self.assertIsNotNone(first)
        second = p._market_context(self.day)
        self.assertIs(second, first[1])    # 命中缓存，返回同一对象


class TestNewScoreDims(unittest.TestCase):
    """舆情与宏观要真的参与打分。"""

    def _feat(self, **kw):
        from app.feature.features import StockFeatures

        return StockFeatures(symbol="000001", **kw)

    def _engine(self):
        from app.alpha.score import ScoreConfig, ScoreEngine

        return ScoreEngine(config=ScoreConfig.load())

    def test_sentiment_dimension(self):
        e = self._engine()
        good, ok, why = e._sentiment(
            self._feat(news_sentiment=0.8, news_positive_ratio=0.9,
                       news_count=8.0), e.config)
        self.assertTrue(ok)
        self.assertGreater(good, 70)
        bad, ok2, _ = e._sentiment(
            self._feat(news_sentiment=-0.8, news_positive_ratio=0.1), e.config)
        self.assertTrue(ok2)
        self.assertLess(bad, 30)

    def test_sentiment_unavailable_without_scores(self):
        """有新闻但一条都没分析过情感 ≠ 情感中性。"""
        e = self._engine()
        v, ok, _ = e._sentiment(self._feat(news_count=5.0), e.config)
        self.assertFalse(ok)
        self.assertIsNone(v)

    def test_macro_dimension(self):
        e = self._engine()
        v, ok, why = e._macro(
            self._feat(macro_pmi=53.0, macro_m2=0.09, macro_lpr=3.1,
                       breadth_sentiment=40.0), e.config)
        self.assertTrue(ok)
        self.assertGreater(v, 60)
        low, _, _ = e._macro(self._feat(macro_pmi=45.0,
                                        breadth_sentiment=-60.0), e.config)
        self.assertLess(low, 35)

    def test_macro_is_market_level_not_stock_specific(self):
        """宏观不参与票与票之间的排序 —— 它对所有票都一样。"""
        e = self._engine()
        a, _, _ = e._macro(self._feat(macro_pmi=51.0), e.config)
        b, _, _ = e._macro(self._feat(macro_pmi=51.0), e.config)
        self.assertEqual(a, b)

    def test_weights_include_new_dims(self):
        from app.alpha.score import ScoreConfig

        w = ScoreConfig.load().weights
        for k in ("sentiment", "macro", "fundamental", "capital"):
            self.assertIn(k, w)
        self.assertLess(w["macro"], w["capital"])   # 宏观权重刻意偏低


class TestStrategyStore(TempWorkspaceMixin, unittest.TestCase):
    """界面自定义策略：保存、校验、转任务结构。"""

    isolated_settings = ("RUNTIME_DIR", "SNAPSHOT_DIR")

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from app.strategy.store import StrategyStore

        self.store = StrategyStore(
            path=str(Path(tempfile.mkdtemp(prefix="aq-stg-")) / "strategies.json"))

    def test_save_and_list(self):
        ok, msg, s = self.store.add(
            "my_rule", "declarative",
            {"entry": {"factor": "composite_score", "op": "gt", "value": 70}})
        self.assertTrue(ok, msg)
        self.assertEqual([x.name for x in self.store.list()], ["my_rule"])

    def test_rejects_bad_name(self):
        ok, msg, _ = self.store.add("1bad", "declarative", {})
        self.assertFalse(ok)

    def test_rejects_unknown_kind(self):
        ok, msg, _ = self.store.add("x", "no_such_kind", {})
        self.assertFalse(ok)
        self.assertIn("未知策略类型", msg)

    def test_rejects_empty_rule(self):
        ok, msg, _ = self.store.add("x", "declarative", {})
        self.assertFalse(ok)
        self.assertIn("至少", msg)

    def test_rejects_inverted_score_thresholds(self):
        ok, msg, _ = self.store.add("x", "score",
                                    {"entry_score": 50, "exit_score": 80})
        self.assertFalse(ok)
        self.assertIn("刚买就卖", msg)

    def test_to_task_strategy_declarative(self):
        _, _, s = self.store.add(
            "r1", "declarative",
            {"entry": {"factor": "mom_20", "op": "gt", "value": 3},
             "exit": {"factor": "mom_20", "op": "lt", "value": -2}},
            risk=[{"type": "max_drawdown", "max_drawdown": 0.15}])
        got = s.to_task_strategy()
        self.assertEqual(got["type"], "declarative")
        self.assertEqual(got["params"]["entry"]["factor"], "mom_20")
        self.assertEqual(s.risk[0]["type"], "max_drawdown")

    def test_to_task_strategy_score(self):
        _, _, s = self.store.add("r2", "score",
                                 {"entry_score": 72, "exit_score": 58})
        got = s.to_task_strategy()
        self.assertEqual(got["type"], "score")
        self.assertAlmostEqual(got["params"]["entry_score"], 72.0)

    def test_to_task_strategy_multi(self):
        _, _, s = self.store.add(
            "r3", "multi_factor",
            {"factors": [{"name": "mom_20", "weight": 1.0}],
             "entry": 0.6, "exit": -0.4})
        got = s.to_task_strategy()
        self.assertEqual(got["type"], "multi_factor")
        self.assertEqual(got["params"]["factors"][0]["name"], "mom_20")

    def test_persists(self):
        from app.strategy.store import StrategyStore

        self.store.add("p1", "score", {"entry_score": 70, "exit_score": 55})
        again = StrategyStore(path=str(self.store.path)).load(force=True)
        self.assertIsNotNone(again.get("p1"))

    def test_remove(self):
        self.store.add("gone", "score", {"entry_score": 70, "exit_score": 55})
        ok, _ = self.store.remove("gone")
        self.assertTrue(ok)
        ok, _ = self.store.remove("gone")
        self.assertFalse(ok)


class TestFactorCatalog(unittest.TestCase):
    """因子总目录：要能按类别展示**全部**因子，而不只是因子库里的。"""

    def test_shows_more_than_library(self):
        """面板曾经只显示 8 条预置因子，其余几十个等于不存在。"""
        from app.feature.catalog import factor_catalog

        cat = factor_catalog(with_data=False)
        self.assertGreater(cat["total"], 30)
        keys = {g["key"] for g in cat["categories"]}
        for must in ("量价技术", "表达式因子", "资金类", "基本面", "舆情", "宏观市场"):
            self.assertIn(must, keys, f"缺少类别 {must}")

    def test_no_duplicate_names(self):
        from app.feature.catalog import factor_catalog

        names = [f["name"] for g in factor_catalog(with_data=False)["categories"]
                 for f in g["factors"]]
        self.assertEqual(len(names), len(set(names)))

    def test_price_factors_are_marked_usable(self):
        """量价技术类只要有K线就能算，不该被标成"无数据"。"""
        from app.feature.catalog import factor_catalog

        cat = factor_catalog()
        price = next(g for g in cat["categories"] if g["key"] == "量价技术")
        self.assertTrue(all(f["has_data"] for f in price["factors"]))

    def test_expression_factors_grouped_together(self):
        """预置因子被子分类拆散成 8 个单条分组会比不分组更难看。"""
        from app.feature.catalog import factor_catalog

        cat = factor_catalog(with_data=False)
        expr = next(g for g in cat["categories"] if g["key"] == "表达式因子")
        self.assertGreaterEqual(len(expr["factors"]), 8)
        self.assertTrue(all(f["sub"] for f in expr["factors"]))

    def test_flat_names_for_dropdown(self):
        from app.feature.catalog import flat_names

        names = flat_names()
        self.assertGreater(len(names), 30)
        self.assertIn("mom_20", names)


class TestCollectionSchedule(unittest.TestCase):
    """采集时间窗口：默认盘前 30 分钟 / 闭市后 30 分钟，且可配置。"""

    def _sch(self, **kw):
        from app.engine.collect.schedule import CollectionSchedule

        return CollectionSchedule(**kw)

    def test_defaults(self):
        s = self._sch()
        self.assertEqual(s.pre_open_time.strftime("%H:%M"), "09:00")
        self.assertEqual(s.post_close_time.strftime("%H:%M"), "15:30")

    def test_pre_open_window(self):
        s = self._sch()
        d = datetime(2026, 10, 9, 9, 15)
        self.assertEqual(s.slot_of(d), "pre")
        self.assertEqual(s.slot_of(datetime(2026, 10, 9, 8, 30)), None)

    def test_post_close_window(self):
        s = self._sch()
        # 15:10 还没到结算时间 —— 拿到的是未定稿数据，所以不采
        self.assertIsNone(s.slot_of(datetime(2026, 10, 9, 15, 10)))
        self.assertEqual(s.slot_of(datetime(2026, 10, 9, 15, 30)), "post")
        self.assertEqual(s.slot_of(datetime(2026, 10, 9, 16, 30)), "post")

    def test_grace_window_ends(self):
        """盘后窗口不能无限长 —— 凌晨还在以为自己在采数据就很荒唐。"""
        s = self._sch(grace_minutes=120)
        self.assertEqual(s.slot_of(datetime(2026, 10, 9, 17, 0)), "post")
        self.assertIsNone(s.slot_of(datetime(2026, 10, 9, 18, 0)))

    def test_no_collection_on_non_trading_day(self):
        s = self._sch()
        # 2026-10-10 是周六
        self.assertIsNone(s.slot_of(datetime(2026, 10, 10, 9, 15)))
        self.assertIsNone(s.slot_of(datetime(2026, 10, 10, 15, 30)))

    def test_intraday_disabled_by_default(self):
        """日频截面数据盘中反复拉是浪费 —— 默认必须关。"""
        s = self._sch()
        self.assertFalse(s.intraday_enabled)
        self.assertIsNone(s.slot_of(datetime(2026, 10, 9, 10, 0)))
        # 显式打开才采
        self.assertEqual(self._sch(intraday_enabled=True).slot_of(
            datetime(2026, 10, 9, 10, 0)), "intraday")

    def test_configurable(self):
        s = self._sch(post_close_minutes=60)
        self.assertEqual(s.post_close_time.strftime("%H:%M"), "16:00")
        self.assertIsNone(s.slot_of(datetime(2026, 10, 9, 15, 45)))

    def test_roundtrip_persist(self):
        import tempfile
        from pathlib import Path

        from app.engine.collect.schedule import load_schedule, save_schedule

        f = Path(tempfile.mkdtemp()) / "collector.json"
        f.write_text('{"enabled": true, "interval": "5m"}', encoding="utf-8")
        save_schedule(self._sch(post_close_minutes=45), path=str(f))
        got = load_schedule(str(f))
        self.assertEqual(got.post_close_minutes, 45)
        # 其它字段必须保留 —— 顺手把别人的配置冲掉是最招人恨的 bug
        raw = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(raw["interval"], "5m")
        self.assertTrue(raw["enabled"])


class TestStrategyCombination(unittest.TestCase):
    """策略要能组合多个因子（All/Any），而不只是单条规则。"""

    def _store(self):
        import tempfile
        from pathlib import Path

        from app.strategy.store import StrategyStore

        return StrategyStore(path=str(Path(tempfile.mkdtemp()) / "s.json"))

    def test_single_rule(self):
        _, _, s = self._store().add(
            "r", "declarative",
            {"entry": {"factor": "mom_20", "op": "gt", "value": 3}})
        self.assertEqual(s.to_task_strategy()["params"]["entry"]["factor"], "mom_20")

    def test_all_combination(self):
        _, _, s = self._store().add("r", "declarative", {
            "entry": {"combine": "all", "rules": [
                {"factor": "mom_20", "op": "gt", "value": 3},
                {"factor": "vol_ratio", "op": "gt", "value": 1.2}]}})
        entry = s.to_task_strategy()["params"]["entry"]
        self.assertIn("all", entry)
        self.assertEqual(len(entry["all"]), 2)

    def test_any_combination(self):
        _, _, s = self._store().add("r", "declarative", {
            "exit": {"combine": "any", "rules": [
                {"factor": "mom_20", "op": "lt", "value": -2},
                {"factor": "news_sentiment", "op": "lt", "value": -0.5}]}})
        exit_ = s.to_task_strategy()["params"]["exit"]
        self.assertIn("any", exit_)

    def test_nested_combination(self):
        """要能表达"资金流好 或者（题材热 且 涨停）"。"""
        _, _, s = self._store().add("r", "declarative", {
            "entry": {"combine": "any", "rules": [
                {"factor": "main_net_pct", "op": "gt", "value": 0.05},
                {"combine": "all", "rules": [
                    {"factor": "theme_rank", "op": "lt", "value": 20},
                    {"factor": "limitup_effect", "op": "gt", "value": 2}]}]}})
        entry = s.to_task_strategy()["params"]["entry"]
        self.assertIn("any", entry)
        self.assertIn("all", entry["any"][1])

    def test_empty_rules_collapse_to_never(self):
        _, _, s = self._store().add("r", "declarative", {
            "entry": {"combine": "all", "rules": [{"op": "gt"}]},   # 缺 factor
            "exit": {"factor": "mom_20", "op": "lt", "value": -2}})
        self.assertEqual(s.to_task_strategy()["params"]["entry"], "never")

    def test_combination_passes_validation(self):
        """组合写法顶层没有 factor —— 只看 factor 会把合法配置误判成空。"""
        ok, msg, _ = self._store().add("r", "declarative", {
            "entry": {"combine": "all", "rules": [
                {"factor": "mom_20", "op": "gt", "value": 3},
                {"factor": "vol_ratio", "op": "gt", "value": 1}]}})
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
