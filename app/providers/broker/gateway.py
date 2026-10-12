#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : gateway.py
# @Description : 券商网关抽象：把"和券商通信"这件事与策略/撮合完全隔离
#
#                为什么必须有一层网关：
#                  每家券商的 API 形状都不一样（CTP / 恒生 UFX / 各家 QMT /
#                  QMT 之外的柜台 / 甚至同花顺的模拟托盘）。如果让撮合层直接
#                  调某家 API，接第二家的时候就要动撮合逻辑 —— 而撮合逻辑
#                  正是最不能乱动的地方（它决定账本对不对）。
#
#                网关只做三件事，而且**只做这三件**：
#                  1. 下单 / 撤单（把意图交给券商）
#                  2. 查（订单、持仓、资金）—— 用于对账与重启恢复
#                  3. 拉回报（成交回报、订单状态变化）
#
#                网关**不负责**：算仓位、判风控、记账本。那些是策略与账本的活。
#                这样换券商时，需要新写的只是一个网关类。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import abc
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.domain.types import OrderStatus, Side
from app.common.logging import logger


# ===========================================================================
# 交换结构（网关与上层的契约）
# ===========================================================================
@dataclass
class OrderRequest:
    """下单意图。字段刻意保持最小：券商侧差异由网关自己适配。"""

    symbol: str
    side: Side
    size: int
    price: float = 0.0                  # 0 = 市价
    order_type: str = "LIMIT"           # LIMIT / MARKET
    client_order_id: str = ""           # 本地订单号，用于回报关联
    task_id: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "side": self.side.value, "size": int(self.size),
            "price": float(self.price), "order_type": self.order_type,
            "client_order_id": self.client_order_id, "task_id": self.task_id,
            "reason": self.reason,
        }


@dataclass
class GatewayOrder:
    """券商侧的订单状态。"""

    broker_order_id: str = ""
    client_order_id: str = ""
    symbol: str = ""
    side: Side = Side.BUY
    size: int = 0
    #: 委托价。必须存下来 —— 成交回报里若没有价格（市价单常见），
    #: 得用委托价兜底；只依赖 ``avg_price`` 会在首次成交前拿到 0，
    #: 而 0 价的成交会让账本拒绝入账（进而整笔成交被静默丢掉）。
    price: float = 0.0
    filled_size: int = 0
    avg_price: float = 0.0
    status: OrderStatus = OrderStatus.SUBMITTED
    message: str = ""
    updated_at: Optional[datetime] = None

    @property
    def remaining(self) -> int:
        return max(0, int(self.size) - int(self.filled_size))

    @property
    def is_final(self) -> bool:
        return self.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED
        )


@dataclass
class FillEvent:
    """一笔成交回报。

    注意 ``size`` 是**本次**成交量，不是累计 —— 累计由上层根据
    ``filled_size`` 自己推。混用这两个口径是成交回报处理最常见的错。
    """

    fill_id: str = ""
    broker_order_id: str = ""
    client_order_id: str = ""
    symbol: str = ""
    side: Side = Side.BUY
    size: int = 0
    price: float = 0.0
    fee: float = 0.0
    filled_size: int = 0          # 该订单的累计成交量（含本笔）
    dt: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fill_id": self.fill_id, "broker_order_id": self.broker_order_id,
            "client_order_id": self.client_order_id, "symbol": self.symbol,
            "side": self.side.value, "size": int(self.size),
            "price": float(self.price), "fee": float(self.fee),
            "filled_size": int(self.filled_size),
            "dt": self.dt.isoformat() if self.dt else None,
        }


@dataclass
class PositionSnapshot:
    symbol: str
    size: int = 0
    avg_price: float = 0.0
    #: T+1：当日买入不可卖出的数量。A 股最容易被忽略的规则之一
    frozen_size: int = 0

    @property
    def sellable(self) -> int:
        return max(0, int(self.size) - int(self.frozen_size))


