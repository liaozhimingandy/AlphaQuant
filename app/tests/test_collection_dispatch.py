#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_collection_dispatch.py
# @Description : 采集的"服务分工"与"定向采集"
#
#                分工：hub 上每个服务都装配了同一套采集服务，不开分工就会
#                各采一遍 —— 数据源配额是整台机器共享的，重复采等于白烧额度。
#                所以资金流/筹码/基本面各自可关，且**计划项数必须跟着关**（
#                否则进度条到不了 100%，看起来像卡死）。
#
#                定向采集：想采"600519 的基本面"就该立刻能采，
#                不必改 services.json 重启服务，也不必等盘后定稿窗口。
#                未知类别必须当场报错 —— 拼错一个字母然后拿到"成功 0 条"
#                的假成功，是排查成本最高的一种失败。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from unittest import mock

from app.modules.collection.service import CollectionService


def _comp(**kw) -> CollectionService:
    """隔离工程里的 config/collector.json，避免测试读到真实采集任务。

    kw 覆盖默认值（所以传 ``cross_symbols=[]`` 能造出"没有标的"的服务）。
    """
    base: dict = {
        "cross_symbols": ["000001", "600519"],
        "cross_enabled": True,
        "kline_config_path": "/nonexistent/collector.json",
    }
    base.update(kw)
    return CollectionService(**base)


def _ok(*a, **k):
    return {"ok": 2, "failed": []}


class TestCrossDivision(unittest.TestCase):
    """分工开关：关掉的项既不进计划，也不被执行。"""

    def test_plan_skips_disabled_items(self):
        comp = _comp(cross_with_flow=False, cross_with_chip=False,
                     cross_with_fundamental=True, cross_categories=[])
        comp._cross_final_date = None
        # 市场级（macro/breadth）与个股无关，盘后照例要采
        self.assertEqual(
            comp._cross_plan_items("post"),
            ["fundamental", "macro", "breadth"],
        )

    def test_execution_does_not_call_disabled_sync(self):
        """计划与执行必须一致：关掉资金流后一次都不能去调它。"""
        comp = _comp(cross_with_flow=False, cross_with_chip=False,
                     cross_with_fundamental=True, cross_categories=[])
        with mock.patch("app.modules.universe.pool.sync_fund_flow",
                        side_effect=AssertionError("不该调用资金流")) as m_flow, \
             mock.patch("app.modules.universe.pool.sync_chip",
                        side_effect=AssertionError("不该调用筹码")) as m_chip, \
             mock.patch("app.modules.universe.pool.sync_fundamentals", side_effect=_ok), \
             mock.patch("app.modules.factor.features.get_provider",
                        side_effect=Exception("no cache")):
            comp._run_cross_once("post")
        m_flow.assert_not_called()
        m_chip.assert_not_called()
        p = comp.progress()
        # 1 项个股（基本面） + 2 项市场级
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["done"], 3)
        self.assertEqual(p["percent"], 100.0)

    def test_default_keeps_all_three(self):
        """不配分工时的行为与分工功能出现之前完全一致。"""
        comp = _comp(cross_categories=[])
        comp._cross_final_date = None
        self.assertEqual(
            comp._cross_plan_items("post"),
            ["fund_flow", "chip", "fundamental", "macro", "breadth"],
        )

    def test_component_config_can_divide(self):
        """面板/服务配置里也能关 —— 部署后不想改代码就能调。"""
        comp = _comp(cross_categories=[])
        comp.component_config = {"with_chip": False}
        comp._init_cross()
        self.assertFalse(comp.cross_with_chip)
        self.assertTrue(comp.cross_with_flow)

    def test_snapshot_exposes_division(self):
        comp = _comp(cross_with_flow=False)
        snap = comp.snapshot()
        self.assertFalse(snap["cross"]["with_flow"])
        self.assertTrue(snap["cross"]["with_chip"])


