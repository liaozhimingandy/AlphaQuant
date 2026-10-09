#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : minute_repository.py
# @Description : 分钟线仓储：幂等入库、查询、覆盖度。
#                与日线分开是为了让"高频数据"不影响日线回测的扫描代价
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.data.datasource import MINUTE_PERIODS, normalize_symbol
from app.db.models import StockMinute

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


def _to_dt(value) -> datetime:
    """把 str / date / datetime / pandas Timestamp 统一成 datetime。"""
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):  # pandas Timestamp
        return value.to_pydatetime()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return pd.to_datetime(str(value)).to_pydatetime()


def normalize_period(period) -> str:
    """把 '5m'/'5min'/'5' 统一成 '5'。非法周期直接报错，别让脏数据进库。"""
    raw = str(period or "1").strip().lower()
    raw = raw.rstrip("min")
    if raw not in MINUTE_PERIODS:
        raise ValueError(
            f"不支持的分钟周期: {period!r} | 可用: {', '.join(MINUTE_PERIODS)}"
        )
    return raw


class MinuteRepository:
    """分钟线仓储。所有方法均为静态方法。"""

    # ---------------- 写入 ----------------
    @staticmethod
    def _rows_from_records(
        records: Sequence[Dict], period: str
    ) -> List[Dict]:
        rows: List[Dict] = []
        for r in records:
            if "symbol" not in r or "dt" not in r:
                raise ValueError(f"记录缺少 symbol/dt 字段: {r}")
            row = {
                "symbol": normalize_symbol(r["symbol"]),
                "period": normalize_period(r.get("period") or period),
                "dt": _to_dt(r["dt"]),
            }
            for col in OHLCV_COLUMNS:
                v = r.get(col)
                row[col] = None if v is None or pd.isna(v) else float(v)
            rows.append(row)
        return rows

    @staticmethod
    def batch_upsert(
        db: Session,
        records: Sequence[Dict],
        period: str = "1",
        batch_size: int = 1000,
        commit: bool = True,
    ) -> int:
        """批量写入（冲突即更新）。

        分钟线的最后一根是**未完成**的：9:35 采到的 5 分钟线只是这一分钟的快照，
        必须能被后续采集覆盖，否则当天所有 Bar 都是第一口的价格。
        """
        if not records:
            return 0

        rows = MinuteRepository._rows_from_records(records, period)
        written = 0
        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            stmt = insert(StockMinute).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "period", "dt"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                },
            )
            db.execute(stmt)
            written += len(chunk)
        if commit:
            db.commit()
        return written

    @staticmethod
    def save_df(
        db: Session,
        df: pd.DataFrame,
        period: str = "1",
        batch_size: int = 1000,
        commit: bool = True,
    ) -> int:
        if df is None or df.empty:
            return 0
        return MinuteRepository.batch_upsert(
            db, df.to_dict(orient="records"), period=period,
            batch_size=batch_size, commit=commit,
        )

    # ---------------- 查询 ----------------
    @staticmethod
    def get_minute_df(
        db: Session,
        symbol: str,
        period: str = "1",
        start=None,
        end=None,
    ) -> pd.DataFrame:
        sym = normalize_symbol(symbol)
        per = normalize_period(period)
        q = db.query(StockMinute).filter(
            StockMinute.symbol == sym, StockMinute.period == per
        )
        if start is not None:
            q = q.filter(StockMinute.dt >= _to_dt(start))
        if end is not None:
            q = q.filter(StockMinute.dt <= _to_dt(end))

        rows = q.order_by(StockMinute.dt.asc()).all()
        if not rows:
            return pd.DataFrame(columns=OHLCV_COLUMNS).rename_axis("dt")

        data = [
            {
                "dt": r.dt,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "amount": r.amount,
            }
            for r in rows
        ]
        df = pd.DataFrame(data)
        df["dt"] = pd.to_datetime(df["dt"])
        df = df.set_index("dt").sort_index()
        for col in OHLCV_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    @staticmethod
    def get_coverage(db: Session, symbol: str, period: str = "1") -> Optional[Dict]:
        sym = normalize_symbol(symbol)
        per = normalize_period(period)
        row = (
            db.query(
                func.count(StockMinute.id).label("rows"),
                func.min(StockMinute.dt).label("start"),
                func.max(StockMinute.dt).label("end"),
            )
            .filter(StockMinute.symbol == sym, StockMinute.period == per)
            .first()
        )
        if not row or not row.rows:
            return None
        return {
            "symbol": sym,
            "period": per,
            "rows": int(row.rows),
            "start": row.start.isoformat() if row.start else None,
            "end": row.end.isoformat() if row.end else None,
        }

    @staticmethod
    def list_symbols(db: Session) -> List[Dict]:
        rows = (
            db.query(
                StockMinute.symbol,
                StockMinute.period,
                func.count(StockMinute.id).label("rows"),
                func.max(StockMinute.dt).label("end"),
            )
            .group_by(StockMinute.symbol, StockMinute.period)
            .order_by(StockMinute.symbol, StockMinute.period)
            .all()
        )
        return [
            {
                "symbol": r.symbol,
                "period": r.period,
                "rows": int(r.rows),
                "end": r.end.isoformat() if r.end else None,
            }
            for r in rows
        ]

    @staticmethod
    def delete_before(db: Session, cutoff, period: Optional[str] = None,
                      symbol: Optional[str] = None, commit: bool = True) -> int:
        """按时间清理过期分钟线（分钟线通常只保留最近若干天）。"""
        q = db.query(StockMinute).filter(StockMinute.dt < _to_dt(cutoff))
        if period is not None:
            q = q.filter(StockMinute.period == normalize_period(period))
        if symbol is not None:
            q = q.filter(StockMinute.symbol == normalize_symbol(symbol))
        n = q.delete(synchronize_session=False)
        if commit:
            db.commit()
        return int(n)


__all__ = ["MinuteRepository", "normalize_period", "OHLCV_COLUMNS"]
