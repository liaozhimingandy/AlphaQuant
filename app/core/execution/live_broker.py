#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : live_broker.py
# @Description : 实盘撮合：下单只是"提交意图"，成交由券商回报驱动
#
#                与 SimulatedBroker 的根本差别（这是"能不能上实盘"的分水岭）：
#
#                  SimulatedBroker   submit() 返回时订单已经是 FILLED
#                  LiveBroker        submit() 返回时订单只是 SUBMITTED，
#                                    成交在**之后的某个时刻**以回报形式到达
#
#                这个差别会连锁影响所有下游：
#                  - 持仓/资金不能在 submit 之后立刻更新，只能等回报
#                  - "持股超时"之类的计数器不能在 submit 时清零
#                  - 必须能处理"挂在半路"的订单（重启后要对账）
#                  - 必须能撤单，而且撤单本身也是异步的
#
#                所以 LiveBroker 的职责是：**维护订单的真实生命周期**，
#                并把每一笔成交准确落到账本上。它不做风控、不做仓位计算。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.core.execution.gateway import (
    FillEvent,
    GatewayError,
    GatewayOrder,
    IBrokerGateway,
    OrderRejected,
    OrderRequest,
    PositionSnapshot,
)
from app.core.market.types import Order, OrderStatus, Side
from app.core.portfolio.manager import Portfolio
from app.utils.logger import log_throttled, logger


