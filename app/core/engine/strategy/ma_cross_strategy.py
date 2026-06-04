#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/6/3 11:45
# @FileName    : ma_cross_strategy.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from app.core.engine.strategy.entities import Order, TradeSignal, Bar
from app.utils.logger import logger
from app.core.engine.strategy.base import IBaseStrategy
from typing import List


class MaCrossStrategy(IBaseStrategy):
    """均线交叉策略（无框架依赖，可插拔）"""

    def on_order_update(self, order: Order) -> None:
        pass

    name = "ma_cross_strategy"
    version = "1.0.0"

    def __init__(self, symbol: str, config=None):
        super().__init__(symbol, config)
        # 策略参数
        self.fast = config.get("fast", 5)
        self.slow = config.get("slow", 20)
        self.trend_slow = config.get("trend_slow", 60)
        self.stop_loss_pct = config.get("stop_loss_pct", 0.05)

        # 内部状态
        self.close_prices: List[float] = []

    def on_init(self):
        logger.info("策略初始化：双均线策略")

    def generate_signal(self, bar: Bar) -> TradeSignal:
        """核心信号"""
        self.close_prices.append(bar.close)
        if len(self.close_prices) < self.slow:
            return TradeSignal(0)

        # 计算均线
        ma_f = sum(self.close_prices[-self.fast:]) / self.fast
        ma_s = sum(self.close_prices[-self.slow:]) / self.slow
        ma_t = sum(self.close_prices[-self.trend_slow:]) / self.trend_slow

        # 趋势过滤
        trend_up = ma_t < bar.close
        # 金叉买入
        if not self.position.size and trend_up and ma_f > ma_s:
            return TradeSignal(1, "金叉+上升趋势")
        # 死叉卖出
        if self.position.size and ma_f < ma_s:
            return TradeSignal(-1, "死叉信号")
        return TradeSignal(0)

    def on_bar(self, bar: Bar):
        """
        触发时机：每天有新的K线时触发
        :param bar:
        :return:
        """
        signal = self.generate_signal(bar)

        if signal.signal == 1:
            size = self._calc_buy_size(bar.close)
            self.buy(price=bar.close, size=size)
        elif signal.signal == -1:
            self.close_position()
        logger.debug(f'{bar.timestamp}')
        # 提交耗时任务（报表生成）
        self.submit_task(self.generate_report, bar)

    def on_stop(self):
        self.close_position()
        logger.info("策略停止，已清仓")

    # --------------------- 工具方法 ---------------------
    def _calc_buy_size(self, price: float) -> float:
        cash = self.account.cash * 0.85
        return int(cash / price / 100) * 100

    def generate_report(self, bar: Bar):
        """耗时任务：生成回测报告（调度器异步执行）"""
        logger.info(f"生成报告：{bar.timestamp} 价格={bar.close}")

    def notify_order(self, order):
        self.on_order_update(order)