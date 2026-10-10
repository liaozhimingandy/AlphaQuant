#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : clean.py
# @Description : 数据清洗：从"数据采集"到"因子计算"之间的那道工序
#
#                为什么必须有这一层：
#                  数据源回给你的东西**不等于**可以喂给策略的东西。
#                  实测会遇到：重复行（重试导致）、时间倒序、OHLC 自相矛盾
#                  （high < low）、停牌日成交量 0 但价格凭空跳变、
#                  不复权与复权数据混在一起导致"涨幅 300%"的假信号。
#                  这些脏数据不会报错，只会让策略在错误的价格上做决定 ——
#                  最坏的一类 bug：回测赚钱，实盘亏钱，且查不出原因。
#
#                设计原则：**默认只标记不丢弃**。
#                  丢弃是不可逆的，而且分不清"真异常"和"你没想到的合法情况"
#                  （比如刚上市的新股首日涨幅本来就大）。
#                  标记出来让下游自己决定（策略可以选择不交易被标记的 bar），
#                  同时留下审计线索。真要丢弃时用 action="drop"。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.market.types import Bar
from app.utils.logger import logger

#: 标记位：出现这些标记的 bar 表示"数据有问题，谨慎使用"
FLAG_DUPLICATE = "duplicate"        # 同一时间戳重复
FLAG_OUT_OF_ORDER = "out_of_order"  # 时间倒序
FLAG_BAD_OHLC = "bad_ohlc"          # high/low/open/close 自相矛盾
FLAG_NON_POSITIVE = "non_positive"  # 价格 <= 0
FLAG_ABNORMAL_MOVE = "abnormal_move"  # 相对上一根跳得离谱
FLAG_ZERO_VOLUME = "zero_volume"    # 停牌或数据缺失
FLAG_LIMIT_UP = "limit_up"          # 涨停（实盘买不进）
FLAG_LIMIT_DOWN = "limit_down"      # 跌停（实盘卖不出）

#: 「不可交易」标记：实盘遇到这些状态是下不进去的
UNTRADABLE_FLAGS = frozenset({FLAG_ZERO_VOLUME, FLAG_LIMIT_UP, FLAG_LIMIT_DOWN,
                              FLAG_BAD_OHLC, FLAG_NON_POSITIVE})


@dataclass
class CleanReport:
    """清洗报告。让"数据被改过什么"这件事可审计。"""

    total: int = 0
    kept: int = 0
    dropped: int = 0
    duplicates_removed: int = 0
    reordered: bool = False
    flags: Dict[str, int] = field(default_factory=dict)
    first_dt: Optional[str] = None
    last_dt: Optional[str] = None
    filled_gaps: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "kept": self.kept,
            "dropped": self.dropped,
            "duplicates_removed": self.duplicates_removed,
            "reordered": self.reordered,
            "flags": dict(self.flags),
            "first_dt": self.first_dt,
            "last_dt": self.last_dt,
            "filled_gaps": self.filled_gaps,
        }

    @property
    def dirty(self) -> bool:
        return bool(self.duplicates_removed or self.flags or self.reordered)

    def summary(self) -> str:
        parts = [f"{self.kept}/{self.total} 保留"]
        if self.duplicates_removed:
            parts.append(f"去重 {self.duplicates_removed}")
        if self.reordered:
            parts.append("已按时间排序")
        for k, v in sorted(self.flags.items()):
            parts.append(f"{k}={v}")
        return " | ".join(parts)


