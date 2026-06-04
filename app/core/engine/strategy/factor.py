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
from enum import Enum


# --------------------- 因子信号标准（统一输出） ---------------------
class FactorSignal(Enum):
    """因子标准化输出信号
    LONG: 看多/买入
    SHORT: 看空/卖出
    NEUTRAL: 观望/无信号
    """
    LONG = 1
    SHORT = -1
    NEUTRAL = 0

# --------------------- 【顶层因子抽象接口】可插拔、可组合 ---------------------
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
