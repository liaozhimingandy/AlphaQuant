#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : store.py
# @Description : 账户仓储 —— 「钱到底在谁名下」这张表的读写
#
#                它是 :class:`app.data.schema.BrokerAccount` 的唯一读写口。
#                文件不长，但有三条纪律值得写下来，它们全都是踩出来的：
#
#                1. **初始资金只在账户为空时写一次**。
#                   本金是收益率的分母，"跑了半年把 initial_cash 从 10 万改成
#                   100 万"会让历史收益率瞬间变成 1/10 —— 这不是数据修正，
#                   这是篡改历史。所以 ``initial_cash`` 只在**新建**时用，
#                   后续所有入口（含 :meth:`update_cash`）都不碰它。
#
#                2. **改经费走的是"加法"，不是"赋值"**。
#                   :meth:`apply_fill` 收的是"这笔成交带来多少现金变化"，
#                   而不是"现金应该变成多少"。并发成交场景下，后者会互相覆盖：
#                   两笔成交同时读到 cash=10 万，各自算出 9.5 万 / 10.5 万写回，
#                   结果取决于谁最后写 —— 钱就这么凭空消失了一万。
#
#                3. **读不出来比读错要好，所以失败一律返回/抛出明确的错**。
#                   账户的钱差一万很难查。宁可让任务失败（能重跑），
#                   也不要默默按 0 处理。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.logging import logger
from app.data.engine import SessionLocal
from app.data.schema import AccountPosition, BrokerAccount

#: 系统内置的虚拟账户代号。**它不是一个能用 Replace 的默认值**：
#: 其它地方（任务默认账户、面板展示、CLI）都按这个代号来定位它。
VIRTUAL_ACCOUNT_ID = "virtual-default"

#: 内置虚拟账户的默认本金。10 万是 A 股一个自然人账户最典型的起步资金量级，
#: 重要的是**它必须显式写死** —— 让它跟着某个配置项漂移，
#: 就会出现"同一段回测在不同机器上收益率分母不同"。
VIRTUAL_INITIAL_CASH = 100_000.0


def ensure_schema(engine: Any = None) -> None:
    """建账户相关的表（幂等）。"""
    try:
        if engine is None:
            bind = getattr(SessionLocal, "kw", {}).get("bind")
            engine = bind if bind is not None else None
        if engine is None:
            from app.data.engine import engine as default_engine

            engine = default_engine
        BrokerAccount.__table__.create(bind=engine, checkfirst=True)
        AccountPosition.__table__.create(bind=engine, checkfirst=True)
        from app.data.migrate import ensure_schema_of

        ensure_schema_of(BrokerAccount, engine)
        ensure_schema_of(AccountPosition, engine)
    except Exception as exc:  # pragma: no cover
        logger.warning(f"账户表就绪检查失败: {exc}")


def list_accounts(status: str = "", mode: str = "", db: Any = None
                  ) -> List[Dict[str, Any]]:
    """账户列表（面板用）。默认全部。"""
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        q = select(BrokerAccount)
        if status:
            q = q.where(BrokerAccount.status == str(status))
        if mode:
            q = q.where(BrokerAccount.mode == str(mode))
        rows = session.execute(q.order_by(BrokerAccount.id)).scalars().all()
        return [r.snapshot() for r in rows]
    finally:
        if own:
            session.close()


def _row(session: Session, account_id: str, for_update: bool = False
         ) -> Optional[BrokerAccount]:
    """按 ``account_id`` 取行。

    注意这里**不能用** ``session.get(BrokerAccount, account_id)``：
    表的主键是自增的 ``id``，``account_id`` 只是业务唯一键，
    ``session.get`` 按主键查，写出来能跑、语义却是错的 ——
    它会永远返回 None，于是每次都当"账户不存在"去 INSERT，
    表现为"第二次启动报 UNIQUE constraint failed"。
    """
    q = select(BrokerAccount).where(BrokerAccount.account_id == str(account_id))
    return session.execute(q).scalars().first()


def get_account(account_id: str, db: Any = None) -> Optional[Dict[str, Any]]:
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        row = _row(session, account_id)
        return row.snapshot() if row else None
    finally:
        if own:
            session.close()


