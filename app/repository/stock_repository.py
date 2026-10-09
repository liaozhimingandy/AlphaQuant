#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : stock_repository.py
# @Description : 日线数据仓储：幂等入库、查询、覆盖度统计
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from typing import Dict, Iterator, List, Optional, Sequence

import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.data.datasource import normalize_symbol
from app.db.database import SessionLocal
from app.db.models import StockDaily
from app.utils.logger import logger

# 查询返回的统一列结构
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


def _to_date(value) -> date:
    """把 '2024-01-01' / '20240101' / date / datetime 统一成 datetime.date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip().replace("-", "").replace("/", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"日期格式非法(需 YYYY-MM-DD 或 YYYYMMDD): {value!r}")
    return datetime.strptime(raw, "%Y%m%d").date()


@contextmanager
def session_scope(session: Optional[Session] = None) -> Iterator[Session]:
    """会话上下文：自动提交/回滚/关闭。

    传入外部 session 时只借用、不关闭；否则自建自管。
    """
    own = session is None
    db = session or SessionLocal()
    try:
        yield db
        if own:
            db.commit()
    except Exception:
        if own:
            db.rollback()
        raise
    finally:
        if own:
            db.close()


class StockRepository:
    """日线行情仓储。所有方法均为静态方法，session 由调用方或上下文管理。"""

    # ---------------- 写入 ----------------
    @staticmethod
    def _rows_from_records(records: Sequence[Dict]) -> List[Dict]:
        rows: List[Dict] = []
        for r in records:
            if "symbol" not in r or "date" not in r:
                raise ValueError(f"记录缺少 symbol/date 字段: {r}")
            d = _to_date(r["date"])
            row = {
                "symbol": normalize_symbol(r["symbol"]),
                "date": d,
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
        batch_size: int = 500,
        commit: bool = True,
    ) -> int:
        """批量写入（冲突即更新）。返回写入条数。

        旧实现用 on_conflict_do_nothing，导致"数据源更新了但库里还是旧值"；
        这里改成 upsert，重复采集可安全修正历史数据。

        :param commit: 是否在函数内提交。被 session_scope 包裹时应传 False，
                       把事务控制权交给外层，这样中途异常才能整体回滚。
        """
        if not records:
            return 0

        rows = StockRepository._rows_from_records(records)
        written = 0
        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            stmt = insert(StockDaily).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "date"],
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
        logger.info(f"入库完成 | 条数: {written}")
        return written

    @staticmethod
    def save_df(
        db: Session, df: pd.DataFrame, batch_size: int = 500, commit: bool = True
    ) -> int:
        """DataFrame 直接入库。"""
        if df is None or df.empty:
            logger.warning("传入的 DataFrame 为空，跳过入库")
            return 0
        records = df.to_dict(orient="records")
        return StockRepository.batch_upsert(
            db, records, batch_size=batch_size, commit=commit
        )

    # ---------------- 查询 ----------------
    @staticmethod
    def get_stock_df(
        db: Session,
        symbol: str,
        start_date=None,
        end_date=None,
    ) -> pd.DataFrame:
        """读取单只股票日线，索引为 date，列对齐 backtrader 需要的 OHLCV。"""
        sym = normalize_symbol(symbol)
        q = db.query(StockDaily).filter(StockDaily.symbol == sym)

        if start_date is not None:
            q = q.filter(StockDaily.date >= _to_date(start_date))
        if end_date is not None:
            q = q.filter(StockDaily.date <= _to_date(end_date))

        rows = q.order_by(StockDaily.date.asc()).all()

        if not rows:
            logger.warning(f"库中无数据 | symbol={sym} {start_date}~{end_date}")
            return pd.DataFrame(columns=OHLCV_COLUMNS).rename_axis("date")

        data = [
            {
                "date": r.date,
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
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        for col in OHLCV_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    @staticmethod
    def list_symbols(db: Session) -> List[Dict]:
        """列出库中所有标的及其覆盖区间。"""
        rows = (
            db.query(
                StockDaily.symbol,
                func.count(StockDaily.id).label("rows"),
                func.min(StockDaily.date).label("start"),
                func.max(StockDaily.date).label("end"),
            )
            .group_by(StockDaily.symbol)
            .order_by(StockDaily.symbol)
            .all()
        )
        return [
            {
                "symbol": r.symbol,
                "rows": int(r.rows),
                "start": str(r.start),
                "end": str(r.end),
            }
            for r in rows
        ]

    @staticmethod
    def get_coverage(db: Session, symbol: str) -> Optional[Dict]:
        """单只标的的覆盖情况，不存在则返回 None。"""
        sym = normalize_symbol(symbol)
        row = (
            db.query(
                func.count(StockDaily.id).label("rows"),
                func.min(StockDaily.date).label("start"),
                func.max(StockDaily.date).label("end"),
            )
            .filter(StockDaily.symbol == sym)
            .first()
        )
        if not row or not row.rows:
            return None
        return {
            "symbol": sym,
            "rows": int(row.rows),
            "start": str(row.start),
            "end": str(row.end),
        }

    @staticmethod
    def has_data(db: Session, symbol: str, start_date=None, end_date=None) -> bool:
        sym = normalize_symbol(symbol)
        q = db.query(func.count(StockDaily.id)).filter(StockDaily.symbol == sym)
        if start_date is not None:
            q = q.filter(StockDaily.date >= _to_date(start_date))
        if end_date is not None:
            q = q.filter(StockDaily.date <= _to_date(end_date))
        return bool(q.scalar())
