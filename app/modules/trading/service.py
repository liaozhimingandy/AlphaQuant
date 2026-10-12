#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : trading.py
# @Description : 交易服务 —— 下单路由 + 成交/权益落库，一个服务管完
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

重构说明
========

由原 ``LiveGatewayComponent``（券商网关）与 ``TradingStoreComponent``
（订单/成交/权益落库）**合并**而成。

为什么要合并
------------

它们不是两件事，是同一件事的两端：

    下单 ----> 券商 ----> 回报 ----> 落库
      ^                              |
      +---------- 对账比对 ----------+

拆成两个平级组件时，"这笔单发出去了但没落库"这类问题
没有任何一个组件能独立回答 —— 因为账本在 A、通道在 B。
而且**对账**这件事天然要同时碰两边：拿所有任务的本地账本汇总，
去比券商账户。分家之后只能靠事件总线来回传，链路一长就丢。

合并后"一笔交易从下单到入账"在一个对象里闭环，
对账是它自己的方法，而不是跨组件的协作。

保留的差异
----------

落库单元有自己的开关（``TRADE_PERSIST_ENABLED``），
与网关是否连接无关——只读档位（不下单）也要能落库。
所以服务级 ``enabled`` 与落库级 ``_store_enabled`` 是**两个开关**，
不要混为一谈。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from twisted.internet import defer, task, threads

from app.engine.capability import Cap, RestartPolicy, ServiceComponent
from app.runtime.event_bus import StandardEvents
from app.providers.broker.endpoints import load_endpoints
from app.providers.broker.gateway import (
    FillEvent,
    GatewayError,
    GatewayOrder,
    IBrokerGateway,
    create_gateway,
    has_gateway,
    list_gateways,
)
from app.data.repo.trade_repo import get_repository
from app.domain.types import OrderStatus
from app.common.config import settings
from app.common.jsonio import json_safe
from app.common.logging import log_throttled, logger