class BarCleaner:
    """K 线清洗器。

    用法::

        cleaner = BarCleaner()
        bars, report = cleaner.clean(bars)
        if report.dirty:
            logger.warning(f"行情有异常: {report.summary()}")

    也可以直接清洗 DataFrame（采集入库前用）。
    """

    #: OHLC 一致性允许的浮点误差
    EPS = 1e-6

    def __init__(
        self,
        max_move_pct: Optional[float] = None,
        action: str = "",
        fill_gaps: bool = False,
        drop_flagged: bool = False,
    ) -> None:
        self.max_move_pct = float(
            settings.CLEAN_MAX_MOVE_PCT if max_move_pct is None else max_move_pct
        )
        self.action = str(action or settings.CLEAN_ACTION).strip().lower()
        #: 停牌日是否用上一根的价格补一根"零成交"的 bar。
        #: 补的好处是时间轴连续（算 N 周均线不会因为缺日而错位）；
        #: 坏处是引入"非真实成交价"。默认不补。
        self.fill_gaps = bool(fill_gaps)
        self.drop_flagged = bool(drop_flagged)

    # ---------------- Bar 列表 ----------------
    def clean(self, bars: Sequence[Bar]) -> Tuple[List[Bar], CleanReport]:
        rep = CleanReport(total=len(bars))
        if not bars:
            return [], rep

        rows = [
            {
                "dt": b.dt,
                "open": float(b.open), "high": float(b.high),
                "low": float(b.low), "close": float(b.close),
                "volume": float(b.volume or 0.0),
                "amount": float(getattr(b, "amount", 0.0) or 0.0),
                "symbol": b.symbol,
            }
            for b in bars
        ]

        # 1) 时间升序。倒序数据会让所有"前一根"语义全部失效
        if any(rows[i]["dt"] < rows[i - 1]["dt"] for i in range(1, len(rows))):
            rows.sort(key=lambda r: r["dt"])
            rep.reordered = True

        # 2) 去重（同一时间戳保留最后一根：后到的通常是修正过的）
        seen: Dict[Any, int] = {}
        deduped: List[Dict[str, Any]] = []
        for r in rows:
            idx = seen.get(r["dt"])
            if idx is None:
                seen[r["dt"]] = len(deduped)
                deduped.append(r)
            else:
                deduped[idx] = r
                rep.duplicates_removed += 1
        rows = deduped

        # 3) 逐根校验并打标记
        out: List[Bar] = []
        prev_close: Optional[float] = None
        for r in rows:
            flags = self._check_row(r, prev_close)
            r["flags"] = flags
            for f in flags:
                rep.flags[f] = rep.flags.get(f, 0) + 1

            if prev_close is None or r["close"] > 0:
                prev_close = r["close"]

            if self.drop_flagged and any(f in UNTRADABLE_FLAGS for f in flags):
                rep.dropped += 1
                continue
            out.append(self._to_bar(r))

        if self.action == "drop":
            # action=drop 时只丢"真正不可用"的，异常波动仍然保留但打标记 ——
            # 因为"波动大"和"数据错"是两件事，跌停打开当天本来就可能 20%
            before = len(out)
            out = [
                b for b in out
                if not (getattr(b, "flags", None) or set()) & UNTRADABLE_FLAGS
            ]
            rep.dropped += before - len(out)

        rep.kept = len(out)
        if out:
            rep.first_dt = out[0].dt.isoformat()
            rep.last_dt = out[-1].dt.isoformat()
        return out, rep

    def _check_row(self, r: Dict[str, Any], prev_close: Optional[float]) -> List[str]:
        flags: List[str] = []
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]

        if min(o, h, l, c) <= 0:
            flags.append(FLAG_NON_POSITIVE)
            return flags

        # high 必须 >= 其它三个，low 必须 <= 其它三个
        if (
            h + self.EPS < max(o, c, l)
            or l - self.EPS > min(o, c, h)
            or h + self.EPS < l
        ):
            flags.append(FLAG_BAD_OHLC)

        if r["volume"] <= 0:
            flags.append(FLAG_ZERO_VOLUME)

        if prev_close and prev_close > 0:
            move = abs(c - prev_close) / prev_close
            if move > self.max_move_pct + 1e-9:
                flags.append(FLAG_ABNORMAL_MOVE)
            # 严格等于阈值附近按涨跌停判定（A 股主板 10%、创业板/科创 20%）
            if abs(move - 0.10) < 0.002 or abs(move - 0.20) < 0.002:
                flags.append(FLAG_LIMIT_UP if c > prev_close else FLAG_LIMIT_DOWN)
        return flags

    def _to_bar(self, r: Dict[str, Any]) -> Bar:
        bar = Bar(
            symbol=r["symbol"], dt=r["dt"],
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r["volume"], amount=r["amount"],
        )
        # 标记挂在对象上而不是丢掉数据 —— 策略可以选择跳过带标记的 bar
        try:
            bar.flags = set(r.get("flags") or ())
        except Exception:  # pragma: no cover - Bar 若被改成 slots 则降级
            pass
        return bar

    # ---------------- DataFrame ----------------
    def clean_frame(self, df: pd.DataFrame, symbol: str = "") -> Tuple[pd.DataFrame, CleanReport]:
        """清洗采集入库前的 DataFrame。

        列约定：open/high/low/close/volume/amount + 时间列。
        缺列不报错而是尽力而为 —— 数据清洗不该成为"拿不到数据"的原因。
        """
        rep = CleanReport(total=len(df))
        if df is None or df.empty:
            return df, rep

        out = df.copy()

        # 时间列归一
        for col in ("dt", "date", "datetime", "time"):
            if col in out.columns:
                if col != "dt":
                    out = out.rename(columns={col: "dt"})
                break

        if "dt" in out.columns:
            if any(out["dt"].astype(str).str.len() > 10):
                out["dt"] = pd.to_datetime(out["dt"], errors="coerce")
            else:
                out["dt"] = pd.to_datetime(out["dt"], errors="coerce")

        # 数值列
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")

        need = {"open", "high", "low", "close"}
        if not need.issubset(out.columns):
            logger.debug(f"清洗跳过（缺 OHLC 列）: {sorted(out.columns)}")
            rep.kept = len(out)
            return out, rep

        # 丢掉价格缺失的行（这是唯一必须丢的：没有价格就没有一切）
        before = len(out)
        out = out.dropna(subset=list(need))
        rep.dropped += before - len(out)

        # 非正价格
        bad = out[(out[list(need)] <= 0).any(axis=1)]
        if len(bad):
            rep.flags[FLAG_NON_POSITIVE] = len(bad)
            out = out[~out.index.isin(bad.index)]

        # OHLC 矛盾
        if len(out):
            mask_bad = (
                (out["high"] + self.EPS < out[["open", "close", "low"]].max(axis=1))
                | (out["low"] - self.EPS > out[["open", "close", "high"]].min(axis=1))
            )
            if mask_bad.any():
                rep.flags[FLAG_BAD_OHLC] = int(mask_bad.sum())
                if self.action == "drop":
                    out = out[~mask_bad]

        # 排序 + 去重
        if "dt" in out.columns and len(out):
            if not out["dt"].is_monotonic_increasing:
                out = out.sort_values("dt")
                rep.reordered = True
            before = len(out)
            out = out.drop_duplicates(subset=["dt"], keep="last")
            rep.duplicates_removed = before - len(out)

        # 异常波动 / 涨跌停标记
        if len(out) > 1:
            prev = out["close"].shift(1)
            with np.errstate(invalid="ignore", divide="ignore"):
                move = (out["close"] - prev).abs() / prev
            abn = move > (self.max_move_pct + 1e-9)
            if abn.any():
                rep.flags[FLAG_ABNORMAL_MOVE] = int(abn.sum())
            out = out.copy()
            out["move_pct"] = move
            out["flags"] = ""
            if abn.any():
                out.loc[abn, "flags"] = FLAG_ABNORMAL_MOVE

        rep.kept = len(out)
        if len(out) and "dt" in out.columns:
            rep.first_dt = str(out["dt"].iloc[0])
            rep.last_dt = str(out["dt"].iloc[-1])
        return out, rep