class LiveBroker:
    """真实券商撮合。

    实现 :class:`IBaseBroker` 的同名接口（submit / cancel / open_orders），
    所以 QuantTask 不需要区分模拟还是实盘 —— 换撮合只换注入的对象。
    """

    def __init__(
        self,
        portfolio: Portfolio,
        gateway: IBrokerGateway,
        fee_rate: float = 0.0003,
        max_order_value: float = 0.0,
        price_limit_pct: float = 0.02,
        order_timeout: float = 30.0,
    ) -> None:
        self.portfolio = portfolio
        self.gateway = gateway
        self.fee_rate = float(fee_rate)
        self.max_order_value = float(max_order_value)
        self.price_limit_pct = float(price_limit_pct)
        self.order_timeout = float(order_timeout)

        self.orders: Dict[str, Order] = {}
        #: 本地订单号 → 券商订单号
        self._broker_ids: Dict[str, str] = {}
        #: 券商订单号 → 本地订单号（回报回来时反查）
        self._local_ids: Dict[str, str] = {}
        self._seen_fills: set = set()
        self._lock = threading.RLock()

        self._last_price: float = 0.0
        #: 订单发生任何变化时回调（QuantTask 用它重置计数、落库组件用它记录）
        self.on_order_update: Optional[Callable[[Order], None]] = None

        self.stats: Dict[str, int] = {
            "submitted": 0, "filled": 0, "partial": 0,
            "cancelled": 0, "rejected": 0, "errors": 0,
        }

    # ============================================================
    # 行情
    # ============================================================
    def set_price(self, price: float) -> None:
        self._last_price = float(price)
        self.portfolio.update_price(price)

    @property
    def last_price(self) -> float:
        return self._last_price

    @property
    def open_orders(self) -> List[Order]:
        """未结订单（已提交未终态）。实盘必须有这个能力 —— 模拟撮合可以没有。"""
        with self._lock:
            return [o for o in self.orders.values()
                    if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED,
                                    OrderStatus.PARTIAL)]

    def new_order_id(self) -> str:
        return f"L{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"

    # ============================================================
    # 下单
    # ============================================================
    def create_order(
        self, symbol: str, side: Side, size: int, task_id: str = "",
        reason: str = "", source: str = "",
    ) -> Order:
        return Order(
            order_id=self.new_order_id(), task_id=task_id, symbol=symbol,
            side=side, size=int(size), price=self._last_price,
            status=OrderStatus.PENDING, reason=reason,
            created_at=datetime.now(), meta={"source": source, "broker": "live"},
        )

    def submit(self, order: Order) -> Order:
        """提交订单。

        **返回时订单通常还是 SUBMITTED**，不要假设已成交。
        仓位与资金的更新发生在 :meth:`on_fill` 里。
        """
        with self._lock:
            self.orders[order.order_id] = order

        if order.size <= 0:
            return self._reject(order, "下单数量为 0")

        # ---- 本地前置校验：能在本地拦掉的就别去打扰券商 ----
        # 券商拒单会占用报单额度，有些柜台还会记违规；本地拦掉更干净。
        price = float(order.price or self._last_price or 0.0)
        if price <= 0:
            return self._reject(order, "无有效价格")

        if self.price_limit_pct > 0 and self._last_price > 0:
            dev = abs(price - self._last_price) / self._last_price
            if dev > self.price_limit_pct:
                return self._reject(
                    order,
                    f"价格偏离现价 {dev:.2%}，超过笼子 {self.price_limit_pct:.2%}",
                )
        if self.max_order_value > 0 and price * order.size > self.max_order_value:
            return self._reject(
                order,
                f"单笔金额 {price * order.size:.0f} 超过上限 {self.max_order_value:.0f}",
            )

        req = OrderRequest(
            symbol=order.symbol, side=order.side, size=order.size, price=price,
            order_type="LIMIT" if order.price else "MARKET",
            client_order_id=order.order_id, task_id=order.task_id, reason=order.reason,
        )
        try:
            broker_id = self.gateway.place_order(req)
        except OrderRejected as exc:
            return self._reject(order, f"券商拒绝: {exc}")
        except GatewayError as exc:
            # 通信失败 ≠ 被拒。状态留在 SUBMITTED 让人工/对账去确认，
            # **绝不能**当成"没下出去"而重发 —— 那可能造成重复下单。
            self.stats["errors"] += 1
            order.status = OrderStatus.SUBMITTED
            order.reject_reason = f"网关通信失败（状态未知，需对账）: {exc}"
            logger.error(f"[{order.task_id}] 下单通信失败，订单状态未知: {exc}")
            self._notify(order)
            return order

        with self._lock:
            order.status = OrderStatus.SUBMITTED
            self._broker_ids[order.order_id] = broker_id
            self._local_ids[broker_id] = order.order_id
        self.stats["submitted"] += 1
        logger.info(
            f"[{order.task_id}] 已报单 | {order.symbol} {order.side.value} "
            f"{order.size}@{price:.3f} | 券商单号 {broker_id}"
        )
        self._notify(order)
        return order

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.stats["rejected"] += 1
        from app.utils.logger import log_throttled

        log_throttled("WARNING", f"[{order.task_id}] 订单被拒: {reason}",
                      key=f"live-reject-{order.task_id}")
        self._notify(order)
        return order

    def cancel(self, order_id: str) -> bool:
        """撤单。撤单本身也是异步的 —— 返回 True 只代表"已请求撤单"。"""
        with self._lock:
            order = self.orders.get(order_id)
            broker_id = self._broker_ids.get(order_id, "")
        if order is None or order.status not in (
            OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL
        ):
            return False
        if not broker_id:
            # 还没真正报出去，本地撤掉
            order.status = OrderStatus.CANCELLED
            self.stats["cancelled"] += 1
            self._notify(order)
            return True
        try:
            ok = self.gateway.cancel_order(broker_id)
        except Exception as exc:
            logger.error(f"撤单失败 {order_id}: {exc}")
            return False
        if ok:
            logger.info(f"[{order.task_id}] 已请求撤单 | {order_id} (券商 {broker_id})")
        return ok

    def cancel_all(self) -> int:
        """撤掉全部未结订单。引擎停止/风控熔断时调用。"""
        return sum(1 for o in list(self.open_orders) if self.cancel(o.order_id))

    # ============================================================
    # 成交回报 → 账本
    # ============================================================
    def on_fill(self, fill: FillEvent) -> Optional[Order]:
        """处理一笔成交回报。这是持仓与资金更新的**唯一入口**。"""
        with self._lock:
            local_id = fill.client_order_id or self._local_ids.get(fill.broker_order_id, "")
            order = self.orders.get(local_id)
            if order is None:
                logger.warning(
                    f"收到未知订单的成交回报 | 券商单号 {fill.broker_order_id} "
                    f"| 本地单号 {fill.client_order_id or '（空）'} —— 已忽略"
                )
                return None
            # 幂等：券商回报可能重复推送
            fkey = fill.fill_id or f"{fill.broker_order_id}:{fill.filled_size}:{fill.price}"
            if fkey in self._seen_fills:
                return None
            self._seen_fills.add(fkey)
            if len(self._seen_fills) > 20000:
                self._seen_fills = set(list(self._seen_fills)[-5000:])

        size = int(fill.size or 0)
        if size <= 0:
            return order

        # 卖出不能超过持仓：账本层的最后一道闸。
        # 实盘上"券商说成交了但账本没有这些股"通常意味着：
        # 手工交易过、别的策略在同一账户交易过、或对账没做。
        # 这里截断并告警，而不是让账本变成负数。
        if order.side == Side.SELL:
            held = int(self.portfolio.position.size)
            if size > held:
                logger.warning(
                    f"[{order.task_id}] 成交回报卖出 {size} 股但账本只有 {held} 股，"
                    f"按 {held} 股入账 —— 请检查是否有人工交易或对账缺失"
                )
                size = max(0, held)
        if size <= 0:
            return order

        try:
            rec = self.portfolio.apply_fill(
                side=order.side, size=size, price=float(fill.price),
                dt=fill.dt or datetime.now(), reason=order.reason,
                source=str(order.meta.get("source", "") or "live"),
            )
        except Exception as exc:
            self.stats["errors"] += 1
            logger.error(f"[{order.task_id}] 成交入账失败: {exc}", exc_info=True)
            return order

        # 账本**拒绝**了这笔成交（价格/数量非法、超卖被截断到 0）。
        # 此时绝不能把订单标记成成交 —— 否则订单显示 FILLED、持仓却是空的，
        # 上层会以为已经建仓，后续所有决策都建立在错误前提上。
        # 正确做法是保留订单的未结状态并告警，让它去对账兜底。
        applied = int(getattr(rec, "size", 0) or 0)
        if applied <= 0:
            self.stats["errors"] += 1
            log_throttled(
                "ERROR",
                f"[{order.task_id}] 成交回报未能入账（回报 {size} 股 @{fill.price}）"
                f"| 订单保持 {order.status.value}，请核对该笔回报与账本状态",
                key=f"live-fill-not-applied-{order.task_id}",
            )
            return order
        if applied < size:
            logger.warning(
                f"[{order.task_id}] 成交 {size} 股中仅 {applied} 股入账（其余被账本截断）"
            )

        with self._lock:
            order.filled_size = int(fill.filled_size or (order.filled_size + applied))
            # 用累计成交额重算均价，避免多笔成交时均值漂移
            prev_amt = order.filled_price * (order.filled_size - applied)
            order.filled_price = (prev_amt + fill.price * applied) / max(1, order.filled_size)
            if order.filled_size >= order.size:
                order.status = OrderStatus.FILLED
                self.stats["filled"] += 1
            else:
                order.status = OrderStatus.PARTIAL
                self.stats["partial"] += 1

        logger.info(
            f"[{order.task_id}] 成交回报 | {order.symbol} {order.side.value} {applied}"
            f"@{fill.price:.3f} | 累计 {order.filled_size}/{order.size} | "
            f"现金 {self.portfolio.account.cash:.2f} 持仓 {self.portfolio.position.size}"
        )
        self._notify(order)
        return order

    def apply_order_update(self, gw: GatewayOrder) -> Optional[Order]:
        """非成交类的订单状态变化（撤单成功、被拒）。"""
        with self._lock:
            local_id = gw.client_order_id or self._local_ids.get(gw.broker_order_id, "")
            order = self.orders.get(local_id)
        if order is None or order.status == gw.status:
            return None
        order.status = gw.status
        if gw.status == OrderStatus.REJECTED:
            order.reject_reason = gw.message or "券商拒绝"
            self.stats["rejected"] += 1
        elif gw.status == OrderStatus.CANCELLED:
            self.stats["cancelled"] += 1
        self._notify(order)
        return order

    def _notify(self, order: Order) -> None:
        if self.on_order_update is None:
            return
        try:
            self.on_order_update(order)
        except Exception as exc:
            logger.debug(f"订单回调异常: {exc}")

    # ============================================================
    # 对账
    # ============================================================
    def reconcile(self, strict: bool = False) -> Dict[str, Any]:
        """与券商对账：查持仓/资金/挂单，报告差异。

        实盘必须定期做这件事。原因很实在：网络抖动、进程重启、
        以及最要命的"有人在同一账户手工下单"——
        这些都不会通过回报告诉你，只有对账能发现。
        """
        report: Dict[str, Any] = {
            "ok": True, "at": datetime.now().isoformat(),
            "position_diffs": [], "open_order_diffs": [],
            "cash_local": 0.0, "cash_broker": 0.0, "errors": [],
        }
        try:
            positions = self.gateway.query_positions()
            account = self.gateway.query_account()
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(f"查询失败: {exc}")
            return report

        # --- 持仓 ---
        local_sym = self.portfolio.position
        broker_pos: Optional[PositionSnapshot] = positions.get(local_sym.symbol)
        broker_size = int(broker_pos.size) if broker_pos else 0
        if broker_size != int(local_sym.size):
            report["position_diffs"].append({
                "symbol": local_sym.symbol,
                "local": int(local_sym.size),
                "broker": broker_size,
                "delta": broker_size - int(local_sym.size),
            })

        # --- 挂单 ---
        try:
            gw_open = {o.client_order_id or o.broker_order_id
                       for o in self.gateway.query_orders(only_open=True)}
        except Exception:
            gw_open = set()
        local_open = {o.order_id for o in self.open_orders}
        only_local = local_open - gw_open
        only_broker = gw_open - local_open
        if only_local or only_broker:
            report["open_order_diffs"] = [
                {"kind": "本地有券商无（可能已成交/已撤，需回查）", "orders": sorted(only_local)},
                {"kind": "券商有本地无（进程重启丢失？手工下单？）", "orders": sorted(only_broker)},
            ]

        report["cash_local"] = round(float(self.portfolio.account.cash), 2)
        report["cash_broker"] = round(float(account.cash), 2)
        # 资金对账只在网关能给出可信账户时才算数。
        # 模拟网关返回 0，硬比会让对账永远不一致，把真实差异埋掉。
        can_check_cash = bool(getattr(self.gateway, "supports_account_reconcile", True))
        report["cash_checked"] = can_check_cash
        mismatch = (
            report["position_diffs"]
            or any(v for v in report["open_order_diffs"])
            or (can_check_cash and abs(report["cash_local"] - report["cash_broker"]) > 1.0)
        )
        report["ok"] = not mismatch
        if mismatch:
            logger.error(f"实盘对账不一致: {report}")
        else:
            logger.info(f"实盘对账一致 | 现金 {report['cash_local']}")
        if strict and not report["ok"]:
            raise GatewayError(f"实盘对账不一致，拒绝继续交易: {report}")
        return report

    # ============================================================
    # 可观测
    # ============================================================
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "broker": "live",
                "gateway": getattr(self.gateway, "name", "?"),
                "connected": self.gateway.is_connected(),
                "open_orders": len(self.open_orders),
                "stats": dict(self.stats),
                "last_price": self._last_price,
            }


__all__ = ["LiveBroker"]
