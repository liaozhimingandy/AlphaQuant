#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_source_weights.py
# @Description : 数据源权重：初始默认 1、不可用降权、恢复回涨、排序生效、可落盘
#
#                为什么要给数据源配权重：
#                  "挂了还一直排第一"是最浪费请求额度的失败模式。
#                  熔断只能挡住"冷却期内不再试"，冷却一过它又回到队首，
#                  于是每批采集的头几个请求又撞一次墙。
#                  权重解决的是后半段：**恢复之后也得排在后面**，
#                  靠连续成功慢慢涨回来。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from app.datasource.compat.registry import (
    DEFAULT_WEIGHT,
    REGISTRY,
    DataUnavailable,
    SourceError,
    SourceRegistry,
)


class _Boom(Exception):
    """源级故障（非瞬时，重试无意义，会走 _penalize 降权）。"""


def _ok_df(n: int = 2) -> pd.DataFrame:
    return pd.DataFrame({"a": range(n)})


class TestWeightBasics(unittest.TestCase):
    """注册时默认权重为 1，可显式指定。"""

    def setUp(self):
        self.reg = SourceRegistry(circuit_threshold=99, cooldown=60.0, retries=0)

    def test_default_weight_is_one(self):
        self.reg.register("a", supports=["k"], fetch=lambda: _ok_df())
        spec = self.reg.sources_for("k")[0]
        self.assertEqual(spec.weight, DEFAULT_WEIGHT)
        self.assertEqual(spec.base_weight, DEFAULT_WEIGHT)

    def test_explicit_weight(self):
        self.reg.register("low", supports=["k"], fetch=lambda: _ok_df(), weight=0.3)
        self.assertEqual(self.reg.sources_for("k")[0].weight, 0.3)
        # 显式指定的权重同时成为基准：恢复不会超过它
        self.assertEqual(self.reg.sources_for("k")[0].base_weight, 0.3)

    def test_all_registered_sources_start_at_one(self):
        """引擎里已注册的所有源，初始权重都必须是 1。

        这是"初始默认都为 1"这条要求的直接断言：
        只要有一个源被写死了别的初值，排序的初始状态就不再等于
        DATA_SOURCE_PRIORITY，配置就失去意义了。
        """
        import app.datasource.service.facade  # noqa: F401  触发注册

        for spec in REGISTRY._specs.values():
            self.assertEqual(spec.base_weight, DEFAULT_WEIGHT,
                             f"数据源 {spec.name} 的初始权重不是 1")

    def test_new_sources_registered_for_daily(self):
        """mootdx 与 a-stock-data 必须注册在 daily 能力上。"""
        import app.datasource.service.facade  # noqa: F401  触发注册

        names = [s.name for s in REGISTRY.sources_for("daily")]
        self.assertIn("mootdx", names)
        self.assertIn("astockdata", names)


class TestWeightDecay(unittest.TestCase):
    """不可用 → 降权；恢复 → 慢慢涨回来。"""

    def setUp(self):
        self.reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0,
                                  weight_decay=0.5, weight_min=0.1,
                                  weight_recover=0.1)
        self.calls: list = []

    def test_failure_decays_weight(self):
        def boom():
            raise _Boom("源挂了")

        self.reg.register("a", supports=["k"], fetch=boom)
        with self.assertRaises(SourceError):
            self.reg.fetch("k")
        self.assertLess(self.reg.sources_for("k")[0].weight, DEFAULT_WEIGHT)

    def test_weight_has_floor(self):
        """权重有下限：降到 0 就等于永久下线，那不如直接 unregister。"""
        reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0,
                             weight_decay=0.01, weight_min=0.1)
        reg.register("a", supports=["k"], fetch=lambda: (_ for _ in ()).throw(_Boom()))
        for _ in range(6):
            try:
                reg.fetch("k")
            except SourceError:
                pass
        self.assertGreaterEqual(reg.sources_for("k")[0].weight, 0.1)

    def test_data_unavailable_does_not_decay(self):
        """数据缺失（停牌/退市）不能降权 —— 源本身是好的。

        反例：连续几只停牌票把健康源降权，后面正常的票全部取不到。
        """
        self.reg.register("a", supports=["k"],
                          fetch=lambda: (_ for _ in ()).throw(DataUnavailable("停牌")))
        with self.assertRaises(SourceError):
            self.reg.fetch("k")
        self.assertEqual(self.reg.sources_for("k")[0].weight, DEFAULT_WEIGHT)

    def test_success_recovers_slowly(self):
        """恢复步长远小于降权幅度：抽风的源要连续成功好几次才回队首。"""
        state = {"fail": True}

        def flaky():
            if state["fail"]:
                raise _Boom("挂")
            return _ok_df()

        self.reg.register("a", supports=["k"], fetch=flaky)
        try:
            self.reg.fetch("k")
        except SourceError:
            pass
        dropped = self.reg.sources_for("k")[0].weight
        self.assertLess(dropped, 1.0)

        state["fail"] = False
        self.reg.fetch("k")
        after_one = self.reg.sources_for("k")[0].weight
        self.assertGreater(after_one, dropped)
        self.assertLess(after_one, DEFAULT_WEIGHT)   # 一次成功不能回到 1

    def test_recovery_never_exceeds_base(self):
        reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0,
                             weight_recover=0.5)
        reg.register("a", supports=["k"], fetch=lambda: _ok_df(), weight=0.8)
        for _ in range(6):
            reg.fetch("k")
        self.assertAlmostEqual(reg.sources_for("k")[0].weight, 0.8, places=6)


