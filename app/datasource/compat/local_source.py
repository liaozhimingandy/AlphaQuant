#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地兜底数据源：外网全挂时，用**库里已有的数据**把榜单撑住。

为什么需要它
------------
人气榜（hot_rank）的三个源全是外网 HTTP（东财 / 雪球），
一旦代理抽风或域名被拦，``REGISTRY`` 会一路换到最后一个源然后抛
``hot_rank: 所有数据源均失败``，候选池直接 0 条。

但**库里往往已经有数据**：要么之前采到过人气榜（落在 ``stock_hot_rank``），
要么至少有日线（``stock_daily``）。行情数据本就是"关注度"的代理变量——
成交额前列 = 市场关注度前列。所以最后的兜底不该是"报错"，
而是**用本地数据出一份榜**，并在日志里说清楚这是兜底、数据是哪天的。

注意 mootdx / a-stock-data **不提供人气榜**：
mootdx 是通达信行情协议（K线/分时），a-stock-data 是行情层 + 资金流，
两者都没有"人气/热度/关注人数"这类舆情字段 —— 所以它们注册在 ``daily``，
**不是**没注册到引擎里，而是这个数据类型它们天生没有。

两级兜底
--------
1. 库里存过人气榜 → 复用最近一天那份（真实榜单，只是可能不是今天）；
2. 连人气榜都没有 → 用最近交易日的日线合成（成交额 × 涨幅排序）。

两级都没有 → 抛 ``DataUnavailable``（这是"没数据"，不是"源坏了"，
因此**不会给本地源降权**）。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.datasource.compat.registry import REGISTRY, DataUnavailable
from app.common.logging import logger

# 合成榜的规模：候选池默认 30 只，给 200 只足够筛，太多反而拖慢评分
SYNTH_TOP = 200


def _latest_hot(day) -> pd.DataFrame:
    """库里最近一天的人气榜（可能不是 day 当天）。"""
    from app.storage.repo import kline_repo as R

    rows = R.latest_pool("hot", day, limit=500)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame([{
        "symbol": str(r.get("symbol") or ""),
        "name": str(r.get("name") or ""),
        "rank": int(r.get("rank") or 0) or (i + 1),
        "pct_change": r.get("pct_change"),
    } for i, r in enumerate(rows)])
    return out[out["symbol"] != ""].reset_index(drop=True)


def _synthesize_from_daily(day) -> pd.DataFrame:
    """用最近交易日的日线合成一份"热度榜"。

    排序口径：**成交额**为主（绝对关注度），涨幅做小幅加权——
    纯成交额会把大盘银行股顶在最前面，对选股没意义；
    纯涨幅又会选出没人接盘的小票。两者相乘是个务实的折中。
    """
    from sqlalchemy import func, select as _select

    from app.storage.engine import SessionLocal
    from app.storage.schema import StockDaily

    with SessionLocal() as s:
        newest = s.execute(
            _select(func.max(StockDaily.date)).where(StockDaily.date <= day)
        ).scalar()
        if newest is None:
            return pd.DataFrame()
        rows = s.execute(
            _select(StockDaily.symbol, StockDaily.close, StockDaily.amount,
                    StockDaily.volume, StockDaily.date)
            .where(StockDaily.date == newest)
        ).all()

    recs = []
    for sym, close, amount, volume, d in rows:
        try:
            close_f = float(close or 0)
        except (TypeError, ValueError):
            continue
        if close_f <= 0:
            continue
        try:
            amt = float(amount or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            # 有些源不返回成交额（比如腾讯K线），用 成交量 × 收盘价 顶上
            try:
                amt = float(volume or 0) * close_f
            except (TypeError, ValueError):
                amt = 0.0
        if amt <= 0:
            continue                      # 停牌 / 无成交，不进榜
        recs.append({"symbol": str(sym), "amount": amt, "close": close_f})

    if not recs:
        return pd.DataFrame()

    df = pd.DataFrame(recs)
    df = df.sort_values("amount", ascending=False).head(SYNTH_TOP).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    # 名称从对照表补，没有就留空（候选池后面还会再兜一次）
    try:
        from app.storage.repo import kline_repo as R

        names = R.resolve_names(list(df["symbol"])) or {}
    except Exception:
        names = {}
    df["name"] = [str(names.get(s, "") or "") for s in df["symbol"]]
    df["pct_change"] = None
    logger.info(f"[本地兜底] 用 {newest} 的日线合成热度榜 {len(df)} 只"
                f"（成交额口径，非真实人气排名）")
    return df[["symbol", "name", "rank", "pct_change"]]


def _hot_rank_local(top: Optional[int] = None, trade_date=None) -> pd.DataFrame:
    """人气榜的最后一道防线：本地库兜底。"""
    from app.domain.calendar import TradeCalendar

    try:
        day = trade_date or TradeCalendar().last_trading_day()
    except Exception:
        from datetime import date as _date

        day = trade_date or _date.today()

    out = _latest_hot(day)
    if not out.empty:
        logger.warning(f"[本地兜底] 所有人气榜外网源均不可用，复用库内最近一天榜单"
                       f" {len(out)} 条（数据日期可能早于 {day}）")
    else:
        out = _synthesize_from_daily(day)

    if out.empty:
        raise DataUnavailable(
            "本地兜底也拿不到数据（库内既无人气榜、也无日线），请先跑一次行情采集"
        )
    return out.head(int(top)) if top else out


# 注册在 hot_rank **最后一位**（fallback=True：恒排最后，不参与权重竞争）。
# 它几乎不会失败，若按权重排迟早会涨回队首、抢在东财前面，
# 那就变成"明明有网却一直用本地旧数据"——比拿不到数据更难发现。
REGISTRY.register("local_hot", supports=["hot_rank"], fetch=_hot_rank_local,
                  fallback=True)

__all__ = ["_hot_rank_local", "SYNTH_TOP"]