@dataclass
class InstrumentInfo:
    """标的信息（交易所口径）。

    ``limit_up`` / ``limit_down`` 必须是**交易所给的**，不能自己估算：
    ST 股 5%、主板 10%、创业板/科创板 20%、新股上市首日不设限。
    用固定 10% 估算会在 ST 股上把"已涨停"判成"还能买"，
    下单直接被柜台拒绝（还白白暴露了策略意图）。
    """

    symbol: str = ""
    name: str = ""
    limit_up: float = 0.0
    limit_down: float = 0.0
    tick_size: float = 0.01
    lot_size: int = 100
    is_suspended: bool = False
    prev_close: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name,
            "limit_up": self.limit_up, "limit_down": self.limit_down,
            "tick_size": self.tick_size, "lot_size": self.lot_size,
            "is_suspended": self.is_suspended, "prev_close": self.prev_close,
        }


@dataclass
class AccountSnapshot:
    cash: float = 0.0
    available: float = 0.0
    frozen: float = 0.0
    market_value: float = 0.0
    total_asset: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cash": self.cash, "available": self.available, "frozen": self.frozen,
            "market_value": self.market_value, "total_asset": self.total_asset,
        }


class GatewayError(RuntimeError):
    """网关通信失败。与"被券商拒绝"区分开：前者可重试，后者不该重试。"""


class OrderRejected(RuntimeError):
    """券商明确拒绝（资金不足 / 价格超出笼子 / 标的不存在）。重试没有意义。"""


# ===========================================================================
# 网关接口
# ===========================================================================
class IBrokerGateway(abc.ABC):
    """券商网关。

    实现者只需要关心"怎么和这家券商说话"，不需要理解订单生命周期、
    T+1、账本 —— 那些在 LiveBroker 与 Portfolio 里。

    构造函数约定：接受 ``account``（资金账号）、``readonly``（只读档位）、
    ``config``（接入点透传的参数 dict）、``endpoint_id``，以及凭据字段。
    ``create_gateway`` 会按签名过滤，实现者**不需要**声明自己不用的参数。
    """

    #: 注册表里的唯一名，用于 BROKER_GATEWAY / 接入点的 gateway 字段
    name: str = ""

    #: 资金账号（子类在 __init__ 里赋值）。面板/对账会显示它，
    #: 用来一眼确认"现在连的是哪个账户"。
    account: str = ""
    #: 只读档位：可以查询与收回报，但不允许下单。首次接入的验证档位。
    readonly: bool = False

    def describe(self) -> Dict[str, Any]:
        """给面板/CLI 的接入信息（不含凭据）。"""
        return {
            "gateway": getattr(self, "name", ""),
            "account": getattr(self, "account", ""),
            "readonly": bool(getattr(self, "readonly", False)),
        }

    # ---------------- 连接 ----------------
    @abc.abstractmethod
    def connect(self) -> None:
        """建立连接并完成登录。失败应抛 :class:`GatewayError`。"""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """断开连接。必须幂等（可能被调用多次）。"""

    @abc.abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError

    # ---------------- 交易 ----------------
    @abc.abstractmethod
    def place_order(self, req: OrderRequest) -> str:
        """下单。返回券商订单号。

        :raises GatewayError: 通信失败（可重试）
        :raises OrderRejected: 券商业务拒绝（不可重试）
        """

    @abc.abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """撤单。返回是否受理。"""

    # ---------------- 查询（对账与重启恢复用）----------------
    @abc.abstractmethod
    def query_orders(self, only_open: bool = True) -> List[GatewayOrder]:
        ...

    @abc.abstractmethod
    def query_positions(self) -> Dict[str, PositionSnapshot]:
        ...

    @abc.abstractmethod
    def query_account(self) -> AccountSnapshot:
        ...

    # ---------------- 可选能力（有默认实现，不强制每家都实现）----------------
    def query_trades(self, since: Any = None) -> List[FillEvent]:
        """当日（或自 since 起）的成交明细。

        与 ``poll_fills`` 的区别：这是**查**而不是**收** ——
        重启恢复、对账补漏都靠它。轮询式网关丢了回报就再也拿不回来，
        所以"能不能查历史成交"直接决定了这套接口能不能上实盘。
        不支持就返回空列表（上层会自动退回只靠 poll_fills）。
        """
        return []

    def query_instrument(self, symbol: str) -> Optional["InstrumentInfo"]:
        """标的信息：名称、涨跌停价、最小价差、是否停牌。

        价格笼子与涨跌停校验需要**交易所的**涨跌停价，
        用"昨日收盘 × 1.1"估算会在 ST 股（5%）、新股、科创板（20%）上算错。
        拿不到就返回 None，上层退回估算值并记一条警告。
        """
        return None

    def ensure_connected(self) -> bool:
        """确保连接可用；断开就重连一次。

        券商连接会**静默断开**（网络抖动、柜台踢会话、隔夜重连）。
        不检查的话，下单会一直抛"未连接"而没人知道该重连 ——
        实盘上这表现为"策略在正常跑，但一笔单都发不出去"。
        默认实现：没连上就连一次，返回最终状态。
        """
        if not self.is_connected():
            try:
                self.connect()
            except Exception as exc:
                logger.warning(f"网关重连失败: {exc}")
                return False
        return self.is_connected()

    def capabilities(self) -> Dict[str, bool]:
        """能力声明。面板据此显示"这家券商能做什么"，避免猜。"""
        return {
            "market_order": True,
            "cancel": True,
            "query_trades": False,
            "query_instrument": False,
            "reconnect": False,
        }

    def health(self) -> Dict[str, Any]:
        """连通性自检（给面板/CLI 展示）。默认只报连接状态。"""
        ok = self.is_connected()
        return {"connected": ok, "detail": "" if ok else "未连接"}

    # ---------------- 回报 ----------------
    def poll_fills(self) -> List[FillEvent]:
        """拉取自上次调用以来的成交回报。

        轮询式（而不是回调式）是刻意的：回调会在券商 SDK 自己的线程里触发，
        很容易在没准备好的时候碰账本；轮询把时机交回引擎，行为可预测。
        """
        return []

    def poll_order_updates(self) -> List[GatewayOrder]:
        """拉取订单状态变化（非成交类，如撤单成功、部分撤单）。"""
        return []


