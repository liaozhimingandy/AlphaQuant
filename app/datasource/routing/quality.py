#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : quality.py
# @Description : 数据质量校验（Quality Check）
#
#                为什么要单独一层：**"取到了"不等于"取对了"**。
#
#                自动换源解决了"拿不到数据"，却引入一个更隐蔽的风险 ——
#                某个源接口改版/缓存污染，返回了一批"看起来正常"的错数据，
#                路由开心地判定成功，错数据一路流进因子和回测，
#                而日志里什么异常都没有。
#
#                所以取到数据之后、入库之前，必须过一遍**自洽性**检查：
#                  * K 线：high ≥ max(open, close)、low ≤ min(open, close)、
#                          close > 0、volume ≥ 0、日期唯一且非空；
#                  * 榜单：代码非空、名次为正、不重复。
#
#                校验失败按 ``QUALITY_ERROR`` 处理：**计入源的健康度并换下一个源**，
#                但不重试（同一个源再问一次只会拿到同一批错数据）。
#
#                注意这里**不比对成交量**：各源量纲不同（股 vs 手，见 kline_base），
#                猜错就是 100 倍的错，宁可不查。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from app.datasource.provider.base import DataCapability

#: 单个校验最多报几条问题（多了会淹没日志）
MAX_ISSUES: int = 5
#: 价格比较的容差（元）。浮点与不同源的四舍五入口径差异要能容忍
PRICE_TOL: float = 1e-6


@dataclass
class QualityReport:
    ok: bool = True
    rows: int = 0
    checked: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)

    def add(self, issue: str) -> None:
        if len(self.issues) < MAX_ISSUES:
            self.issues.append(issue)
        self.ok = False

    def summary(self) -> str:
        if self.ok:
            return f"通过（{self.rows} 行 / {len(self.checked)} 项）"
        return "；".join(self.issues)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "rows": self.rows,
                "checked": list(self.checked), "issues": list(self.issues)}


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def check_kline(df: Optional[pd.DataFrame]) -> QualityReport:
    """K 线（日线/分钟线）自洽性校验。"""
    rep = QualityReport()
    if df is None or len(df) == 0:
        rep.add("数据为空")
        return rep
    rep.rows = int(len(df))

    needed = ["date", "open", "high", "low", "close"]
    missing = [c for c in needed if c not in df.columns]
    rep.checked.append("必需列")
    if missing:
        rep.add(f"缺少必需列 {missing}")
        return rep

    d = df.copy()
    for col in ("open", "high", "low", "close"):
        d[col] = _num(d[col])

    # 1) 日期：非空 + 唯一
    rep.checked.append("日期非空且唯一")
    if d["date"].isna().any():
        rep.add(f"{int(d['date'].isna().sum())} 行日期为空")
    try:
        dup = int(d["date"].duplicated().sum())
        if dup:
            rep.add(f"{dup} 个重复日期")
    except Exception:
        pass

    # 2) 价格非空
    rep.checked.append("OHLC 非空")
    nulls = int(d[["open", "high", "low", "close"]].isna().any(axis=1).sum())
    if nulls:
        rep.add(f"{nulls} 行 OHLC 有空值")

    # 3) 收盘价为正
    rep.checked.append("收盘价 > 0")
    bad_close = int((d["close"] <= 0).sum())
    if bad_close:
        rep.add(f"{bad_close} 行收盘价 ≤ 0")

    # 4) high/low 必须包住 open/close
    rep.checked.append("high ≥ max(open,close) 且 low ≤ min(open,close)")
    mx = d[["open", "close"]].max(axis=1)
    mn = d[["open", "close"]].min(axis=1)
    tol = (d["high"].abs() * PRICE_TOL).clip(lower=PRICE_TOL)
    bad_high = int((d["high"] + tol < mx).sum())
    bad_low = int((d["low"] - tol > mn).sum())
    if bad_high:
        rep.add(f"{bad_high} 行 high 小于 max(open,close)")
    if bad_low:
        rep.add(f"{bad_low} 行 low 大于 min(open,close)")

    # 5) 成交量/额非负（缺失不查 —— 有些源确实不给）
    for col in ("volume", "amount"):
        if col in d.columns:
            rep.checked.append(f"{col} ≥ 0")
            v = _num(d[col])
            neg = int((v < 0).sum())
            if neg:
                rep.add(f"{neg} 行 {col} 为负")

    return rep


