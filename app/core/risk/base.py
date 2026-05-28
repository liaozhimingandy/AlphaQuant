#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:43
# @FileName    : base.py
# @Description : 风控层,比如：最大回撤风控
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc


class IBaseRiskManager(abc.ABC):

    @abc.abstractmethod
    def allow_entry(self) -> bool:
        raise NotImplementedError