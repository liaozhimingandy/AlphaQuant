#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_datasource.py
# @Description : 数据源单元测试（离线，不依赖网络）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
import types
import unittest

import pandas as pd

from app.data import datasource as ds


class TestNormalize(unittest.TestCase):
    def test_symbol(self):
        cases = {
            "000001": "000001",
            "000001.SZ": "000001",
            "sz.000001": "000001",
            "600000": "600000",
            "sh600000": "600000",
            "SH.600000": "600000",
        }
        for raw, expected in cases.items():
            self.assertEqual(ds.normalize_symbol(raw), expected, raw)

    def test_symbol_invalid(self):
        with self.assertRaises(ValueError):
            ds.normalize_symbol("abc")

    def test_date(self):
        self.assertEqual(ds.normalize_date("2024-01-01"), "20240101")
        self.assertEqual(ds.normalize_date("20240101"), "20240101")

    def test_date_invalid(self):
        with self.assertRaises(ValueError):
            ds.normalize_date("2024-1-1")


class TestStandardize(unittest.TestCase):
    def test_adds_symbol_and_sorts(self):
        df = pd.DataFrame(
            {
                "trade_date": ["2024-01-03", "2024-01-02"],
                "open": [2.0, 1.0],
                "high": [2.5, 1.5],
                "low": [1.5, 0.5],
                "close": [2.2, 1.2],
                "volume": [200, 100],
                "amount": [440.0, 120.0],
            }
        )
        out = ds.IBaseDataSource._standardize_df(df, "000001.SZ")
        self.assertEqual(list(out.columns), ds.IBaseDataSource.REQUIRED_COLUMNS)
        self.assertEqual(out["symbol"].unique().tolist(), ["000001"])
        self.assertEqual(len(out), 2)
        # 必须按日期升序
        self.assertTrue(out["date"].is_monotonic_increasing)

    def test_drops_dirty_rows(self):
        df = pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-03"],
                "open": [1.0, None],
                "high": [1.5, None],
                "low": [0.5, None],
                "close": [1.2, None],
                "volume": [100, None],
                "amount": [120.0, None],
            }
        )
        out = ds.IBaseDataSource._standardize_df(df, "000001")
        self.assertEqual(len(out), 1)

    def test_missing_column_raises(self):
        df = pd.DataFrame({"date": ["2024-01-02"], "open": [1.0]})
        with self.assertRaises(ds.DataFetchError):
            ds.IBaseDataSource._standardize_df(df, "000001")


class TestAkshare(unittest.TestCase):
    """回归测试：旧实现把 '日期' 映射成 trade_date 后再 set_index('date') 会 KeyError。"""

    def _fake_akshare(self):
        fake = types.SimpleNamespace()

        def stock_zh_a_hist(symbol, period, start_date, end_date, adjust):
            return pd.DataFrame(
                {
                    "日期": ["2024-01-02", "2024-01-03"],
                    "开盘": [1.0, 1.1],
                    "最高": [1.2, 1.3],
                    "最低": [0.9, 1.0],
                    "收盘": [1.1, 1.2],
                    "成交量": [100, 200],
                    "成交额": [110.0, 240.0],
                }
            )

        fake.stock_zh_a_hist = stock_zh_a_hist
        return fake

    def test_fetch_data(self):
        original = ds._ak
        ds._ak = self._fake_akshare()
        try:
            df = ds.AkshareDataSource().fetch_data(
                "000001.SZ", "2024-01-01", "2024-01-31"
            )
        finally:
            ds._ak = original

        self.assertEqual(list(df.columns), ds.IBaseDataSource.REQUIRED_COLUMNS)
        self.assertEqual(len(df), 2)
        self.assertEqual(df["symbol"].unique().tolist(), ["000001"])


class TestFactory(unittest.TestCase):
    def test_unknown_source_raises(self):
        with self.assertRaises(ds.DataFetchError):
            ds.DataSourceFactory.get("not_exist")

    def test_register_and_get(self):
        sentinel = object()
        ds.DataSourceFactory.register_data_source("sentinel", sentinel)
        self.assertIs(ds.DataSourceFactory.get("sentinel"), sentinel)

    def test_start_and_end_validation(self):
        with self.assertRaises(ValueError):
            ds.DataSourceFactory.get_stock_data("000001", "2024-06-01", "2024-01-01")


if __name__ == "__main__":
    unittest.main()
