#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : portfolio.py
# @Description : 组合服务：账户级聚合视图 + 跨任务下单额度预留
#
#               为什么需要这个服务（它解决的两个真问题）
#               ------------------------------------------
#               **问题一：没有"账户"这个概念。**
#               ``Portfolio`` 是 **per-task** 的——每个任务一本账
#               （``task.py`` 里 ``self.portfolio = Portfolio(...)``）。
#               于是不存在一个地方能回答"整个账户现在有多少钱、总回撤多少"：
#                 - 面板只能一个任务一个任务地看；
#                 - 全账户回撤没人算（每个任务只算自己的 drawdown）；
#                 - 风控拿到的 account 是**单个任务**的账本，
#                   它看不见别的任务刚刚下了什么。
#
#               这里要明确一件事，**全账户回撤不能从各任务的回撤推出来**：
#               任务 A 亏 10%、任务 B 赚 10%，两个任务的 drawdown 都是 10%，
#               但账户可能压根没回撤；反过来各任务都没回撤，账户也可能在回撤
#               （钱在任务之间腾挪的时点错配）。所以本服务**自己维护聚合权益
#               的峰值**，不做任何"取各任务最大值/平均值"的近似。
#
#               **问题二：并发下单会超额度。**
#               风控是在单个任务的账本上做的判断。实盘所有任务共享一个券商
#               账户，于是：任务 A 过风控放行 8 万 → 任务 B 过风控也放行 8 万
#               → 账户一共 10 万，两笔都发下去就是 16 万。
#               ``task._has_open_order`` 只挡"同一任务同一方向的重复单"，
#               挡不住跨任务。
#
#               解决办法是**额度预留**：风控放行之后、真正报单之前，把预计
#               占用的资金从全账户额度里先扣住；订单落到终态（成交/撤单/拒单）
#               再按实际情况释放。这样第二个任务再过风控时，看到的"可用额度"
#               已经把第一个任务的在途单算进去了。
#
#               三个不做就会出事的设计点
#               ------------------------
#               1. **预留必须有 TTL。** 实盘订单可能永远收不到回报（券商断线、
#                  进程重启、订单号丢了）。没有 TTL 的预留会永久占住额度，
#                  症状是"系统跑着跑着就再也不下单了"，而且**日志里没有任何
#                  错误**——这是最难查的一类故障。所以 TTL 不是锦上添花，
#                  是唯一的兜底。
#               2. **释放必须幂等。** 同一笔订单可能先 PARTIAL 再 FILLED，
#                  也可能成交回报和主动查询各来一次。重复释放会把额度还多，
#                  于是预留机制形同虚设。
#               3. **只预留买入。** 现金是跨任务共享的（同一个券商账户），
#                  所以买入会互相挤占；持仓则是 per-task 的独立账本，
#                  任务 A 卖不掉任务 B 的仓位，不存在跨任务卖超，
#                  而且 ``Portfolio.plan_size`` 已经把卖出量截断到持仓量了。
#
#               为什么是"聚合视图"而不是"统一账本"
#               ----------------------------------
#               本服务**不抢走** per-task 的持仓与资金所有权，只在上面做一层
#               只读聚合。这样 task.py / execution / portfolio 三处的语义
#               完全不用动，回归面最小；代价是聚合有滞后（惰性重算），
#               但对"看账户"和"算额度"这两个用途来说，一根 K 线的滞后无所谓。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from twisted.internet import defer

from app.common.logging import logger
from app.engine.component.kinds import Cap, RestartPolicy, ServiceComponent


class Reservation:
    """一笔额度预留。

    ``amount`` 是预留的金额，``created`` 用于 TTL 判断。留着 ``side`` /
    ``symbol`` / ``task_id`` 只是为了让日志和快照能回答"这笔额度被谁占着"。
    """

    __slots__ = ("rid", "task_id", "symbol", "side", "amount", "created",
                 "ttl", "order_id", "note")

    def __init__(
        self,
        rid: str,
        task_id: str,
        symbol: str,
        side: str,
        amount: float,
        ttl: float,
        order_id: str = "",
        note: str = "",
    ) -> None:
        self.rid = rid
        self.task_id = task_id
        self.symbol = symbol
        self.side = side
        self.amount = float(amount)
        self.created = time.monotonic()
        self.ttl = float(ttl)
        self.order_id = order_id
        self.note = note

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created) > self.ttl

    @property
    def age(self) -> float:
        return time.monotonic() - self.created

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rid": self.rid,
            "task_id": self.task_id,
            "symbol": self.symbol,
            "side": self.side,
            "amount": round(self.amount, 2),
            "age": round(self.age, 1),
            "ttl": round(self.ttl, 1),
            "order_id": self.order_id,
            "note": self.note,
        }