#: 进程内默认清洗器（配置变了就重建）
_default: Optional[BarCleaner] = None


def default_cleaner() -> BarCleaner:
    global _default
    if _default is None:
        _default = BarCleaner()
    return _default


def clean_bars(bars: Sequence[Bar],
               cleaner: Optional[BarCleaner] = None) -> Tuple[List[Bar], CleanReport]:
    return (cleaner or default_cleaner()).clean(bars)


def clean_frame(df: pd.DataFrame, symbol: str = "",
                cleaner: Optional[BarCleaner] = None) -> Tuple[pd.DataFrame, CleanReport]:
    return (cleaner or default_cleaner()).clean_frame(df, symbol)


def is_tradable(bar: Bar) -> bool:
    """这根 bar 在实盘上是否可成交（停牌/涨跌停/数据坏 → False）。

    策略层可以据此跳过下单：明知涨停买不进还去下单，
    只会产生一堆被拒订单把审计日志淹掉。
    """
    flags = getattr(bar, "flags", None) or set()
    return not (set(flags) & UNTRADABLE_FLAGS)


__all__ = [
    "BarCleaner",
    "CleanReport",
    "clean_bars",
    "clean_frame",
    "default_cleaner",
    "is_tradable",
    "FLAG_ABNORMAL_MOVE",
    "FLAG_BAD_OHLC",
    "FLAG_DUPLICATE",
    "FLAG_LIMIT_DOWN",
    "FLAG_LIMIT_UP",
    "FLAG_NON_POSITIVE",
    "FLAG_OUT_OF_ORDER",
    "FLAG_ZERO_VOLUME",
    "UNTRADABLE_FLAGS",
]
