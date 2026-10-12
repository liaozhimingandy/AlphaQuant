#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略视图：列表/保存/删除/部署/重新扫描。"""
from __future__ import annotations


from datetime import date, datetime
from typing import Any, Deque, Dict, List, Optional

from app.common.logging import logger


class StrategyViewsMixin:
    # ============================================================
    # 用户策略库（界面自定义策略）
    # ============================================================
    def strategies_view(self) -> Dict[str, Any]:
        """已注册策略 + 用户自定义策略 + 可用因子/风控（界面要靠它渲染表单）。

        注意：界面「策略库」表单里的因子下拉依赖 ``groups``（按类别分组的
        全量因子目录，带 has_data 标记），不是 ``factors``（只有因子库里的
        表达式因子）。之前只返回了 factors，导致规则策略的因子下拉只剩
        "（不选）"一个选项 —— 这里同时把 groups 给上。
        """
        from app.modules.strategy.store import KINDS, get_store

        store = get_store()
        out: Dict[str, Any] = {
            "kinds": KINDS,
            "user": [s.to_dict() for s in store.list()],
            "path": str(store.path),
        }
        try:
            from app.modules.factor.catalog import CATEGORY_DOCS, factor_catalog
            from app.modules.factor import list_factors
            from app.modules.risk import list_risks
            from app.modules.strategy import list_strategies

            out["factors"] = list_factors()
            out["groups"] = factor_catalog()["categories"]
            out["group_docs"] = CATEGORY_DOCS
            out["risk_rules"] = list_risks()
            out["registered"] = list_strategies()
        except Exception as exc:
            logger.debug(f"策略清单读取失败: {exc}")
        return out

    def strategy_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.modules.strategy.store import get_store

            ok, msg, s = get_store().add(
                name=str(body.get("name") or ""),
                kind=str(body.get("kind") or ""),
                spec=dict(body.get("spec") or {}),
                description=str(body.get("description") or ""),
                risk=list(body.get("risk") or []),
                overwrite=bool(body.get("overwrite", True)),
            )
            out: Dict[str, Any] = {"ok": ok, "message": msg}
            if s is not None:
                out["strategy"] = s.to_dict()
            return out
        except Exception as exc:
            logger.error(f"保存策略失败: {exc}", exc_info=True)
            return {"ok": False, "message": str(exc)}

    def strategy_remove(self, name: str) -> Dict[str, Any]:
        try:
            from app.modules.strategy.store import get_store

            ok, msg = get_store().remove(name)
            return {"ok": ok, "message": msg}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def strategy_deploy(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """把自定义策略上线为实盘任务。

        这一步会**真的开始下单**，所以：
          1. 必须走控制面（统一指令入口，带审计）；
          2. 默认带一条 max_drawdown 风控兜底 —— 用户可能忘记配。
        """
        try:
            from app.modules.strategy.store import get_store

            name = str(body.get("name") or "")
            s = get_store().get(name)
            if s is None:
                return {"ok": False, "error": f"策略不存在: {name}"}
            symbol = str(body.get("symbol") or "").strip()
            if not symbol:
                return {"ok": False, "error": "缺少标的 symbol"}

            risk = [dict(r) for r in (s.risk or [])]
            if not any(str(r.get("type")) == "max_drawdown" for r in risk):
                risk.append({"type": "max_drawdown", "max_drawdown": 0.2})
            spec = {
                "task_id": str(body.get("task_id") or f"{name}-{symbol}"),
                "name": s.description or name,
                "symbol": symbol,
                "enabled": True,
                "initial_cash": float(body.get("initial_cash") or 100000),
                "strategy": s.to_task_strategy(),
                "risk": risk,
            }
            result = self.control.apply_command({"action": "add_task", "spec": spec})
            self.audit.append({
                "at": datetime.now().isoformat(), "action": "deploy_strategy",
                "detail": {"strategy": name, "symbol": symbol,
                           "task_id": spec["task_id"]},
            })
            return {"ok": True, "task_id": spec["task_id"], "result": result}
        except Exception as exc:
            logger.error(f"策略上线失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    # ============================================================
    # 自定义策略热加载
    # ============================================================
    def reload_strategies(self) -> Dict[str, Any]:
        """重新扫描用户策略目录。

        改完策略代码不想重启服务时用。**只重扫目录、不动已有任务** ——
        已经建好的任务持有的仍是旧策略实例（换策略要重建任务，
        因为策略内部可能已经积累了状态）。
        """
        out: Dict[str, Any] = {"ok": True}
        try:
            from app.modules.backtest.registry import load_user_strategies as bt_load

            out["backtest"] = sorted(bt_load(force=True))
        except Exception as exc:
            out["backtest_error"] = str(exc)
        try:
            from app.modules.strategy.registry import load_user_strategies as live_load

            out["engine"] = sorted(live_load(force=True))
        except Exception as exc:
            out["engine_error"] = str(exc)
        self.audit.append({
            "at": datetime.now().isoformat(), "action": "reload_strategies",
            "detail": {"backtest": out.get("backtest", []),
                       "engine": out.get("engine", [])},
        })
        logger.info(f"用户策略已重新扫描 | 回测 {out.get('backtest')} | 引擎 {out.get('engine')}")
        return out
