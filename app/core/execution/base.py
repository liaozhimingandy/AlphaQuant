#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:46
# @FileName    : base.py
# @Description : 执行层
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc


class IBaseExecution(abc.ABC):

    @abc.abstractmethod
    def buy(self, size: int):
        raise NotImplementedError

    @abc.abstractmethod
    def sell(self, size: int):
        raise NotImplementedError