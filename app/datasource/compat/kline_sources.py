#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : kline_sources.py
# @Description : 日线（K线）数据源的注册入口
#
#                注册表原本只管截面数据（资金流/人气榜/资金榜），K线走的是
#                `DataSourceFactory` 那条老链路。结果就是：新加一个 K线源
#                （mootdx / a-stock-data）注册进去也没人用——`fetch_daily`
#                根本不看注册表。
#
#                这里把 K线也接进同一套机制，好处是立刻继承到注册表的全部能力：
#                  * 权重排序（不可用就自动降权，见 docs/DATA_SOURCES.md）
#                  * 每源重试 N 次 → 换下一个源 → 全挂告警
#                  * 熔断 + 采集留痕（这批 K线是哪个源给的，日志里可查）
#
#                **老链路保留不动**：`DataSourceFactory` / `MarketDataService`
#                仍在用（采集组件、回测取数都依赖它），两条链路并存直到迁移完成。
#                新的数据校验服务（data_guard）走的是注册表这条，
#                因为补全缺口最需要的就是"一个源挂了自动换下一个"。
#
#                加载时机：引擎 import `app.datasource.service.facade` 时由 marketx 末尾
#                import 本模块，注册即完成（见 docs 里的"注册到引擎"三步）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from app.domain.symbol import (
    DataFetchError,
    DataSourceFactory,
    normalize_date,
    normalize_symbol,
)
from app.datasource.compat.kline_base import DAILY_COLUMNS, normalize_daily
from app.datasource.provider import bootstrap as _bootstrap
from app.datasource.provider.base import DataCapability, ProviderError
from app.datasource.service.data_service import data_service
from app.datasource.compat.registry import (
    REGISTRY,
    DataUnavailable,
    SourceError,
)
from app.common.logging import logger

#: 日线数据类型名（注册表的 kind）
DAILY_KIND = "daily"


def _fmt(value: Any, default: str) -> str:
    """把 date / 'YYYY-MM-DD' / 'YYYYMMDD' 统一成 'YYYYMMDD'。缺省给 default。"""
    if value is None:
        return default
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    try:
        return normalize_date(str(value))
    except Exception:
        return default


def _default_start(days: int = 400) -> str:
    return (date.today() - timedelta(days=days)).strftime("%Y%m%d")


def _default_end() -> str:
    return date.today().strftime("%Y%m%d")