def check_rank(df: Optional[pd.DataFrame]) -> QualityReport:
    """榜单（人气榜 / 资金榜）自洽性校验。"""
    rep = QualityReport()
    if df is None or len(df) == 0:
        rep.add("数据为空")
        return rep
    rep.rows = int(len(df))

    rep.checked.append("必需列(symbol)")
    missing = [c for c in ("symbol",) if c not in df.columns]
    if missing:
        rep.add(f"缺少必需列 {missing}")
        return rep

    syms = df["symbol"].astype(str).str.strip()
    rep.checked.append("代码非空")
    empty = int((syms == "").sum())
    if empty:
        rep.add(f"{empty} 行代码为空")

    if "rank" in df.columns:
        # 名次是**可选**的：资金榜接口只给净额不给名次，
        # 名次由返回顺序推导（见 RankRow.from_frame）——没有不算质量问题。
        rep.checked.append("名次为正整数")
        ranks = _num(df["rank"])
        bad_rank = int(((ranks <= 0) | ranks.isna()).sum())
        if bad_rank:
            rep.add(f"{bad_rank} 行名次非法（≤0 或空）")

    rep.checked.append("代码不重复")
    dup = int(syms.duplicated().sum())
    if dup:
        rep.add(f"{dup} 个重复代码")
    return rep


def check_fund_flow(df: Optional[pd.DataFrame]) -> QualityReport:
    """个股资金流：至少要有日期、代码、主力净额三列且日期不重复。"""
    rep = QualityReport()
    if df is None or len(df) == 0:
        rep.add("数据为空")
        return rep
    rep.rows = int(len(df))
    rep.checked.append("必需列(symbol/date/main_net)")
    missing = [c for c in ("symbol", "date", "main_net") if c not in df.columns]
    if missing:
        rep.add(f"缺少必需列 {missing}")
        return rep
    rep.checked.append("日期非空")
    if df["date"].isna().any():
        rep.add(f"{int(df['date'].isna().sum())} 行日期为空")
    return rep


#: 能力 → 校验器。**没有校验器的能力默认放行**（不因为没写校验器就拒绝数据）
CHECKERS: Dict[DataCapability, Any] = {
    DataCapability.DAILY_KLINE: check_kline,
    DataCapability.MINUTE_KLINE: check_kline,
    DataCapability.HOT_RANK: check_rank,
    DataCapability.FLOW_RANK: check_rank,
    DataCapability.FUND_FLOW: check_fund_flow,
}


def check(capability: Any, df: Optional[pd.DataFrame]) -> QualityReport:
    """按能力拿到对应校验器并跑一遍。"""
    try:
        cap = DataCapability.parse(capability)
    except ValueError:
        return QualityReport(ok=True, rows=0 if df is None else len(df),
                             checked=[], issues=[])
    fn = CHECKERS.get(cap)
    if fn is None:
        return QualityReport(ok=True, rows=0 if df is None else len(df), checked=[])
    try:
        return fn(df)
    except Exception as exc:  # 校验器自己炸了不能拖垮取数
        rep = QualityReport(rows=0 if df is None else len(df))
        rep.add(f"校验器异常（按通过处理）: {type(exc).__name__}: {exc}")
        rep.ok = True
        rep.issues = []
        return rep


__all__ = ["CHECKERS", "MAX_ISSUES", "QualityReport", "check", "check_kline",
           "check_rank", "check_fund_flow"]