# ===========================================================================
# 内置网关：本地模拟
# ===========================================================================
class SimulatedGateway(IBrokerGateway):
    """进程内模拟网关。

    用途不是"替代回测"（回测不需要网关），而是：
      - **让实盘链路在没接券商时就能完整跑通**（下单→回报→账本→对账）
      - 作为新网关实现的活参照：看它就知道每个方法该返回什么
      - 冒烟测试的靶子

    它按"下一步查询时成交"的节奏给回报，模拟真实异步性 ——
    如果实现里假设了下单即成交，用这个网关立刻就会暴露。
    """

    name = "simulated"

    def __init__(
        self,
        price_source: Optional[Any] = None,
        slippage: float = 0.0005,
        fee_rate: float = 0.0003,
        fill_delay_calls: int = 1,
        initial_cash: float = 0.0,
        account: str = "",
        readonly: bool = False,
        config: Optional[Dict[str, Any]] = None,
        endpoint_id: str = "",
        **kwargs: Any,
    ) -> None:
        self._connected = False
        # 可重入锁：poll_fills 持锁期间要调 _apply_to_book，而后者也需要锁。
        # 用普通 Lock 会当场死锁（自己等自己），而且是那种"看起来只是卡住"的 bug。
        self._lock = threading.RLock()
        #: 取价函数：() -> Dict[symbol, price]，不传则用订单自己的价格
        self.price_source = price_source
        self.slippage = float(slippage)
        self.fee_rate = float(fee_rate)
        #: 下单后过几次 poll 才成交，用来模拟"不是立刻成交"
        self.fill_delay_calls = max(0, int(fill_delay_calls))
        # 接入点信息：模拟网关也照收，这样"换接入点"在两条路径上行为一致
        self.account = str(account or "") or "SIMULATED"
        self.readonly = bool(readonly)
        self.config: Dict[str, Any] = dict(config or {})
        self.endpoint_id = str(endpoint_id or "simulated")
        self._orders: Dict[str, GatewayOrder] = {}
        self._fills: List[FillEvent] = []
        self._pending: Dict[str, int] = {}     # broker_order_id -> 剩余轮次
        self._updates: List[GatewayOrder] = []  # 待回流的非成交类状态变化
        #: 网关自己的持仓与资金账本（模拟券商侧），对账时用作参照
        self._positions: Dict[str, PositionSnapshot] = {}
        self._cash: float = float(initial_cash)
        self._seq = 0
        self.stats = {
            "placed": 0, "rejected": 0, "filled": 0, "cancelled": 0,
            "blocked_by_readonly": 0,
        }

    # ---------------- 连接 ----------------
    def connect(self) -> None:
        self._connected = True
        logger.info(
            f"[模拟网关] 已连接 | 接入点={self.endpoint_id} | 账号={self.account}"
            + ("（只读）" if self.readonly else "")
        )

    def disconnect(self) -> None:
        self._connected = False
        logger.info("[模拟网关] 已断开")

    def is_connected(self) -> bool:
        return self._connected

    def describe(self) -> Dict[str, Any]:
        return {
            "gateway": self.name,
            "endpoint_id": self.endpoint_id,
            "account": self.account,
            "readonly": self.readonly,
        }

    # ---------------- 交易 ----------------
    def place_order(self, req: OrderRequest) -> str:
        if not self._connected:
            raise GatewayError("模拟网关未连接")
        if self.readonly:
            # 只读档位：查询、收回报都正常，但不允许把单子送出去。
            # 这是首次接券商时最该先跑的档位 —— 能验证账本与回报链路，
            # 又不会因为策略有 bug 而在真实账户上产生交易。
            self.stats["blocked_by_readonly"] += 1
            raise OrderRejected("接入点为只读档位（readonly=true），已阻止下单")
        if req.size <= 0:
            self.stats["rejected"] += 1
            raise OrderRejected("下单数量必须为正")
        price = self._resolve_price(req)
        if price <= 0:
            self.stats["rejected"] += 1
            raise OrderRejected("无有效价格")

        with self._lock:
            self._seq += 1
            bid = f"SIM{self._seq:08d}"
            self._orders[bid] = GatewayOrder(
                broker_order_id=bid, client_order_id=req.client_order_id,
                symbol=req.symbol, side=req.side, size=int(req.size),
                price=float(price),
                status=OrderStatus.SUBMITTED, updated_at=datetime.now(),
            )
            self._pending[bid] = self.fill_delay_calls
        self.stats["placed"] += 1
        return bid

    def cancel_order(self, broker_order_id: str) -> bool:
        with self._lock:
            o = self._orders.get(broker_order_id)
            if o is None or o.is_final:
                return False
            o.status = OrderStatus.CANCELLED
            o.updated_at = datetime.now()
            self._pending.pop(broker_order_id, None)
            # 撤单也是**异步**的：这里只代表券商受理了。
            # 真正的"已撤销"要通过 poll_order_updates 回流给上层 ——
            # 如果这里直接改上层的订单状态，就掩盖了"撤单可能失败"这个事实。
            self._updates.append(o)
        self.stats["cancelled"] += 1
        return True

    def poll_order_updates(self) -> List[GatewayOrder]:
        with self._lock:
            out = list(self._updates)
            self._updates.clear()
        return out

    def _resolve_price(self, req: OrderRequest) -> float:
        if self.price_source is not None:
            try:
                table = self.price_source() or {}
                p = float(table.get(req.symbol) or 0.0)
                if p > 0:
                    return p
            except Exception as exc:  # pragma: no cover - 取价失败退回订单价
                logger.debug(f"[模拟网关] 取价失败: {exc}")
        return float(req.price or 0.0)

    # ---------------- 查询 ----------------
    def query_orders(self, only_open: bool = True) -> List[GatewayOrder]:
        with self._lock:
            items = list(self._orders.values())
        return [o for o in items if not o.is_final] if only_open else items

    def query_positions(self) -> Dict[str, PositionSnapshot]:
        """模拟网关也记账持仓 —— 否则对账会永远报"本地有、券商无"，
        让真正的差异淹没在噪音里（对账机制本身就失去了意义）。"""
        with self._lock:
            return {
                sym: PositionSnapshot(
                    symbol=sym, size=p.size, avg_price=p.avg_price,
                    frozen_size=p.frozen_size,
                )
                for sym, p in self._positions.items()
            }

    def query_account(self) -> AccountSnapshot:
        """模拟网关的资金也真实维护：对账要能比出差异才有价值。"""
        return AccountSnapshot(
            cash=self._cash, available=self._cash,
            frozen=0.0, market_value=self._market_value(),
            total_asset=self._cash + self._market_value(),
        )

    def _market_value(self) -> float:
        with self._lock:
            return sum(p.size * p.avg_price for p in self._positions.values())

    def query_trades(self, since: Any = None) -> List[FillEvent]:
        """模拟网关把成交留了一份，所以能查历史 —— 真实网关也应如此。"""
        with self._lock:
            out = list(self._fills)
        if since is None:
            return out
        try:
            if isinstance(since, str):
                since = datetime.fromisoformat(since)
            if isinstance(since, datetime):
                out = [f for f in out if (f.dt or datetime.min) >= since]
        except Exception:
            pass
        return out

    def query_instrument(self, symbol: str) -> Optional[InstrumentInfo]:
        """模拟网关没有交易所数据，按 ±10% 给一个可测的笼子。

        真实网关**必须**覆盖这个方法返回交易所口径的涨跌停价，
        否则 price_limit 风控会在 ST / 创业板 / 科创板上判错。
        """
        px = 0.0
        if self.price_source is not None:
            try:
                px = float((self.price_source() or {}).get(symbol) or 0.0)
            except Exception:
                px = 0.0
        if px <= 0:
            with self._lock:
                p = self._positions.get(symbol)
            px = float(p.avg_price) if p else 10.0
        return InstrumentInfo(
            symbol=symbol, name=symbol,
            limit_up=round(px * 1.1, 2), limit_down=round(px * 0.9, 2),
            tick_size=0.01, lot_size=100, is_suspended=False, prev_close=px,
        )

    def ensure_connected(self) -> bool:
        if not self._connected:
            self.connect()
        return self._connected

    def capabilities(self) -> Dict[str, bool]:
        return {
            "market_order": True,
            "cancel": True,
            "query_trades": True,
            "query_instrument": True,
            "reconnect": True,
        }

    def health(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "detail": "" if self._connected else "未连接",
            "orders": len(self._orders),
            "fills": len(self._fills),
        }

    def _apply_to_book(self, f: FillEvent) -> None:
        """把成交记进网关自己的持仓与资金，模拟券商侧账本。"""
        with self._lock:
            p = self._positions.get(f.symbol) or PositionSnapshot(symbol=f.symbol)
            if f.side == Side.BUY:
                total = p.avg_price * p.size + f.price * f.size
                p.size += f.size
                p.avg_price = total / p.size if p.size else 0.0
                # 当日买入部分计入冻结（T+1）
                p.frozen_size += f.size
                self._cash -= f.price * f.size + f.fee
            else:
                p.size = max(0, p.size - f.size)
                if p.size == 0:
                    p.avg_price = 0.0
                    p.frozen_size = 0
                else:
                    p.frozen_size = min(p.frozen_size, p.size)
                self._cash += f.price * f.size - f.fee
            self._positions[f.symbol] = p

    #: 是否提供可信的资金对账。模拟网关自己记账，所以为 True。
    supports_account_reconcile = True

    # ---------------- 回报 ----------------
    def poll_fills(self) -> List[FillEvent]:
        out: List[FillEvent] = []
        with self._lock:
            for bid, left in list(self._pending.items()):
                o = self._orders.get(bid)
                if o is None or o.is_final:
                    self._pending.pop(bid, None)
                    continue
                left -= 1
                if left > 0:
                    self._pending[bid] = left
                    continue
                self._pending.pop(bid, None)

                # 成交价优先用外部报价源，其次退回**委托价**。
                # 不能用 o.avg_price：那是"已成交部分的均价"，首次成交前是 0，
                # 而 0 价成交会被账本当成非法入账直接丢掉（订单却已标记成交）。
                px = self._resolve_price(OrderRequest(
                    symbol=o.symbol, side=o.side, size=o.size, price=o.price
                ))
                px = px * (1 + self.slippage) if o.side == Side.BUY else px * (1 - self.slippage)
                o.filled_size = o.size
                o.avg_price = px
                o.status = OrderStatus.FILLED
                o.updated_at = datetime.now()
                out.append(FillEvent(
                    fill_id=f"{bid}-1", broker_order_id=bid,
                    client_order_id=o.client_order_id, symbol=o.symbol,
                    side=o.side, size=o.size, price=px,
                    fee=px * o.size * self.fee_rate,
                    filled_size=o.size, dt=datetime.now(),
                ))
                self.stats["filled"] += 1
                self._apply_to_book(out[-1])
        return out


