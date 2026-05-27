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
class BaseFactor(abc.ABC, bt.Indicator):
    """所有因子的统一抽象基类，所有新因子只需要实现is_true方法"""
    lines = ("value",)
    params = dict()

    @abc.abstractmethod
    def is_true(self, idx: int = 0) -> bool:
        """因子是否满足条件，所有因子统一接口"""
        pass


# -------------------------- 2. 规则基类：把因子组合成买入/卖出规则 --------------------------
class BaseRule(abc.ABC):
    """所有规则的统一抽象基类"""
    @abc.abstractmethod
    def is_satisfied(self) -> bool:
        """规则是否满足"""
        pass

class AllRule(BaseRule):
    """所有因子同时满足才触发（与规则）"""
    def __init__(self, factors: List[BaseFactor]):
        self.factors = factors
    def is_satisfied(self) -> bool:
        return all(f.is_true() for f in self.factors)

class AnyRule(BaseRule):
    """任意一个因子满足就触发（或规则）"""
    def __init__(self, factors: List[BaseFactor]):
        self.factors = factors
    def is_satisfied(self) -> bool:
        return any(f.is_true() for f in self.factors)

def main(name: str = ''):
    print(f'Hi, {name}')


if __name__ == '__main__':
    main()
