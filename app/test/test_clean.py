#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_clean.py
# @Description : 数据清洗测试：脏数据必须被识别出来，而且是"标记"而不是"悄悄丢"
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.core.market.clean import (
    FLAG_ABNORMAL_MOVE,
    FLAG_BAD_OHLC,
    FLAG_LIMIT_UP,
    FLAG_NON_POSITIVE,
    FLAG_ZERO_VOLUME,
    BarCleaner,
    clean_bars,
    clean_frame,
    is_tradable,
)
from app.core.market.types import Bar


def _mk(i: int, o=10.0, h=10.5, l=9.5, c=10.2, v=1000.0, d0=None) -> Bar:
    base = d0 or datetime(2026, 1, 5)
    return Bar(symbol="T", dt=base + timedelta(days=i), open=o, high=h, low=l, close=c, volume=v)


class TestBarCleaner(unittest.TestCase):
    def test_clean_data_untouched(self):
        bars = [_mk(i) for i in range(5)]
        out, rep = BarCleaner().clean(bars)
        self.assertEqual(len(out), 5)
        self.assertFalse(rep.dirty)
        self.assertEqual(rep.flags, {})
        self.assertTrue(all(is_tradable(b) for b in out))

    def test_duplicate_timestamp_keeps_last(self):
        """重复时间戳保留**后到**的那根：后到的通常是修正过的数据。"""
        first = _mk(0, c=10.0)
        fixed = _mk(0, c=11.0)
        out, rep = BarCleaner().clean([first, fixed])
        self.assertEqual(len(out), 1)
        self.assertEqual(rep.duplicates_removed, 1)
        self.assertAlmostEqual(out[0].close, 11.0)

    def test_out_of_order_sorted(self):
        bars = [_mk(2), _mk(0), _mk(1)]
        out, rep = BarCleaner().clean(bars)
        self.assertTrue(rep.reordered)
        self.assertEqual([b.dt for b in out], sorted(b.dt for b in out))

    def test_bad_ohlc_flagged(self):
        """high < close 是自相矛盾的数据，必须被标出来。"""
        bars = [_mk(0, o=10, h=10, l=10, c=15)]
        out, rep = BarCleaner().clean(bars)
        self.assertIn(FLAG_BAD_OHLC, rep.flags)
        self.assertFalse(is_tradable(out[0]))

    def test_non_positive_price_flagged(self):
        bars = [_mk(0, o=0, h=0, l=0, c=0)]
        out, rep = BarCleaner().clean(bars)
        self.assertIn(FLAG_NON_POSITIVE, rep.flags)
        self.assertFalse(is_tradable(out[0]))

    def test_zero_volume_flagged_as_suspended(self):
        bars = [_mk(0), _mk(1, v=0)]
        out, rep = BarCleaner().clean(bars)
        self.assertIn(FLAG_ZERO_VOLUME, rep.flags)
        self.assertFalse(is_tradable(out[1]))

    def test_abnormal_move_flagged(self):
        bars = [_mk(0, c=10.0), _mk(1, c=20.0)]   # +100%
        out, rep = BarCleaner().clean(bars)
        self.assertIn(FLAG_ABNORMAL_MOVE, rep.flags)
        self.assertFalse(is_tradable(out[1]))

    def test_limit_up_flagged(self):
        bars = [_mk(0, o=10, h=10, l=10, c=10.0), _mk(1, o=11, h=11, l=11, c=11.0)]
        out, rep = BarCleaner().clean(bars)
        self.assertIn(FLAG_LIMIT_UP, rep.flags)

    def test_default_is_mark_not_drop(self):
        """默认只标记不丢弃：丢了就分不清"真异常"和"你没想到的合法情况"。"""
        bars = [_mk(0), _mk(1, c=99.0)]
        out, _ = BarCleaner().clean(bars)
        self.assertEqual(len(out), 2)

    def test_drop_mode_discards_unusable(self):
        bars = [_mk(0), _mk(1, v=0), _mk(2)]
        out, rep = BarCleaner(action="drop").clean(bars)
        self.assertEqual(len(out), 2)
        self.assertGreaterEqual(rep.dropped, 1)

    def test_empty_input(self):
        out, rep = BarCleaner().clean([])
        self.assertEqual(out, [])
        self.assertEqual(rep.total, 0)

    def test_report_serializable(self):
        _, rep = BarCleaner().clean([_mk(0), _mk(1, c=99.0)])
        d = rep.to_dict()
        self.assertIn("flags", d)
        self.assertIsInstance(d["flags"], dict)

    def test_clean_bars_helper(self):
        out, rep = clean_bars([_mk(0)])
        self.assertEqual(len(out), 1)
        self.assertEqual(rep.kept, 1)


class TestCleanFrame(unittest.TestCase):
    def _frame(self, rows):
        return pd.DataFrame(rows)

    def test_missing_dt_column_still_works(self):
        df = self._frame([
            {"dt": "2026-01-05", "open": 10, "high": 10.5, "low": 9.5, "close": 10.2, "volume": 100},
            {"dt": "2026-01-06", "open": 10.2, "high": 10.6, "low": 10.0, "close": 10.4, "volume": 120},
        ])
        out, rep = clean_frame(df)
        self.assertEqual(len(out), 2)
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(out["dt"]))

    def test_drops_rows_without_price(self):
        """没有价格的行是唯一必须丢的：没有价格就没有一切。"""
        df = self._frame([
            {"dt": "2026-01-05", "open": 10, "high": 10.5, "low": 9.5, "close": 10.2, "volume": 1},
            {"dt": "2026-01-06", "open": None, "high": None, "low": None, "close": None, "volume": 1},
        ])
        out, rep = clean_frame(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(rep.dropped, 1)

    def test_dedup_and_sort(self):
        df = self._frame([
            {"dt": "2026-01-06", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
            {"dt": "2026-01-05", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
            {"dt": "2026-01-05", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
        ])
        out, rep = clean_frame(df)
        self.assertEqual(len(out), 2)
        self.assertEqual(rep.duplicates_removed, 1)
        self.assertTrue(rep.reordered)
        self.assertEqual(float(out["close"].iloc[0]), 10.5)

    def test_non_positive_dropped(self):
        df = self._frame([
            {"dt": "2026-01-05", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 1},
            {"dt": "2026-01-06", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
        ])
        out, rep = clean_frame(df)
        self.assertEqual(len(out), 1)
        self.assertIn(FLAG_NON_POSITIVE, rep.flags)

    def test_empty_frame(self):
        out, rep = clean_frame(pd.DataFrame())
        self.assertTrue(out.empty)
        self.assertEqual(rep.total, 0)

    def test_marks_abnormal_move(self):
        df = self._frame([
            {"dt": "2026-01-05", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
            {"dt": "2026-01-06", "open": 20, "high": 20, "low": 20, "close": 20, "volume": 1},
        ])
        out, rep = clean_frame(df)
        self.assertIn(FLAG_ABNORMAL_MOVE, rep.flags)
        self.assertIn(FLAG_ABNORMAL_MOVE, str(out["flags"].iloc[1]))


if __name__ == "__main__":
    unittest.main()
