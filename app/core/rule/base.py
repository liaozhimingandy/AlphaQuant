#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:33
# @FileName    : base.py
# @Description : 规则层,组合各种逻辑
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc


class IBaseRule(abc.ABC):

    @abc.abstractmethod
    def is_satisfied(self) -> bool:
        raise NotImplementedError
