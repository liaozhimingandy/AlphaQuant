#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘视图：对账、账户刷新、全撤。"""
from __future__ import annotations


from datetime import date, datetime
from typing import Any, Deque, Dict, List, Optional

from app.common.logging import logger


class LiveViewsMixin:
    def reconcile_now(self) -> Dict[str, Any]:
        gw = None
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
            except Exception:
                gw = None
        if gw is None:
            return {"ok": False, "error": "本引擎未启用实盘网关"}
        try:
            return {"ok": True, **gw.reconcile_now()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def refresh_live_account(self) -> Dict[str, Any]:
        """让运行中的实盘网关立刻去券商拉一次账户/持仓。"""
        gw = None
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
            except Exception:
                gw = None
        if gw is None:
            return {"ok": False, "error": "本引擎未启用实盘网关"}
        try:
            data = gw.refresh_account()
            self.audit.append({
                "at": datetime.now().isoformat(), "action": "refresh_account",
                "detail": {"positions": len(data.get("positions") or {})},
            })
            return {"ok": True, **data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def cancel_all_orders(self, task_id: str = "") -> Dict[str, Any]:
        """撤销未结订单。实盘出事时最常用的一个动作。"""
        sm = None
        if self.bound_engine is not None:
            try:
                sm = self.bound_engine.get_component("strategy_manager")
            except Exception:
                sm = None
        runtime = getattr(sm, "runtime", None) if sm is not None else None
        if runtime is None:
            return {"ok": False, "error": "策略中枢不可用"}
        n = 0
        for t in list(runtime):
            if task_id and getattr(t, "task_id", "") != task_id:
                continue
            try:
                n += t.cancel_open_orders()
            except Exception as exc:
                logger.debug(f"任务 {getattr(t, 'task_id', '?')} 撤单失败: {exc}")
        self.audit.append({
            "at": datetime.now().isoformat(), "action": "cancel_all_orders",
            "detail": {"task_id": task_id or "*", "count": n},
        })
        logger.warning(f"面板下发撤单指令 | 任务={task_id or '*'} | 已请求 {n} 笔")
        return {"ok": True, "cancelled": n}