class TradingService(ServiceComponent):
    """交易服务。

    **提供的能力**：
      * ``trade.route``    —— 把订单路由到券商（进程内唯一连接）
      * ``trade.account``  —— 查券商资金/持仓（带缓存）
      * ``trade.reconcile``—— 本地账本 ⟷ 券商账户 对账
      * ``trade.persist``  —— 订单/成交/权益/事件落库

    两个内部单元
    ------------
    ``_gateway``  券商通道：连接、报单、回报轮询、撤单、对账
    ``_store``    账本落库：订单状态、成交、权益曲线、重要事件

    共用一个网关连接是硬约束——券商通常不允许同账号多连接。
    所以这里持有**进程内唯一**的网关实例，分发给各任务的 LiveBroker。
    """

    name = "trading_service"
    provides = frozenset({
        Cap.ORDER_ROUTE, Cap.ACCOUNT_QUERY, Cap.RECONCILE, Cap.TRADE_PERSIST,
    })
    restart_policy = RestartPolicy.NEVER

    def __init__(
        self,
        # ---- 网关单元 ----
        gateway: Optional[IBrokerGateway] = None,
        gateway_name: str = "",
        endpoint_id: str = "",
        poll_interval: float = 1.0,
        reconcile_interval: float = 300.0,
        account_interval: float = 30.0,
        strict_reconcile: Optional[bool] = None,
        # ---- 落库单元 ----
        store_enabled: Optional[bool] = None,
        equity_interval: Optional[float] = None,
        run_id: str = "",
        engine: Any = None,
        # ---- 服务级 ----
        enabled: Optional[bool] = None,
        gateway_enabled: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()

        # ---------------- 网关单元 ----------------
        #: 网关单元是否启用。**只有 LIVE 模式才该是 True**。
        #: 模拟/回测下这个服务仍然装配 —— 它还要落库 ——
        #: 但不能去连真券商。为 False 时网关单元整体不初始化、不启动、不体检，
        #: 落库照常工作。
        self.gateway_enabled = True if gateway_enabled is None else bool(gateway_enabled)
        self.gateway = gateway
        self.gateway_name = str(gateway_name or settings.BROKER_GATEWAY)
        self.endpoint_id = str(endpoint_id or settings.BROKER_ENDPOINT or "")
        self.poll_interval = float(poll_interval)
        self.reconcile_interval = float(reconcile_interval)
        self.account_interval = float(account_interval)
        self.strict_reconcile = (
            settings.LIVE_STRICT_RECONCILE if strict_reconcile is None
            else bool(strict_reconcile)
        )
        self._brokers: Dict[str, Any] = {}
        self._poll_loop: Optional[task.LoopingCall] = None
        self._reconcile_loop: Optional[task.LoopingCall] = None
        self._account_loop: Optional[task.LoopingCall] = None
        self._busy = False
        self.account: Dict[str, Any] = {}
        self.positions: Dict[str, Any] = {}
        self.account_at: str = ""
        self.account_error: str = ""
        self.endpoint_info: Dict[str, Any] = {}
        self.stats_gateway: Dict[str, int] = {
            "polls": 0, "fills": 0, "updates": 0, "unknown": 0,
            "reconciles": 0, "mismatches": 0, "errors": 0, "account_refreshes": 0,
            "stale": 0,   # 被回收的超时单（回报丢失兜底）
        }
        self.last_reconcile: Dict[str, Any] = {}
        self._seeded_tasks: set = set()
        self._capital_taken_over = False

        # ---------------- 落库单元 ----------------
        self._store_enabled = (
            settings.TRADE_PERSIST_ENABLED if store_enabled is None
            else bool(store_enabled)
        )
        self.equity_interval = float(
            equity_interval if equity_interval is not None
            else settings.EQUITY_SAMPLE_INTERVAL
        )
        self.bound_run_id = run_id or ""
        self.bound_engine = engine
        self.repo = get_repository()
        self._store_loop: Optional[task.LoopingCall] = None
        self._last_equity_at: Dict[str, float] = {}
        self.stats_store: Dict[str, int] = {
            "orders": 0, "trades": 0, "rejected": 0,
            "equity_points": 0, "events": 0, "errors": 0,
        }

        # ---------------- 服务级 ----------------
        if enabled is not None:
            self.enabled = bool(enabled)

    # ============================================================
    # 开关（只读视图）
    # ============================================================
    @property
    def store_enabled(self) -> bool:
        """落库单元是否启用。

        注意它和**服务级** ``enabled`` 是两件事：只读档位（不下单）
        也要能落库，所以网关关着的时候这里依然可能是 True。
        """
        return bool(self._store_enabled)

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        # 落库先就绪：网关初始化会读一次账户并可能写事件，
        # 落库没订阅上就会漏掉"服务启动"这一条最有价值的记录。
        self._init_store()
        if self.gateway_enabled:
            self._init_gateway()
        else:
            logger.info(
                "交易服务已装配（仅落库）| 券商网关未启用 "
                "（非 LIVE 模式，或网关被显式关闭）"
            )

    @defer.inlineCallbacks
    def on_start(self) -> defer.Deferred:
        yield self._start_store()
        if self.gateway_enabled:
            yield self._start_gateway()

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        # 逆序：先断网关（它会撤掉留在券商的挂单），再收尾落库
        if self.gateway_enabled:
            yield self._stop_gateway()
        yield self._stop_store()

    def is_busy(self) -> bool:
        return self._is_busy_gateway() if self.gateway_enabled else False

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base["gateway_enabled"] = self.gateway_enabled
        if self.gateway_enabled:
            try:
                base["gateway"] = self._snapshot_gateway()
            except Exception as exc:
                base["gateway_error"] = str(exc)
        try:
            base["store"] = self._snapshot_store()
        except Exception as exc:
            base["store_error"] = str(exc)
        return base

    def health_check(self):
        if not self.enabled:
            return True, "disabled"
        if self.state != "RUNNING":
            return False, self.state
        units = [("落库", self._health_check_store)]
        if self.gateway_enabled:
            units.insert(0, ("网关", self._health_check_gateway))
        bad: List[str] = []
        for label, fn in units:
            try:
                ok, detail = fn()
                if not ok:
                    bad.append(f"{label}: {detail}")
            except Exception as exc:
                bad.append(f"{label}: 检查异常 {exc}")
        if bad:
            return False, " | ".join(bad)
        return True, "交易服务正常"


# ============================================================
# 两个单元的方法（由脚本从原组件搬来，方法体未改写）
# ============================================================


# ------------------------------------------------------------------
# 单元：gateway（原 LiveGatewayComponent）
# ------------------------------------------------------------------

    # ============================================================
    # 生命周期
    # ============================================================
    def _init_gateway(self) -> None:
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
    def _start_gateway(self) -> defer.Deferred:
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
    def _stop_gateway(self, graceful: bool = True):
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
                # cancel_all() 返回的是撤掉的单数，不是 True/False。
                # 写成 ``1 for ... if b.cancel_all()`` 数出来的是"有几个 broker
                # 撤成功过"——日志里的数字会严重偏小，排查时以为只撤了 1 单。
                n = sum(
                    (getattr(b, "cancel_all", None) and (b.cancel_all() or 0)) or 0
                    for b in self._brokers.values()
                )
                if n:
                    logger.warning(f"退出前已撤销 {n} 笔未结订单，等待回报收尾")
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
        logger.info(f"实盘网关已关闭 | {self.stats_gateway}")
    def _is_busy_gateway(self) -> bool:
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
        #
        # 两个坑，都踩过：
        #  1) 接入点配置里也有 initial_cash（给"单独用网关"的场景兜底）。
        #     直接往上加会算两遍 —— 本地 28 万、券商 38 万，对账必然不一致，
        #     而且严格模式会直接阻断交易。所以**第一个任务挂上来时清零**，
        #     任务本金才是权威来源。
        #  2) 同一个任务可能被挂两次（装载阶段 + 启动阶段各一次），
        #     本金就会被计两遍。按 task_id 去重，只有第一次计入。
        try:
            if getattr(self.gateway, "name", "") == "simulated":
                tid = str(getattr(task, "task_id", "") or "")
                cash = 0.0 if (tid and tid in self._seeded_tasks) else float(
                    getattr(task.portfolio.account, "initial_cash", 0.0) or 0.0
                )
                if cash:
                    if not self._capital_taken_over:
                        self.gateway._cash = 0.0
                        self._capital_taken_over = True
                    self.gateway._cash = float(
                        getattr(self.gateway, "_cash", 0.0) or 0.0
                    ) + cash
                if tid:
                    self._seeded_tasks.add(tid)
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
            self.stats_gateway["errors"] += 1
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
            self.stats_gateway["errors"] += 1
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
        self.stats_gateway["account_refreshes"] += 1
        return {"account": self.account, "positions": self.positions}
    def _poll_once(self) -> int:
        """拉一次成交与订单状态回报，路由到对应 Broker。"""
        if self.gateway is None or not self.gateway.is_connected():
            return 0
        self.stats_gateway["polls"] += 1
        n = 0

        try:
            fills: List[FillEvent] = self.gateway.poll_fills() or []
        except Exception as exc:
            self.stats_gateway["errors"] += 1
            log_throttled("ERROR", f"拉取成交回报失败: {exc}", key="live-poll-fills")
            fills = []

        for f in fills:
            broker = self._find_broker(f.client_order_id)
            if broker is None:
                self.stats_gateway["unknown"] += 1
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
                self.stats_gateway["fills"] += 1
                self._publish_fill(f, broker)
            except Exception as exc:
                self.stats_gateway["errors"] += 1
                logger.error(f"成交回报处理失败: {exc}", exc_info=True)

        try:
            updates: List[GatewayOrder] = self.gateway.poll_order_updates() or []
        except Exception as exc:
            self.stats_gateway["errors"] += 1
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
                    self.stats_gateway["updates"] += 1
            except Exception as exc:
                logger.debug(f"订单状态更新处理失败: {exc}")

        # 回报收完后顺带回收超时单。
        # 放在这里而不是新起一个 LoopingCall：轮询本来就在定期跑，
        # 多一个定时器就多一处"停不干净"的风险，而回收完全不需要准时。
        n += self._sweep_stale()
        return n

    def _sweep_stale(self) -> int:
        """让每个 Broker 回收自己的超时未结订单。

        没有这道兜底，一次券商回报丢失就会让该任务永久停止下单，
        而日志里一条 ERROR 都没有——这是最难排查的一类故障。
        """
        n = 0
        for broker in list(self._brokers.values()):
            sweep = getattr(broker, "sweep_stale_orders", None)
            if sweep is None:
                continue          # 模拟撮合瞬时成交，不需要回收
            try:
                handled = sweep() or []
            except Exception as exc:
                log_throttled(
                    "ERROR", f"超时单回收异常: {exc}", key="live-sweep-stale")
                continue
            n += len(handled)
            if handled:
                self.stats_gateway["stale"] = \
                    self.stats_gateway.get("stale", 0) + len(handled)
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
        self.stats_gateway["reconciles"] += 1
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
                self.stats_gateway["errors"] += 1
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
            self.stats_gateway["errors"] += 1
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
            self.stats_gateway["mismatches"] += 1
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
    def _snapshot_gateway(self) -> Dict[str, Any]:
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
            "stats": dict(self.stats_gateway),
            "last_reconcile": self.last_reconcile,
        }
    def _health_check_gateway(self):
        if self.gateway is None:
            return False, "未配置网关"
        if not self.gateway.is_connected():
            return False, "与券商连接已断开"
        if self.stats_gateway["errors"] > 0 and self.stats_gateway["polls"] and \
                self.stats_gateway["errors"] > self.stats_gateway["polls"] * 0.5:
            return False, f"回报轮询大量失败（{self.stats_gateway['errors']} 次）"
        if self.last_reconcile and not self.last_reconcile.get("ok"):
            return False, "对账存在不一致"
        return True, f"已成交 {self.stats_gateway['fills']} 笔"


