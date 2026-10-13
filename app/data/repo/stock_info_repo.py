#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : stock_info_repo.py
# @Description : 标的主表仓储（symbol → 这只票是谁、哪天上市、退没退市）
#
#                这里最重要的一个函数其实是 :func:`filter_listed`，
#                它体现了整张表唯一的设计难点：
#
#                    **"没有上市日期"不等于"已经上市"。**
#
#                因为 ``list_date`` 只能靠联网同步得到，而同步随时可能失败、
#                也可能压根还没跑过。若把 None 当成"已上市"，那么第一次同步
#                之前的所有K线任务都会去抓一堆还没上市的新股 —— 抓不到
#                任何数据，还平白消耗数据源的请求额度。反之若把
#                None 当成"未上市"，那么主表一旦同步失败，整套采集就停摆：
#                一个外部接口的故障导致本地功能不可用。
#
#                所以这里的取舍是：**None 一律放行，但计数回报**。
#                既不因缺失而误杀，也不隐瞒"这批标的的上市日期其实是未知的"。
#                "未知"这件事要让人看见（界面的提示、K线报告里的
#                ``unknown_list_date`` 字段），而不是被静默处理掉。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from app.common.logging import logger
from app.data.engine import SessionLocal
from app.data.schema import StockInfo
from app.domain.symbol import normalize_symbol

#: 表的读写都会用到的列（含主键）。写入时缺失的列留 None，不做臆测。
PROFILE_FIELDS: Tuple[str, ...] = (
    "symbol", "name", "market", "board", "industry",
    "list_date", "delisted", "total_share", "float_share",
    "source", "updated_at",
)

#: 允许被 upsert 覆盖的业务列。**刻意不含 ``name``**：
#: 名称由另一条链路（全市场快照）维护，那里更新得更勤，
#: 但这种"两条链路写同一张表"的安排必须精确约定谁写哪几列，
#: 否则覆盖面广的那个（只给名称的快照）会用 None 擦掉另一个刚同步好的
#: 上市日期 —— 这是本仓库所有 bug 里最难查的一类（数据不是丢了，是变空了）。
_UPDATABLE: Tuple[str, ...] = (
    "market", "board", "industry", "list_date", "delisted",
    "total_share", "float_share", "source", "updated_at",
)

_SCHEMA_READY_FOR = ""