# ===========================================================================
# 网关注册表
# ===========================================================================
_GATEWAYS: Dict[str, type] = {}


def register_gateway(cls: Optional[type] = None, *, name: str = ""):
    """注册网关实现。加一家券商 = 一行装饰器 + 一个类。"""

    def _do(klass: type) -> type:
        key = name or getattr(klass, "name", "")
        if not key:
            raise ValueError(f"网关 {klass.__name__} 必须定义 name")
        _GATEWAYS[key] = klass
        return klass

    return _do(cls) if cls is not None else _do


def create_gateway(name: str, **kwargs: Any) -> IBrokerGateway:
    """按名字（+ 接入点参数）构造网关。

    参数会先按构造函数签名过滤：接入点配置里的字段是通用的
    （account/readonly/config/凭据…），各家网关不一定都用得上，
    直接透传会在"多一个不认识的参数"上报 TypeError —— 而这恰恰
    是接新券商时最容易踩的坑。过滤掉的同时记一条 debug，便于排查
    "我配了参数怎么没生效"。
    """
    key = str(name or "").strip().lower()
    klass = _GATEWAYS.get(key)
    if klass is None:
        raise KeyError(
            f"未注册的券商网关: {name!r} | 可用: {', '.join(sorted(_GATEWAYS)) or '（无）'}"
        )
    accepted, dropped = filter_kwargs(klass, kwargs)
    if dropped:
        logger.debug(
            f"网关 {key} 不接受这些接入点参数，已忽略: {sorted(dropped)}"
            "（若它们是必需的，说明该网关还没适配接入点配置）"
        )
    return klass(**accepted)


