#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/6/3 17:22
# @FileName    : factor.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc
from dataclasses import dataclass, field
from typing import List

from utils.logger import logger
from .entities import Bar, FactorSignal


class IFactor(abc.ABC):
    """所有因子（指标）必须继承此类
    独立计算、独立输出信号，与策略完全解耦
    """
    name: str = "base_factor"

    @abc.abstractmethod
    def on_init(self) -> None:
        """因子初始化（仅1次）"""
        pass

    @abc.abstractmethod
    def on_bar(self, bar: Bar) -> FactorSignal:
        """每根K线计算因子，返回标准信号
        【核心】因子只干一件事：计算 → 输出信号
        """
        pass

    @abc.abstractmethod
    def reset(self) -> None:
        """重置因子状态"""
        pass


@dataclass
class MaCrossFactor(IFactor):
    """
    520均线
    """
    items: List[float] = field(default_factory=list)

    def on_init(self) -> None:
        pass

    def on_bar(self, bar: Bar) -> FactorSignal:
        self.items.append(bar.close)

        if len(self.items) < 5:
            return FactorSignal.NEUTRAL

        # 计算均线
        ma_f = sum(self.items[-5:]) / 5
        ma_s = sum(self.items[-20:]) / 20
        ma_t = sum(self.items[-60:]) / 60

        # 趋势过滤
        trend_up = ma_t < bar.close
        # 金叉买入
        if trend_up and ma_f > ma_s:
            return FactorSignal.LONG
        # 死叉卖出
        if ma_f < ma_s:
            return FactorSignal.SHORT
        return FactorSignal.NEUTRAL

    def reset(self) -> None:
        pass