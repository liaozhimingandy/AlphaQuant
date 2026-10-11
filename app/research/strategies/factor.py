#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/27 15:41
# @FileName    : factor.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc
from typing import List

import backtrader as bt


# -------------------------- 1. 因子基类：所有因子统一接口，一次编写永久复用 --------------------------
# 合并两个父类的元类，创建专属的因子元类，一劳永逸
class ABCIndicatorMeta(bt.MetaIndicator, abc.ABCMeta):
    pass


class IBaseFactor(bt.Indicator, abc.ABC, metaclass=ABCIndicatorMeta):
    """所有因子的统一抽象基类，所有新因子只需要实现is_true方法"""
    lines = ("value",)
    params = dict()

    @abc.abstractmethod
    def is_true(self, idx: int = 0) -> bool:
        """因子是否满足条件，所有因子统一接口"""
        raise NotImplementedError


class MaCrossOverFactorI(IBaseFactor):
    """
    5/20均线金叉因子：仅金叉发生的当天返回True，其他时间永远False
    仅用于开仓规则，100%精准，不会和死叉混淆
    """
    params = dict(fast=5, slow=20)
    lines = ("cross",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 底层调用原生交叉指标，只有交叉当天返回非0
        self.lines.cross = bt.indicators.CrossOver(
            bt.indicators.SMA(self.data.close, period=self.p.fast),
            bt.indicators.SMA(self.data.close, period=self.p.slow)
        )

    def is_true(self, idx: int = 0) -> bool:
        # 仅金叉（cross=1）当天返回True，死叉/无交叉永远False
        return self.lines.cross[0] > 0


class MaCrossDownFactorI(IBaseFactor):
    """
    5/20均线死叉因子：仅死叉发生的当天返回True，其他时间永远False
    仅用于平仓规则，100%精准，不会和金叉混淆
    """
    params = dict(fast=5, slow=20)
    lines = ("cross",)

    def __init__(self):
        super().__init__()
        self.lines.cross = bt.indicators.CrossOver(
            bt.indicators.SMA(self.data.close, period=self.p.fast),
            bt.indicators.SMA(self.data.close, period=self.p.slow)
        )

    def is_true(self, idx: int = 0) -> bool:
        # 仅死叉（cross=-1）当天返回True，金叉/无交叉永远False
        return self.lines.cross[0] < 0


# -------------------------- 2. 规则基类：把因子组合成买入/卖出规则 --------------------------
class BaseRule(abc.ABC):
    """所有规则的统一抽象基类"""

    @abc.abstractmethod
    def is_satisfied(self) -> bool:
        """规则是否满足"""
        raise NotImplementedError


class AllRule(BaseRule):
    """所有因子同时满足才触发（与规则）"""

    def __init__(self, factors: List[IBaseFactor]):
        self.factors = factors

    def is_satisfied(self) -> bool:
        return all(f.is_true() for f in self.factors)


class AnyRule(BaseRule):
    """任意一个因子满足就触发（或规则）"""

    def __init__(self, factors: List[IBaseFactor]):
        self.factors = factors

    def is_satisfied(self) -> bool:
        return any(f.is_true() for f in self.factors)


def main(name: str = ''):
    print(f'Hi, {name}')


if __name__ == '__main__':
    main()
