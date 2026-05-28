#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:45
# @FileName    : base.py
# @Description : 仓位管理层
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc


class IBaseSizer(abc.ABC):

    @abc.abstractmethod
    def size(self, price: float) -> int:
        raise NotImplementedError
