#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/28 11:28
# @FileName    : interfaces.py
# @Description : 数据层,数据采集器
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import abc
import pandas as pd


class IDataSource(abc.ABC):

    @abc.abstractmethod
    def get_bars(
            self,
            symbol: str,
            start: str,
            end: str,
            timeframe: str = "1d"
    ) -> pd.DataFrame:
        """
        获取K线数据
        """
        raise NotImplementedError