class TestWeightOrdering(unittest.TestCase):
    """权重决定尝试顺序；同为 1 时顺序 == 配置优先级。"""

    def setUp(self):
        self.reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0,
                                  weight_decay=0.5, weight_min=0.1)

    def test_higher_weight_tried_first(self):
        order: list = []

        def mk(name):
            def fn():
                order.append(name)
                return _ok_df()
            return fn

        # 注册顺序：a 在先；但 b 权重更高 → 应先试 b
        self.reg.register("a", supports=["k"], fetch=mk("a"), weight=1.0)
        self.reg.register("b", supports=["k"], fetch=mk("b"), weight=2.0)
        self.reg.fetch("k")
        self.assertEqual(order, ["b"])
        self.assertEqual([s.name for s in self.reg.sources_for("k")], ["b", "a"])

    def test_equal_weight_keeps_registration_order(self):
        self.reg.register("a", supports=["k"], fetch=lambda: _ok_df())
        self.reg.register("b", supports=["k"], fetch=lambda: _ok_df())
        self.assertEqual([s.name for s in self.reg.sources_for("k")], ["a", "b"])

    def test_decayed_source_moves_to_the_back(self):
        """降权后必须真的排到后面 —— 这是权重唯一的意义。"""
        state = {"fail": True}

        def flaky():
            if state["fail"]:
                raise _Boom("挂")
            return _ok_df()

        self.reg.register("a", supports=["k"], fetch=flaky)
        self.reg.register("b", supports=["k"], fetch=lambda: _ok_df())
        self.assertEqual([s.name for s in self.reg.sources_for("k")], ["a", "b"])

        try:
            self.reg.fetch("k")
        except SourceError:
            pass
        self.assertEqual([s.name for s in self.reg.sources_for("k")], ["b", "a"])

    def test_set_weight_changes_base(self):
        """显式设权重会同时把基准改掉 —— "它就值 0.2"是人的判断，不是故障。"""
        self.reg.register("a", supports=["k"], fetch=lambda: _ok_df())
        self.reg.set_weight("a", 0.2)
        spec = self.reg.sources_for("k")[0]
        self.assertEqual(spec.weight, 0.2)
        self.assertEqual(spec.base_weight, 0.2)

    def test_reset_weight_restores_base_after_decay(self):
        """降权之后 reset → 回到基准（默认 1），并清掉熔断。"""
        state = {"fail": True}

        def flaky():
            if state["fail"]:
                raise _Boom("挂")
            return _ok_df()

        self.reg.register("a", supports=["k"], fetch=flaky)
        try:
            self.reg.fetch("k")
        except SourceError:
            pass
        spec = self.reg.sources_for("k")[0]
        self.assertLess(spec.weight, DEFAULT_WEIGHT)
        self.assertGreater(spec.fail_count, 0)

        self.reg.reset_weight("a")
        self.assertEqual(spec.weight, DEFAULT_WEIGHT)
        self.assertEqual(spec.fail_count, 0)

    def test_health_exposes_weight(self):
        self.reg.register("a", supports=["k"], fetch=lambda: _ok_df(), weight=1.5)
        entry = self.reg.health()[0]
        self.assertEqual(entry["weight"], 1.5)
        self.assertEqual(entry["base_weight"], 1.5)


class TestWeightPersistence(unittest.TestCase):
    """权重跨重启记忆：昨晚挂了的源，今早别再排第一。"""

    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_persist_and_reload(self):
        path = self.dir / "w.json"
        reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0,
                             persist_path=str(path))

        def boom():
            raise _Boom("挂")

        reg.register("a", supports=["k"], fetch=boom)
        reg.register("b", supports=["k"], fetch=lambda: _ok_df())
        try:
            reg.fetch("k")
        except SourceError:
            pass
        self.assertTrue(path.exists())

        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertLess(saved["weights"]["a"], 1.0)

        # 再建一个注册表 = 模拟进程重启：应读到落盘的权重，b 排到 a 前面
        reg2 = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0,
                              persist_path=str(path))
        reg2.register("a", supports=["k"], fetch=lambda: _ok_df())
        reg2.register("b", supports=["k"], fetch=lambda: _ok_df())
        self.assertEqual([s.name for s in reg2.sources_for("k")], ["b", "a"])
        # 基准仍是 1 —— 那是它应有的地位，恢复时能涨回来
        self.assertEqual(reg2.sources_for("k")[1].base_weight, DEFAULT_WEIGHT)

    def test_corrupted_file_falls_back_to_default(self):
        path = self.dir / "bad.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        reg = SourceRegistry(persist_path=str(path))
        reg.register("a", supports=["k"], fetch=lambda: _ok_df())
        self.assertEqual(reg.sources_for("k")[0].weight, DEFAULT_WEIGHT)

    def test_reset_clears_persisted_decay(self):
        """reset() 之后落盘值也要恢复默认，否则"手动恢复"第二天又回去了。"""
        path = self.dir / "w.json"
        reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0,
                             persist_path=str(path))
        reg.register("a", supports=["k"],
                     fetch=lambda: (_ for _ in ()).throw(_Boom("挂")))
        try:
            reg.fetch("k")
        except SourceError:
            pass
        self.assertLess(reg.sources_for("k")[0].weight, 1.0)
        reg.reset()
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["weights"]["a"], DEFAULT_WEIGHT)


if __name__ == "__main__":
    unittest.main()
