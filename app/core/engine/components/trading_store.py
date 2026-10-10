#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : trading_store.py
# @Description : 交易数据落库组件：把订单 / 成交 / 权益 / 事件写进 SQLite
#
#                为什么单独做成一个组件，而不是在 QuantTask 里直接写库：
#                  1. **交易路径不该知道"存哪里"**。任务只负责决策与撮合，
#                     换存储（SQLite → PostgreSQL / 云库）不该改 QuantTask。
#                  2. **写库必须能整体关掉**。用户要求"日志/落盘可配置"，
#                     组件化之后 collector/store 都是开关，不写库时零开销。
#                  3. **写入必须异步**。这里订阅事件、攒批量，
#                     真正落盘交给 TradingRepository 的后台线程 ——
#                     reactor 线程绝不等 SQLite 的 fsync。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from twisted.internet import defer, task

from app.core.config import settings
from app.core.engine.components.ibase import IBaseComponent
from app.core.engine.event import StandardEvents
from app.core.market.types import OrderStatus
from app.repository.trading_repository import get_repository
from app.utils.logger import logger


class TradingStoreComponent(IBaseComponent):
    """交易落库。

    订阅的每个事件都对应一条**应当长期保留的事实**：
    订单状态、成交、数据采集结果、异常。这些是"事后能不能复盘"的关键，
    而逐根K线之类的过程数据一律不落库（那是日志的活，而且会被清理）。
    """

    name = "trading_store"

    def __init__(
        self,
        enabled: Optional[bool] = None,
        equity_interval: Optional[float] = None,
        run_id: str = "",
        engine: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.enabled = settings.TRADE_PERSIST_ENABLED if enabled is None else bool(enabled)
        self.equity_interval = float(
            equity_interval if equity_interval is not None
            else settings.EQUITY_SAMPLE_INTERVAL
        )
        self.bound_run_id = run_id or ""
        #: 引擎引用。取任务列表需要它 —— 这里刻意用显式注入而不是全局单例，
        #: 因为测试与多服务场景下"当前引擎"并不是唯一的。
        self.bound_engine = engine
        self.repo = get_repository()
        self._loop: Optional[task.LoopingCall] = None
        self._last_equity_at: Dict[str, float] = {}
        self.stats: Dict[str, int] = {
            "orders": 0, "trades": 0, "rejected": 0,
            "equity_points": 0, "events": 0, "errors": 0,
        }

    # ============================================================
    # 生命周期
    # ============================================================
    @property
    def run_id(self) -> str:
        if self.bound_run_id:
            return self.bound_run_id
        return self.context.run_id if self.context else ""

    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        if "enabled" in cfg:
            self.enabled = bool(cfg["enabled"])
        if cfg.get("equity_interval") is not None:
            self.equity_interval = float(cfg["equity_interval"])

        if not self.enabled:
            logger.info("交易数据落库已关闭（TRADE_PERSIST_ENABLED=false）")
            return

        # 建表：用户可能没跑过 init-db 就直接起服务
        self._ensure_tables()
        self.repo.start()

        self.subscribe_event(StandardEvents.ORDER_CREATED, self._on_order)
        self.subscribe_event(StandardEvents.ORDER_FILLED, self._on_order)
        self.subscribe_event(StandardEvents.ORDER_CANCELLED, self._on_order)
        self.subscribe_event(StandardEvents.SIGNAL_REJECTED, self._on_order)
        self.subscribe_event(StandardEvents.DATA_COLLECTED, self._on_collected)
        self.subscribe_event(StandardEvents.COMPONENT_ERROR, self._on_error)
        self.subscribe_event(StandardEvents.ENGINE_ERROR, self._on_error)

        self.repo.record_run(
            self.run_id,
            mode=(self.context.run_mode.value if self.context else ""),
            namespace=str(cfg.get("namespace") or "default"),
            status="RUNNING",
        )
        logger.info(
            f"交易数据落库就绪 | run_id={self.run_id} | "
            f"权益采样间隔={self.equity_interval}s | 库={settings.DATABASE_URL}"
        )

    def _ensure_tables(self) -> None:
        try:
            from app.db.database import engine as db_engine
            from app.db.models import Base

            Base.metadata.create_all(bind=db_engine)
        except Exception as exc:
            logger.warning(f"交易数据表补建失败（忽略，写入时仍会尝试）: {exc}")

    def on_start(self) -> defer.Deferred:
        if not self.enabled:
            return defer.succeed(None)
        self._register_tasks()
        self._loop = task.LoopingCall(self._sample_equity)
        self._loop.start(max(1.0, self.equity_interval), now=False)
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None
        if not self.enabled:
            return defer.succeed(None)

        # 收尾：先补一次权益，再更新运行状态，最后把队列刷干净。
        # 顺序很重要 —— 队列是异步的，先把该写的都排进去再 stop。
        try:
            self._sample_equity()
        except Exception as exc:
            logger.debug(f"收尾权益采样失败: {exc}")
        try:
            self.repo.record_run(self.run_id, status="STOPPED")
        except Exception:
            pass
        try:
            self.repo.stop()
        except Exception as exc:
            logger.warning(f"落库线程收尾失败: {exc}")
        return defer.succeed(None)

    def is_busy(self) -> bool:
        return False

    # ============================================================
    # 任务登记
    # ============================================================
    def _register_tasks(self) -> None:
        runtime = self._runtime()
        if runtime is None:
            return
        try:
            for t in list(runtime):
                self._record_task(t)
        except Exception as exc:
            logger.debug(f"批量登记任务失败: {exc}")

    def _runtime(self):
        engine = self.bound_engine
        if engine is None:
            return None
        sm = engine.get_component("strategy_manager")
        return getattr(sm, "runtime", None) if sm is not None else None

    def _record_task(self, task: Any) -> None:
        try:
            spec = getattr(task, "meta", {}) or {}
            self.repo.record_task(
                self.run_id,
                task_id=str(getattr(task, "task_id", "")),
                symbol=str(getattr(task, "symbol", "")),
                strategy=str(getattr(getattr(task, "strategy", None), "name", "")),
                run_mode=str(getattr(task.run_mode, "value", task.run_mode)),
                initial_cash=float(getattr(task.portfolio.account, "initial_cash", 0.0)),
                status=str(getattr(task.status, "value", task.status)),
                spec=spec if isinstance(spec, dict) else {},
            )
        except Exception as exc:
            self.stats["errors"] += 1
            logger.debug(f"任务登记失败: {exc}")

    def register_task(self, task: Any) -> None:
        """运行时新增任务时由控制面调用。"""
        if self.enabled:
            self._record_task(task)

    # ============================================================
    # 事件 → 落库
    # ============================================================
    def _on_order(self, order: Any = None, task_id: str = "", reason: str = "",
                  **kwargs: Any) -> None:
        if not self.enabled or order is None:
            return
        try:
            self.repo.record_order(self.run_id, order)
            self.stats["orders"] += 1
        except Exception as exc:
            self.stats["errors"] += 1
            logger.debug(f"订单落库失败: {exc}")

        status = getattr(order, "status", None)
        if status == OrderStatus.FILLED:
            self._record_trade(order)
        elif status == OrderStatus.REJECTED:
            self.stats["rejected"] += 1
            self.repo.record_event(
                self.run_id,
                message=f"订单被拒 | {getattr(order, 'symbol', '')} "
                        f"{getattr(order, 'side', '')} | {reason or getattr(order, 'reject_reason', '')}",
                level="WARNING", category="risk",
                task_id=str(getattr(order, "task_id", "") or task_id),
                symbol=str(getattr(order, "symbol", "")),
                detail={"order_id": getattr(order, "order_id", "")},
            )
            self.stats["events"] += 1

    def _record_trade(self, order: Any) -> None:
        """成交落库。用订单里的 task_id 去账本取最后一笔成交记录。"""
        if not self.enabled:
            return
        try:
            tid = str(getattr(order, "task_id", ""))
            task = self._find_task(tid)
            if task is None:
                return
            trades = getattr(task.portfolio, "trades", None) or []
            if not trades:
                return
            rec = trades[-1]
            self.repo.record_trade(
                self.run_id, tid, rec,
                order_id=str(getattr(order, "order_id", "")),
                cash_after=float(task.portfolio.account.cash),
                position_after=int(task.portfolio.position.size),
            )
            self.stats["trades"] += 1
        except Exception as exc:
            self.stats["errors"] += 1
            logger.debug(f"成交落库失败: {exc}")

    def _find_task(self, task_id: str):
        runtime = self._runtime()
        if runtime is None:
            return None
        try:
            return runtime.get(task_id)
        except Exception:
            return None

    def _on_collected(self, symbol: str = "", period: str = "", rows: int = 0,
                      **kwargs: Any) -> None:
        if not self.enabled:
            return
        self.repo.record_event(
            self.run_id,
            message=f"行情采集 {symbol} {period} 入库 {rows} 条",
            level="INFO", category="collect", symbol=str(symbol),
            detail={"period": period, "rows": int(rows or 0)},
        )
        self.stats["events"] += 1

    def _on_error(self, **kwargs: Any) -> None:
        if not self.enabled:
            return
        msg = str(kwargs.get("message") or kwargs.get("error") or kwargs.get("component") or "")
        component = str(kwargs.get("component") or kwargs.get("name") or "")
        self.repo.record_event(
            self.run_id,
            message=f"组件异常 | {component} | {msg}"[:800],
            level="ERROR", category="error",
            detail={k: str(v)[:200] for k, v in kwargs.items()},
        )
        self.stats["events"] += 1

    # ============================================================
    # 权益采样
    # ============================================================
    def _sample_equity(self) -> None:
        """按间隔给每个任务采一个权益点。

        **不逐笔存**：曲线看的是趋势，逐笔既没人看也把表撑爆。
        """
        if not self.enabled:
            return
        sm = self.bound_engine.get_component("strategy_manager") if self.bound_engine else None
        runtime = getattr(sm, "runtime", None) if sm is not None else None
        if runtime is None:
            return
        now = time.monotonic()
        try:
            tasks = list(runtime)
        except Exception:
            return
        for t in tasks:
            try:
                tid = str(getattr(t, "task_id", ""))
                last = self._last_equity_at.get(tid, 0.0)
                if now - last < self.equity_interval * 0.9:
                    continue
                p = t.portfolio
                self.repo.record_equity(
                    self.run_id, tid, str(getattr(t, "symbol", "")),
                    equity=float(p.equity), cash=float(p.account.cash),
                    position_size=int(p.position.size),
                    market_value=float(p.position.market_value),
                    drawdown=float(p.drawdown),
                )
                self._last_equity_at[tid] = now
                self.stats["equity_points"] += 1
            except Exception as exc:
                self.stats["errors"] += 1
                logger.debug(f"权益采样失败: {exc}")

    # ============================================================
    # 可观测
    # ============================================================
    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "run_id": self.run_id,
            "equity_interval": self.equity_interval,
            "stats": dict(self.stats),
            "repo": self.repo.health(),
        }

    def health_check(self):
        if not self.enabled:
            return True, "disabled"
        h = self.repo.health()
        if h["errors"] and not h["written"]:
            return False, f"落库持续失败: {h['last_error'][:80]}"
        if h["pending"] > 1000:
            return True, f"积压 {h['pending']} 条（磁盘可能较慢）"
        return True, f"已写 {h['written']} 条"


__all__ = ["TradingStoreComponent"]