def default_account_id(db: Any = None) -> Optional[str]:
    """**目前该用哪个账户**下单。

    优先级：可用的实盘账户 > 内置虚拟账户。

    为什么优先实盘：一旦有人配了 live 账户，就说明他想用它。若因为
    "还有一个 virtual 账户"而继续下到模拟盘，症状是"明明配了真账户、
    却一笔真单都没有" —— 这类让系统看起来在工作、实际什么都没做的错，
    是所有故障里最难发现的一种。
    """
    accounts = list_accounts(status="active", db=db)
    live = [a for a in accounts if a.get("mode") == "live"]
    if live:
        return str(live[0]["account_id"])
    virtual = [a for a in accounts if a.get("mode") == "virtual"]
    if virtual:
        return str(virtual[0]["account_id"])
    return None


def create_account(account_id: str, name: str = "", initial_cash: float = 0.0,
                   gateway: str = "simulated", endpoint_id: str = "",
                   broker_no: str = "", mode: str = "virtual",
                   status: str = "active", builtin: bool = False,
                   note: str = "", db: Any = None) -> Dict[str, Any]:
    """建一个账户。**已存在则原样返回**（幂等，重复调用不会改坏本金）。"""
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        exist = _row(session, account_id)
        if exist is not None:
            return exist.snapshot()
        row = BrokerAccount(
            account_id=str(account_id),
            name=str(name or account_id),
            initial_cash=float(initial_cash or 0.0),
            cash=float(initial_cash or 0.0),
            peak_equity=float(initial_cash or 0.0),
            gateway=str(gateway or "simulated"),
            endpoint_id=str(endpoint_id or ""),
            broker_no=str(broker_no or ""),
            mode=str(mode or "virtual"),
            status=str(status or "active"),
            builtin=bool(builtin),
            note=str(note or ""),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        session.add(row)
        if own:
            session.commit()
            session.refresh(row)
        return row.snapshot()
    finally:
        if own:
            session.close()


def ensure_virtual_account(initial_cash: float = 0.0, db: Any = None,
                           gateway: str = "simulated", endpoint_id: str = ""
                           ) -> Dict[str, Any]:
    """确保内置虚拟账户存在（**幂等**，每次启动都可以调）。

    虚拟账户存在的意义见 :class:`~app.data.schema.BrokerAccount` 的注释：
    它接模拟网关、走完全相同的下单链路，但**永远不会把单子发到交易所**。
    """
    return create_account(
        VIRTUAL_ACCOUNT_ID,
        name="内置虚拟账户",
        initial_cash=float(initial_cash or VIRTUAL_INITIAL_CASH),
        gateway=gateway,
        endpoint_id=endpoint_id,
        mode="virtual",
        builtin=True,
        note="系统内置：模拟盘账户，走完整交易链路但永不下真单",
        db=db,
    )


def set_cash(account_id: str, cash: float, frozen: Optional[float] = None,
             market_value: Optional[float] = None, db: Any = None
             ) -> Optional[Dict[str, Any]]:
    """**整体赋值**式更新资金 —— 只给"从券商拉回全量"的场景用。

    券商回报的是账户的**当前状态**（总资产/可用/冻结），那是一次对账结果，
    必须用赋值；而逐笔成交必须用 :meth:`apply_fill` 的增量方式。
    用错方向的症状见文件头第 2 条。
    """
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        row = _row(session, account_id)
        if row is None:
            return None
        row.cash = float(cash)
        if frozen is not None:
            row.frozen = float(frozen)
        if market_value is not None:
            row.market_value = float(market_value)
        row.updated_at = datetime.now()
        if own:
            session.commit()
            session.refresh(row)
        return row.snapshot()
    finally:
        if own:
            session.close()


def apply_fill(account_id: str, cash_delta: float, realized_pnl: float = 0.0,
               fee: float = 0.0, db: Any = None) -> Optional[Dict[str, Any]]:
    """记一笔成交对现金的影响（**增量**）。

    :param cash_delta: 现金变化，**买入为负、卖出为正**（已扣除手续费）
    :param realized_pnl: 这笔成交实现盈亏（只由卖出产生）

    成交额打入 But 头部（``= cash + market_value``）之后会顺带抬高峰值权益；
    峰值只增不减 —— 回撤是"离最高点有多远"，把峰值往下调会让回撤凭空减小。
    """
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        row = _row(session, account_id)
        if row is None:
            return None
        row.cash = float(row.cash or 0.0) + float(cash_delta)
        row.realized_pnl = float(row.realized_pnl or 0.0) + float(realized_pnl)
        equity = float(row.cash or 0.0) + float(row.market_value or 0.0)
        row.peak_equity = max(float(row.peak_equity or 0.0), equity)
        row.updated_at = datetime.now()
        if own:
            session.commit()
            session.refresh(row)
        return row.snapshot()
    finally:
        if own:
            session.close()


def mark_prices(account_id: str, prices: Dict[str, float],
                db: Any = None) -> Optional[Dict[str, Any]]:
    """按最新价重算持仓市值与账户权益。

    与 :meth:`set_cash` 的区别：它**不动现金**，只动市值那一侧。
    盘中价格一动就更新权益，是为让"最大回撤"这类规则在**盘中发现**回撤，
    而不是等收盘才发现 —— 事后才知道的风险，拦不住任何东西。
    """
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        row = _row(session, account_id)
        if row is None:
            return None
        total = 0.0
        if prices:
            rows = session.execute(
                select(AccountPosition).where(
                    AccountPosition.account_id == str(account_id))
            ).scalars().all()
            for pos in rows:
                price = float(prices.get(pos.symbol, pos.last_price or 0.0) or 0.0)
                pos.last_price = price
                pos.market_value = float(pos.size or 0) * price
                total += float(pos.market_value or 0.0)
            row.market_value = total
        equity = float(row.cash or 0.0) + float(row.market_value or 0.0)
        row.peak_equity = max(float(row.peak_equity or 0.0), equity)
        row.updated_at = datetime.now()
        if own:
            session.commit()
            session.refresh(row)
        return row.snapshot()
    finally:
        if own:
            session.close()


def save_positions(account_id: str, positions: Sequence[Dict[str, Any]],
                   db: Any = None) -> int:
    """整份覆盖某个账户的持仓快照。

    为什么是覆盖而不是增量：这里存的是**当前状态**，不是流水。
    来源（券商查询/本地重算）本身就是一份完整清单，
    增量更新反而要处理"清单里没出现的是不是该清掉"这个无解的问题。
    """
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        wanted = {}
        for p in positions:
            sym = str(p.get("symbol") or "").strip()
            if not sym:
                continue
            wanted[sym] = p
        existing = {
            r.symbol: r for r in session.execute(
                select(AccountPosition).where(
                    AccountPosition.account_id == str(account_id))
            ).scalars().all()
        }
        n = 0
        for sym, p in wanted.items():
            row = existing.get(sym)
            if row is None:
                row = AccountPosition(account_id=str(account_id), symbol=sym)
                session.add(row)
            row.size = int(p.get("size") or 0)
            row.sellable = int(p.get("sellable", row.size) or 0)
            row.avg_price = float(p.get("avg_price") or 0.0)
            row.last_price = float(p.get("last_price") or 0.0)
            row.market_value = float(p.get("market_value")
                                     or (row.size * row.last_price))
            row.updated_at = datetime.now()
            n += 1
        # 清单里没出现 = 已经不持有（清仓了）。删掉，否则"早就卖了
        # 却还显示有持仓"，而 T+1 规则会用这个幽灵持仓拒绝一切卖出。
        for sym, row in existing.items():
            if sym not in wanted:
                session.delete(row)
        if own:
            session.commit()
        return n
    finally:
        if own:
            session.close()


def list_positions(account_id: str, db: Any = None) -> List[Dict[str, Any]]:
    ensure_schema()
    session = db or SessionLocal()
    own = db is None
    try:
        rows = session.execute(
            select(AccountPosition).where(
                AccountPosition.account_id == str(account_id))
        ).scalars().all()
        return [{
            "symbol": r.symbol, "size": int(r.size or 0),
            "sellable": int(r.sellable or 0),
            "avg_price": float(r.avg_price or 0.0),
            "last_price": float(r.last_price or 0.0),
            "market_value": float(r.market_value or 0.0),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        } for r in rows]
    finally:
        if own:
            session.close()


def summary(db: Any = None) -> Dict[str, Any]:
    """给面板用的一句话概况。"""
    accounts = list_accounts(db=db)
    return {
        "accounts": accounts,
        "total": len(accounts),
        "active": sum(1 for a in accounts if a.get("status") == "active"),
        "virtual": sum(1 for a in accounts if a.get("mode") == "virtual"),
        "live": sum(1 for a in accounts if a.get("mode") == "live"),
        "default_account_id": default_account_id(db=db),
        "total_equity": sum(float(a.get("equity") or 0.0) for a in accounts),
    }


__all__ = [
    "VIRTUAL_ACCOUNT_ID",
    "VIRTUAL_INITIAL_CASH",
    "ensure_schema",
    "list_accounts",
    "get_account",
    "default_account_id",
    "create_account",
    "ensure_virtual_account",
    "set_cash",
    "apply_fill",
    "mark_prices",
    "save_positions",
    "list_positions",
    "summary",
]