def ensure_schema(engine: Any = None, force: bool = False) -> None:
    """建表 + 给老库补后加的列。幂等，可以放心每轮任务都调。

    :param engine: 显式指定库。**不传时跟随 SessionLocal 的引擎**
                   （测试替换 SessionLocal 指向临时库，这里就跟着补临时库），
                   而不是认死全局 engine —— 那样会出现"测试通过但补的是开发库"。
    """
    global _SCHEMA_READY_FOR
    try:
        if engine is None:
            bind = getattr(SessionLocal, "kw", {}).get("bind")
            engine = bind if bind is not None else None
        if engine is None:
            from app.data.engine import engine as default_engine  # 延迟导入避免循环

            engine = default_engine
        try:
            key = str(getattr(engine, "url", ""))
        except Exception:  # pragma: no cover
            key = repr(engine)
        # 换库了必须重来：一次性任务想串起"临时库 A → 临时库 B"的场景时，
        # 一个全局布尔会让第二个库悄悄地少了那些列。
        if key and _SCHEMA_READY_FOR == key and not force:
            return
        from app.data.migrate import ensure_columns

        StockInfo.__table__.create(bind=engine, checkfirst=True)
        ensure_columns(engine, StockInfo.__tablename__, {
            "market": "VARCHAR(8)",
            "board": "VARCHAR(16)",
            "industry": "VARCHAR(64)",
            "list_date": "DATE",
            "delisted": "BOOLEAN",
            "total_share": "FLOAT",
            "float_share": "FLOAT",
            "source": "VARCHAR(32)",
        })
        _SCHEMA_READY_FOR = key
    except Exception as exc:  # pragma: no cover - 建表失败不该拖垮整个任务
        logger.warning(f"标的主表就绪检查失败: {exc}")


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    txt = str(value).strip()
    if not txt:
        return None
    txt = txt.split(" ")[0].replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def _clean(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一行外部数据规整成可入库的形态。代码不合法就丢弃该行。"""
    try:
        sym = normalize_symbol(row.get("symbol") or "")
    except Exception:
        return None
    if not sym:
        return None
    out: Dict[str, Any] = {"symbol": sym}
    name = row.get("name")
    out["name"] = str(name).strip() if name is not None else ""
    out["market"] = (str(row.get("market") or "").strip() or None)
    out["board"] = (str(row.get("board") or "").strip() or None)
    out["industry"] = (str(row.get("industry") or "").strip() or None)
    out["list_date"] = _as_date(row.get("list_date"))
    delisted = row.get("delisted")
    out["delisted"] = bool(delisted) if delisted is not None else False
    for f in ("total_share", "float_share"):
        try:
            v = row.get(f)
            out[f] = float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            out[f] = None
    out["source"] = str(row.get("source") or "").strip() or None
    out["updated_at"] = datetime.now()
    return out


def save_profiles(rows: Sequence[Dict[str, Any]], db: Any = None) -> int:
    """写入/更新标的主表。

    合并语义（很重要）：``None`` **不覆盖**已有值。
    数据源是分批到的 —— 一次可能只带名称、下一次才带上市日期；
    若按"最后一次写入覆盖全部列"来做，第二轮的 None 会把第一轮写好的
    值擦掉，表现为"同步越勤，数据越少"。

    :return: 写入/更新的行数
    """
    clean = [r for r in (_clean(x) for x in rows) if r]
    if not clean:
        return 0
    ensure_schema()

    session = db or SessionLocal()
    own = db is None
    try:
        # 同一份输入里同一个代码出现多次时，保留信息最全的那条
        merged: Dict[str, Dict[str, Any]] = {}
        for r in clean:
            prev = merged.get(r["symbol"])
            if prev is None:
                merged[r["symbol"]] = r
                continue
            merged[r["symbol"]] = {
                k: (v if v not in (None, "", False) else prev.get(k))
                for k, v in r.items()
            }
        total = 0
        for sym, r in merged.items():
            existing = session.get(StockInfo, sym)
            if existing is None:
                session.add(StockInfo(**{k: v for k, v in r.items()}))
                total += 1
                continue
            changed = False
            for f in _UPDATABLE:
                v = r.get(f)
                if v in (None, ""):
                    continue
                if getattr(existing, f) != v:
                    setattr(existing, f, v)
                    changed = True
            # 名称为空说明这次同步没带名称，不是"这只票改名叫空字符串"
            if r.get("name"):
                if existing.name != r["name"]:
                    existing.name = r["name"]
                    changed = True
            if changed:
                total += 1
        if own:
            session.commit()
        return total
    finally:
        if own:
            session.close()


def get(symbol: str, db: Any = None) -> Optional[Dict[str, Any]]:
    """取一条标的信息。没有记录返回 None（不是"空的标的信息"）。"""
    try:
        sym = normalize_symbol(symbol)
    except Exception:
        return None
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        row = session.get(StockInfo, sym)
        return _to_dict(row) if row else None
    finally:
        if own:
            session.close()


def _to_dict(row: StockInfo) -> Dict[str, Any]:
    ld = _as_date(row.list_date)
    return {
        "symbol": row.symbol,
        "name": row.name or "",
        "market": row.market or "",
        "board": row.board or "",
        "industry": row.industry or "",
        "list_date": ld.isoformat() if ld else None,
        "delisted": bool(row.delisted),
        "total_share": row.total_share,
        "float_share": row.float_share,
        "source": row.source or "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def count(db: Any = None) -> int:
    """主表里有多少只票。"""
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        return int(session.execute(func.count(StockInfo.symbol)).scalar() or 0)
    finally:
        if own:
            session.close()


def all_symbols(db: Any = None, include_delisted: bool = False) -> List[str]:
    """主表里的全部代码。"""
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        q = select(StockInfo.symbol)
        if not include_delisted:
            q = q.where(StockInfo.delisted.isnot(True))
        return [str(r[0]) for r in session.execute(q).all()]
    finally:
        if own:
            session.close()


def missing_list_date(limit: int = 0, db: Any = None) -> List[str]:
    """列出"还没有上市日期"的标的 —— 它们是待补全的对象。"""
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        q = (select(StockInfo.symbol)
             .where(StockInfo.list_date.is_(None))
             .where(StockInfo.delisted.isnot(True))
             .order_by(StockInfo.symbol))
        if limit and limit > 0:
            q = q.limit(int(limit))
        return [str(r[0]) for r in session.execute(q).all()]
    finally:
        if own:
            session.close()


def filter_listed(symbols: Sequence[str], day: Any = None,
                  db: Any = None) -> Tuple[List[str], Dict[str, List[str]]]:
    """把一批代码按"今天该不该有K线"分类。

    :return: ``(可采集的, {"unknown": [...], "unlisted": [...], "delisted": [...]})``

    三个出口的意义各不相同，调用方必须分别处理：
      * ``unlisted`` —— 还没上市，永远不该出现在采集清单里；
      * ``delisted`` —— 已退市，历史数据要留，但不要再补；
      * ``unknown``  —— **放行**（见文件头说明），但要在报告里标出来，
                        否则"主表还没同步"这件事会一直没人知道。
    """
    codes = list(dict.fromkeys(symbols or []))
    if not codes:
        return [], {"unknown": [], "unlisted": [], "delisted": []}
    ensure_schema()
    day_d = _as_date(day) or date.today()

    session = db or SessionLocal()
    own = db is None
    try:
        rows = session.execute(
            select(StockInfo).where(StockInfo.symbol.in_(codes))
        ).scalars().all()
        known = {r.symbol: r for r in rows}
    finally:
        if own:
            session.close()

    keep: List[str] = []
    buckets: Dict[str, List[str]] = {"unknown": [], "unlisted": [], "delisted": []}
    for s in codes:
        row = known.get(s)
        if row is None:
            # 主表里查无此人：**放行**。宁可多采一只，也不要因为主表
            # 还没同步就让整套K线任务空转 —— 那是"外部数据缺失
            # 导致本地功能停摆"，属于必须避免的耦合方向。
            buckets["unknown"].append(s)
            keep.append(s)
            continue
        if row.delisted:
            buckets["delisted"].append(s)
            continue
        ld = _as_date(row.list_date)
        if ld is None:
            buckets["unknown"].append(s)
            keep.append(s)
            continue
        if day_d < ld:
            buckets["unlisted"].append(s)
            continue
        keep.append(s)
    return keep, buckets


def board_stats(db: Any = None) -> Dict[str, int]:
    """按板块统计主表覆盖情况（面板用）。"""
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        rows = session.execute(
            select(StockInfo.board, func.count(StockInfo.symbol)).group_by(StockInfo.board)
        ).all()
    finally:
        if own:
            session.close()
    out: Dict[str, int] = {}
    for board, n in rows:
        out[str(board or "未知")] = int(n or 0)
    return out


def stale_days(db: Any = None) -> Optional[int]:
    """主表离现在多久没更新了（天）。空表返回 None。

    这条服务于"什么时候该重同步"的判断。**主表不全比主表陈旧更致命** ——
    少一千只票的本表会让K线任务跳过一千只标的，而陈旧一天的本表
    最多让一两只新股晚一天进池。所以判断顺序是：先看缺口数量，再看更新时间。
    """
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        latest = session.execute(func.max(StockInfo.updated_at)).scalar()
    finally:
        if own:
            session.close()
    if latest is None:
        return None
    try:
        return (datetime.now() - latest).days
    except Exception:  # pragma: no cover
        return None


def summary(db: Any = None) -> Dict[str, Any]:
    """给面板的一句话概况。"""
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        _n = lambda cond: int(  # noqa: E731 - 三个口径一样的计数，抽成局部函数更清楚
            session.execute(select(func.count(StockInfo.symbol)).where(cond)).scalar() or 0
        )
        total = int(session.execute(func.count(StockInfo.symbol)).scalar() or 0)
        no_date = _n(StockInfo.list_date.is_(None))
        delisted = _n(StockInfo.delisted.is_(True))
        latest = session.execute(func.max(StockInfo.updated_at)).scalar()
    finally:
        if own:
            session.close()
    return {
        "total": total,
        "missing_list_date": no_date,
        "delisted": delisted,
        "coverage": (1 - no_date / total) if total else 0.0,
        "updated_at": latest.isoformat() if latest else None,
        "boards": board_stats(db),
    }


__all__ = [
    "PROFILE_FIELDS",
    "ensure_schema",
    "save_profiles",
    "get",
    "count",
    "all_symbols",
    "missing_list_date",
    "filter_listed",
    "board_stats",
    "stale_days",
    "summary",
]
