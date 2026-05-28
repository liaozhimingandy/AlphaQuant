#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:30
# @FileName    : base.py
# @Description : 指标层：只负责计算
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc


class IBaseIndicator(abc.ABC):

    @abc.abstractmethod
    def value(self, idx: int = 0):
        """
        返回指标值
        """
        raise NotImplementedError
