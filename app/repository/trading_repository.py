#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : trading_repository.py
# @Description : 交易数据仓储：订单 / 成交 / 权益 / 运行记录 / 系统事件 → SQLite
#
#                设计要点（每一条都是被现实逼出来的）：
#
#                1. **绝不在交易主链路上写库**。SQLite 提交要 fsync，
#                   在 reactor 线程里做会让下一根K线的处理被推迟，
#                   实盘上就是"行情来了但系统还在等磁盘"。所以这里是一个
#                   后台线程 + 队列的写后（write-behind）模型：
#                   业务侧只是把记录丢进队列，立刻返回。
#
#                2. **订单是 upsert，成交是 insert**。订单状态会变
#                   （PENDING→SUBMITTED→FILLED/PARTIAL/CANCELLED），
#                   覆盖写才能回答"现在还有哪些挂着没成交"；
#                   成交是既成事实，只追加不修改。
#
#                3. **失败不能反噬业务**。写库失败只记日志并计入 stats，
#                   绝不抛回交易链路。磁盘满/锁冲突不该让策略停止工作。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import (
    BacktestResult,
    EquityPoint,
    OrderRecord,
    RunRecord,
    SystemEvent,
    TaskRecord,
    TradeRecordTable,
)
from app.utils.jsonio import dumps
from app.utils.logger import logger

#: 队列上限。满了就丢最旧的并计数 —— 宁可丢审计记录，
#: 也不能让内存被无界队列吃光（那会直接拖垮整个服务）。
MAX_QUEUE = 20000


