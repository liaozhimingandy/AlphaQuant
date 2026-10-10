#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户自定义策略发现 / 声明式策略 / 回测默认参数 的单元测试。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.backtest.defaults import BacktestDefaults
from app.core.strategy.discovery import snake, strategy_key
from app.test.helpers import TempWorkspaceMixin

BT_SAMPLE = '''
import backtrader as bt

class MyAlphaStrategy(bt.Strategy):
    STRATEGY_NAME = "my_alpha"
    params = dict(fast=3, slow=9)
    def next(self):
        pass

class UnnamedStrategy(bt.Strategy):
    def next(self):
        pass
'''

ENGINE_SAMPLE = '''
from app.core.strategy.base import IBaseStrategy

class MyLiveStrategy(IBaseStrategy):
    STRATEGY_NAME = "my_live"
    name = "my_live"
    def decide(self, ctx, state, source=None, event=None):
        return None
'''

BROKEN_SAMPLE = '''
this is not valid python !!!
'''


class TestNaming(unittest.TestCase):
    def test_snake(self):
        self.assertEqual(snake("DualMaStrategy"), "dual_ma_strategy")
        self.assertEqual(snake("MyAlpha"), "my_alpha")

    def test_explicit_name_wins(self):
        class A:
            STRATEGY_NAME = "Custom-Name"
        self.assertEqual(strategy_key(A), "custom-name")

    def test_derived_from_classname(self):
        class MyAlphaStrategy:
            pass
        self.assertEqual(strategy_key(MyAlphaStrategy), "my_alpha")

    def test_strategy_name_attr_also_accepted(self):
        class B:
            strategy_name = "lower"
        self.assertEqual(strategy_key(B), "lower")


