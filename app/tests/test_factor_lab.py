#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_factor_lab.py
# @Description : 因子实验室：表达式引擎 / 因子库 / 因子检验
#
#                这一组测试守的是"研究闭环能不能转起来"：
#                  写公式 → 存进库 → 能被引擎算出来 → 能检验出有没有用。
#                任何一环断了，"因子是策略的核心"就只是一句话。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.feature.expr import ExprError, evaluate, func_catalog, validate


def _bars(close: np.ndarray, spread: float = 0.01) -> dict:
    return {
        "open": close * (1 + spread),
        "high": close * (1 + spread * 2),
        "low": close * (1 - spread * 2),
        "close": close,
        "volume": np.full(len(close), 1e6),
        "amount": close * 1e6,
    }


class TestExprEngine(unittest.TestCase):
    def setUp(self) -> None:
        rng = np.random.default_rng(7)
        n = 240
        self.close = 10 + np.cumsum(rng.normal(0, 0.08, n))
        self.data = _bars(self.close)
        self.n = n

    def test_basic_arithmetic(self):
        v = evaluate("close / MA(close, 20) - 1", self.data)
        self.assertEqual(len(v), self.n)
        self.assertFalse(np.isnan(v[-1]))

    def test_rolling_window_has_warmup_nan(self):
        """滚动窗口前 N-1 个必然算不出来 —— 这是**正确**行为，不是 bug。"""
        v = evaluate("MA(close, 20)", self.data)
        self.assertTrue(np.all(np.isnan(v[:19])))
        self.assertFalse(np.isnan(v[19]))

    def test_ref_and_delta(self):
        v = evaluate("close - REF(close, 5)", self.data)
        self.assertAlmostEqual(float(v[-1]),
                               float(self.close[-1] - self.close[-6]), places=8)

    def test_comparison_returns_zero_one(self):
        v = evaluate("(close > MA(close, 10)) * 1", self.data)
        self.assertTrue(set(np.unique(v[~np.isnan(v)])).issubset({0.0, 1.0}))

    def test_if(self):
        v = evaluate("IF(close > MA(close, 10), 1, 0)", self.data)
        self.assertTrue(set(np.unique(v[~np.isnan(v)])).issubset({0.0, 1.0}))

    def test_rank_is_causal(self):
        """RANK 只能看到过去：截断历史后，前面部分的值不应该变。"""
        full = evaluate("RANK(close)", self.data)
        half = {k: v[:120] for k, v in self.data.items()}
        part = evaluate("RANK(close)", half)
        np.testing.assert_allclose(full[:120], part, rtol=1e-12)

    def test_scalar_broadcast(self):
        v = evaluate("close / 100", self.data)
        np.testing.assert_allclose(v, self.close / 100, rtol=1e-12)

    # ---------------- 安全 ----------------
    def test_rejects_unknown_variable(self):
        with self.assertRaises(ExprError):
            evaluate("clse", self.data)

    def test_rejects_unknown_function(self):
        with self.assertRaises(ExprError):
            evaluate("EVIL(close)", self.data)

    def test_rejects_code_injection(self):
        """表达式会来自界面输入框 —— 绝不能有代码执行的口子。"""
        for bad in ("__import__('os')", "close; import os", "exec(1)",
                    "eval('1+1')", "open('x')", "close.__class__"):
            with self.assertRaises(ExprError, msg=bad):
                evaluate(bad, self.data)

    def test_rejects_unbalanced_parens(self):
        with self.assertRaises(ExprError):
            evaluate("MA(close, 5", self.data)

    # ---------------- 校验 ----------------
    def test_validate_accepts_good(self):
        r = validate("MA(close, 5) / MA(close, 20)")
        self.assertTrue(r["ok"])
        self.assertIn("close", r["variables"])
        self.assertEqual(r["max_window"], 20)

    def test_validate_rejects_typo_with_hint(self):
        """手滑写成 closee 是最常见的错 —— 报错要提示"是不是想写 close"。"""
        r = validate("MA(closee, 5)")
        self.assertFalse(r["ok"])
        self.assertIn("close", r["error"])

    def test_validate_reports_syntax_error(self):
        self.assertFalse(validate("MA(close, 5")["ok"])

    def test_catalog_available_for_ui(self):
        """界面要能列出可用算子与变量，用户不该靠猜。"""
        self.assertGreaterEqual(len(func_catalog()), 20)
        names = {f["name"] for f in func_catalog()}
        for must in ("MA", "EMA", "STD", "REF", "RANK", "CORR", "TS_MAX"):
            self.assertIn(must, names)