# ------------------------------------------------------------------
# 单元：store（原 TradingStoreComponent）
# ------------------------------------------------------------------

    # ============================================================
    # 生命周期
    # ============================================================
    @property
    def run_id(self) -> str:
        if self.bound_run_id:
            return self.bound_run_id
        return self.context.run_id if self.context else ""
    def _init_store(self) -> None:
        cfg = self.component_config or {}
        if "enabled" in cfg:
            self._store_enabled = bool(cfg["enabled"])
        if cfg.get("equity_interval") is not None:
            self.equity_interval = float(cfg["equity_interval"])

        if not self._store_enabled:
            logger.info("交易数据落库已关闭（TRADE_PERSIST_ENABLED=false）")
            return

        # 建表：用户可能没跑过 init-db 就直接起服务
        self._ensure_store_tables()
        self.repo.start()

        self.subscribe_event(StandardEvents.ORDER_CREATED, self._on_store_order)
        self.subscribe_event(StandardEvents.ORDER_FILLED, self._on_store_order)
        self.subscribe_event(StandardEvents.ORDER_CANCELLED, self._on_store_order)
        self.subscribe_event(StandardEvents.SIGNAL_REJECTED, self._on_store_order)
        self.subscribe_event(StandardEvents.DATA_COLLECTED, self._on_store_collected)
        self.subscribe_event(StandardEvents.COMPONENT_ERROR, self._on_store_error)
        self.subscribe_event(StandardEvents.ENGINE_ERROR, self._on_store_error)

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
    def _ensure_store_tables(self) -> None:
        try:
            from app.data.engine import engine as db_engine
            from app.data.schema import Base

            Base.metadata.create_all(bind=db_engine)
        except Exception as exc:
            logger.warning(f"交易数据表补建失败（忽略，写入时仍会尝试）: {exc}")
    def _start_store(self) -> defer.Deferred:
        if not self._store_enabled:
            return defer.succeed(None)
        self._register_store_tasks()
        self._store_loop = task.LoopingCall(self._sample_store_equity)
        self._store_loop.start(max(1.0, self.equity_interval), now=False)
        return defer.succeed(None)
    def _stop_store(self, graceful: bool = True) -> defer.Deferred:
        if self._store_loop is not None and self._store_loop.running:
            self._store_loop.stop()
        self._store_loop = None
        if not self._store_enabled:
            return defer.succeed(None)

        # 收尾：先补一次权益，再更新运行状态，最后把队列刷干净。
        # 顺序很重要 —— 队列是异步的，先把该写的都排进去再 stop。
        try:
            self._sample_store_equity()
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
    # ============================================================
    # 任务登记
    # ============================================================
    def _register_store_tasks(self) -> None:
        runtime = self._store_runtime()
        if runtime is None:
            return
        try:
            for t in list(runtime):
                self._record_store_task(t)
        except Exception as exc:
            logger.debug(f"批量登记任务失败: {exc}")
    def _store_runtime(self):
        engine = self.bound_engine
        if engine is None:
            return None
        sm = engine.get_component("strategy")
        return getattr(sm, "runtime", None) if sm is not None else None
    def _record_store_task(self, task: Any) -> None:
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
            self.stats_store["errors"] += 1
            logger.debug(f"任务登记失败: {exc}")
    def register_task(self, task: Any) -> None:
        """运行时新增任务时由控制面调用。"""
        if self._store_enabled:
            self._record_store_task(task)
    # ============================================================
    # 事件 → 落库
    # ============================================================
    def _on_store_order(self, order: Any = None, task_id: str = "", reason: str = "",
                  **kwargs: Any) -> None:
        if not self._store_enabled or order is None:
            return
        try:
            self.repo.record_order(self.run_id, order)
            self.stats_store["orders"] += 1
        except Exception as exc:
            self.stats_store["errors"] += 1
            logger.debug(f"订单落库失败: {exc}")

        status = getattr(order, "status", None)
        if status == OrderStatus.FILLED:
            self._record_store_trade(order)
        elif status == OrderStatus.REJECTED:
            self.stats_store["rejected"] += 1
            self.repo.record_event(
                self.run_id,
                message=f"订单被拒 | {getattr(order, 'symbol', '')} "
                        f"{getattr(order, 'side', '')} | {reason or getattr(order, 'reject_reason', '')}",
                level="WARNING", category="risk",
                task_id=str(getattr(order, "task_id", "") or task_id),
                symbol=str(getattr(order, "symbol", "")),
                detail={"order_id": getattr(order, "order_id", "")},
            )
            self.stats_store["events"] += 1
    def _record_store_trade(self, order: Any) -> None:
        """成交落库。用订单里的 task_id 去账本取最后一笔成交记录。"""
        if not self._store_enabled:
            return
        try:
            tid = str(getattr(order, "task_id", ""))
            task = self._find_store_task(tid)
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
            self.stats_store["trades"] += 1
        except Exception as exc:
            self.stats_store["errors"] += 1
            logger.debug(f"成交落库失败: {exc}")
    def _find_store_task(self, task_id: str):
        runtime = self._store_runtime()
        if runtime is None:
            return None
        try:
            return runtime.get(task_id)
        except Exception:
            return None
    def _on_store_collected(self, symbol: str = "", period: str = "", rows: int = 0,
                      **kwargs: Any) -> None:
        if not self._store_enabled:
            return
        self.repo.record_event(
            self.run_id,
            message=f"行情采集 {symbol} {period} 入库 {rows} 条",
            level="INFO", category="collect", symbol=str(symbol),
            detail={"period": period, "rows": int(rows or 0)},
        )
        self.stats_store["events"] += 1
    def _on_store_error(self, **kwargs: Any) -> None:
        if not self._store_enabled:
            return
        msg = str(kwargs.get("message") or kwargs.get("error") or kwargs.get("component") or "")
        component = str(kwargs.get("component") or kwargs.get("name") or "")
        self.repo.record_event(
            self.run_id,
            message=f"组件异常 | {component} | {msg}"[:800],
            level="ERROR", category="error",
            detail={k: str(v)[:200] for k, v in kwargs.items()},
        )
        self.stats_store["events"] += 1
    # ============================================================
    # 权益采样
    # ============================================================
    def _sample_store_equity(self) -> None:
        """按间隔给每个任务采一个权益点。

        **不逐笔存**：曲线看的是趋势，逐笔既没人看也把表撑爆。
        """
        if not self._store_enabled:
            return
        sm = self.bound_engine.get_component("strategy") if self.bound_engine else None
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
                self.stats_store["equity_points"] += 1
            except Exception as exc:
                self.stats_store["errors"] += 1
                logger.debug(f"权益采样失败: {exc}")
    # ============================================================
    # 可观测
    # ============================================================
    def _snapshot_store(self) -> Dict[str, Any]:
        return {
            "enabled": self._store_enabled,
            "run_id": self.run_id,
            "equity_interval": self.equity_interval,
            "stats": dict(self.stats_store),
            "repo": self.repo.health(),
        }
    def _health_check_store(self):
        if not self._store_enabled:
            return True, "disabled"
        h = self.repo.health()
        if h["errors"] and not h["written"]:
            return False, f"落库持续失败: {h['last_error'][:80]}"
        if h["pending"] > 1000:
            return True, f"积压 {h['pending']} 条（磁盘可能较慢）"
        return True, f"已写 {h['written']} 条"


__all__ = ["TradingService"]