class TradingRepository:
    """交易数据的写入与查询。

    写入侧是线程安全的单例式设计：所有调用方共用一条后台写线程，
    避免多线程各自开 session 造成 SQLite 写锁竞争。
    """

    def __init__(self, enabled: Optional[bool] = None) -> None:
        self.enabled = settings.TRADE_PERSIST_ENABLED if enabled is None else bool(enabled)
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stats: Dict[str, int] = {
            "queued": 0, "written": 0, "dropped": 0, "errors": 0, "batches": 0,
        }
        self._lock = threading.Lock()
        self._last_error: str = ""

    # ============================================================
    # 生命周期
    # ============================================================
    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker, name="trading-writer", daemon=True
        )
        self._thread.start()
        logger.debug("交易数据落库线程已启动")

    def stop(self, timeout: float = 5.0) -> None:
        """停写线程并尽量把队列里剩下的刷完。"""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None
        self._flush_remaining()
        logger.info(
            f"交易数据落库已停止 | 写入 {self.stats['written']} 条 | "
            f"丢弃 {self.stats['dropped']} 条 | 错误 {self.stats['errors']}"
        )

    # ============================================================
    # 入队（业务侧调用，永不阻塞、永不抛异常）
    # ============================================================
    def _offer(self, kind: str, payload: Any) -> None:
        if not self.enabled:
            return
        # 队列没起线程时自动起一个，调用方不必关心时机
        if self._thread is None:
            self.start()
        try:
            self._q.put_nowait((kind, payload))
            self.stats["queued"] += 1
        except queue.Full:
            self.stats["dropped"] += 1
            # 丢数据要吭声，但按时间窗节流——队列满会连续触发
            from app.utils.logger import log_throttled
            log_throttled(
                "ERROR",
                f"交易数据写入队列已满（{MAX_QUEUE}），正在丢弃最早的记录",
                key="trading-repo-queue-full",
            )

    def record_run(self, run_id: str, mode: str = "", namespace: str = "default",
                   status: str = "RUNNING", note: str = "") -> None:
        self._offer("run", {
            "run_id": run_id, "mode": mode, "namespace": namespace,
            "status": status, "started_at": datetime.now(), "note": note,
            "pid": _pid(), "host": _host(),
        })

    def record_task(self, run_id: str, task_id: str, symbol: str = "",
                    strategy: str = "", run_mode: str = "",
                    initial_cash: float = 0.0, status: str = "",
                    spec: Optional[Dict[str, Any]] = None) -> None:
        self._offer("task", {
            "run_id": run_id, "task_id": task_id, "symbol": symbol,
            "strategy": strategy, "run_mode": run_mode,
            "initial_cash": float(initial_cash), "status": status,
            "spec_json": dumps(spec or {}),
            "created_at": datetime.now(),
        })

    def record_order(self, run_id: str, order: Any) -> None:
        """订单 upsert。状态变化时重复调用即可。"""
        self._offer("order", {
            "order_id": str(getattr(order, "order_id", "")),
            "run_id": run_id,
            "task_id": str(getattr(order, "task_id", "")),
            "symbol": str(getattr(order, "symbol", "")),
            "side": _enum_value(getattr(order, "side", "")),
            "size": int(getattr(order, "size", 0) or 0),
            "price": float(getattr(order, "price", 0.0) or 0.0),
            "status": _enum_value(getattr(order, "status", "")),
            "filled_size": int(getattr(order, "filled_size", 0) or 0),
            "filled_price": float(getattr(order, "filled_price", 0.0) or 0.0),
            "reason": str(getattr(order, "reason", "") or ""),
            "reject_reason": str(getattr(order, "reject_reason", "") or ""),
            "source": str((getattr(order, "meta", {}) or {}).get("source", "")),
            "created_at": getattr(order, "created_at", None) or datetime.now(),
            "updated_at": datetime.now(),
        })

    def record_trade(self, run_id: str, task_id: str, trade: Any,
                     order_id: str = "", cash_after: float = 0.0,
                     position_after: int = 0) -> None:
        self._offer("trade", {
            "run_id": run_id, "task_id": task_id, "order_id": order_id,
            "symbol": str(getattr(trade, "symbol", "")),
            "side": _enum_value(getattr(trade, "side", "")),
            "size": int(getattr(trade, "size", 0) or 0),
            "price": float(getattr(trade, "price", 0.0) or 0.0),
            "fee": float(getattr(trade, "fee", 0.0) or 0.0),
            "realized_pnl": float(getattr(trade, "realized_pnl", 0.0) or 0.0),
            "cash_after": float(cash_after), "position_after": int(position_after),
            "reason": str(getattr(trade, "reason", "") or ""),
            "source": str(getattr(trade, "source", "") or ""),
            "dt": getattr(trade, "dt", None) or datetime.now(),
        })

    def record_equity(self, run_id: str, task_id: str, symbol: str, equity: float,
                      cash: float, position_size: int, market_value: float,
                      drawdown: float = 0.0, dt: Optional[datetime] = None) -> None:
        self._offer("equity", {
            "run_id": run_id, "task_id": task_id, "symbol": symbol,
            "equity": float(equity), "cash": float(cash),
            "position_size": int(position_size),
            "market_value": float(market_value), "drawdown": float(drawdown),
            "dt": dt or datetime.now(),
        })

    def record_event(self, run_id: str, message: str, level: str = "INFO",
                     category: str = "engine", task_id: str = "",
                     symbol: str = "", detail: Optional[Dict[str, Any]] = None) -> None:
        self._offer("event", {
            "run_id": run_id, "ts": datetime.now(), "level": level.upper(),
            "category": category, "task_id": task_id, "symbol": symbol,
            "message": message, "detail_json": dumps(detail or {}),
        })

    def record_backtest(self, run_id: str, result: Dict[str, Any]) -> None:
        self._offer("backtest", {
            "run_id": run_id,
            "task_id": str(result.get("task_id") or ""),
            "symbol": str(result.get("symbol") or ""),
            "strategy": str(result.get("strategy") or ""),
            "start_date": str(result.get("start_date") or ""),
            "end_date": str(result.get("end_date") or ""),
            "initial_cash": float(result.get("initial_cash") or 0.0),
            "final_equity": float(result.get("final_equity") or 0.0),
            "total_return": float(result.get("total_return") or 0.0),
            "max_drawdown": float(result.get("max_drawdown") or 0.0),
            "sharpe": float(result.get("sharpe") or 0.0),
            "trade_count": int(result.get("trade_count") or 0),
            "win_rate": float(result.get("win_rate") or 0.0),
            "metrics_json": dumps(result.get("metrics") or {}),
            "equity_json": dumps(result.get("equity_curve") or []),
            "created_at": datetime.now(),
        })

    # ============================================================
    # 后台写线程
    # ============================================================
    def _worker(self) -> None:
        interval = max(0.2, float(settings.TRADE_FLUSH_INTERVAL))
        batch_size = max(1, int(settings.TRADE_FLUSH_BATCH))
        while not self._stop.is_set():
            batch = self._collect(batch_size, timeout=interval)
            if batch:
                self._write_batch(batch)
        # 退出前再刷一次
        while True:
            batch = self._collect(batch_size, timeout=0.0)
            if not batch:
                break
            self._write_batch(batch)

    def _collect(self, batch_size: int, timeout: float) -> List[tuple]:
        batch: List[tuple] = []
        deadline = time.monotonic() + timeout
        while len(batch) < batch_size:
            wait = max(0.0, deadline - time.monotonic())
            if not batch and wait <= 0 and batch_size > 0 and timeout <= 0:
                break
            try:
                batch.append(self._q.get(timeout=wait if not batch else 0))
            except queue.Empty:
                break
            if wait <= 0:
                break
        return batch

    def _write_batch(self, batch: Sequence[tuple]) -> None:
        """整批落库。

        两个必须做对的地方：

        1. **批内先按主键收敛**。同一批里同一个订单可能出现两次
           （PENDING 然后立刻 FILLED —— 模拟撮合是同步的，这是常态），
           而 ``s.get()`` 在 flush 之前看不到本批新加的行，
           两次都会被判成"新记录"→ 两次 INSERT → 唯一约束冲突。
           这里先按主键去重、只保留最后一次状态，等价于 upsert 语义。
           注意只对**可更新**的实体去重；成交/权益/事件是 append-only，
           同一批里出现多次就是多行，不能合并。

        2. **单条失败不能拖垮整批**。用 savepoint 包住每条记录，
           坏行只回滚自己。否则一条约束冲突会让整批审计记录一起消失——
           而审计记录恰恰是出问题时最需要的。
        """
        session: Optional[Session] = None
        try:
            session = SessionLocal()
            rows = _dedup_batch(batch)
            ok = 0
            for kind, payload in rows:
                try:
                    with session.begin_nested():
                        _APPLY[kind](session, payload)
                    ok += 1
                except Exception as exc:
                    self.stats["errors"] += 1
                    self._last_error = str(exc)
                    _log_write_error(kind, payload, exc)
            session.commit()
            self.stats["written"] += ok
            self.stats["batches"] += 1
        except Exception as exc:
            # 连 session/commit 都失败（磁盘满、数据库被锁）
            self.stats["errors"] += len(batch)
            self._last_error = str(exc)
            if session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
            from app.utils.logger import log_throttled
            log_throttled(
                "ERROR",
                f"交易数据落库失败（本批 {len(batch)} 条已丢弃）: {exc}",
                key="trading-repo-write-fail",
            )
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def flush(self, timeout: float = 5.0) -> int:
        """把队列里已排队的记录立刻写掉，返回写入条数。

        **不要用 stop() 来"刷一下"** —— stop 会关掉后台写线程，
        而这个仓储多半是进程内共享的（引擎也在用），
        停掉之后引擎的订单/成交就再也写不进去了。

        :param timeout: 最长等待秒数（写线程在处理别的批次时可能占着）
        """
        if not self.enabled:
            return 0
        written = 0
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            pending = []
            while True:
                try:
                    pending.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if not pending:
                return written
            self._write_batch(pending)
            written += len(pending)
        return written

    def _flush_remaining(self) -> None:
        left = []
        while True:
            try:
                left.append(self._q.get_nowait())
            except queue.Empty:
                break
        if left:
            self._write_batch(left)

    def health(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "alive": self._thread is not None and self._thread.is_alive(),
            "pending": self._q.qsize(),
            "last_error": self._last_error,
            **self.stats,
        }

    # ============================================================
    # 查询
    # ============================================================
    @staticmethod
    def recent_orders(task_id: str = "", run_id: str = "", status: str = "",
                      limit: int = 200) -> List[Dict[str, Any]]:
        with SessionLocal() as s:
            q = s.query(OrderRecord)
            if task_id:
                q = q.filter(OrderRecord.task_id == task_id)
            if run_id:
                q = q.filter(OrderRecord.run_id == run_id)
            if status:
                q = q.filter(OrderRecord.status == status)
            rows = q.order_by(desc(OrderRecord.created_at)).limit(limit).all()
            return [_row_dict(r) for r in rows]

    @staticmethod
    def recent_trades(task_id: str = "", run_id: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        with SessionLocal() as s:
            q = s.query(TradeRecordTable)
            if task_id:
                q = q.filter(TradeRecordTable.task_id == task_id)
            if run_id:
                q = q.filter(TradeRecordTable.run_id == run_id)
            rows = q.order_by(desc(TradeRecordTable.dt)).limit(limit).all()
            return [_row_dict(r) for r in rows]

    @staticmethod
    def equity_curve(task_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
        with SessionLocal() as s:
            rows = (
                s.query(EquityPoint)
                .filter(EquityPoint.task_id == task_id)
                .order_by(EquityPoint.dt)
                .limit(limit)
                .all()
            )
            return [
                {"dt": r.dt.isoformat() if r.dt else None, "equity": r.equity}
                for r in rows
            ]

    @staticmethod
    def runs(limit: int = 50) -> List[Dict[str, Any]]:
        with SessionLocal() as s:
            rows = (
                s.query(RunRecord).order_by(desc(RunRecord.started_at)).limit(limit).all()
            )
            return [_row_dict(r) for r in rows]

    @staticmethod
    def backtests(limit: int = 50, symbol: str = "", strategy: str = "") -> List[Dict[str, Any]]:
        with SessionLocal() as s:
            q = s.query(BacktestResult)
            if symbol:
                q = q.filter(BacktestResult.symbol == symbol)
            if strategy:
                q = q.filter(BacktestResult.strategy == strategy)
            rows = q.order_by(desc(BacktestResult.created_at)).limit(limit).all()
            out = []
            for r in rows:
                d = _row_dict(r)
                d.pop("equity_json", None)   # 列表里不带曲线，太重
                out.append(d)
            return out

    @staticmethod
    def backtest_detail(result_id: int) -> Optional[Dict[str, Any]]:
        from app.utils.jsonio import read_json_str

        with SessionLocal() as s:
            r = s.query(BacktestResult).filter(BacktestResult.id == result_id).first()
            if r is None:
                return None
            d = _row_dict(r)
            d["equity_curve"] = read_json_str(r.equity_json) or []
            d["metrics"] = read_json_str(r.metrics_json) or {}
            return d

    @staticmethod
    def events(run_id: str = "", category: str = "", level: str = "",
               limit: int = 200) -> List[Dict[str, Any]]:
        with SessionLocal() as s:
            q = s.query(SystemEvent)
            if run_id:
                q = q.filter(SystemEvent.run_id == run_id)
            if category:
                q = q.filter(SystemEvent.category == category)
            if level:
                q = q.filter(SystemEvent.level == level)
            rows = q.order_by(desc(SystemEvent.ts)).limit(limit).all()
            return [_row_dict(r) for r in rows]

    @staticmethod
    def order_stats(task_id: str = "", run_id: str = "") -> Dict[str, Any]:
        """按状态聚合订单数。这就是"为什么必须进 SQLite"的典型场景：
        按条件分组统计，文件方案根本做不了。"""
        with SessionLocal() as s:
            q = s.query(OrderRecord.status, func.count(OrderRecord.order_id))
            if task_id:
                q = q.filter(OrderRecord.task_id == task_id)
            if run_id:
                q = q.filter(OrderRecord.run_id == run_id)
            rows = q.group_by(OrderRecord.status).all()
            return {str(st): int(n) for st, n in rows}


# ===========================================================================
# 批内每条记录的落库实现
# ===========================================================================
def _apply_run(s: Session, p: Dict[str, Any]) -> None:
    obj = s.get(RunRecord, p["run_id"])
    if obj is None:
        s.add(RunRecord(**p))
    else:
        for k in ("status", "stopped_at", "note", "mode", "namespace"):
            if k in p and p[k] is not None:
                setattr(obj, k, p[k])


def _apply_task(s: Session, p: Dict[str, Any]) -> None:
    existing = (
        s.query(TaskRecord)
        .filter(TaskRecord.run_id == p["run_id"], TaskRecord.task_id == p["task_id"])
        .first()
    )
    if existing is None:
        s.add(TaskRecord(**p))
    else:
        for k in ("status", "spec_json", "initial_cash", "strategy"):
            if k in p and p[k]:
                setattr(existing, k, p[k])


def _apply_order(s: Session, p: Dict[str, Any]) -> None:
    """订单 upsert：同一 order_id 的状态变化覆盖旧行。"""
    obj = s.get(OrderRecord, p["order_id"])
    if obj is None:
        s.add(OrderRecord(**p))
    else:
        for k, v in p.items():
            if k != "order_id":
                setattr(obj, k, v)


def _apply_trade(s: Session, p: Dict[str, Any]) -> None:
    s.add(TradeRecordTable(**p))


def _apply_equity(s: Session, p: Dict[str, Any]) -> None:
    s.add(EquityPoint(**p))


def _apply_event(s: Session, p: Dict[str, Any]) -> None:
    s.add(SystemEvent(**p))


def _apply_backtest(s: Session, p: Dict[str, Any]) -> None:
    s.add(BacktestResult(**p))


_APPLY = {
    "run": _apply_run,
    "task": _apply_task,
    "order": _apply_order,
    "trade": _apply_trade,
    "equity": _apply_equity,
    "event": _apply_event,
    "backtest": _apply_backtest,
}

#: 哪些实体是「可更新」的（同一主键重复出现只保留最后一次）。
#: 不在表里的（trade / equity / event / backtest）是 append-only，
#: 同一批出现多次就是多行，必须全部保留。
_UPSERT_KEY = {
    "run": lambda p: ("run", p.get("run_id")),
    "task": lambda p: ("task", p.get("run_id"), p.get("task_id")),
    "order": lambda p: ("order", p.get("order_id")),
}


def _dedup_batch(batch: Sequence[tuple]) -> List[tuple]:
    """批内按主键收敛可更新实体，保持原有相对顺序。"""
    seen: Dict[tuple, int] = {}
    out: List[tuple] = []
    for kind, payload in batch:
        keyfn = _UPSERT_KEY.get(kind)
        if keyfn is None:
            out.append((kind, payload))
            continue
        key = keyfn(payload)
        idx = seen.get(key)
        if idx is None:
            seen[key] = len(out)
            out.append((kind, payload))
        else:
            # 后到的覆盖先到的 —— 状态推进只看最新
            out[idx] = (kind, payload)
    return out


def _log_write_error(kind: str, payload: Dict[str, Any], exc: Exception) -> None:
    from app.utils.logger import log_throttled

    ident = payload.get("order_id") or payload.get("task_id") or payload.get("run_id") or "?"
    log_throttled(
        "WARNING",
        f"单条交易记录落库被跳过 | 类型={kind} | 标识={ident} | {exc}",
        key=f"trading-repo-row-{kind}",
    )


def _enum_value(v: Any) -> str:
    return str(getattr(v, "value", v) or "")


def _pid() -> int:
    import os

    return os.getpid()


def _host() -> str:
    import socket

    try:
        return socket.gethostname()
    except Exception:  # pragma: no cover
        return ""


def _row_dict(row: Any) -> Dict[str, Any]:
    """ORM 行 → 可 JSON 化的 dict（日期转 iso）。"""
    out: Dict[str, Any] = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name, None)
        if isinstance(v, datetime):
            v = v.isoformat()
        out[col.name] = v
    return out


#: 进程内共享实例。业务组件用它，避免每个任务各开一条写线程。
_repo: Optional[TradingRepository] = None
_repo_lock = threading.Lock()


def get_repository() -> TradingRepository:
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                _repo = TradingRepository()
    return _repo


__all__ = ["TradingRepository", "get_repository", "MAX_QUEUE"]