class TestFactorLibrary(unittest.TestCase):
    """因子库：写了要能存、存了要能注册、删了要能注销。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp(prefix="aq-lib-"))
        from app.feature.library import FactorLibrary

        self.lib = FactorLibrary(path=str(self.dir / "factors.json"))

    def test_add_and_get(self):
        ok, msg, spec = self.lib.add("my_mom", "(close / REF(close, 10) - 1) * 100",
                                     description="10 日动量")
        self.assertTrue(ok, msg)
        self.assertEqual(self.lib.get("my_mom").expr, "(close / REF(close, 10) - 1) * 100")

    def test_rejects_bad_expr(self):
        ok, msg, _ = self.lib.add("bad1", "MA(closee, 5)")
        self.assertFalse(ok)
        self.assertIn("表达式有错", msg)

    def test_rejects_bad_name(self):
        ok, msg, _ = self.lib.add("1abc", "close")
        self.assertFalse(ok)

    def test_no_overwrite_without_flag(self):
        self.lib.add("dup", "close")
        ok, msg, _ = self.lib.add("dup", "open")
        self.assertFalse(ok)
        ok, _, _ = self.lib.add("dup", "open", overwrite=True)
        self.assertTrue(ok)
        self.assertEqual(self.lib.get("dup").expr, "open")

    def test_persists_to_disk(self):
        self.lib.add("persisted", "close / MA(close, 5)")
        from app.feature.library import FactorLibrary

        again = FactorLibrary(path=str(self.dir / "factors.json")).load(force=True)
        self.assertIsNotNone(again.get("persisted"))

    def test_register_unregister(self):
        from app.feature.registry import has_factor

        self.lib.add("reg_me", "close / MA(close, 3)")
        self.assertTrue(has_factor("reg_me"))
        self.lib.remove("reg_me")
        self.assertFalse(has_factor("reg_me"))

    def test_registered_factor_computes(self):
        import app.feature  # noqa: F401  确保注册表已初始化
        from app.feature.context import FactorContext
        from app.feature.registry import create_factor
        from app.domain.series import BarSeries
        from app.domain.types import Bar

        self.lib.add("live_mom", "(close / REF(close, 5) - 1) * 100")
        s = BarSeries("000001", maxlen=500)
        px = 10.0
        rng = np.random.default_rng(3)
        for i in range(40):
            px = float(px + rng.normal(0, 0.1))
            s.append(Bar(symbol="000001", dt=datetime(2026, 1, 1) + timedelta(days=i),
                         open=px, high=px * 1.01, low=px * 0.99, close=px,
                         volume=1e6))
        got = create_factor("live_mom").compute(FactorContext("000001", s))
        self.assertIsNotNone(got)
        self.assertIsInstance(got, float)


class TestFactorEvaluation(unittest.TestCase):
    """检验必须能区分"真有预测力"和"纯噪音"。"""

    def _panel(self, n_symbols: int = 8, n_days: int = 260, seed: int = 11):
        """造一组面板数据，其中**20 日反转**是真实存在的规律。

        逐日模拟而不是"先造价格再造收益" —— 后者很容易写出自相矛盾的数据
        （收益序列和价格序列对不上），最后测的是数据而不是代码。
        """
        rng = np.random.default_rng(seed)
        panel: dict = {}
        dates = pd.bdate_range("2024-01-01", periods=n_days)
        k = 0.35          # 反转强度：过去 20 日跌 10% → 未来 5 日涨约 3.5%
        noise = 0.004
        for s in range(n_symbols):
            px = np.empty(n_days)
            px[0] = 10.0 + rng.uniform(-1, 1)
            for t in range(1, n_days):
                # t 日的收益由 t-1 日的"过去 20 日涨跌"决定 → 因果且可预测
                if t - 1 >= 20:
                    past = px[t - 1] / px[t - 21] - 1.0
                    drift = -past * k / 5.0
                else:
                    drift = 0.0
                px[t] = px[t - 1] * (1.0 + drift + rng.normal(0, noise))
            panel[f"9{s:04d}"] = pd.DataFrame(
                {"open": px, "high": px * 1.01, "low": px * 0.99,
                 "close": px,
                 # 成交量是**独立**的随机数 → 基于它构造的因子应该是纯噪音
                 "volume": rng.uniform(5e5, 2e6, n_days),
                 "amount": px * rng.uniform(5e5, 2e6, n_days)},
                index=dates,
            )
        return panel

    def _matrix(self, expr: str, panel: dict, horizon: int = 5):
        from app.feature.evaluate import build_matrix

        return build_matrix(expr, panel, horizon=horizon)

    def test_detects_real_signal(self):
        """20 日反转因子应当被判定为有效（IC 显著为负、分层单调）。"""
        from app.feature.evaluate import evaluate_matrix, FactorReport

        panel = self._panel()
        m = self._matrix("-(close / REF(close, 20) - 1) * 100", panel)
        rep = evaluate_matrix(m, FactorReport(name="rev20", expr=""), groups=5,
                              total_rows=sum(len(d) for d in panel.values()))
        self.assertIsNotNone(rep.ic_mean)
        self.assertGreater(rep.score or 0, 45)
        # 构造的规律是"跌得多未来涨"，所以反转因子（取负号后）IC 应为正
        self.assertGreater(rep.ic_mean, 0)

    def test_rejects_pure_noise(self):
        """纯噪音因子不该被判成有效 —— 否则挖掘出来的全是垃圾。"""
        from app.feature.evaluate import evaluate_matrix, FactorReport

        panel = self._panel(seed=99)
        # 用成交量构造一个与收益无关的量（成交量是独立生成的随机数）
        m = self._matrix("SUM(volume, 3) / SUM(volume, 10)", panel)
        rep = evaluate_matrix(m, FactorReport(name="noise", expr=""), groups=5,
                              total_rows=sum(len(d) for d in panel.values()))
        self.assertIsNotNone(rep.score)
        self.assertLess(rep.score, 60)

    def test_momentum_factor_has_negative_ic_for_reversal_data(self):
        """反向验证：在反转数据上，动量因子 IC 应为负。"""
        from app.feature.evaluate import evaluate_matrix, FactorReport

        panel = self._panel()
        m = self._matrix("(close / REF(close, 20) - 1) * 100", panel)
        rep = evaluate_matrix(m, FactorReport(name="mom20", expr=""), groups=5,
                              total_rows=sum(len(d) for d in panel.values()))
        self.assertIsNotNone(rep.ic_mean)
        self.assertLess(rep.ic_mean, 0)

    def test_quantile_returns_have_all_groups(self):
        from app.feature.evaluate import evaluate_matrix, FactorReport

        panel = self._panel()
        m = self._matrix("-(close / REF(close, 20) - 1) * 100", panel)
        rep = evaluate_matrix(m, FactorReport(name="rev20", expr=""), groups=5,
                              total_rows=sum(len(d) for d in panel.values()))
        self.assertEqual(len(rep.quantile_returns), 5)

    def test_too_few_samples_refuses_to_conclude(self):
        """样本不够时必须说"无法下结论"，而不是给一个漂亮的数字。"""
        from app.feature.evaluate import evaluate_matrix, FactorReport

        m = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=3).repeat(2),
                          "symbol": ["a", "b"] * 3,
                          "factor": [1.0, 2.0] * 3, "fwd": [0.1, 0.2] * 3})
        rep = evaluate_matrix(m, FactorReport(name="tiny", expr=""))
        self.assertIsNone(rep.score)
        self.assertIn("样本太少", rep.verdict)


class TestML(unittest.TestCase):
    """机器学习挑因子：要能从噪音里把真因子选出来。"""

    def _data(self, n: int = 900, seed: int = 5):
        rng = np.random.default_rng(seed)
        f1, f2, f3 = (rng.normal(size=n) for _ in range(3))
        y = 2.0 * f1 - 1.5 * f2 + 0.8 * f3 + rng.normal(0, 0.5, n)
        feats = {"alpha1": f1, "alpha2": f2, "alpha3": f3,
                 "alpha1_clone": f1 + rng.normal(0, 0.01, n)}
        feats.update({f"noise{i}": rng.normal(size=n) for i in range(5)})
        return feats, y

    def test_lasso_recovers_true_weights(self):
        from app.feature.mining.ml import fit_factors

        feats, y = self._data()
        r = fit_factors(feats, y, method="lasso")
        self.assertGreater(r.r2_test or -1, 0.8)
        self.assertGreater(r.ic_test or -1, 0.9)
        for name, expect in (("alpha1", 2.0), ("alpha2", -1.5), ("alpha3", 0.8)):
            self.assertAlmostEqual(r.weights[name], expect, delta=0.35, msg=name)

    def test_lasso_selects_only_real_factors(self):
        """8 个候选里应该只留下 3 个真因子 —— 这正是 Lasso 的用途。"""
        from app.feature.mining.ml import fit_factors

        feats, y = self._data()
        r = fit_factors(feats, y, method="lasso")
        self.assertEqual(sorted(r.selected), ["alpha1", "alpha2", "alpha3"])

    def test_collinear_factor_is_dropped_before_fit(self):
        """共线的因子必须先剔除：否则 Lasso 会在两列之间**随机**留一个。"""
        from app.feature.mining.ml import fit_factors

        feats, y = self._data()
        r = fit_factors(feats, y, method="lasso")
        self.assertIn("alpha1_clone", r.dropped)
        self.assertTrue(any(c[0] == "alpha1_clone" for c in r.collinear))

    def test_ridge_shrinks_but_never_zeroes(self):
        """岭回归只会把权重缩小，一个都不删 —— 所以它不适合用来**筛**因子。

        这也是为什么要同时提供 Lasso：Lasso 的 L1 惩罚能把权重压到 0。
        """
        from app.feature.mining.ml import fit_factors

        feats, y = self._data()
        r = fit_factors(feats, y, method="ridge")
        nonzero = [n for n, v in r.weights.items() if abs(v) > 1e-12]
        self.assertEqual(len(nonzero), 8)
        # 噪音因子的权重被压得很小，但**不是 0**
        self.assertTrue(all(abs(r.weights[f"noise{i}"]) > 0 for i in range(5)))
        self.assertLess(max(abs(r.weights[f"noise{i}"]) for i in range(5)), 0.1)

    def test_relative_selection_ignores_tiny_weights(self):
        """数值求解后 Lasso 也很少压到严格的 0，所以要按相对量级判显著。"""
        from app.feature.mining.ml import significant_factors

        w = {"a": 1.0, "b": 0.4, "c": 0.02, "d": 0.001}
        self.assertEqual(significant_factors(w), ["a", "b"])

    def test_time_split_not_random(self):
        """训练集必须严格在测试集之前 —— 随机切分会让样本外表现失真。"""
        from app.feature.mining.ml import time_split

        X = np.arange(100).reshape(100, 1).astype(float)
        y = np.arange(100).astype(float)
        Xtr, ytr, Xte, yte = time_split(X, y, 0.7)
        self.assertLess(ytr.max(), yte.min())

    def test_nan_rows_dropped_not_filled(self):
        """用均值填 NaN 会凭空造出"这个因子当时是中性"的假象。"""
        from app.feature.mining.ml import prepare

        X, y, names = prepare({"a": np.array([1.0, np.nan, 3.0, 4.0]),
                               "b": np.array([1.0, 2.0, 3.0, 4.0])},
                              np.array([1.0, 2.0, 3.0, 4.0]))
        self.assertEqual(len(y), 3)
        self.assertTrue(np.isfinite(X).all())


class TestMining(unittest.TestCase):
    """遗传算法挖掘：生成的表达式必须合法，且能找出真信号。"""

    def _panel(self, n_symbols: int = 6, n_days: int = 300):
        return TestFactorEvaluation("test_detects_real_signal")._panel(
            n_symbols=n_symbols, n_days=n_days)

    def test_random_trees_are_always_valid(self):
        """在树上做变异/交叉的意义：生成出来的每一个候选都保证能解析。"""
        import random

        from app.feature.expr import validate
        from app.feature.mining.mining import mutate, random_tree, render, crossover

        rng = random.Random(1)
        trees = [random_tree(rng, depth=3) for _ in range(30)]
        trees += [mutate(rng, t, 3) for t in trees[:10]]
        trees += [crossover(rng, trees[i], trees[i + 1]) for i in range(10)]
        for t in trees:
            r = validate(render(t))
            self.assertTrue(r["ok"], f"{render(t)} → {r.get('error')}")

    def test_mutation_changes_expression(self):
        import random

        from app.feature.mining.mining import mutate, random_tree, render

        rng = random.Random(2)
        t = random_tree(rng, depth=3)
        changed = {render(mutate(rng, t, 3)) for _ in range(12)}
        self.assertTrue(changed - {render(t)})

    def test_mining_finds_signal(self):
        """在"反转规律真实存在"的数据上，挖掘应该能挖出可用因子。"""
        from app.feature.mining.mining import FactorMiner

        miner = FactorMiner(self._panel(), horizon=5, seed=42)
        found = miner.run(generations=6, population=30, top=5,
                          max_seconds=60, min_score=60)
        self.assertGreater(len(found), 0)
        best = found[0]
        self.assertIsNotNone(best.ic)
        self.assertGreater(best.score, 60)
        # 挖出来的因子必须能被引擎重新算出来（否则入库了也用不了）
        from app.feature.expr import validate

        self.assertTrue(validate(best.expr)["ok"])

    def test_mining_respects_time_budget(self):
        """挖掘很容易跑飞 —— 必须有时间预算兜底。"""
        import time

        from app.feature.mining.mining import FactorMiner

        s = time.monotonic()
        FactorMiner(self._panel(n_days=400), horizon=5, seed=7).run(
            generations=50, population=100, max_seconds=6)
        self.assertLess(time.monotonic() - s, 30)


class TestMultiFactorStrategy(unittest.TestCase):
    """多因子策略：把因子库里的因子加权成决策。"""

    def _ctx(self, n: int = 120, seed: int = 9):
        from app.feature.context import FactorContext
        from app.domain.series import BarSeries
        from app.domain.types import Bar

        s = BarSeries("000001", maxlen=500)
        rng = np.random.default_rng(seed)
        px = 10.0
        for i in range(n):
            px = float(px * (1 + rng.normal(0, 0.012)))
            s.append(Bar(symbol="000001",
                         dt=datetime(2026, 1, 1) + timedelta(days=i),
                         open=px, high=px * 1.01, low=px * 0.99, close=px,
                         volume=float(rng.uniform(8e5, 1.5e6))))
        return FactorContext("000001", s)

    def _strategy(self, **kw):
        from app.strategy import create_strategy

        return create_strategy("multi_factor", **kw)

    def test_registered(self):
        from app.strategy import has_strategy

        self.assertTrue(has_strategy("multi_factor"))

    def test_weights_three_forms(self):
        st = self._strategy(factors=["mom_20"])
        self.assertEqual(st.weights, {"mom_20": 1.0})
        st = self._strategy(factors=[{"name": "mom_20", "weight": 2.5}])
        self.assertEqual(st.weights, {"mom_20": 2.5})
        st = self._strategy(factors={"mom_20": 0.5})
        self.assertEqual(st.weights, {"mom_20": 0.5})

    def test_rejects_inverted_thresholds(self):
        """exit ≥ entry 会造成"刚买就卖"的抖动 —— 配置阶段就该拒绝。"""
        with self.assertRaises(ValueError):
            self._strategy(entry=0.2, exit=0.5)

    def test_composite_is_standardized(self):
        """量纲不同的因子直接相加会让量纲大的独占话语权，所以必须先标准化。"""
        st = self._strategy(factors=[{"name": "mom_20", "weight": 1.0},
                                     {"name": "vol_ratio", "weight": 1.0}])
        v = st.composite(self._ctx())
        self.assertIsNotNone(v)
        self.assertLess(abs(v), 3.0)      # z-score 截断在 ±3

    def test_defaults_to_library_factors(self):
        """不配因子时用因子库里全部启用的（等权）—— 先能用起来再谈优化。"""
        st = self._strategy()
        self.assertFalse(st.weights)          # 构造时不读库
        self.assertTrue(st._default_weights())  # 用到时才解析
        self.assertIsNotNone(st.composite(self._ctx()))

    def test_missing_factor_is_skipped_not_zero(self):
        """算不出的因子要跳过并重分配权重，而不是当 0 分制造假看空。"""
        st = self._strategy(factors=[{"name": "mom_20", "weight": 1.0},
                                     {"name": "no_such_factor", "weight": 1.0}])
        v = st.composite(self._ctx())
        self.assertIsNotNone(v)

    def test_no_factor_at_all_refuses_to_trade(self):
        from app.strategy.state import DecisionState

        st = self._strategy(factors=[{"name": "no_such_factor", "weight": 1.0}])
        self.assertIsNone(st.decide(self._ctx(), DecisionState()))

    def test_buy_and_sell_thresholds(self):
        from app.strategy.state import DecisionState

        # 入口压到极低（必然买入），出口更低（不会立刻卖出）
        st = self._strategy(factors=[{"name": "mom_20", "weight": 1.0}],
                            entry=-99, exit=-100)
        sig = st.decide(self._ctx(), DecisionState())
        self.assertIsNotNone(sig)
        self.assertEqual(str(sig.side), "Side.BUY")


if __name__ == "__main__":
    unittest.main()
