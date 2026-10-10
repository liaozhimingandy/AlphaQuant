#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : backtest_api.py
# @Description : 回测控制面：把"跑一次回测"变成可以从面板/CLI 触发的任务
#
#                关键约束：回测是 CPU 密集 + 可能几十秒的操作，
#                它**必须在工作线程里跑**。面板与引擎共用同一个 reactor，
#                在 reactor 线程跑回测会让行情、策略、撮合、面板一起停摆。
#
#                所以这里的模型是"提交作业 → 后台执行 → 轮询结果"：
#                  - submit() 立即返回 job_id
#                  - 执行在线程池
#                  - 结果落 SQLite（backtest_result 表），列表页直接查库
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import itertools
import threading
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from app.core.config import settings
from app.repository.trading_repository import TradingRepository, get_repository
from app.utils.jsonio import dumps, json_safe
from app.utils.logger import logger


class BacktestJob:
    """一次回测作业的状态。"""

    __slots__ = ("job_id", "status", "params", "run_id", "created_at",
                 "started_at", "finished_at", "error", "result_id", "summary")

    def __init__(self, job_id: str, params: Dict[str, Any]) -> None:
        self.job_id = job_id
        self.status = "QUEUED"          # QUEUED / RUNNING / DONE / FAILED
        self.params = params
        self.run_id = ""
        self.created_at = datetime.now()
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.error = ""
        self.result_id: Optional[int] = None
        self.summary: Dict[str, Any] = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "params": self.params,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed": (
                (self.finished_at or datetime.now()) - self.started_at
            ).total_seconds() if self.started_at else None,
            "error": self.error,
            "result_id": self.result_id,
            "summary": self.summary,
        }


