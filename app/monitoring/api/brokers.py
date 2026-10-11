#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""券商接入点视图：列出与连通性自检。"""
from __future__ import annotations


from typing import Any, Deque, Dict, List, Optional


class BrokerViewsMixin:
    # ============================================================
    # 券商接入点
    # ============================================================
    def endpoints_view(self) -> Dict[str, Any]:
        """接入点配置视图（凭据已由 endpoints 层脱敏）。"""
        from app.execution.endpoints import describe_endpoints
        from app.execution.gateway import gateway_signature, list_gateways

        try:
            data = describe_endpoints()
        except Exception as exc:
            return {"error": str(exc), "endpoints": []}
        # 标注"当前引擎连的是哪个" —— 光看配置列表判断不出正在用的是谁
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
                data["in_use"] = getattr(gw, "endpoint_id", "") if gw else ""
            except Exception:
                data["in_use"] = ""
        data["gateways"] = [
            {"name": g, "params": gateway_signature(g)} for g in list_gateways()
        ]
        return data

    def check_endpoint(self, endpoint_id: str = "") -> Dict[str, Any]:
        """试连接一个接入点：**只查询，不下单**。

        工作线程里跑（调用方用 deferToThread 包起来），因为它会做网络 IO。
        用途很具体：改完 brokers.json 先验一下能不能连上、账号对不对，
        别等到开了实盘才发现连错账户。
        """
        from app.execution.endpoints import load_endpoints

        try:
            cfg = load_endpoints()
        except Exception as exc:
            return {"ok": False, "error": f"读取接入点配置失败: {exc}"}
        try:
            endpoint = cfg.resolve(endpoint_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if endpoint is None:
            return {
                "ok": False,
                "error": f"未找到接入点（配置 {cfg.path} 里没有 default_endpoint）",
                "available": [e.id for e in cfg.list()],
            }
        if not endpoint.enabled:
            return {"ok": False, "error": f"接入点 {endpoint.id} 已禁用"}

        from app.execution.gateway import create_gateway, has_gateway, list_gateways

        if not has_gateway(endpoint.gateway):
            return {
                "ok": False,
                "error": f"网关 {endpoint.gateway!r} 未注册",
                "available_gateways": list_gateways(),
            }
        gw = None
        try:
            gw = create_gateway(endpoint.gateway, **endpoint.gateway_kwargs())
            gw.connect()
            acct = gw.query_account()
            poss = gw.query_positions()
            return {
                "ok": True,
                "endpoint": endpoint.to_dict(),
                "gateway": getattr(gw, "name", endpoint.gateway),
                "account": acct.to_dict() if hasattr(acct, "to_dict") else {},
                "positions": {
                    sym: {"size": int(p.size), "avg_price": float(p.avg_price),
                          "sellable": int(getattr(p, "sellable", p.size))}
                    for sym, p in (poss or {}).items()
                },
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if gw is not None:
                try:
                    gw.disconnect()
                except Exception:
                    pass
