#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:47
# @FileName    : base.py
# @Description : 调度层，只负责调度
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc

from app.core.signal.base import IBaseSignal


class IBaseStrategy(abc.ABC):

    def __init__(self):

        self.entry_rule = self.build_entry_rule()
        self.exit_rule = self.build_exit_rule()

    # =========================
    # 生命周期
    # =========================

    def on_init(self):
        """策略初始化"""
        pass

    def on_start(self):
        """开始运行"""
        pass

    def on_stop(self):
        """结束运行"""
        pass

    def on_order(self, order):
        """订单回调"""
        pass

    def on_trade(self, trade):
        """成交回调"""
        pass

    # =========================
    # 核心入口
    # =========================

    def on_bar(
            self,
            context
    ) -> IBaseSignal | None:

        # 没持仓
        if not context.position.has_position:

            if self.entry_rule.is_satisfied():

                return self.generate_buy_signal(context)

        # 持仓中
        else:

            if self.exit_rule.is_satisfied():

                return self.generate_sell_signal(context)

        return None

    # =========================
    # 信号生成
    # =========================

    @abc.abstractmethod
    def generate_buy_signal(
            self,
            context
    ) -> IBaseSignal:
        pass

    @abc.abstractmethod
    def generate_sell_signal(
            self,
            context
    ) -> IBaseSignal:
        pass

    # =========================
    # 规则构建
    # =========================

    @abc.abstractmethod
    def build_entry_rule(self):
        pass

    @abc.abstractmethod
    def build_exit_rule(self):
        pass