class BacktestService:
    """回测作业管理器。

    作业记录保留在内存里（数量有限、生命周期短），
    结果摘要与曲线落 SQLite（要长期查、要能列表排序）。
    """

    #: 内存里最多保留多少个作业记录
    MAX_JOBS = 100
    #: 同时最多跑几个回测。超过会让 CPU 打满、拖慢实盘链路
    MAX_CONCURRENT = 2

    def __init__(self) -> None:
        self._jobs: Dict[str, BacktestJob] = {}
        self._order: List[str] = []
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._running = 0
        self.stats = {"submitted": 0, "done": 0, "failed": 0}

    # ============================================================
    # 提交
    # ============================================================
    def submit(self, params: Dict[str, Any], executor=None) -> BacktestJob:
        """提交一个回测作业。立即返回，不阻塞调用方。

        :param executor: 线程池执行器（reactor 的线程池）。
                         不传则用一个普通线程（CLI/脚本场景）。
        """
        cleaned = self._normalize(params)
        with self._lock:
            job = BacktestJob(f"bt{next(self._seq):05d}", cleaned)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            while len(self._order) > self.MAX_JOBS:
                old = self._order.pop(0)
                self._jobs.pop(old, None)
            self.stats["submitted"] += 1

        if executor is not None:
            executor(job.job_id)
        else:
            threading.Thread(
                target=self.run_job, args=(job.job_id,), daemon=True
            ).start()
        logger.info(
            f"回测作业已提交 | {job.job_id} | {cleaned.get('symbol')} "
            f"{cleaned.get('strategy')} {cleaned.get('start')}~{cleaned.get('end')}"
        )
        return job

    @classmethod
    def _normalize(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        """归一化入参：**默认值来自 config/backtest.json**，显式入参优先。

        策略参数支持 dict，也支持 ``"fast=5 slow=20"`` 这种字符串（命令行/面板手填）。
        金额类（cash/commission/slippage）走同一份默认值，
        这样"命令行跑的"和"面板跑的"结果一致 —— 它们读的是同一个来源。
        """
        from app.backtest.defaults import BacktestDefaults

        defaults = BacktestDefaults.load()
        merged = defaults.merge(params)

        raw_params = params.get("params") if isinstance(params, Mapping) else None
        if raw_params is None:
            raw_params = merged.get("strategy_params") or {}
        if isinstance(raw_params, str):
            parsed: Dict[str, Any] = {}
            for chunk in raw_params.replace(",", " ").split():
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    parsed[k.strip()] = _coerce(v.strip())
            raw_params = parsed
        if not isinstance(raw_params, Mapping):
            raw_params = {}
        raw_params = dict(raw_params)

        # 声明式策略的规则可以放在三个地方，面板会把 spec 提到顶层更顺手：
        #   params.spec / 顶层 spec / 顶层 entry+exit
        # 三种都收敛到 strategy_params 里，DeclarativeStrategy 只认一种格式。
        if isinstance(params, Mapping):
            if params.get("spec") is not None:
                raw_params.setdefault("spec", params["spec"])
            if params.get("entry") is not None:
                raw_params.setdefault("entry", params["entry"])
            if params.get("exit") is not None:
                raw_params.setdefault("exit", params["exit"])
            if params.get("entry_file"):
                raw_params.setdefault("entry_file", params["entry_file"])
            if params.get("exit_file"):
                raw_params.setdefault("exit_file", params["exit_file"])

        return {
            "symbol": str(merged.get("symbol") or "").strip(),
            "start": str(merged.get("start") or defaults.start),
            "end": str(merged.get("end") or defaults.resolved_end()),
            "strategy": str(merged.get("strategy") or defaults.strategy),
            "cash": float(merged.get("cash") or defaults.cash),
            "commission": float(
                merged.get("commission")
                if merged.get("commission") is not None else defaults.commission
            ),
            "slippage": float(
                merged.get("slippage")
                if merged.get("slippage") is not None else defaults.slippage
            ),
            "adjust": str(merged.get("adjust") or defaults.adjust),
            "data_source": str(merged.get("data_source") or defaults.data_source),
            "risk_free_rate": float(
                merged.get("risk_free_rate")
                if merged.get("risk_free_rate") is not None
                else defaults.risk_free_rate
            ),
            "trading_days_per_year": int(
                merged.get("trading_days_per_year") or defaults.trading_days_per_year
            ),
            "params": dict(raw_params),
            "defaults_source": defaults.source,
        }

    # ============================================================
    # 执行（工作线程）
    # ============================================================
    def run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "QUEUED":
                return
            if self._running >= self.MAX_CONCURRENT:
                job.status = "FAILED"
                job.error = f"并发回测已达上限 {self.MAX_CONCURRENT}，请稍后重试"
                job.finished_at = datetime.now()
                self.stats["failed"] += 1
                return
            job.status = "RUNNING"
            job.started_at = datetime.now()
            self._running += 1

        try:
            self._execute(job)
            job.status = "DONE"
            self.stats["done"] += 1
        except Exception as exc:
            job.status = "FAILED"
            job.error = str(exc)[:500]
            self.stats["failed"] += 1
            logger.error(f"回测作业 {job_id} 失败: {exc}", exc_info=True)
        finally:
            job.finished_at = datetime.now()
            with self._lock:
                self._running = max(0, self._running - 1)

    def _execute(self, job: BacktestJob) -> None:
        from app.backtest import BacktestConfig, run_backtest

        p = job.params
        cfg = BacktestConfig(
            symbol=p["symbol"],
            start=p["start"],
            end=p["end"],
            strategy=p["strategy"],
            strategy_params=p.get("params") or {},
            cash=p["cash"],
            commission=p["commission"],
            slippage_perc=p["slippage"],
            data_source=p["data_source"],
            adjust=str(p.get("adjust") or "qfq"),
            risk_free_rate=float(p.get("risk_free_rate") or 0.0),
            trading_days_per_year=int(p.get("trading_days_per_year") or 252),
            plot=False,
            print_log=False,
            export=False,
        )
        result = run_backtest(cfg)

        job.run_id = f"bt_{job.job_id}_{datetime.now().strftime('%H%M%S')}"
        summary = json_safe(result.summary()) if hasattr(result, "summary") else {}
        job.summary = summary

        # 曲线降采样后落库：曲线看趋势，不需要逐点存
        curve = []
        try:
            eq = getattr(result, "equity_curve", None)
            if eq is not None and not eq.empty:
                step = max(1, len(eq) // 400)
                for dt, row in eq.iloc[::step].iterrows():
                    v = row.iloc[0] if hasattr(row, "iloc") else None
                    if v is None:
                        continue
                    curve.append([str(dt), float(v)])
        except Exception as exc:
            logger.debug(f"回测曲线采样失败（忽略）: {exc}")

        repo = get_repository()
        repo.record_backtest(job.run_id, {
            "task_id": job.job_id,
            "symbol": result.symbol,
            "strategy": result.strategy,
            "start_date": result.start,
            "end_date": result.end,
            "initial_cash": result.initial_cash,
            "final_equity": result.final_cash,
            "total_return": result.total_return_pct / 100.0,
            "max_drawdown": result.max_drawdown_pct / 100.0,
            "sharpe": result.sharpe,
            "trade_count": result.trade_count,
            "win_rate": result.win_rate_pct / 100.0,
            "metrics": summary,
            "equity_curve": curve,
        })
        # 立刻刷一次队列，让面板轮询能尽快看到结果（不等批量周期）。
        # 注意用 flush 而不是 stop：仓储是进程内共享的，stop 会关掉
        # 引擎正在使用的后台写线程。
        repo.flush()
        job.result_id = self._latest_result_id(job.run_id)
        logger.info(
            f"回测作业完成 | {job.job_id} | {result.symbol} {result.strategy} | "
            f"收益 {result.total_return_pct:.2f}% | 回撤 {result.max_drawdown_pct:.2f}% | "
            f"夏普 {result.sharpe:.2f} | 成交 {result.trade_count} 笔"
        )

    @staticmethod
    def _latest_result_id(run_id: str) -> Optional[int]:
        try:
            rows = TradingRepository.backtests(limit=200)
            for r in rows:
                if r.get("run_id") == run_id:
                    return r.get("id")
        except Exception:
            pass
        return None

    # ============================================================
    # 查询
    # ============================================================
    def get(self, job_id: str) -> Optional[BacktestJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i].to_dict() for i in ids if i in self._jobs]

    def stats_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {**self.stats, "running": self._running,
                    "queued": sum(1 for j in self._jobs.values() if j.status == "QUEUED"),
                    "max_concurrent": self.MAX_CONCURRENT}


#: 进程内单例
_service: Optional[BacktestService] = None
_service_lock = threading.Lock()


def get_backtest_service() -> BacktestService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = BacktestService()
    return _service


def _coerce(text: str) -> Any:
    """把 "5" / "0.3" / "true" 这类字符串转成合适的类型。"""
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


__all__ = ["BacktestJob", "BacktestService", "get_backtest_service"]