def filter_kwargs(klass: type, kwargs: Dict[str, Any]) -> tuple:
    """按 ``__init__`` 签名挑出能传的参数。返回 (可用参数, 被丢弃的键)。

    显式支持 ``**kwargs`` 的类一把梭全传 —— 那种情况下类自己会处理。
    """
    import inspect

    params = inspect.signature(klass.__init__).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs), set()
    allowed = {k: v for k, v in kwargs.items() if k in params and k != "self"}
    return allowed, set(kwargs) - set(allowed)


def gateway_signature(name: str) -> Dict[str, Any]:
    """列出某个网关构造函数接受的参数名（面板/CLI 用它提示"能配什么"）。"""
    import inspect

    klass = _GATEWAYS.get(str(name or "").strip().lower())
    if klass is None:
        return {}
    params = inspect.signature(klass.__init__).parameters
    out = {}
    for pname, p in params.items():
        if pname in ("self", "kwargs", "args"):
            continue
        if p.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        out[pname] = None if p.default is inspect.Parameter.empty else p.default
    return out


def has_gateway(name: str) -> bool:
    return str(name or "").strip().lower() in _GATEWAYS


def list_gateways() -> List[str]:
    return sorted(_GATEWAYS)


register_gateway(SimulatedGateway)


__all__ = [
    "AccountSnapshot",
    "FillEvent",
    "GatewayError",
    "GatewayOrder",
    "IBrokerGateway",
    "InstrumentInfo",
    "OrderRejected",
    "OrderRequest",
    "PositionSnapshot",
    "SimulatedGateway",
    "create_gateway",
    "filter_kwargs",
    "gateway_signature",
    "has_gateway",
    "list_gateways",
    "register_gateway",
]
