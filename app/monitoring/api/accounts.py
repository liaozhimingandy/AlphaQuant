#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : accounts.py
# @Description : 面板视图：账户、交易时段、标的主表
#
#                这三个视图的共同点是**它们回答的都是"系统现在处于什么状态"**，
#                而不是"某个对象长什么样"。状态类视图的写法有个坑：
#                它们的数据来源容易变成"能拿到就读，拿不到就编一个"。
#
#                这里的纪律是：**拿不到就返回空并写明原因**。
#                账户余额显示成 0（其实没读到）比显示成"服务不在场"危险得多 ——
#                前者看起来像"钱亏光了"，后者才是真相。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict


class AccountViewsMixin:
    """账户 / 时段 / 标的主表 的展示逻辑。

    依赖 ``self.component``（由 ``MonitorWebComponent`` 提供），
    并且**只读**，不做任何会改变账户状态的动作。
    """

    # ============================================================
    # 账户
    # ============================================================
    def accounts_view(self) -> Dict[str, Any]:
        """账户概况 + 交易时段。

        :return: ``{"ok": bool, "reason": str, "accounts": [...], "session": {...}}``
        """
        out: Dict[str, Any] = {
            "ok": False, "reason": "", "accounts": [], "session": {},
            "default_account_id": "", "total_equity": 0.0,
        }
        # 优先走账户服务（它手上有"能不能交易"与"时段"这类运行态）；
        # 服务不在场时退回**直接读仓储** —— 面板看账户不该依赖某个组件
        # 是否装配，能看多少看多少。
        svc = self._account_service()
        if svc is not None:
            try:
                snap = svc.accounts()
                out.update({
                    "ok": True,
                    "accounts": list(snap.get("accounts") or []),
                    "default_account_id": snap.get("default_account_id") or "",
                    "total_equity": float(snap.get("total_equity") or 0.0),
                })
                out["session"] = svc.session_info()
                out["settles"] = int(getattr(svc, "settles", 0) or 0)
                out["fills_applied"] = int(getattr(svc, "fills_applied", 0) or 0)
                return out
            except Exception as exc:
                out["reason"] = f"账户服务读取失败: {exc}"
        try:
            from app.account import store as S

            snap = S.summary()
            out.update({
                "ok": True,
                "accounts": list(snap.get("accounts") or []),
                "default_account_id": snap.get("default_account_id") or "",
                "total_equity": float(snap.get("total_equity") or 0.0),
            })
        except Exception as exc:
            out["ok"] = False
            out["reason"] = out.get("reason") or f"账户表读取失败: {exc}"

        try:
            from app.domain.calendar import get_calendar

            cal = get_calendar()
            out["session"] = {"state": cal.state()}
        except Exception as exc:
            out["session"] = {"state": "", "error": str(exc)[:120]}
        return out

    def session_view(self) -> Dict[str, Any]:
        """交易时段：现在能不能下单，为什么。"""
        svc = self._account_service()
        if svc is not None:
            try:
                info = dict(svc.session_info())
                info["source"] = "account_service"
                return info
            except Exception:
                pass
        try:
            from app.domain.calendar import get_calendar

            cal = get_calendar()
            state = cal.state()
        except Exception as exc:
            return {"state": "", "tradable": False, "error": str(exc)[:160]}
        return {
            "state": state,
            "tradable": state in ("open", "lunch", "post_close"),
            "source": "calendar",
        }

    # ============================================================
    # 标的主表
    # ============================================================
    def profile_view(self) -> Dict[str, Any]:
        """标的主表概况：有多少只票、多少只还没有上市日期。"""
        try:
            from app.data.repo import stock_info_repo as P

            return {"ok": True, **P.summary()}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    # ============================================================
    # 内部
    # ============================================================
    def _account_service(self) -> Any:
        comp = getattr(self, "component", None)
        engine = getattr(comp, "engine", None) or getattr(comp, "_engine", None)
        if engine is None:
            return None
        for getter in ("get_component", "service_by_capability"):
            fn = getattr(engine, getter, None)
            if not callable(fn):
                continue
            try:
                svc = fn("account_service")
            except Exception:
                svc = None
            if svc is not None:
                return svc
        return None


__all__ = ["AccountViewsMixin"]
