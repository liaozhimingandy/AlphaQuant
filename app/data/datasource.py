#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
=================================================
    @Project: AlphaQuant
    @File： datasource.py
    @Desc:  多数据源统一接入层：标准化输出 + 自动降级 + 限流
=================================================
"""
from __future__ import annotations

import abc
import threading
import time
from typing import Dict, List, Optional

import pandas as pd
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.utils.logger import logger

# akshare / baostock 都是重量级导入，放到真正使用时再加载，
# 避免 "import app.xxx" 就把网络库拖起来（也让 CLI 的 --help 秒开）
_ak = None
_bs = None


def _lazy_akshare():
    global _ak
    if _ak is None:
        import akshare as ak  # noqa: WPS433

        _ak = ak
    return _ak


def _lazy_baostock():
    global _bs
    if _bs is None:
        import baostock as bs  # noqa: WPS433

        _bs = bs
    return _bs


def normalize_symbol(code: str) -> str:
    """把各种写法归一化成 6 位纯数字代码。

    '000001' / '000001.SZ' / 'sz.000001' / 'sh600000' -> '000001' / '600000'
    """
    raw = str(code).strip().lower()

    # 先处理带点的写法：sh.600000 / 000001.SZ
    if "." in raw:
        left, right = raw.split(".", 1)
        raw = right if left in {"sh", "sz", "bj"} else left

    # 再处理无点前缀写法：sh600000
    for marker in ("sh", "sz", "bj"):
        if raw.startswith(marker):
            raw = raw[len(marker):]
            break

    raw = "".join(ch for ch in raw if ch.isdigit())
    if not raw:
        raise ValueError(f"无法解析股票代码: {code!r}")
    return raw.zfill(6)


def normalize_date(value: str) -> str:
    """把 '2024-01-01' / '20240101' 统一成 '20240101'。"""
    raw = str(value).strip().replace("-", "").replace("/", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"日期格式非法(需 YYYY-MM-DD 或 YYYYMMDD): {value!r}")
    return raw


class DataFetchError(RuntimeError):
    """数据源获取失败。"""


class IBaseDataSource(abc.ABC):
    """数据源抽象类。子类只需实现 fetch_data，输出格式由 _standardize_df 统一。"""

    name: str = "base"

    REQUIRED_COLUMNS = [
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]

    @abc.abstractmethod
    def fetch_data(
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        """统一数据获取接口。

        输出：DataFrame，含 date/symbol/ohlcv/amount 列，date 升序、索引重置。
        """
        raise NotImplementedError

    # ---------------- 通用工具 ----------------
    @staticmethod
    def _throttle() -> None:
        """简单限速：距上次请求不足最小间隔就 sleep。"""
        interval = settings.DATA_FETCH_MIN_INTERVAL
        if interval <= 0:
            return
        last = getattr(IBaseDataSource, "_last_request_ts", 0.0)
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        IBaseDataSource._last_request_ts = time.monotonic()

    @classmethod
    def _standardize_df(cls, df: pd.DataFrame, code: str) -> pd.DataFrame:
        """统一格式化输出，保证所有数据源列结构 100% 一致。"""
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=cls.REQUIRED_COLUMNS)

        df = df.copy()

        # 列名统一成小写，容忍大小写差异
        df.columns = [str(c).strip().lower() for c in df.columns]

        # 日期列：兼容 trade_date / date
        if "date" not in df.columns and "trade_date" in df.columns:
            df = df.rename(columns={"trade_date": "date"})

        # symbol 永远以入参为准，避免数据源缺失该列导致 KeyError
        df["symbol"] = normalize_symbol(code)

        missing = [c for c in cls.REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise DataFetchError(
                f"[{cls.name}] 返回数据缺少必需列: {missing}，实际列: {list(df.columns)}"
            )

        df = df[cls.REQUIRED_COLUMNS]

        numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date"] = pd.to_datetime(df["date"], errors="coerce")

        # 删除脏数据（停牌/空行）
        df = df.dropna(subset=["date", "open", "high", "low", "close"])

        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        df = df.reset_index(drop=True)
        return df


def _retry_kwargs(fn_name: str):
    return dict(
        stop=stop_after_attempt(settings.DATA_FETCH_MAX_RETRY),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type(Exception),
        reraise=True,
        # 只在真正重试时打日志，首次尝试不打扰
        before=lambda rs: logger.warning(f"[{fn_name}] 第 {rs.attempt_number} 次重试")
        if rs.attempt_number > 1
        else None,
    )


class BaostockDataSource(IBaseDataSource):
    """Baostock：稳定、无需 token，作为首选数据源。"""

    name = "baostock"

    def __init__(self):
        self._lock = threading.Lock()
        self._logged_in = False

    def _ensure_login(self) -> None:
        bs = _lazy_baostock()
        with self._lock:
            if self._logged_in:
                return
            lg = bs.login()
            if lg.error_code != "0":
                raise DataFetchError(f"Baostock 登录失败: {lg.error_msg}")
            self._logged_in = True
            logger.debug("Baostock 登录成功")

    def logout(self) -> None:
        bs = _lazy_baostock()
        with self._lock:
            if not self._logged_in:
                return
            try:
                bs.logout()
            except Exception as exc:  # pragma: no cover - 退出失败无需中断
                logger.debug(f"Baostock 登出异常(可忽略): {exc}")
            finally:
                self._logged_in = False

    def __del__(self):  # pragma: no cover - 解释器退出时的兜底清理
        try:
            self.logout()
        except Exception:
            pass

    @retry(**_retry_kwargs("baostock.fetch_data"))
    def fetch_data(
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        bs = _lazy_baostock()
        self._throttle()
        self._ensure_login()

        symbol = normalize_symbol(code)
        start = normalize_date(start_date)
        end = normalize_date(end_date)
        bs_code = f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"

        # 1=后复权 2=前复权 3=不复权
        adjust_map = {"qfq": "2", "hfq": "1", "none": "3", "不复权": "3"}
        adjust_flag = adjust_map.get(adjust, "2")

        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}"  # noqa: E731
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=fmt(start),
            end_date=fmt(end),
            frequency="d",
            adjustflag=adjust_flag,
        )
        if rs.error_code != "0":
            raise DataFetchError(f"Baostock 查询失败: {rs.error_msg}")

        rows: List[list] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        df = pd.DataFrame(rows, columns=rs.fields)
        df = self._standardize_df(df, symbol)
        if df.empty:
            raise DataFetchError(f"Baostock 未返回 {symbol} 在 {start}~{end} 的数据")
        return df


class AkshareDataSource(IBaseDataSource):
    """Akshare：数据全，作为降级数据源。"""

    name = "akshare"

    @retry(**_retry_kwargs("akshare.fetch_data"))
    def fetch_data(
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        ak = _lazy_akshare()
        self._throttle()

        symbol = normalize_symbol(code)
        start = normalize_date(start_date)
        end = normalize_date(end_date)

        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
        # 注意：这里必须把 "日期" 直接映射成 "date"，
        # 旧实现映射成 trade_date 后再 set_index("date") 会 KeyError。
        df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        df = self._standardize_df(df, symbol)
        if df.empty:
            raise DataFetchError(f"Akshare 未返回 {symbol} 在 {start}~{end} 的数据")
        return df


class CsvDataSource(IBaseDataSource):
    """本地 CSV 数据源：离线/测试兜底，文件名约定 <symbol>.csv。"""

    name = "csv"

    def __init__(self, csv_dir=None):
        self.csv_dir = csv_dir or settings.CSV_DIR

    def fetch_data(
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        symbol = normalize_symbol(code)
        path = self.csv_dir / f"{symbol}.csv"
        if not path.exists():
            raise DataFetchError(f"本地无该标的 CSV: {path}")

        df = pd.read_csv(path)
        df = df.rename(
            columns={
                "trade_date": "date",
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        df = self._standardize_df(df, symbol)

        start = pd.to_datetime(normalize_date(start_date))
        end = pd.to_datetime(normalize_date(end_date))
        df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
        if df.empty:
            raise DataFetchError(f"CSV 中 {symbol} 在 {start_date}~{end_date} 无数据")
        return df


class DataSourceFactory:
    """数据源工厂：懒加载 + 单例 + 按优先级自动降级。"""

    _instances: Dict[str, IBaseDataSource] = {}
    _builders = {
        "baostock": BaostockDataSource,
        "akshare": AkshareDataSource,
        "csv": CsvDataSource,
    }
    _lock = threading.Lock()

    @classmethod
    def register_data_source(cls, name: str, data_source: IBaseDataSource) -> None:
        with cls._lock:
            cls._instances[name] = data_source
            cls._builders[name] = lambda: data_source

    @classmethod
    def get(cls, name: str) -> IBaseDataSource:
        """按名字获取（懒加载）数据源实例。"""
        with cls._lock:
            if name not in cls._instances:
                if name not in cls._builders:
                    raise DataFetchError(f"未注册的数据源: {name}")
                cls._instances[name] = cls._builders[name]()
            return cls._instances[name]

    @classmethod
    def available_sources(cls) -> List[str]:
        return list(cls._builders.keys())

    @classmethod
    def get_stock_data(
        cls,
        code: str,
        start: str,
        end: str,
        adjust: str = "qfq",
        priority: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """统一入口：按优先级依次尝试，失败自动降级到下一个数据源。"""
        symbol = normalize_symbol(code)
        start_d = normalize_date(start)
        end_d = normalize_date(end)
        if start_d > end_d:
            raise ValueError(f"开始日期不能晚于结束日期: {start} > {end}")

        priority = priority or settings.priority_list()
        errors: List[str] = []

        for source_name in priority:
            if source_name not in cls._builders:
                logger.warning(f"数据源 {source_name} 未注册，跳过")
                continue
            try:
                logger.info(f"尝试数据源 [{source_name}] | {symbol} {start_d}~{end_d}")
                df = cls.get(source_name).fetch_data(symbol, start_d, end_d, adjust)
                if df.empty:
                    raise DataFetchError("返回空数据")
                logger.success(
                    f"数据源 [{source_name}] 获取成功 | {len(df)} 条"
                )
                return df
            except Exception as exc:
                msg = f"[{source_name}] {type(exc).__name__}: {exc}"
                logger.warning(f"⚠️ {msg}，自动切换下一个数据源")
                errors.append(msg)
                continue

        raise DataFetchError(
            f"所有数据源均获取失败 ({symbol} {start_d}~{end_d})：\n  " + "\n  ".join(errors)
        )

    @classmethod
    def shutdown(cls) -> None:
        """释放数据源持有的连接（如 baostock 会话）。"""
        with cls._lock:
            for name, instance in cls._instances.items():
                close = getattr(instance, "logout", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:  # pragma: no cover
                        logger.debug(f"关闭数据源 {name} 异常: {exc}")
