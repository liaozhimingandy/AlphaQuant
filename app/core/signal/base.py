#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:30
# @FileName    : base.py
# @Description : 市场事件,比如：金叉
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc


class IBaseSignal(abc.ABC):

    @abc.abstractmethod
    def is_triggered(self, idx: int = 0) -> bool:
        raise NotImplementedError