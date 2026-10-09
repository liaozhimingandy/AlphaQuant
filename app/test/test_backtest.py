#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_backtest.py
# @Description : 回测闭环测试（合成数据，不依赖网络与真实数据库）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
import unittest
from unittest import mock

import pandas as pd

from app.backtest import BacktestConfig, run_backtest
from app.backtest.registry import get_strategy, list_strategies
from app.backtest.result import annualized_return, max_drawdown, sharpe_ratio
from app.backtest.runner import BacktestError, BacktestRunner
from app.test.helpers import make_ohlcv, make_uptrend


class TestConfig(unittest.TestCase):
    def test_normalizes_symbol_and_date(self):
        cfg = BacktestConfig(symbol="000001.SZ", start="2023-01-01", end="2023-12-31")
        self.assertEqual(cfg.symbol, "000001")
        self.assertEqual(cfg.start, "20230101")
        self.assertEqual(cfg.start_iso, "2023-01-01")

    def test_rejects_bad_range(self):
        with self.assertRaises(ValueError):
            BacktestConfig(symbol="000001", start="2023-12-31", end="2023-01-01")

    def test_rejects_bad_cash(self):
        with self.assertRaises(ValueError):
            BacktestConfig(symbol="000001", start="2023-01-01", end="2023-12-31", cash=0)

    def test_roundtrip_dict(self):
        cfg = BacktestConfig(symbol="000001", start="2023-01-01", end="2023-12-31")
        self.assertEqual(BacktestConfig.from_dict(cfg.to_dict()).symbol, "000001")


class TestMetrics(unittest.TestCase):
    def test_max_drawdown(self):
        equity = pd.Series([100, 120, 60, 80])
        self.assertAlmostEqual(max_drawdown(equity), 50.0, places=2)

    def test_max_drawdown_empty(self):
        self.assertEqual(max_drawdown(pd.Series(dtype=float)), 0.0)

    def test_annualized_return(self):
        # 一年翻倍 -> 100%
        self.assertAlmostEqual(annualized_return(100, 200, 365), 100.0, places=2)

    def test_annualized_return_zero_days(self):
        self.assertEqual(annualized_return(100, 200, 0), 0.0)

    def test_sharpe_zero_volatility(self):
        # 恒定收益不应抛除零异常
        self.assertEqual(sharpe_ratio(pd.Series([1.0, 1.0, 1.0])), 0.0)

    def test_sharpe_short_series(self):
        self.assertEqual(sharpe_ratio(pd.Series([1.0])), 0.0)


class TestRegistry(unittest.TestCase):
    def test_list_contains_builtin(self):
        names = list_strategies()
        self.assertIn("ma_cross", names)
        self.assertIn("precise_ma_cross", names)

    def test_unknown_strategy(self):
        with self.assertRaises(KeyError):
            get_strategy("no_such_strategy")


class TestRunner(unittest.TestCase):
    def _patch_load(self, df):
        return mock.patch(
            "app.backtest.runner.MarketDataService.load", return_value=df
        )

    def test_runs_end_to_end(self):
        df = make_ohlcv(days=300)
        with self._patch_load(df):
            result = run_backtest(
                BacktestConfig(
                    symbol="000001",
                    start="2023-01-01",
                    end="2024-12-31",
                    strategy="ma_cross",
                    cash=100000,
                    strategy_params={"printlog": False},
                )
            )

        self.assertEqual(result.symbol, "000001")
        self.assertGreater(result.bar_count, 100)
        self.assertGreater(len(result.equity_curve), 100)
        # 权益曲线不能有 NaN（曾因 Series 索引对齐 bug 整列为 NaN）
        self.assertEqual(int(result.equity_curve["value"].isna().sum()), 0)
        # 指标必须是有限数，不能是 nan
        for value in (
            result.total_return_pct,
            result.max_drawdown_pct,
            result.sharpe,
            result.volatility_pct,
        ):
            self.assertFalse(pd.isna(value), f"指标出现 NaN: {value}")

    def test_uptrend_generates_trades(self):
        df = make_uptrend(days=200)
        with self._patch_load(df):
            result = run_backtest(
                BacktestConfig(
                    symbol="000001",
                    start="2023-01-01",
                    end="2023-12-31",
                    strategy="ma_cross",
                    strategy_params={"printlog": False},
                )
            )
        self.assertGreater(result.trade_count, 0)
        self.assertGreater(len(result.trades), 0)

    def test_no_negative_position(self):
        """回归：曾出现"僵尸止损单"在空仓时卖出导致 -7600 股空头持仓。

        纯多头策略的最小持仓必须恒 >= 0。
        """
        for name in ("ma_cross", "precise_ma_cross", "trend_ma_cross"):
            df = make_ohlcv(days=300)
            with self._patch_load(df):
                result = run_backtest(
                    BacktestConfig(
                        symbol="000001",
                        start="2023-01-01",
                        end="2024-12-31",
                        strategy=name,
                        strategy_params={"printlog": False},
                    )
                )
            self.assertGreaterEqual(
                result.min_position, 0, f"策略 {name} 出现空头持仓"
            )

    def test_all_builtin_strategies_run(self):
        df = make_ohlcv(days=300)
        for name in list_strategies():
            with self._patch_load(df):
                result = run_backtest(
                    BacktestConfig(
                        symbol="000001",
                        start="2023-01-01",
                        end="2024-12-31",
                        strategy=name,
                        strategy_params={"printlog": False},
                    )
                )
            self.assertEqual(result.strategy, name)
            self.assertGreater(result.bar_count, 100)

    def test_rejects_too_little_data(self):
        df = make_ohlcv(days=1)
        with self._patch_load(df):
            with self.assertRaises(BacktestError):
                BacktestRunner(
                    BacktestConfig(symbol="000001", start="2023-01-01", end="2023-12-31")
                ).run()

    def test_filters_dirty_rows(self):
        df = make_ohlcv(days=100)
        df.iloc[5, df.columns.get_loc("close")] = None
        runner = BacktestRunner(
            BacktestConfig(symbol="000001", start="2023-01-01", end="2023-12-31")
        )
        cleaned = runner._prepare_feed(df)
        self.assertEqual(len(cleaned), 99)
        self.assertEqual(int(cleaned.isna().sum().sum()), 0)


if __name__ == "__main__":
    unittest.main()