class TestDiscovery(TempWorkspaceMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.dir = self.workdir()
        # 每个用例一个空目录，避免互相干扰
        for f in self.dir.glob("*.py"):
            f.unlink()
        self._added: set = set()

    def tearDown(self) -> None:
        # 发现流程会往**全局注册表**里塞东西。测试用完必须摘干净，
        # 否则后面的用例（比如"跑一遍所有内置策略"）会莫名其妙多跑几个
        # 只存在于临时文件里的策略。临时文件早被删了，类对象却还在内存里。
        from app.backtest import registry as bt_reg

        for name in self._added:
            bt_reg._REGISTRY.pop(name, None)
            bt_reg._USER_NAMES.discard(name)
        if self._added:
            bt_reg._LOADED_DIRS.discard(tuple(str(d) for d in (self.dir,)))

    def _write(self, name: str, content: str) -> Path:
        p = self.dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def _load(self, *extra_dirs):
        from app.backtest.registry import load_user_strategies

        dirs = [str(self.dir), *[str(d) for d in extra_dirs]]
        found = load_user_strategies(dirs, force=True)
        self._added.update(found)
        return found

    def test_discovers_backtest_strategy(self):
        self._write("alpha.py", BT_SAMPLE)
        found = self._load()
        self.assertIn("my_alpha", found)
        # 没写 STRATEGY_NAME 的按类名推导，去掉 Strategy 后缀
        self.assertIn("unnamed", found)

    def test_discovers_engine_strategy(self):
        from app.core.strategy.registry import load_user_strategies as live_load

        self._write("live.py", ENGINE_SAMPLE)
        found = live_load([str(self.dir)], force=True, register=False)
        self._added.update(found)
        self.assertIn("my_live", found)

    def test_same_file_registers_both_chains(self):
        """一个文件里可以同时有回测策略与实盘策略 —— 这是刻意的用法。"""
        from app.core.strategy.registry import load_user_strategies as live_load

        self._write("both.py", BT_SAMPLE + ENGINE_SAMPLE)
        bt_found = self._load()
        live_found = live_load([str(self.dir)], force=True, register=False)
        self._added.update(live_found)
        self.assertIn("my_alpha", bt_found)
        self.assertIn("my_live", live_found)
        # 各自只挑自己认识的，不串台
        self.assertNotIn("my_live", bt_found)
        self.assertNotIn("my_alpha", live_found)

    def test_broken_file_is_skipped_not_fatal(self):
        """一个写坏的策略文件不该让整个功能不可用。"""
        self._write("bad.py", BROKEN_SAMPLE)
        self._write("good.py", BT_SAMPLE)
        found = self._load()
        self.assertIn("my_alpha", found)

    def test_missing_dir_is_fine(self):
        from app.backtest.registry import load_user_strategies

        found = load_user_strategies([str(self.dir / "nope")], force=True)
        self.assertEqual(found, {})
        self._added.update(found)

    def test_same_filename_in_two_dirs_does_not_collide(self):
        """不同目录下的同名文件必须互不覆盖，否则排查起来莫名其妙。"""
        d2 = self.workdir("second")
        self._write("alpha.py", BT_SAMPLE)
        (d2 / "alpha.py").write_text(
            BT_SAMPLE.replace("my_alpha", "other_alpha"), encoding="utf-8")
        found = self._load(d2)
        self.assertIn("my_alpha", found)
        self.assertIn("other_alpha", found)

    def test_user_load_first_does_not_hide_builtins(self):
        """**先加载用户策略**时，内置策略必须还在。

        回归测试：`_load()` 曾用 `if _REGISTRY: return` 做守卫，
        于是"先加载用户策略"会让 `_REGISTRY` 提前非空 → 内置策略永远补不上，
        用户执行 `main.py strategies` 会发现框架自带的策略全没了。
        """
        from app.backtest import registry as R

        saved = dict(R._REGISTRY)
        saved_dirs = set(R._LOADED_DIRS)
        saved_builtin = R._BUILTIN_LOADED
        try:
            R._REGISTRY.clear()
            R._LOADED_DIRS.clear()
            R._BUILTIN_LOADED = False
            self._write("alpha.py", BT_SAMPLE)
            R.load_user_strategies([str(self.dir)], force=True)
            names = R.list_strategies()
            for builtin in ("ma_cross", "precise_ma_cross", "trend_ma_cross", "declarative"):
                self.assertIn(builtin, names, f"内置策略 {builtin} 被用户策略挤掉了")
            self.assertIn("my_alpha", names)
        finally:
            R._REGISTRY.clear()
            R._REGISTRY.update(saved)
            R._LOADED_DIRS.clear()
            R._LOADED_DIRS.update(saved_dirs)
            R._BUILTIN_LOADED = saved_builtin


class TestBacktestDefaults(TempWorkspaceMixin, unittest.TestCase):
    def test_internal_defaults_when_file_missing(self):
        d = BacktestDefaults.load(str(self.workdir() / "missing.json"))
        self.assertGreater(d.cash, 0)
        self.assertTrue(d.strategy)
        self.assertTrue(d.resolved_end())

    def test_load_from_file(self):
        p = self.workdir() / "backtest.json"
        p.write_text(json.dumps({
            "symbol": "600000", "strategy": "dual_ma", "cash": 50000,
            "commission": 0.0002, "slippage": 0.002, "start": "2021-01-01",
            "end": "2022-01-01", "adjust": "hfq", "data_source": "db",
        }), encoding="utf-8")
        d = BacktestDefaults.load(str(p))
        self.assertEqual(d.symbol, "600000")
        self.assertEqual(d.strategy, "dual_ma")
        self.assertEqual(d.cash, 50000)
        self.assertEqual(d.adjust, "hfq")
        self.assertEqual(d.resolved_end(), "2022-01-01")

    def test_explicit_params_win(self):
        d = BacktestDefaults(cash=100000, strategy="ma_cross", symbol="000001")
        merged = d.merge({"cash": 777, "symbol": "600000"})
        self.assertEqual(merged["cash"], 777)
        self.assertEqual(merged["symbol"], "600000")
        self.assertEqual(merged["strategy"], "ma_cross")   # 没传就保默认

    def test_empty_values_do_not_override(self):
        """空值不该覆盖默认 —— 面板表单里没填的字段就是这样传上来的。"""
        d = BacktestDefaults(cash=100000, symbol="000001")
        merged = d.merge({"cash": None, "symbol": "   ", "start": ""})
        self.assertEqual(merged["cash"], 100000)
        self.assertEqual(merged["symbol"], "000001")
        self.assertTrue(merged["start"])

    def test_broken_file_falls_back(self):
        p = self.workdir() / "bad.json"
        p.write_text("[]", encoding="utf-8")
        d = BacktestDefaults.load(str(p))
        self.assertGreater(d.cash, 0)


class TestBacktestNormalize(TempWorkspaceMixin, unittest.TestCase):
    """回测作业入参归一化：三处入口共用一份默认值。"""

    def test_string_params_parsed(self):
        from app.core.monitor.backtest_api import BacktestService

        out = BacktestService._normalize({
            "symbol": "000001", "params": "fast=5 slow=20 flag=true ratio=0.3",
        })
        self.assertEqual(out["params"], {"fast": 5, "slow": 20,
                                        "flag": True, "ratio": 0.3})
        self.assertTrue(out["end"])

    def test_spec_moved_into_strategy_params(self):
        """面板把规则提到顶层更顺手，后端要收敛成策略参数。"""
        from app.core.monitor.backtest_api import BacktestService

        spec = {"entry": {"cross_up": {"left": "ma", "right": "ma"}}}
        out = BacktestService._normalize({
            "symbol": "000001", "strategy": "declarative", "spec": spec,
        })
        self.assertEqual(out["params"]["spec"], spec)

    def test_defaults_applied_when_missing(self):
        from app.core.monitor.backtest_api import BacktestService

        out = BacktestService._normalize({"symbol": "000001"})
        self.assertGreater(out["cash"], 0)
        self.assertTrue(out["strategy"])
        self.assertEqual(out["adjust"], "qfq")


class TestDeclarativeSpec(unittest.TestCase):
    def test_json_string_spec(self):
        from app.strategy.spec_strategy import _as_spec

        spec = _as_spec('{"entry": {"always": true}}')
        self.assertEqual(spec, {"entry": {"always": True}})

    def test_dict_spec_passthrough(self):
        from app.strategy.spec_strategy import _as_spec

        raw = {"exit": {"never": True}}
        self.assertEqual(_as_spec(raw), raw)

    def test_empty_returns_none(self):
        from app.strategy.spec_strategy import _as_spec

        self.assertIsNone(_as_spec(None))
        self.assertIsNone(_as_spec(""))

    def test_bad_spec_raises_with_context(self):
        from app.strategy.spec_strategy import _as_spec

        with self.assertRaises(ValueError):
            _as_spec("{not json and not a path/that/exists}")

    def test_default_spec_is_buildable(self):
        """默认规则必须真能构造出来，否则"不传规则就跑"会当场炸。"""
        from app.core.rule.spec import build_rule
        from app.strategy.spec_strategy import DEFAULT_SPEC

        build_rule(DEFAULT_SPEC["entry"])
        build_rule(DEFAULT_SPEC["exit"])

    def test_declarative_registered(self):
        from app.backtest.registry import get_strategy, list_strategies

        self.assertIn("declarative", list_strategies())
        self.assertEqual(get_strategy("declarative").STRATEGY_NAME, "declarative")


if __name__ == "__main__":
    unittest.main()
