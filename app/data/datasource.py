#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
=================================================
    @Project: AlphaQuant
    @File： datasource.py
    @Author：liaozhimingandy
    @Email: liaozhimingandy@gmail.com
    @Date：2026/5/26 21:45
    @Desc: 
=================================================
"""
import abc
from typing import Dict, Optional, List

import akshare as ak
import baostock as bs
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.utils.logger import logger


class IBaseDataSource(abc.ABC):
    """
    数据源抽象类
    """
    name: str = "base"

    REQUIRED_COLUMNS = [
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount"
    ]

    @abc.abstractmethod
    @retry(
        stop=stop_after_attempt(5),  # 最多重试5次
        wait=wait_exponential(multiplier=2, max=60),  # 指数退避
        retry=retry_if_exception_type(Exception),  # 捕获所有异常
        reraise=False,
        before=lambda rs: logger.info(f"🔁 第 {rs.attempt_number} 次尝试"),
    )
    def fetch_data(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        """
        统一数据获取接口，所有子类必须实现
        输出格式强制统一：索引为datetime，列名open/high/low/close/volume，直接兼容backtrader
        """
        pass

    @staticmethod
    def _standardize_df(df: pd.DataFrame) -> pd.DataFrame:
        """统一格式化输出，所有数据源都走这个方法，保证100%格式一致"""
        df = df.copy()

        # 只保留统一字段
        df = df[IBaseDataSource.REQUIRED_COLUMNS]

        # NaN -> None
        df = df.where(pd.notnull(df), None)

        # 数值列统一
        numeric_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount"
        ]

        for col in numeric_cols:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        # 日期统一
        df["date"] = pd.to_datetime(df["date"])

        # 删除脏数据
        df = df.dropna()

        # 排序
        df = df.sort_values("date")

        # 重置索引
        df = df.reset_index(drop=True)

        return df


class AkshareDataSource(IBaseDataSource):
    """Akshare数据源：免费、数据全，优先级第二"""
    name = "akshare"

    @retry(
        stop=stop_after_attempt(5),  # 最多重试5次
        wait=wait_exponential(multiplier=2, max=60),  # 指数退避
        retry=retry_if_exception_type(Exception),  # 捕获所有异常
        reraise=False,
        before=lambda rs: logger.info(f"[{rs.fn.__qualname__}] 🔁 第 {rs.attempt_number} 次尝试"),
    )
    def fetch_data(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust
            )
            df = df.rename(columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount"
            })
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("date")
            return self._standardize_df(df)
        except Exception as e:
            raise e


class BaoStockDataSource(IBaseDataSource):

    name = "baostock"

    def __init__(self):
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(
                f"Baostock登录失败: {lg.error_msg}"
            )
    def __del__(self):
        try:
            bs.logout()
        except Exception as e:
            pass

    @retry(
        stop=stop_after_attempt(5),  # 最多重试5次
        wait=wait_exponential(multiplier=2, max=60),  # 指数退避
        retry=retry_if_exception_type(Exception),  # 捕获所有异常
        reraise=True,
        before=lambda rs: logger.info(f"🔁 第 {rs.attempt_number} 次尝试"),
    )
    def fetch_data(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:

        bs_code = f"sh.{code}" if code.startswith(("6", "9")) else f"sz.{code}"
        start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

        # -------------------------- 3. 复权参数映射（避免写反） --------------------------
        adjust_map = {
            "qfq": "2",  # 前复权（和行情软件一致）
            "hfq": "1",  # 后复权
            "不复权": "3"  # 不复权
        }
        adjust_flag = adjust_map.get(adjust, "2")

        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag=adjust_flag
        )

        data_list = []
        while rs.error_code == "0" and rs.next():
            data_list.append(rs.get_row_data())

        df = pd.DataFrame(data_list, columns=rs.fields)
        df["symbol"] = code

        return self._standardize_df(df)

class DataSourceFactory:

    _data_sources: Dict[str, IBaseDataSource] = {
        "akshare": AkshareDataSource(),
        "baostock": BaoStockDataSource()
    }

    @classmethod
    def register_data_source(cls, name: str, data_source: IBaseDataSource):
        """注册自定义数据源"""
        cls._data_sources[name] = data_source

    @classmethod
    def get_stock_data(
        cls,
        code: str,
        start: str,
        end: str,
        adjust: str = "qfq",
        priority: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        对外唯一统一入口：按优先级自动尝试数据源，失败自动降级
        :param code: 股票代码 000001/600000
        :param start: 开始日期 2020-01-01 或 20200101
        :param end: 结束日期 2025-12-31 或 20251231
        :param adjust: 复权方式 qfq/hfq/不复权
        :param priority: 自定义数据源优先级，不填则用全局配置
        """
        # 日期格式统一处理
        start_date = start.replace("-", "")
        end_date = end.replace("-", "")
        priority = priority or ["baostock", "akshare"]

        # 按优先级依次尝试，失败自动切换下一个
        for source_name in priority:

            if source_name not in cls._data_sources:
                continue
            try:
                return cls._data_sources[source_name].fetch_data(code, start_date, end_date, adjust)
            except Exception as e:
                logger.warning(f"⚠️ [{source_name}] 获取失败: {str(e)}，自动切换下一个数据源")
                continue

        raise RuntimeError("❌ 所有数据源均获取失败，请检查网络或配置")