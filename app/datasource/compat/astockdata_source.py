#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : astockdata_source.py
# @Description : a-stock-data 数据源 —— https://github.com/simonlin1212/a-stock-data
#
#                先说清楚它是什么，否则很容易接错：
#                  它**不是一个 pip 包**，而是一份自包含的 SKILL.md
#                  （结构化 Markdown + 内嵌 Python），把 34 个数据源、87 个端点
#                  的调用细节（腾讯 K线的分段参数、通达信盘后包的二进制格式、
#                  东财的 Referer 头……）整理成 AI 助手直接能用的工具集。
#                  所以它对本项目的价值是：**一份经过实测校准的端点说明书**，
#                  我们按它给出的端点实现适配器，而不是 pip install 它。
#
#                这里实现的是它的**行情层主源**（见 SKILL.md §1.2）：腾讯财经 K线。
#                选它的理由：
#                  * **零鉴权**（除 iwencai 外全部免 Key），不像 Tushare 要积分；
#                  * **零依赖**：就是一个 HTTPS GET，用标准库 urllib 即可，
#                    不用为了一个源往 pyproject 里塞包；
#                  * **和东财不同风控面** —— 本项目踩过"代理拦 push2his"
#                    导致 akshare 日线全线失败，腾讯这条能独立活着。
#
#                已知边界（照 a-stock-data V3.10.1 的说明）：
#                  * 腾讯 K线**不返回成交额** → amount 列留空；
#                  * 成交量单位是「手」，但**科创板（688/689）是股**
#                    （V3.10.1 专门更正过这一条）；
#                  * qfq/hfq 由服务端计算，不需要本地复权因子。
#
#                想接它的其它层（资金流/研报/龙虎榜/公告…）完全同构：
#                按 SKILL.md 对应章节的端点写一个适配器 + 一行 register 即可，
#                见 docs/DATA_SOURCES.md「扩展数据源」。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import pandas as pd

from app.common.config import settings
from app.datasource.transport import net
from app.domain.symbol import normalize_symbol
from app.datasource.compat.kline_base import market_prefix, normalize_daily
from app.datasource.compat.registry import REGISTRY, DataUnavailable, SourceError
from app.common.logging import logger, log_throttled

#: 腾讯财经 K线端点（a-stock-data §1.2 的行情层主源）
TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
#: 单次最多取多少根。腾讯对 count 有上限，取 640 ≈ 两年半日线
_MAX_BARS = 640
#: 复权方式 → 返回体里的键名。不复权是 "day"，前/后复权是 "qfqday" / "hfqday"
_FQ_KEYS = {"qfq": "qfqday", "hfq": "hfqday", "": "day", "none": "day"}
#: 超时（秒）。腾讯这个接口在国内很快，给短一点，别拖住整批采集
_TIMEOUT = 15.0


def _http_get(url: str, params: Dict[str, Any], timeout: float = _TIMEOUT) -> Any:
    """GET + 解析 JSON。用标准库是有意的：不为单个源引入新依赖。

    异常语义：网络/HTTP 类 → ``SourceError``（降权 + 熔断）；
    返回体不是 JSON → 也按源故障处理（代理拦截页就是这种表现）。
    """
    # 走统一传输层：请求头、通道兜底（代理挂了改直连）、域名健康度
    # 都由 net 负责，这里只管业务语义。
    # 之前是自己 urllib 直连，于是"代理是僵尸端口"这种故障在每个源里
    # 各表现一次、各等一遍超时 —— 收敛到 net 后第一次失败就会被记住。
    try:
        return net.get_json(url, params=params, timeout=timeout)
    except net.NetworkError as exc:
        raise SourceError(f"腾讯K线请求失败: {exc}") from exc
    except Exception as exc:
        raise SourceError(
            f"腾讯K线请求失败: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc


def _param(symbol: str, period: str = "day", count: int = _MAX_BARS,
           adjust: str = "qfq", start: str = "", end: str = "") -> str:
    """拼腾讯的 param 串：`sh600000,day,20240101,20240201,320,qfq`。

    起止日期是可选的（留空就是"取最近 count 根"），
    但**位置必须保留** —— 少一个逗号整个参数就错位了。
    """
    code = normalize_symbol(symbol)
    fq = str(adjust or "").lower()
    if fq not in _FQ_KEYS:
        fq = ""
    return f"{market_prefix(code)}{code},{period},{start},{end},{int(count)},{fq}"


def fetch_tx_kline(symbol: str, adjust: str = "qfq", count: int = _MAX_BARS,
                   start: str = "", end: str = "") -> pd.DataFrame:
    """腾讯财经日线原始表（列 = date/open/close/high/low/volume）。

    注意腾讯给的顺序是 **open, close, high, low**（不是 open,high,low,close）——
    照 OHLC 的惯性取值会把收盘价当成最低价，而且**不会报错**（值都在合理范围），
    这是最难发现的一类错。
    """
    code = normalize_symbol(symbol)
    fq = str(adjust or "").lower()
    payload = _http_get(TX_KLINE_URL, {"param": _param(code, "day", count, fq,
                                                       start, end)})
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0", None):
        raise SourceError(f"腾讯K线返回异常 code={payload.get('code') if isinstance(payload, dict) else '?'}")
    data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
    node = data.get(f"{market_prefix(code)}{code}") or {}
    if not isinstance(node, dict):
        raise DataUnavailable(f"腾讯K线未包含 {code} 的数据")
    rows = node.get(_FQ_KEYS.get(fq, "day")) or node.get("day") or []
    if not rows:
        raise DataUnavailable(f"腾讯K线 {code} 返回空序列（停牌/新股/代码不存在）")

    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        # [日期, 开, 收, 高, 低, 量]
        out.append({
            "date": r[0], "open": r[1], "close": r[2],
            "high": r[3], "low": r[4], "volume": r[5],
        })
    if not out:
        raise DataUnavailable(f"腾讯K线 {code} 解析后为空")
    return pd.DataFrame(out)


# ===========================================================================
# 适配器（注册到 REGISTRY 的 daily 能力）
# ===========================================================================
def _astockdata_daily(symbol: str, start: Any = None, end: Any = None,
                      adjust: str = "qfq") -> pd.DataFrame:
    """a-stock-data 行情层（腾讯 K线）日线。

    :param start/end: 交给 :func:`normalize_daily` 做区间裁剪。
        腾讯接口本身也支持起止日期，但传了之后"最近 N 根"的语义会变，
        容易取到一段很短的历史；所以这里固定拉最近 N 根再由本地裁剪，
        行为更可预期。
    """
    code = normalize_symbol(symbol)
    raw = fetch_tx_kline(code, adjust=adjust)
    out = normalize_daily(raw, code, start=start, end=end)
    if out.empty:
        raise DataUnavailable(f"a-stock-data(腾讯K线) 日线解析后为空: {code}")
    logger.debug(f"a-stock-data 日线 | {code} | {len(out)} 条"
                 f"（腾讯K线，无成交额列）")
    return out


REGISTRY.register("astockdata", supports=["daily"], fetch=_astockdata_daily)


__all__ = ["TX_KLINE_URL", "fetch_tx_kline"]
