#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:31
# @FileName    : base.py
# @Description : 因子层,例如市场的趋势,
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc


class IBaseFactor(abc.ABC):

    @abc.abstractmethod
    def evaluate(self) -> bool:
        raise NotImplementedError