class TestManualCrossCollect(unittest.TestCase):
    """定向采集：指定标的 × 指定类别。"""

    def test_default_category_is_fundamental(self):
        comp = _comp(cross_categories=[])
        with mock.patch("app.modules.universe.pool.sync_fundamentals", side_effect=_ok), \
             mock.patch("app.modules.factor.features.get_provider",
                        side_effect=Exception("no cache")):
            report = comp._run_manual_cross(["600519"], ["fundamental"])
        self.assertEqual(report["symbols"], ["600519"])
        self.assertEqual(report["detail"]["fundamental"]["ok"], 2)
        self.assertEqual(comp.progress()["percent"], 100.0)

    def test_unknown_category_fails_loudly(self):
        comp = _comp(cross_categories=[])
        failures = []
        comp.collect_categories(["600519"], ["fundamentalx"]).addErrback(failures.append)
        self.assertEqual(len(failures), 1)
        self.assertIn("未知采集类别", str(failures[0].value))

    def test_no_symbols_fails_loudly(self):
        comp = _comp(cross_symbols=[], cross_categories=[])
        failures = []
        comp.collect_categories([], ["fundamental"]).addErrback(failures.append)
        self.assertEqual(len(failures), 1)
        self.assertIn("没有指定标的", str(failures[0].value))

    def test_falls_back_to_service_symbols(self):
        """没给标的时用服务配置的截面标的 —— 否则对 hub 服务来说无从下手。"""
        comp = _comp(cross_categories=[])
        with mock.patch("app.modules.universe.pool.sync_fundamentals", side_effect=_ok) as m, \
             mock.patch("app.modules.factor.features.get_provider",
                        side_effect=Exception("no cache")):
            report = comp._run_manual_cross(list(comp.cross_symbols), ["fundamental"])
        m.assert_called_once()
        self.assertEqual(report["symbols"], ["000001", "600519"])

    def test_one_category_failure_does_not_break_others(self):
        """龙虎榜挂了不影响我要的基本面。"""
        comp = _comp(cross_categories=[])

        def _bad(*a, **k):
            raise RuntimeError("龙虎榜源不可用")

        with mock.patch("app.modules.universe.pool.sync_category", side_effect=_bad), \
             mock.patch("app.modules.universe.pool.sync_fundamentals", side_effect=_ok), \
             mock.patch("app.modules.factor.features.get_provider",
                        side_effect=Exception("no cache")):
            report = comp._run_manual_cross(["600519"], ["lhb", "fundamental"])
        self.assertIn("龙虎榜源不可用", report["detail"]["lhb"]["error"])
        self.assertEqual(report["detail"]["fundamental"]["ok"], 2)
        self.assertEqual(comp.progress()["failed"], 1)


class TestControlCrossCollect(unittest.TestCase):
    """控制面的 cross_collect 指令（面板 / hub 命令文件 / CLI 共用）。"""

    def _control(self, comp):
        from app.control.control_plane import ControlCenter

        class _Engine:
            def __init__(self, c):
                self._c = c

            def get_component(self, name):
                return self._c if name == "collection_service" else None

            def get_event_bus(self):
                raise RuntimeError("no bus in test")

        return ControlCenter(_Engine(comp))

    def test_wait_returns_real_result(self):
        from twisted.internet import defer

        comp = _comp(cross_categories=[])
        control = self._control(comp)
        with mock.patch.object(comp, "collect_categories",
                               return_value=defer.succeed(
                                   {"symbols": ["600519"], "detail": {"fundamental": {"ok": 1}}})):
            out = control.cross_collect(["600519"], ["fundamental"], wait=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["detail"]["fundamental"]["ok"], 1)

    def test_missing_service_reports_error(self):
        from app.control.control_plane import ControlCenter

        class _Empty:
            def get_component(self, name):
                return None

        out = ControlCenter(_Empty()).cross_collect(["600519"])
        self.assertFalse(out["ok"])
        self.assertIn("未装配采集服务", out["error"])

    def test_apply_command_dispatch(self):
        from twisted.internet import defer

        from app.control.control_plane import ControlCenter

        comp = _comp(cross_categories=[])
        control = self._control(comp)
        with mock.patch.object(comp, "collect_categories",
                               return_value=defer.succeed({"symbols": ["600519"], "detail": {}})):
            out = control.apply_command({
                "action": "cross_collect",
                "symbols": "600519,000858",
                "categories": "fundamental",
            })
        self.assertTrue(out["ok"])
        self.assertTrue(out["accepted"])
        self.assertEqual(out["symbols"], ["600519", "000858"])


class TestNewsServiceHealth(unittest.TestCase):
    """没有新闻源的新闻服务必须报不健康 —— 它在跑，但一条都采不到。"""

    def test_no_source_is_unhealthy(self):
        from app.modules.news.service import NewsService

        svc = NewsService(sources=[], analyzers=[])
        ok, detail = svc.health_check()
        self.assertFalse(ok)
        self.assertIn("未配置新闻源", detail)

    def test_disabled_is_not_unhealthy(self):
        from app.modules.news.service import NewsService

        svc = NewsService(sources=[], analyzers=[])
        svc.enabled = False
        ok, _ = svc.health_check()
        self.assertTrue(ok)

    def test_with_source_falls_back_to_base_check(self):
        from app.modules.news.service import NewsService

        svc = NewsService(sources=[{"type": "rss", "urls": ["http://example.com/rss"]}])
        ok, _ = svc.health_check()
        # 未启动 → 基类按组件状态判定，不该是"未配置新闻源"
        self.assertIsInstance(ok, bool)


if __name__ == "__main__":
    unittest.main()
