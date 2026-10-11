#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : mootdx_source.py
# @Description : mootdx（通达信）日线数据源 —— https://github.com/mootdx/mootdx
#
#                为什么值得加它（在已经有 baostock / akshare 的前提下）：
#                  * **不依赖东财域名**。baostock 走自己的服务器、
#                    akshare 的日线走东财 push2his —— 本项目踩过"本机代理把
#                    push2his 拦掉"的坑，多一个**不同风控面**的源是实打实的冗余；
#                  * **可以完全离线**。装了通达信客户端的话，
#                    `Reader` 直接读本地 .day 二进制文件，不联网、不限流、
#                    不怕接口改版（改的是文件格式，几年才变一次）；
#                  * 通达信的行情源是券商在用的同一套，数据本身质量没问题。
#
#                两条取数路径（自动选）：
#                  1. 配置了 `MOOTDX_TDX_DIR`（通达信数据目录）→ 走本地 Reader；
#                  2. 否则 → 走线上 `Quotes`（TCP 直连通达信行情服务器，需国内 IP）。
#
#                异常语义（注册表靠它决定"降权还是跳过"）：
#                  * 没装 mootdx / 没配目录且连不上 → ``DataUnavailable``（源没坏，
#                    只是这条路径不可用）；
#                  * 接口调用本身报错 → ``SourceError``（计入熔断并降权）。
#
#                安装：``pip install 'mootdx'``（可选依赖，不装不影响其它源）。
#                注意它上游锁了 ``httpx<0.26``；若与其它依赖冲突可用
#                ``pip install --no-deps mootdx`` 后手动装回高版本 httpx
#                （mootdx 取行情走 TCP，运行时不经过 httpx）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import os
import threading
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from app.domain.symbol import normalize_symbol
from app.datasource.compat.kline_base import normalize_daily
from app.datasource.compat.registry import REGISTRY, DataUnavailable, SourceError
from app.common.logging import logger, log_throttled

#: 通达信本地数据目录（读本地 .day 文件用）。没配就只走线上行情。
TDX_DIR_ENV = "MOOTDX_TDX_DIR"
#: 线上行情每次最多取多少根日线（通达信单次上限 800）
_MAX_BARS = 800

_lock = threading.Lock()
_client: Any = None      # 线上 Quotes（懒建，复用连接）
_reader: Any = None      # 本地 Reader（懒建）


def _require_mootdx():
    """导入 mootdx。没装就抛 DataUnavailable —— 可选依赖不能用硬失败。"""
    try:
        from mootdx.quotes import Quotes  # noqa: PLC0415
    except Exception as exc:
        raise DataUnavailable(
            f"未安装 mootdx（pip install mootdx），跳过通达信源: "
            f"{type(exc).__name__}"
        ) from exc
    return Quotes


def tdx_dir() -> str:
    return str(os.getenv(TDX_DIR_ENV) or "").strip()


# ===========================================================================
# 两条取数路径
# ===========================================================================
def _local_reader():
    """本地通达信数据目录读取器（离线、零限流）。

    只在配置了 `MOOTDX_TDX_DIR` 时可用。目录里要有 vipdoc/sh/lday/*.day
    这类文件（装了通达信并下载过日线就有）。
    """
    global _reader
    with _lock:
        if _reader is not None:
            return _reader
        folder = tdx_dir()
        if not folder:
            return None
        try:
            from mootdx.reader import Reader  # noqa: PLC0415
        except Exception as exc:
            logger.debug(f"mootdx Reader 不可用: {exc}")
            return None
        try:
            _reader = Reader.factory(market="std", tdxdir=folder)
        except Exception as exc:
            log_throttled("WARNING",
                          f"通达信本地数据目录 {folder} 打开失败: "
                          f"{type(exc).__name__}: {str(exc)[:120]}",
                          key="mootdx-reader-fail")
            _reader = None
        return _reader


def _online_client():
    """线上通达信行情客户端（TCP）。复用连接，别每只票建一次。"""
    global _client
    with _lock:
        if _client is not None:
            return _client
        Quotes = _require_mootdx()
        try:
            _client = Quotes.factory(market="std", multithread=False,
                                     heartbeat=False, bestip=False, timeout=10)
        except TypeError:
            # 不同版本 factory 的参数不完全一致，退回最保守的调用
            _client = Quotes.factory(market="std")
        except Exception as exc:
            raise SourceError(
                f"mootdx 通达信行情连接失败: {type(exc).__name__}: {str(exc)[:160]}"
            ) from exc
        return _client


def _from_local(symbol: str) -> Optional[pd.DataFrame]:
    reader = _local_reader()
    if reader is None:
        return None
    code = normalize_symbol(symbol)
    try:
        df = reader.daily(symbol=code)
    except Exception as exc:
        logger.debug(f"通达信本地日线读取失败 {code}: {type(exc).__name__}: {exc}")
        return None
    if df is None or len(df) == 0:
        return None
    return df


def _from_online(symbol: str) -> pd.DataFrame:
    client = _online_client()
    code = normalize_symbol(symbol)
    try:
        # frequency=9 = 日线（通达信的 K 线种类编号，与分钟线的 0~8 区分）
        df = client.bars(symbol=code, frequency=9, offset=_MAX_BARS)
    except Exception as exc:
        raise SourceError(
            f"mootdx bars 调用失败 {code}: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc
    if df is None or len(df) == 0:
        raise DataUnavailable(f"mootdx 未返回 {code} 的日线（停牌或代码不存在）")
    return df


# ===========================================================================
# 适配器（注册到 REGISTRY 的 daily 能力）
# ===========================================================================
def _mootdx_daily(symbol: str, start: Any = None, end: Any = None,
                  adjust: str = "qfq") -> pd.DataFrame:
    """通达信日线。

    :param adjust: 复权方式。通达信线上接口只给**不复权**数据，本地文件同理；
        传 qfq/hfq 时这里无法真正复权，会按不复权返回并记一条 DEBUG
        —— 与其悄悄给一个"看起来复权其实没有"的序列，不如说明白。
    """
    code = normalize_symbol(symbol)

    # 本地优先：不联网、不限流，还能在断网时救急
    raw = _from_local(code)
    origin = "本地通达信数据目录"
    if raw is None:
        raw = _from_online(code)
        origin = "通达信线上行情"

    if str(adjust or "").lower() in ("qfq", "hfq"):
        logger.debug(
            f"mootdx 只提供不复权日线（{origin}），{adjust} 请求按不复权返回"
        )

    out = normalize_daily(raw, code, start=start, end=end)
    if out.empty:
        raise DataUnavailable(f"mootdx 日线解析后为空: {code}")
    logger.debug(f"mootdx 日线 | {code} | {len(out)} 条 | 来源: {origin}")
    return out


REGISTRY.register("mootdx", supports=["daily"], fetch=_mootdx_daily)


def reset_client() -> None:
    """丢弃已建立的连接（换网络环境 / 单测里用）。"""
    global _client, _reader
    with _lock:
        _client = None
        _reader = None


__all__ = ["TDX_DIR_ENV", "reset_client", "tdx_dir"]
