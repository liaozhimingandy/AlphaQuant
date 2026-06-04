#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/6/3 11:46
# @FileName    : backtrader_adapter.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import backtrader as bt

from app.db.database import SessionLocal
from app.repository.stock_repository import StockRepository
from app.core.engine.strategy.base import IBaseStrategy, Bar
from ma_cross_strategy import MaCrossStrategy


class BacktraderStrategyAdapter(bt.Strategy):
    """
    Backtrader → 抽象策略 适配器
    作用：桥接 backtrader 与 通用抽象接口
    """
    params = dict(
        strategy_instance=None,  # 注入抽象策略
    )

    def __init__(self):
        super().__init__()
        self.strategy: IBaseStrategy = self.p.strategy_instance
        self.strategy.on_init()

    def next(self):
        # 一个K线调用一次
        bar = Bar(
            symbol=self.data._name,
            timestamp=self.data.datetime.date(0).isoformat(),
            open=self.data.open[0],
            high=self.data.high[0],
            low=self.data.low[0],
            close=self.data.close[0],
            volume=self.data.volume[0],
        )
        self.strategy.on_bar(bar)

    def notify_order(self, order):
        """backtrader 有订单时回调"""
        pass


if __name__ == "__main__":
    cerebro = bt.Cerebro()

    # 从数据库加载数据
    db = SessionLocal()
    df = StockRepository.get_stock_df(
        db=db,
        symbol="000001",
        start_date="2023-01-01",
        end_date="2026-06-02"
    )

    data = bt.feeds.PandasData(
        dataname=df
    )

    cerebro.adddata(data)
    cerebro.broker.setcash(10000)
    cerebro.broker.setcommission(
        commission=0.0003
    )
    cerebro.broker.set_slippage_perc(
        perc=0.001
    )
    # 禁止做空
    cerebro.broker.set_shortcash(False)
    # 开启资金检查
    cerebro.broker.set_checksubmit(True)
    # 收盘价成交
    cerebro.broker.set_coc(True)

    strategy = MaCrossStrategy(symbol='', config={})
    cerebro.addstrategy(BacktraderStrategyAdapter, strategy_instance=strategy)

    cerebro.run()