class PortfolioService(ServiceComponent):
    """组合服务：账户聚合视图 + 跨任务额度预留。

    服务名 ``portfolio_service``，提供 ``portfolio.view`` 与
    ``portfolio.quota`` 两个能力。
    """

    name = "portfolio_service"
    provides = frozenset({Cap.ACCOUNT_VIEW, Cap.QUOTA_RESERVE})
    #: 组合服务不该被自动重启：它持有额度预留这种"账外状态"，
    #: 重启等于把在途单的占款悄悄抹掉，正好制造它本来要防的超额事故。
    restart_policy = RestartPolicy.NEVER

    def __init__(
        self,
        budget: Optional[float] = None,
        reserve_ttl: float = 180.0,
        enabled: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        if enabled is not None:
            self.enabled = bool(enabled)
        #: 全账户下单额度上限（元）。None = 不限制，此时 reserve 永远成功，
        #: 只做记账——这样同一个调用方代码在"没配额度"的环境里也能跑。
        self.budget: Optional[float] = None if budget is None else float(budget)
        #: 单笔预留的存活秒数。默认 3 分钟：够覆盖"报单 → 成交回报"的正常
        #: 间隔，又不至于让一笔丢失的回报把额度卡太久。
        self.reserve_ttl = float(reserve_ttl)

        #: 取 TaskRuntime 的回调。为什么是回调而不是直接给 runtime：
        #: 服务在装配阶段就建好了，那时策略组件还没初始化、runtime 还没有。
        #: 直接塞对象只会拿到 None（上一轮踩过一模一样的坑，见
        #: ``runtime.gateway_provider``）。
        self._runtime_provider: Optional[Callable[[], Any]] = None

        self._reservations: Dict[str, Reservation] = {}
        self._by_order: Dict[str, str] = {}       # order_id -> rid
        self._seq = 0

        #: 聚合缓存 + 脏标记。每次 BAR_RECEIVED 都把 N 个任务的账本重算一遍
        #: 太重（回测里一根 bar 就要算一次），所以只在**被读的时候**重算。
        self._agg: Dict[str, Any] = {}
        self._dirty = True
        #: 聚合权益的历史峰值。**全账户回撤的唯一依据**，见文件头说明。
        self._peak_equity = 0.0

        self.stats = {
            "reserves": 0,        # 预留成功次数
            "rejects": 0,         # 额度不足被拒次数
            "releases": 0,        # 主动释放次数
            "expiries": 0,        # TTL 到期被清理次数
            "leaked": 0,          # 找不到对应预留的释放（调用方 bug 的信号）
            "aggregations": 0,    # 聚合重算次数
        }

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    def attach_runtime(self, provider: Callable[[], Any]) -> None:
        """挂上取 ``TaskRuntime`` 的回调（由装配器或策略组件调用）。

        传**函数**而不是对象，理由是服务比 runtime 建得早。
        """
        self._runtime_provider = provider
        self._dirty = True

    def _runtime(self) -> Optional[Any]:
        if self._runtime_provider is None:
            return None
        try:
            return self._runtime_provider()
        except Exception as exc:      # pragma: no cover - 防御
            logger.debug(f"组合服务取 runtime 失败: {exc}")
            return None

    def mark_dirty(self) -> None:
        """外部账本变了（成交/撤单/新增任务）时调一下，让聚合失效。"""
        self._dirty = True

    # ------------------------------------------------------------------
    # 账户聚合视图
    # ------------------------------------------------------------------
    def _iter_portfolios(self) -> List[Any]:
        rt = self._runtime()
        if rt is None:
            return []
        out = []
        tasks = getattr(rt, "tasks", None) or {}
        for t in tasks.values():
            pf = getattr(t, "portfolio", None)
            if pf is not None:
                out.append(pf)
        return out

    def aggregate(self, force: bool = False) -> Dict[str, Any]:
        """账户级聚合。默认走缓存（惰性重算）。

        返回的 ``positions`` 按标的聚合：同一个标的可能被多个任务持有，
        面板要能一眼看出"这只票全账户一共多少股"。
        """
        if not self._dirty and not force and self._agg:
            return self._agg

        cash = 0.0
        market_value = 0.0
        realized_pnl = 0.0
        initial_cash = 0.0
        positions: Dict[str, Dict[str, Any]] = {}
        per_task: List[Dict[str, Any]] = []

        for pf in self._iter_portfolios():
            try:
                acc = pf.account
                c = float(acc.cash)
                mv = float(pf.market_value)
                cash += c
                market_value += mv
                realized_pnl += float(getattr(pf, "realized_pnl", 0.0))
                initial_cash += float(getattr(acc, "initial_cash", 0.0))

                pos = pf.position
                sym = getattr(pos, "symbol", "") or pf.symbol
                size = int(getattr(pos, "size", 0) or 0)
                bucket = positions.setdefault(sym, {
                    "symbol": sym,
                    "size": 0,
                    "market_value": 0.0,
                    "avg_price": 0.0,
                    "last_price": float(getattr(pos, "last_price", 0.0) or 0.0),
                    "cost": 0.0,          # 用于加权平均成本
                    "tasks": [],
                })
                bucket["size"] += size
                bucket["market_value"] += mv
                bucket["cost"] += float(getattr(pos, "avg_price", 0.0) or 0.0) * size
                bucket["last_price"] = float(getattr(pos, "last_price", 0.0) or 0.0)
                bucket["tasks"].append(pf.task_id)

                per_task.append({
                    "task_id": pf.task_id,
                    "symbol": pf.symbol,
                    "cash": round(c, 2),
                    "equity": round(float(pf.equity), 2),
                    "position_size": size,
                    "drawdown": round(float(pf.drawdown), 4),
                })
            except Exception as exc:
                # 单个任务账本坏了不能拖垮整个账户视图
                logger.debug(f"组合服务聚合跳过了任务 {getattr(pf, 'task_id', '?')}: {exc}")

        equity = cash + market_value
        for b in positions.values():
            b["avg_price"] = round(b["cost"] / b["size"], 4) if b["size"] else 0.0
            b["market_value"] = round(b["market_value"], 2)
            b.pop("cost", None)
        # 零仓位的标的不算持仓。留着它们会让面板的持仓列表里出现一堆
        # size=0 的行 —— 那不是持仓，是"有人在盯这只票"，应该由别处表达。
        positions = {k: v for k, v in positions.items()
                     if v["size"] or abs(v["market_value"]) > 1e-9}

        # 峰值只增不减，且由**聚合权益**而非各任务权益推出
        if equity > self._peak_equity:
            self._peak_equity = equity
        drawdown = (
            max(0.0, (self._peak_equity - equity) / self._peak_equity)
            if self._peak_equity > 0 else 0.0
        )

        self._agg = {
            "task_count": len(per_task),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(equity, 2),
            "initial_cash": round(initial_cash, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_pnl": round(equity - initial_cash, 2),
            "peak_equity": round(self._peak_equity, 2),
            "drawdown": round(drawdown, 4),
            "position_value": round(market_value, 2),
            "position_ratio": round(market_value / equity, 4) if equity else 0.0,
            "positions": sorted(positions.values(),
                                key=lambda x: -abs(x["market_value"])),
            "tasks": per_task,
        }
        self._dirty = False
        self.stats["aggregations"] += 1
        return self._agg

    @property
    def account_view(self) -> Dict[str, Any]:
        """给风控/面板看的账户口径。等价于 :meth:`aggregate`。"""
        return self.aggregate()

    # ------------------------------------------------------------------
    # 额度预留
    # ------------------------------------------------------------------
    @property
    def reserved(self) -> float:
        """当前被在途单占住的额度。

        读之前先 :meth:`_sweep`。清理是"懒"的（只在 reserve 时顺带跑），
        但读路径不清会有真实后果：**最后一笔**预留过期后如果再没有新买单，
        它就永远留在字典里 —— ``available`` 被永久低估、:meth:`is_busy`
        永久为真，引擎再也判定不了空闲，而日志里没有任何错误。
        """
        self._sweep()
        return sum(r.amount for r in self._reservations.values())

    def _effective_budget(self) -> Optional[float]:
        """真正生效的额度上限。

        显式配了 ``budget`` 就用它；没配时退化为**聚合账户的可用现金** ——
        这样即使一行配置都不写，"下单占用的钱不能超过账户里有的钱"
        这条底线也自动成立，而且随账户规模自适应，不用为不同账号配不同的数。

        唯一的例外是"聚合不到任何任务"（task_count == 0）：那时现金是 0，
        照搬会把所有下单都拒掉。**看不见账户的时候不该瞎限制**，
        这种情况返回 None 表示不限制。
        """
        if self.budget is not None:
            return self.budget
        agg = self.aggregate()
        if not agg.get("task_count"):
            return None
        return float(agg.get("cash", 0.0))

    @property
    def available(self) -> Optional[float]:
        """还剩多少可用额度。不限制时返回 None。"""
        b = self._effective_budget()
        if b is None:
            return None
        return max(0.0, b - self.reserved)

    def _sweep(self) -> None:
        """清掉过期预留。

        只在 :meth:`reserve` 里顺带跑（懒清理），不单独起定时器——
        多一个 LoopingCall 就多一处"停不干净"的风险，而这个清理
        完全不需要准时。
        """
        if not self._reservations:
            return
        for rid, r in list(self._reservations.items()):
            if not r.expired:
                continue
            self._reservations.pop(rid, None)
            if r.order_id:
                self._by_order.pop(r.order_id, None)
            self.stats["expiries"] += 1
            logger.warning(
                f"额度预留 {rid} 超时释放 | {r.task_id} {r.symbol} "
                f"{r.amount:.2f}元 已占 {r.age:.0f}s（订单 {r.order_id or '未绑定'} "
                f"始终没到终态，按放弃处理）"
            )

    def reserve(
        self,
        task_id: str,
        symbol: str,
        amount: float,
        side: str = "BUY",
        ttl: Optional[float] = None,
        order_id: str = "",
        note: str = "",
    ) -> Optional[str]:
        """占住一笔额度。成功返回预留号，额度不足返回 ``None``。

        返回 None（而不是抛异常）是有意的：额度不够是**正常的业务结果**，
        不是错误，调用方应当按"这一笔被否决"处理并留痕。
        """
        amount = float(amount or 0.0)
        if amount <= 0:
            # 零额度的预留没有意义，但也不能算失败——直接给个空号，
            # 调用方照常走下去，只是没有东西要释放。
            return ""

        self._sweep()

        budget = self._effective_budget()
        if budget is not None and self.reserved + amount > budget:
            self.stats["rejects"] += 1
            logger.info(
                f"额度不足，拒绝预留 | {task_id} {symbol} 需要 {amount:.2f} "
                f"| 已占 {self.reserved:.2f} / 上限 {budget:.2f}"
            )
            return None

        self._seq += 1
        rid = f"r{self._seq}"
        self._reservations[rid] = Reservation(
            rid=rid, task_id=task_id, symbol=symbol, side=side,
            amount=amount, ttl=self.reserve_ttl if ttl is None else float(ttl),
            order_id=order_id, note=note,
        )
        if order_id:
            self._by_order[order_id] = rid
        self.stats["reserves"] += 1
        return rid

    def bind_order(self, rid: str, order_id: str) -> None:
        """把预留绑到订单号上（下单时才拿得到订单号），供按订单释放。"""
        r = self._reservations.get(rid)
        if r is None:
            return
        r.order_id = order_id
        self._by_order[order_id] = rid

    def release(self, rid: str, note: str = "") -> bool:
        """释放一笔预留。**幂等**：重复调用返回 False，不会把额度还多。"""
        if not rid:
            return False
        r = self._reservations.pop(rid, None)
        if r is None:
            # 释放一个不存在的预留 = 调用方把生命周期搞错了（多半是重复释放）。
            # 不抛异常，但计数——这个数不为 0 就说明链路有问题。
            self.stats["leaked"] += 1
            logger.debug(f"释放了一个不存在的额度预留: {rid}（{note}）")
            return False
        if r.order_id:
            self._by_order.pop(r.order_id, None)
        self.stats["releases"] += 1
        logger.debug(
            f"额度预留释放 | {rid} {r.task_id} {r.symbol} {r.amount:.2f}元"
            f"{(' | ' + note) if note else ''}"
        )
        return True

    def release_order(self, order_id: str, note: str = "") -> bool:
        """按订单号释放。订单到终态（成交/撤单/拒单）时调这个。"""
        if not order_id:
            return False
        rid = self._by_order.get(order_id)
        if rid is None:
            # 这**不一定**是 bug：卖出单不占额度，本来就没有预留。
            # 所以这里不计数，静默返回。
            return False
        return self.release(rid, note=note or f"order:{order_id}")

    def release_task(self, task_id: str, note: str = "") -> int:
        """释放某个任务的全部预留（任务停止/熔断时用）。返回释放笔数。"""
        n = 0
        for rid, r in list(self._reservations.items()):
            if r.task_id == task_id:
                if self.release(rid, note=note or f"task:{task_id}"):
                    n += 1
        return n

    def release_all(self, note: str = "") -> int:
        """释放全部预留（引擎停止时用）。"""
        n = len(self._reservations)
        for rid in list(self._reservations.keys()):
            self.release(rid, note=note or "release_all")
        return n

    # ------------------------------------------------------------------
    # 生命周期与快照
    # ------------------------------------------------------------------
    def on_initialize(self) -> None:
        self._dirty = True
        logger.info(
            f"组合服务已装配 | 额度上限="
            f"{'不限' if self.budget is None else f'{self.budget:.2f}'} "
            f"| 预留 TTL={self.reserve_ttl:.0f}s"
        )

    def on_stop(self, graceful: bool = True):
        """停服务时把所有在途预留还回去。

        进程都要退了，占着的额度留着没有意义；留着反而会让下一次启动
        （同一个进程内重启引擎的场景）一开始就少一块额度。
        """
        n = self.release_all(note="service_stop")
        if n:
            logger.info(f"组合服务停止 | 释放 {n} 笔在途额度预留")
        return defer.succeed(None)

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        eff = self._effective_budget()
        base.update({
            "budget": self.budget,
            "effective_budget": (None if eff is None else round(eff, 2)),
            "reserved": round(self.reserved, 2),
            "available": (None if self.available is None
                          else round(self.available, 2)),
            "reserve_ttl": self.reserve_ttl,
            "pending_count": len(self._reservations),
            "pending": [r.to_dict() for r in self._reservations.values()],
            "account": self.aggregate(),
            "stats": dict(self.stats),
        })
        return base

    def is_busy(self) -> bool:
        """有待结算的预留就算忙——引擎不该在这时候判定为空闲并退出。

        先 :meth:`_sweep`：过期的预留不算"待结算"，否则引擎会永远忙。
        """
        self._sweep()
        return bool(self._reservations)

    def health_check(self) -> Tuple[bool, str]:
        """健康检查。**契约是 ``(是否健康, 说明文本)``，不是 dict。**

        这里以前写成 dict：``h = super().health_check()`` 拿到的是基类
        返回的**元组**，接着 ``h["budget"] = ...`` 就抛
        ``TypeError: 'tuple' object does not support item assignment``。
        服务注册表在 ``registry.py`` 里捕获它，面板上就只显示
        "health_check 异常"，真正的额度信息一条都看不到。

        要暴露的预算/预留信息放进说明文本；结构化数据走 :meth:`snapshot`。
        """
        ok, detail = super().health_check()
        parts = [detail]
        if self.budget is not None:
            parts.append(f"预算 {self.budget:.0f}")
        parts.append(f"在途预留 {self.reserved:.2f}（{len(self._reservations)} 笔）")
        return ok, " | ".join(parts)
