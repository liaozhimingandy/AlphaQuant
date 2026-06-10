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

from app.core.engine.strategy.factor import MaCrossFactor


class MaCrossStrategy(IBaseStrategy):
    """均线交叉策略（无框架依赖，可插拔）"""

    name = "ma_cross_strategy"
    version = "1.0.0"

    def __init__(self, symbol: str, config=None):
        super().__init__(symbol, config)
        # 策略参数
        self.fast = config.get("fast", 5)
        self.slow = config.get("slow", 20)
        self.trend_slow = config.get("trend_slow", 60)
        self.stop_loss_pct = config.get("stop_loss_pct", 0.05)

    def on_init(self):
        logger.info("策略初始化：双均线策略")
        # 添加因子
        self.add_factor(MaCrossFactor())

    def on_stop(self):
        logger.info("策略停止，已清仓")


    def _calc_buy_size(self, price: float) -> float:
        """
        仓位计算
        :param price:
        :return:
        """
        cash = self.account.cash * 0.85
        return int(cash / price / 100) * 100

    def generate_report(self, bar: Bar):
        """耗时任务：生成回测报告（调度器异步执行）"""
        logger.info(f"生成报告：{bar.timestamp} 价格={bar.close}")
