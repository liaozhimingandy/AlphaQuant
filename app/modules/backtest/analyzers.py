#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : analyzers.py
# @Description : 自定义 backtrader 分析器
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import backtrader as bt
import pandas as pd


class EquityCurveAnalyzer(bt.Analyzer):
    """逐根 K 线记录账户总权益，用于计算回撤/波动/夏普。"""

    params = dict()

    def start(self) -> None:
        self._dates: list = []
        self._values: list = []
        self._positions: list = []

    def next(self) -> None:
        self._dates.append(self.strategy.data.datetime.date(0))
        self._values.append(self.strategy.broker.getvalue())
        self._positions.append(self.strategy.position.size)

    def stop(self) -> None:
        # 保证最后一根 K 线的收盘权益被记录
        try:
            self._dates.append(self.strategy.data.datetime.date(0))
            self._values.append(self.strategy.broker.getvalue())
            self._positions.append(self.strategy.position.size)
        except Exception:
            pass

    def get_analysis(self) -> pd.DataFrame:
        if not self._dates:
            return pd.DataFrame(columns=["value", "position"]).rename_axis("date")
        # 注意：这里必须传 list 而不是 Series。
        # 传 Series 会带上默认的 RangeIndex，与下面的日期 index 做对齐后整列变成 NaN。
        df = pd.DataFrame(
            {
                "value": [float(v) for v in self._values],
                "position": [float(p) for p in self._positions],
            },
            index=pd.to_datetime(list(self._dates)),
        )
        df.index.name = "date"
        # stop() 会重复记录最后一根，去重保留最后一次
        return df[~df.index.duplicated(keep="last")].sort_index()