# ===========================================================================
# 内置源适配器
# ===========================================================================
def _baostock_daily(symbol: str, start: Any = None, end: Any = None,
                    adjust: str = "qfq") -> pd.DataFrame:
    """Baostock 日线。稳定、无需 token，本项目的一贯首选。"""
    code = normalize_symbol(symbol)
    try:
        raw = DataSourceFactory.get("baostock").fetch_data(
            code,
            _fmt(start, _default_start()),
            _fmt(end, _default_end()),
            adjust=adjust or "qfq",
        )
    except DataFetchError as exc:
        # Baostock 查不到这只票（退市/代码错/区间内没数据）是"数据缺失"，
        # 不是"源坏了" —— 别让它把 baostock 降权，否则全市场都跟着遭殃。
        text = str(exc)
        if "未返回" in text or "无数据" in text or "empty" in text.lower():
            raise DataUnavailable(text) from exc
        raise SourceError(f"Baostock 日线不可用: {text}") from exc
    except Exception as exc:
        raise SourceError(
            f"Baostock 日线失败: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc

    out = normalize_daily(raw, code, start=start, end=end)
    if out.empty:
        raise DataUnavailable(f"Baostock 日线解析后为空: {code}")
    return out


def _akshare_daily(symbol: str, start: Any = None, end: Any = None,
                   adjust: str = "qfq") -> pd.DataFrame:
    """Akshare 日线（东财 push2his）。数据全，但和东财共用风控面。"""
    code = normalize_symbol(symbol)
    try:
        raw = DataSourceFactory.get("akshare").fetch_data(
            code,
            _fmt(start, _default_start()),
            _fmt(end, _default_end()),
            adjust=adjust or "qfq",
        )
    except DataFetchError as exc:
        # akshare 底层多是东财域名；被代理拦时是 ProxyError → 交给
        # is_transient 判定为瞬时错误，注册表会先重试再换源。
        text = str(exc)
        if "未返回" in text or "无数据" in text:
            raise DataUnavailable(text) from exc
        raise SourceError(text) from exc
    except Exception as exc:
        raise SourceError(
            f"Akshare 日线失败: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc

    out = normalize_daily(raw, code, start=start, end=end)
    if out.empty:
        raise DataUnavailable(f"Akshare 日线解析后为空: {code}")
    return out


# baostock / akshare 与 DataCollectorComponent 用的是同一批实现，
# 这里只是把它们包成"注册表适配器"，让 K线也享受权重/换源/熔断。
REGISTRY.register("baostock_daily", supports=[DAILY_KIND], fetch=_baostock_daily)
REGISTRY.register("akshare_daily", supports=[DAILY_KIND], fetch=_akshare_daily)

# 两个新源：可选依赖，未安装时适配器自己抛 DataUnavailable 静默跳过
import app.datasource.compat.astockdata_source  # noqa: E402,F401  注册 astockdata（腾讯K线）
import app.datasource.compat.mootdx_source  # noqa: E402,F401  注册 mootdx（通达信）

# 把上面这些源**同时**装配进新的 Provider 层（能力/熔断/路由）。
# 旧注册表注册保留 —— 迁移期内两条链路并存，取数入口统一走新链路。
_bootstrap()


# ===========================================================================
# 统一入口
# ===========================================================================
def fetch_daily(symbol: str, start: Any = None, end: Any = None,
                adjust: str = "qfq", retries: Optional[int] = None,
                source: str = "") -> pd.DataFrame:
    """取日线（**走 Provider Router**，不再自己写降级顺序）。

    顺序来自 config/providers.json 的 ``routes.daily_kline``：
        baostock_daily → akshare_daily → astockdata → mootdx
    运行时再按"健康分 70% + 配置优先级 30%"自动调整；
    被限流/被拦的源会按 **(源, 能力, 路由)** 熔断，而不是把整个源判死。

    :param source: 指定只用某个源（排障 / 单测用），留空走自动链路
    :param retries: 覆盖本次每个源的重试次数（默认取配置）
    :return: 列 = :data:`app.datasource.compat.kline_base.DAILY_COLUMNS`
    :raises RuntimeError: 所有源都失败
    """
    code = normalize_symbol(symbol)
    try:
        df = data_service.get_kline_df(code, start=start, end=end, adjust=adjust,
                                       source=source, retries=retries)
    except ProviderError as exc:
        raise RuntimeError(f"{code} 日线获取失败: {exc}") from exc
    except RuntimeError:
        raise
    except Exception as exc:                      # 兼容旧调用方的异常契约
        raise RuntimeError(f"{code} 日线获取失败: {exc}") from exc
    if df is None or df.empty:
        raise RuntimeError(f"{code} 日线为空（所有数据源均无数据）")
    return df.reset_index(drop=True)


def daily_sources() -> list[str]:
    """当前可用的日线源（按路由排序）—— 面板/日志里说明"缺口会用谁来补"。

    优先给新链路的路由顺序；拿不到（尚未装配）时退回旧注册表，
    保证迁移期内任何时刻都能回答"日线有谁能给"。
    """
    try:
        names = [r.name for r in data_service.router.resolve(DAILY_KIND)]
        if names:
            return names
    except Exception:
        pass
    return [s.name for s in REGISTRY.sources_for(DAILY_KIND)]


__all__ = [
    "DAILY_COLUMNS",
    "DAILY_KIND",
    "daily_sources",
    "fetch_daily",
]
