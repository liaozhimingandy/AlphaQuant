#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""总览视图：引擎/任务/能力清单/配置/统计/日志尾部。"""
from __future__ import annotations


from datetime import date, datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from app.common.config import settings
from app.common.jsonio import dumps, tail_lines
from app.common.logging import logger
from app.storage.repo.trade_repo import TradingRepository, get_repository

# 原文件缺失、按实际引用补齐：
from app.common.jsonio import json_safe


class OverviewViewsMixin:
    def build_overview(self) -> Dict[str, Any]:
        self.stats["api_calls"] += 1
        control = self.control
        engine_snap: Dict[str, Any] = {}
        if self.bound_engine is not None:
            try:
                engine_snap = self.bound_engine.snapshot().get("engine", {})
            except Exception as exc:
                engine_snap = {"error": str(exc)}

        tasks = control.list_tasks() if control is not None else []
        symbols = set()
        for t in tasks:
            if t.get("symbol"):
                symbols.add(t["symbol"])

        return {
            "engine": engine_snap,
            "components": control.component_states() if control is not None else [],
            "tasks": tasks,
            "task_count": len(tasks),
            "symbol_count": len(symbols),
            "orders": len(self.store.recent_orders(10_000)) if self.store else 0,
            "monitor": self.stats_dict(),
            "audit": list(self.audit),
        }

    def order_feed(self, limit: int = 100) -> List[Dict[str, Any]]:
        if self.store is None:
            return []
        rows = self.store.order_history(limit)
        if rows:
            return rows
        return self.store.recent_orders(limit)

    # ============================================================
    # 实盘
    # ============================================================
    def build_live_overview(self) -> Dict[str, Any]:
        """实盘总览：网关状态、对账结论、未结订单。"""
        gw = None
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
            except Exception:
                gw = None
        if gw is None:
            return {
                "available": False,
                "reason": "本引擎未启用实盘网关（运行模式非 LIVE）",
                "mode": (self.bound_engine.snapshot().get("engine", {}).get("mode")
                         if self.bound_engine else ""),
            }
        out = {"available": True}
        out.update(json_safe(gw.snapshot()))
        # 未结订单按任务汇总：实盘最需要盯的就是"挂在外面还没成交的单"
        pending = []
        try:
            for o in TradingRepository.recent_orders(limit=500):
                if o.get("status") in ("PENDING", "SUBMITTED", "PARTIAL"):
                    pending.append(o)
        except Exception as exc:
            logger.debug(f"查询未结订单失败: {exc}")
        out["pending_orders"] = pending[:200]
        out["pending_count"] = len(pending)
        return out

    # ============================================================
    # 配置视图
    # ============================================================
    def build_capabilities(self) -> Dict[str, Any]:
        """能力清单。面板的下拉框从这里取 —— 硬编码一份清单迟早和代码不一致，
        用户会看到一个"填了却报未注册"的策略名。

        也把**接入点**与**回测默认参数**一并返回：面板要能显示"我现在连的是哪个账户"，
        以及"表单里没填的字段默认是什么"。
        """
        from app.research.defaults import BacktestDefaults
        from app.research.registry import (
            list_strategies as bt_strategies,
            user_strategy_dir,
            user_strategy_names,
        )
        from app.execution.endpoints import describe_endpoints
        from app.execution.gateway import gateway_signature, list_gateways
        from app.feature import list_factors
        from app.feature.indicator.registry import describe_indicator, list_indicators
        from app.strategy.rule import RULE_TYPES
        from app.risk import list_risks
        from app.strategy import list_strategies as live_strategies

        def _safe(fn, default):
            try:
                return fn()
            except Exception as exc:
                logger.debug(f"能力清单读取失败: {exc}")
                return default

        indicators = _safe(list_indicators, [])
        gateways = _safe(list_gateways, [])
        return {
            "backtest_strategies": _safe(bt_strategies, []),
            "user_strategies": _safe(user_strategy_names, []),
            "user_strategy_dir": _safe(user_strategy_dir, ""),
            "live_strategies": _safe(live_strategies, []),
            "indicators": [
                {"name": n, "params": _safe(lambda n=n: describe_indicator(n), {})}
                for n in indicators
            ],
            "factors": _safe(list_factors, []),
            "risk_rules": _safe(list_risks, []),
            "rule_types": list(_safe(lambda: RULE_TYPES, ())),
            "gateways": [
                {"name": g, "params": _safe(lambda g=g: gateway_signature(g), {})}
                for g in gateways
            ],
            "endpoints": _safe(describe_endpoints, {}),
            "backtest_defaults": _safe(lambda: BacktestDefaults.load().to_dict(), {}),
            "snapshot_modes": list(self.SNAPSHOT_MODES),
        }

    def build_config_view(self) -> Dict[str, Any]:
        """当前生效的运行时配置。面板据此显示"我现在是什么档位"。"""
        from app.common.logging import is_debug, throttle_stats

        return {
            "log": {
                "level": settings.LOG_LEVEL,
                "file_level": settings.LOG_FILE_LEVEL or settings.LOG_LEVEL,
                "debug_mode": is_debug(),
                "retention": settings.LOG_RETENTION,
                "rotation": settings.LOG_ROTATION,
                "throttle_window": settings.LOG_THROTTLE_WINDOW,
                "throttled": throttle_stats(),
                "dir": str(settings.LOG_DIR),
            },
            "snapshot": {
                "enabled": self.snapshot_enabled,
                "mode": self.snapshot_mode,
                "check_interval": self.snapshot_interval,
                "min_gap": self.snapshot_min_gap,
                "debounce": self.snapshot_debounce,
                "trigger_events": list(self.TRIGGER_EVENTS),
                "writes": self.stats["writes"],
                "triggers": self.stats["triggers"],
                "last_trigger": self.stats["last_trigger"],
                "skipped_unchanged": self.stats["skipped_unchanged"],
                "skipped_too_soon": self.stats["skipped_too_soon"],
                "pending": bool(self._dirty),
            },
            "persist": get_repository().health(),
            "engine": (self.bound_engine.snapshot().get("engine", {})
                       if self.bound_engine else {}),
        }

    def stats_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "snapshot_interval": self.snapshot_interval,
            "snapshot_enabled": self.snapshot_enabled,
            "snapshot_mode": self.snapshot_mode,
            "snapshot_min_gap": self.snapshot_min_gap,
            "snapshot_debounce": self.snapshot_debounce,
            "writes": self.stats["writes"],
            "triggers": self.stats["triggers"],
            "last_trigger": self.stats["last_trigger"],
            "skipped_unchanged": self.stats["skipped_unchanged"],
            "skipped_too_soon": self.stats["skipped_too_soon"],
            "snapshot_pending": bool(self._dirty),
            "orders": self.stats["orders"],
            "rejections": self.stats["rejections"],
            "last_snapshot_at": self.stats["last_snapshot_at"],
            "run_id": self.run_id,
            "namespace": self.namespace,
            "run_dir": str(self.store.root) if self.store else None,
            "listen_ok": self._listening is not None,
            "listen_error": self.stats.get("listen_error", ""),
            "api_calls": self.stats["api_calls"],
        }

    def log_tail(self, n: int = 200):
        today = Path(settings.LOG_DIR) / f"alphaquant_{date.today().strftime('%Y-%m-%d')}.log"
        if today.exists():
            return tail_lines(today, n), today.name
        daemon = Path(settings.DAEMON_LOG)
        if daemon.exists():
            return tail_lines(daemon, n), daemon.name
        cands = sorted(Path(settings.LOG_DIR).glob("alphaquant_*.log"))
        if cands:
            return tail_lines(cands[-1], n), cands[-1].name
        return [], "未找到日志文件"
