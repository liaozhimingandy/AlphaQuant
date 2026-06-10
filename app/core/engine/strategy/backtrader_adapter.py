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
from app.utils.logger import logger
from ma_cross_strategy import MaCrossStrategy


class BacktraderStrategyAdapter(bt.Strategy):
    """
    Backtrader → 抽象策略 适配器
    作用：桥接 backtrader 与 通用抽象接口
    """
    params = dict(
        strategy_instance=None,  # 注入抽象策略
    )

    def start(self):
        super().start()
        self.strategy.on_start()
        logger.debug(f"策略 {self.strategy.symbol} 开始回测")

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

    def stop(self):
        super().stop()
        self.strategy.on_stop()
        symbol = self.strategy.symbol

        # ========== ==========
        # 假设 self.strategy.trades 是你自己维护的交易列表
        trade_count = len(self.strategy.trades) // 2  # 整数除法，避免浮点数
        logger.debug(f"【回测结束】标的{symbol}，共交易 {trade_count} 次")

        # 示例1：打印最终资金
        final_cash = self.strategy.account.total_assets
        logger.debug(f"【回测结束】标的{symbol}，最终总资产：{final_cash:.2f}")

        # 示例2：调用自研策略内部的收尾方法（解耦推荐）
        if hasattr(self.strategy, "on_finish"):
            self.strategy.on_finish()

        # 示例3：临时数据清理
        self.strategy.trades.clear()

if __name__ == "__main__":
    cerebro = bt.Cerebro()

    # 从数据库加载数据
    db = SessionLocal()
    df = StockRepository.get_stock_df(
        db=db,
        symbol="000001",
        start_date="2022-01-01",
        end_date="2026-06-02"
    )

    data = bt.feeds.PandasData(dataname=df)

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

    strategy = MaCrossStrategy(symbol='000001', config={})
    cerebro.addstrategy(BacktraderStrategyAdapter, strategy_instance=strategy)

    cerebro.run()



