#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : live_gateway.py
# @Description : 实盘网关组件：连接券商、轮询成交回报、路由回对应任务、定期对账
#
#                为什么要做成组件：
#                  回报是**外部事件**，它的到达时机和行情无关。做成组件之后
#                  它有自己的生命周期（引擎启动时连券商、停止时先撤单再断开），
#                  而且能被统一开关、统一监控、统一纳入优雅退出流程。
#
#                退出顺序是这里最容易出事的地方：
#                  引擎收到停止指令 → **先撤销所有未结订单** → 再等回报收尾
#                  → 最后断连。
#                  如果直接断连，挂在券商那边的单会变成"孤儿单"：
#                  进程没了、单还在，第二天开盘才知道成交了。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from twisted.internet import defer, task, threads

from app.core.config import settings
from app.core.engine.components.ibase import IBaseComponent
from app.core.engine.event import StandardEvents
from app.core.execution.endpoints import load_endpoints
from app.core.execution.gateway import (
    FillEvent,
    GatewayError,
    GatewayOrder,
    IBrokerGateway,
    create_gateway,
    has_gateway,
    list_gateways,
)
from app.core.market.types import OrderStatus, RunMode
from app.utils.jsonio import json_safe
from app.utils.logger import log_throttled, logger


class LiveGatewayComponent(IBaseComponent):
    """实盘网关。

    只在 ``RUN_MODE=LIVE`` 时装配。它持有**进程内唯一的网关连接**，
    并把它分发给各个任务的 LiveBroker —— 券商通常不允许一个账号开多条长连接。

    它同时负责"从券商源读账户信息"：资金、可用、持仓、冻结。
    本地账本再怎么准，最终**以券商为准** —— 这两者是独立的两本账，
    定期比对（对账）才能发现差异。所以这里会定期把券商账户刷进内存，
    面板和 CLI 直接读这份缓存，不用每次都去问券商（问一次是秒级网络 IO）。
    """

    name = "live_gateway"

    def __init__(
        self,
        gateway: Optional[IBrokerGateway] = None,
        gateway_name: str = "",
        endpoint_id: str = "",
        poll_interval: float = 1.0,
        reconcile_interval: float = 300.0,
        account_interval: float = 30.0,
        strict_reconcile: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.gateway = gateway
        self.gateway_name = str(gateway_name or settings.BROKER_GATEWAY)
        #: 接入点标识。空 = 用 brokers.json 里的默认接入点
        self.endpoint_id = str(endpoint_id or settings.BROKER_ENDPOINT or "")
        #: 回报轮询间隔。实盘建议 0.5~1s：太快没必要（券商回报本身有延迟），
        #: 太慢会让"下单后多久知道成交"变成用户能感知的等待
        self.poll_interval = float(poll_interval)
        self.reconcile_interval = float(reconcile_interval)
        #: 账户信息刷新间隔（秒）。券商查询接口通常有频率限制，
        #: 15~30s 足够让面板显示"账户现在多少钱、持了什么仓"。
        self.account_interval = float(account_interval)
        self.strict_reconcile = (
            settings.LIVE_STRICT_RECONCILE if strict_reconcile is None
            else bool(strict_reconcile)
        )
        #: 本地订单号 → LiveBroker。回报靠它路由回正确的任务
        self._brokers: Dict[str, Any] = {}
        self._poll_loop: Optional[task.LoopingCall] = None
        self._reconcile_loop: Optional[task.LoopingCall] = None
        self._account_loop: Optional[task.LoopingCall] = None
        self._busy = False
        #: 从券商读到的账户与持仓（面板直接读这份缓存）
        self.account: Dict[str, Any] = {}
        self.positions: Dict[str, Any] = {}
        self.account_at: str = ""
        self.account_error: str = ""
        self.endpoint_info: Dict[str, Any] = {}
        self.stats: Dict[str, int] = {
            "polls": 0, "fills": 0, "updates": 0, "unknown": 0,
            "reconciles": 0, "mismatches": 0, "errors": 0, "account_refreshes": 0,
        }
        self.last_reconcile: Dict[str, Any] = {}

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        if cfg.get("gateway_name"):
            self.gateway_name = str(cfg["gateway_name"])
        if cfg.get("endpoint_id") is not None:
            self.endpoint_id = str(cfg["endpoint_id"])
        if cfg.get("poll_interval") is not None:
            self.poll_interval = float(cfg["poll_interval"])
        if cfg.get("reconcile_interval") is not None:
            self.reconcile_interval = float(cfg["reconcile_interval"])
        if cfg.get("account_interval") is not None:
            self.account_interval = float(cfg["account_interval"])

        if self.gateway is None:
            self.gateway = self._build_gateway()

        try:
            self.gateway.connect()
        except Exception as exc:
            # 连不上券商属于**致命错误**：不能假装在交易。
            # 与监控面板端口占用不同（那是可选增强），这里必须让引擎起不来。
            raise RuntimeError(f"券商网关连接失败: {exc}") from exc

        # 启动即读一次券商账户：这样面板一打开就是真实资金/持仓，
        # 而不是"要等第一次对账才显示"。
        self.refresh_account()

        logger.info(
            f"实盘网关就绪 | 网关={getattr(self.gateway, 'name', '?')} | "
            f"接入点={self.endpoint_info.get('id') or '(直接构造)'} | "
            f"账号={getattr(self.gateway, 'account', '') or '(未配置)'}"
            + ("（只读档位：只查询不下单）" if getattr(self.gateway, "readonly", False) else "")
            + f" | 回报轮询 {self.poll_interval}s | 对账 {self.reconcile_interval}s"
        )

    def _build_gateway(self) -> IBrokerGateway:
        """按接入点配置构造网关。"""
        endpoints = load_endpoints()
        endpoint = endpoints.resolve(self.endpoint_id)
        if endpoint is None:
            if not self.gateway_name:
                raise RuntimeError(
                    "实盘模式必须指定券商接入点或网关（BROKER_ENDPOINT / BROKER_GATEWAY）。"
                    f"可用网关: {list_gateways() or '（无）'} | "
                    f"接入点配置: {endpoints.path or '（未找到 brokers.json）'}"
                )
            endpoint_gateway = self.gateway_name
            kwargs: Dict[str, Any] = {}
        else:
            if not endpoint.enabled:
                raise RuntimeError(
                    f"券商接入点 {endpoint.id!r} 已被禁用（enabled=false）"
                )
            endpoint_gateway = self.gateway_name or endpoint.gateway
            if not endpoint_gateway:
                raise RuntimeError(
                    f"接入点 {endpoint.id!r} 没有配置 gateway（{load_endpoints().path}）。"
                    f"可用网关: {list_gateways() or '（无）'}"
                )
            self.endpoint_id = endpoint.id
            kwargs = endpoint.gateway_kwargs()
            self.endpoint_info = endpoint.to_dict()

        if not has_gateway(endpoint_gateway):
            raise RuntimeError(
                f"未注册的券商网关 {endpoint_gateway!r} | 可用: {list_gateways()}"
            )
        return create_gateway(endpoint_gateway, **kwargs)

    def on_start(self) -> defer.Deferred:
        self._poll_loop = task.LoopingCall(self._tick_poll)
        self._poll_loop.start(max(0.2, self.poll_interval), now=False)
        if self.reconcile_interval > 0:
            self._reconcile_loop = task.LoopingCall(self._tick_reconcile)
            self._reconcile_loop.start(max(10.0, self.reconcile_interval), now=False)
        if self.account_interval > 0:
            # 账户查询放线程池：券商查询接口是同步网络 IO，
            # 直接在 reactor 线程跑会把行情、策略、撮合、面板一起堵住。
            self._account_loop = task.LoopingCall(self._tick_account)
            self._account_loop.start(max(5.0, self.account_interval), now=False)
        return defer.succeed(None)

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True):
        for loop in (self._poll_loop, self._reconcile_loop, self._account_loop):
            if loop is not None and loop.running:
                loop.stop()
        self._poll_loop = None
        self._reconcile_loop = None
        self._account_loop = None

        # 停服务前把留在券商的挂单撤掉。
        # 不撤的话进程没了、单还在，第二天开盘才发现成交了 —— 这是实盘最典型的事故。
        if graceful:
            try:
                n = sum(
                    1 for b in self._brokers.values()
                    if getattr(b, "cancel_all", None) and b.cancel_all()
                )
                if n:
                    logger.warning(f"退出前已撤销 {n} 个任务的未结订单，等待回报收尾")
                    # 给券商一点时间把撤单回报推回来
                    yield task.deferLater(__import__("twisted.internet.reactor",
                                                     fromlist=["reactor"]).reactor, 1.5,
                                          lambda: None)
                    self._poll_once()
            except Exception as exc:
                logger.error(f"退出前撤单失败（请人工确认挂单状态）: {exc}")

        try:
            self.gateway.disconnect()
        except Exception as exc:
            logger.warning(f"网关断开异常（忽略）: {exc}")
        logger.info(f"实盘网关已关闭 | {self.stats}")

    def is_busy(self) -> bool:
        """回报还没处理完时，引擎不该被判定为空闲（实盘模式本来也不空闲）。"""
        return self._busy

    # ============================================================
    # Broker 注册
    # ============================================================
    def register_broker(self, order_id_prefix: str, broker: Any) -> None:
        """登记一个任务的 LiveBroker。回报按订单号前缀路由。"""
        self._brokers[order_id_prefix] = broker

    def attach(self, task: Any) -> None:
        """把一个任务接到网关上。QuantTask 建好后由策略中枢调用。"""
        broker = getattr(task, "broker", None)
        if broker is None or not hasattr(broker, "gateway"):
            return
        # 共用同一条网关连接：券商通常不允许同账号多连接
        broker.gateway = self.gateway
        self._brokers[getattr(task, "task_id", "")] = broker

        # 模拟网关自己也要有一本账，否则对账永远报"本地有、券商无"。
        # 券商账户是**一个**，所以本金要累加所有任务，不能各算各的。
        try:
            if getattr(self.gateway, "name", "") == "simulated":
                cash = float(getattr(task.portfolio.account, "initial_cash", 0.0) or 0.0)
                if cash:
                    self.gateway._cash = float(
                        getattr(self.gateway, "_cash", 0.0) or 0.0
                    ) + cash
        except Exception:
            pass
        logger.debug(f"任务 {getattr(task, 'task_id', '?')} 已挂到实盘网关")

    def _find_broker(self, local_order_id: str) -> Optional[Any]:
        for bid, broker in self._brokers.items():
            if local_order_id in getattr(broker, "orders", {}):
                return broker
        return None

    # ============================================================
    # 轮询回报
    # ============================================================
    def _tick_poll(self):
        if self._busy:
            return defer.succeed(None)
        self._busy = True
        d = threads.deferToThread(self._poll_once)
        d.addBoth(self._release)
        return d

    def _release(self, result: Any) -> Any:
        self._busy = False
        if isinstance(result, Exception):
            self.stats["errors"] += 1
            log_throttled("ERROR", f"实盘回报轮询异常: {result}",
                          key="live-poll-error")
            return None
        return result

    # ============================================================
    # 从券商源读账户
    # ============================================================
    def _tick_account(self):
        return threads.deferToThread(self.refresh_account)

    def refresh_account(self) -> Dict[str, Any]:
        """查询券商账户与持仓，刷新内存缓存。

        **为什么要缓存而不是每次现查**：券商查询接口是秒级网络 IO
        且有频率限制，面板每 2 秒刷新一次根本承受不起。
        缓存的是"券商侧的真相"，与本地账本是两本账，对账时比对这两本。
        """
        if self.gateway is None or not self.gateway.is_connected():
            self.account_error = "网关未连接"
            return {}
        try:
            acct = self.gateway.query_account()
            poss = self.gateway.query_positions()
        except Exception as exc:
            self.stats["errors"] += 1
            self.account_error = str(exc)
            log_throttled("WARNING", f"读取券商账户失败: {exc}",
                          key="live-account-error")
            return {}
        self.account_error = ""
        self.account = json_safe(acct.to_dict() if hasattr(acct, "to_dict") else acct)
        self.positions = {
            sym: json_safe({
                "symbol": p.symbol, "size": int(p.size),
                "avg_price": float(p.avg_price),
                "frozen_size": int(getattr(p, "frozen_size", 0) or 0),
                "sellable": int(getattr(p, "sellable", p.size) or 0),
            })
            for sym, p in (poss or {}).items()
        }
        self.account_at = datetime.now().isoformat()
        self.stats["account_refreshes"] += 1
        return {"account": self.account, "positions": self.positions}

    def _poll_once(self) -> int:
        """拉一次成交与订单状态回报，路由到对应 Broker。"""
        if self.gateway is None or not self.gateway.is_connected():
            return 0
        self.stats["polls"] += 1
        n = 0

        try:
            fills: List[FillEvent] = self.gateway.poll_fills() or []
        except Exception as exc:
            self.stats["errors"] += 1
            log_throttled("ERROR", f"拉取成交回报失败: {exc}", key="live-poll-fills")
            fills = []

        for f in fills:
            broker = self._find_broker(f.client_order_id)
            if broker is None:
                self.stats["unknown"] += 1
                log_throttled(
                    "WARNING",
                    f"收到无法归属的成交回报 | 本地单号 {f.client_order_id or '（空）'} "
                    f"券商单号 {f.broker_order_id} —— 已忽略（可能来自手工交易或上一轮进程）",
                    key="live-fill-orphan",
                )
                continue
            try:
                broker.on_fill(f)
                n += 1
                self.stats["fills"] += 1
                self._publish_fill(f, broker)
            except Exception as exc:
                self.stats["errors"] += 1
                logger.error(f"成交回报处理失败: {exc}", exc_info=True)

        try:
            updates: List[GatewayOrder] = self.gateway.poll_order_updates() or []
        except Exception as exc:
            self.stats["errors"] += 1
            updates = []
        for u in updates:
            local_id = u.client_order_id or ""
            broker = self._find_broker(local_id) if local_id else None
            if broker is None:
                continue
            try:
                order = broker.apply_order_update(u)
                if order is not None:
                    n += 1
                    self.stats["updates"] += 1
            except Exception as exc:
                logger.debug(f"订单状态更新处理失败: {exc}")
        return n

    def _publish_fill(self, fill: FillEvent, broker: Any) -> None:
        if self.event_bus is None:
            return
        try:
            self.event_bus.publish(
                StandardEvents.ORDER_FILLED,
                order=None, fill=fill.to_dict(),
                task_id=getattr(broker, "portfolio", None)
                and broker.portfolio.task_id or "",
            )
        except Exception:
            pass

    # ============================================================
    # 对账
    # ============================================================
    def _tick_reconcile(self):
        """**账户级**对账。

        为什么不是逐任务对账：一个券商账户对应 N 个策略任务，
        柜台只会告诉你"账户里现在有 2900 股 000001"，
        不会告诉你是哪个策略买的。所以逐任务拿自己的那一份去和账户总额比，
        除了"只有一个任务"的情况以外**永远对不上** —— 全是噪音，
        真差异反而被淹没。

        正确做法是把所有任务的持仓/资金汇总，和券商账户比一次。
        """
        self.stats["reconciles"] += 1
        if self.gateway is None or not self.gateway.is_connected():
            return {"ok": False, "error": "网关未连接", "targets": []}

        # ---- 本地汇总 ----
        local_cash = 0.0
        local_pos: Dict[str, int] = {}
        local_open: Dict[str, int] = {}
        for tid, broker in self._brokers.items():
            try:
                local_cash += float(broker.portfolio.account.cash)
                pos = broker.portfolio.position
                if pos.symbol:
                    local_pos[pos.symbol] = local_pos.get(pos.symbol, 0) + int(pos.size)
                for o in broker.open_orders:
                    local_open[o.order_id] = int(o.size) - int(o.filled_size)
            except Exception as exc:
                self.stats["errors"] += 1
                logger.debug(f"汇总任务 {tid} 账本失败: {exc}")

        # ---- 券商侧 ----
        try:
            broker_pos = self.gateway.query_positions()
            account = self.gateway.query_account()
            gw_open = {
                (o.client_order_id or o.broker_order_id): o.remaining
                for o in self.gateway.query_orders(only_open=True)
            }
        except Exception as exc:
            self.stats["errors"] += 1
            return {"ok": False, "error": f"查询券商失败: {exc}", "targets": []}

        position_diffs = []
        for sym in sorted(set(local_pos) | set(broker_pos)):
            local = int(local_pos.get(sym, 0))
            snap = broker_pos.get(sym)
            remote = int(snap.size) if snap else 0
            if local != remote:
                position_diffs.append({
                    "symbol": sym, "local": local, "broker": remote,
                    "delta": remote - local,
                })

        open_diffs = []
        only_local = set(local_open) - set(gw_open)
        only_broker = set(gw_open) - set(local_open)
        if only_local:
            open_diffs.append({
                "kind": "本地有券商无（可能已成交/已撤，需回查）",
                "orders": sorted(only_local),
            })
        if only_broker:
            open_diffs.append({
                "kind": "券商有本地无（进程重启丢失？手工下单？）",
                "orders": sorted(only_broker),
            })

        can_check_cash = bool(getattr(self.gateway, "supports_account_reconcile", True))
        cash_diff = None
        if can_check_cash and abs(local_cash - float(account.cash)) > 1.0:
            cash_diff = {
                "local": round(local_cash, 2),
                "broker": round(float(account.cash), 2),
                "delta": round(float(account.cash) - local_cash, 2),
            }

        report: Dict[str, Any] = {
            "ok": not (position_diffs or open_diffs or cash_diff),
            "at": datetime.now().isoformat(),
            "tasks": len(self._brokers),
            "position_diffs": position_diffs,
            "open_order_diffs": open_diffs,
            "cash_diff": cash_diff,
            "cash_checked": can_check_cash,
            "local_cash": round(local_cash, 2),
            "broker_cash": round(float(account.cash), 2),
            "local_positions": local_pos,
        }
        self.last_reconcile = report
        if report["ok"]:
            logger.info(
                f"实盘对账一致 | {len(self._brokers)} 个任务 | "
                f"现金 {report['local_cash']:.2f} | 持仓 {local_pos or '空'}"
            )
        else:
            self.stats["mismatches"] += 1
            logger.error(
                f"实盘对账发现不一致 | 持仓差异 {position_diffs} | "
                f"资金差异 {cash_diff} | 挂单差异 {open_diffs}"
            )
            if self.event_bus is not None:
                try:
                    self.event_bus.publish(
                        StandardEvents.COMPONENT_ERROR,
                        component=self.name,
                        message=f"实盘对账不一致: 持仓{position_diffs} 资金{cash_diff}",
                    )
                except Exception:
                    pass

        if self.strict_reconcile and not report["ok"]:
            # 严格模式下对不上就拒绝继续交易。
            # 账本和券商不一致时继续下单，只会把差异越滚越大。
            raise GatewayError(f"实盘对账不一致，已按严格模式阻断交易: {report}")
        return report

    def reconcile_now(self) -> Dict[str, Any]:
        return self._tick_reconcile()

    # ============================================================
    # 可观测
    # ============================================================
    def snapshot(self) -> Dict[str, Any]:
        return {
            "gateway": getattr(self.gateway, "name", ""),
            "connected": bool(self.gateway and self.gateway.is_connected()),
            "endpoint": self.endpoint_id,
            "endpoint_info": self.endpoint_info,
            "account_id": getattr(self.gateway, "account", ""),
            "readonly": bool(getattr(self.gateway, "readonly", False)),
            # 券商侧的真相（从券商源读，不是本地账本的推算）
            "broker_account": self.account,
            "broker_positions": self.positions,
            "account_at": self.account_at,
            "account_error": self.account_error,
            "poll_interval": self.poll_interval,
            "reconcile_interval": self.reconcile_interval,
            "account_interval": self.account_interval,
            "strict_reconcile": self.strict_reconcile,
            "tasks": len(self._brokers),
            "stats": dict(self.stats),
            "last_reconcile": self.last_reconcile,
        }

    def health_check(self):
        if self.gateway is None:
            return False, "未配置网关"
        if not self.gateway.is_connected():
            return False, "与券商连接已断开"
        if self.stats["errors"] > 0 and self.stats["polls"] and \
                self.stats["errors"] > self.stats["polls"] * 0.5:
            return False, f"回报轮询大量失败（{self.stats['errors']} 次）"
        if self.last_reconcile and not self.last_reconcile.get("ok"):
            return False, "对账存在不一致"
        return True, f"已成交 {self.stats['fills']} 笔"


__all__ = ["LiveGatewayComponent"]
