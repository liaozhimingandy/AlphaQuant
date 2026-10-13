#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : service.py
# @Description : 账户服务 —— 虚拟账户、交易时段、以及"钱记在哪儿"
#
#                它做三件事，看上去不相干，其实是同一件事的三个面：
#
#                1. **内置虚拟账户**（``account_service`` 启动时确保存在）
#                   见 :class:`app.data.schema.BrokerAccount` ——
#                   内置一个不是为了偷懒省配置，而是为了**让整条交易链路
#                   在不必承担真实风险的前提下每天被真实地走一遍**。
#                   一个从没下过单的系统，"能不能下单"永远是个未知数。
#
#                2. **交易时段的事件化**（``trade.session.opened/closed``）
#                   这是本服务最容易被低估的部分。市场是否开盘是**客观时间**，
#                   "我们因此开始/停止交易"是**系统的主观动作**；
#                   两者之间还夹着一个"开盘了但网关还没连上"的状态。
#                   把它们合成一个布尔值，最终的代价一定是：
#                   某一天单被拒了，而查不出为什么。
#
#                3. **收盘清算**（``settle()``）
#                   盘中追求延迟，收盘后追求准确，两件事对时间预算的要求相反，
#                   就不该挤在同一个调用栈里。

#                ===========================================================
#                一个必须说清楚的边界：**两本账**
#                ===========================================================
#                ``QuantTask.portfolio`` 是 **per-task** 的 —— 每个任务一本
#               、各带自己的 ``initial_cash``；本服务维护的 ``broker_account``
#                是**跨任务共享**的钱包。目前两者**互不约束**：
#                任务的本金还不是从钱包里划出来的。
#
#                这是**已知且尚未打通**的一步，而不是无意遗漏 ——
#                划拨牵扯的策略分配、额度回收、失败回滚都需要单独的设计与回归，
#                仓促接上去比维持现状更危险。本服务先把"钱包"这一侧建起来：
#                成交事件在这里真实地增减现金，持仓快照在这里维护，
#                从而为下一步的"任务从钱包领钱"准备好唯一的资金来源。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from twisted.internet import task as twisted_task

from app.common.logging import logger
from app.engine.capability import Cap, RestartPolicy, ServiceComponent


class AccountService(ServiceComponent):
    """资金账本的所有者。

    :param initial_cash: 虚拟账户的初始资金。**只对新建账户生效** ——
                         已经跑过钱的账户不会因为改这个参数而变化，
                         否则"把本金改大"就会变成一种篡改历史收益率的手法。
    :param session_poll_seconds: 交易时段轮询间隔（秒）。
    :param settle_enabled: 是否允许收盘清算被外部任务调用。
    """

    name = "account_service"
    provides = frozenset({Cap.ACCOUNT_LEDGER})
    restart_policy = RestartPolicy.ON_ERROR

    #: 那些状态算"可以下单"。与 :class:`~app.modules.risk.builtin.TradingSessionRisk`
    #: 刻意共用同一份定义 —— 风控规则管"这一单能不能下"，
    #: 这里的监视器管"我们对外宣布能不能交易"，两处口径一旦分叉，
    #: 就会出现"服务说可以交易、每单都被风控拒了"这种互相矛盾的界面。
    _TRADABLE_STATES = ("open", "lunch", "post_close")

    def __init__(
        self,
        initial_cash: float = 0.0,
        session_poll_seconds: float = 30.0,
        enabled: Optional[bool] = None,
        gateway: str = "simulated",
        endpoint_id: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.initial_cash = float(initial_cash or 0.0)
        self.session_poll_seconds = max(5.0, float(session_poll_seconds or 30.0))
        self.gateway = str(gateway or "simulated")
        self.endpoint_id = str(endpoint_id or "")
        if enabled is not None:
            self.enabled = bool(enabled)

        self._loop = None
        self._session_state: str = ""
        self._tradable: bool = False
        self._session_since: str = ""
        self.settles: int = 0
        self.fills_applied: int = 0
        self.stats: Dict[str, Any] = {
            "session_changes": 0,
            "opens": 0,
            "closes": 0,
        }

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        from app.account import store as S

        S.ensure_schema()
        try:
            acc = S.ensure_virtual_account(
                initial_cash=self.initial_cash, gateway=self.gateway,
                endpoint_id=self.endpoint_id)
            self._virtual_id = acc.get("account_id") or S.VIRTUAL_ACCOUNT_ID
            logger.info(
                f"账户服务就绪 | 虚拟账户 {self._virtual_id} | "
                f"本金 {acc.get('initial_cash')} | "
                f"网关 {acc.get('gateway')}（永不下真单，实盘需显式配 live 账户）"
            )
        except Exception as exc:
            # 账户表建不起来 → 服务不可用，但**不该拖垮整个引擎**：
            # 采集、回测、看板都不依赖它。这里只禁用自己。
            logger.error(f"虚拟账户初始化失败，账户服务自我禁用: {exc}")
            self.enabled = False
            return

        self.subscribe_event("trade.order.filled", self._on_filled)
        self.subscribe_event("system.market.session.changed", self._on_session_event)

    def on_start(self) -> Any:
        # 起手先看一眼当前处在什么时段，避免"服务起来了但一个 tick 之前
        # 的状态是空的"。面板在启动后头 30 秒内读到的是空字符串会
        # 显示成"未知"，而它其实马上就有值了。
        self._refresh_session(publish_on_change=False)
        self._loop = twisted_task.LoopingCall(self._tick)
        self._loop.start(self.session_poll_seconds, now=False)
        logger.info(
            f"账户服务已启动 | 交易时段监视每 {self.session_poll_seconds:.0f}s | "
            f"当前时段 {self._session_state or '未知'}"
        )
        return None

    def on_stop(self, graceful: bool = True) -> None:
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None

    # ============================================================
    # 交易时段
    # ============================================================
    def _tick(self) -> None:
        try:
            self._refresh_session(publish_on_change=True)
        except Exception as exc:  # 轮询里崩了不该让 LoopingCall 死掉
            logger.warning(f"交易时段检查失败: {exc}")

    def _refresh_session(self, publish_on_change: bool = True) -> str:
        """刷一次市场状态；变化时才发事件。"""
        try:
            from app.domain.calendar import get_calendar

            state = get_calendar().state()
        except Exception as exc:
            logger.debug(f"读取交易时段失败: {exc}")
            return self._session_state
        if state == self._session_state:
            return state

        prev, self._session_state = self._session_state, state
        self._session_since = datetime.now().isoformat(timespec="seconds")
        self.stats["session_changes"] += 1
        self._publish("system.market.session.changed",
                      state=state, previous=prev or "")
        if not publish_on_change:
            return state

        tradable = state in self._TRADABLE_STATES
        if tradable != self._tradable:
            self._tradable = tradable
            if tradable:
                self.stats["opens"] += 1
                self._publish("trade.session.opened", state=state)
                logger.info(f"交易时段开始 | 状态 {state}")
            else:
                self.stats["closes"] += 1
                self._publish("trade.session.closed", state=state)
                logger.info(f"交易时段结束 | 状态 {state}")
        return state

    def _on_session_event(self, *args: Any, **kwargs: Any) -> None:
        """别人已经宣布了时段变化 —— 那就别再发一遍。

        为什么需要这个订阅：``system.market.session.changed`` 是**客观事实**，
        任何组件都可能发布它。若本服务只认自己 tick 出来的状态，
        两条链路会各自表述，"谁说的算"就变成一个说不清的问题。
        """
        payload = kwargs.get("payload") or kwargs
        state = str(payload.get("state") or "")
        if not state or state == self._session_state:
            return
        self._session_state = state
        tradable = state in self._TRADABLE_STATES
        if tradable != self._tradable:
            self._tradable = tradable
            if tradable:
                self.stats["opens"] += 1
            else:
                self.stats["closes"] += 1

    # ============================================================
    # 成交记账
    # ============================================================
    def _on_filled(self, *args: Any, **kwargs: Any) -> None:
        """成交 → 改钱包里的现金。

        取不到金额就**不写**：宁可钱包少记一笔（后续可对账补上），
        也不要凭猜测编一个数字进去 —— 钱是最难纠错的那种数据。
        """
        payload = kwargs.get("payload") or kwargs
        try:
            side = str(payload.get("side") or "").lower()
            price = float(payload.get("price") or 0)
            size = int(payload.get("size") or 0)
        except (TypeError, ValueError):
            logger.debug("成交事件缺 side/price/size，跳过记账")
            return
        if price <= 0 or size <= 0:
            return
        # 买入花钱（-），卖出收钱（+）。费用统一按买入方向扣。
        fee = float(payload.get("fee") or 0.0)
        if "buy" in side:
            delta = -(price * size) - fee
        elif "sell" in side:
            delta = (price * size) - fee
        else:
            logger.debug(f"未知成交方向 {side!r}，跳过记账")
            return
        try:
            from app.account import store as S

            S.apply_fill(self._virtual_id, delta)
            self.fills_applied += 1
            self._publish("account.updated",
                          account_id=self._virtual_id, cash_delta=delta,
                          symbol=str(payload.get("symbol") or ""))
        except Exception as exc:
            logger.warning(f"成交记账失败（不影响已成交的订单）: {exc}")

    # ============================================================
    # 收盘清算
    # ============================================================
    def settle(self, day: str = "", note: str = "") -> Dict[str, Any]:
        """收盘清算：把钱和票固化进账户表。

        幂等：同一天跑三次与前跑一次结果相同 —— 持仓是靠
        **覆盖**写的（不是累加），峰值权益只增不减。
        """
        from app.account import store as S

        # ``_virtual_id`` 只在 on_initialize 成功时赋值。清算被独立任务
        # （account.settle）调用时可能还没走完初始化，这里兜回内置默认账户，
        # 而不是抛 AttributeError —— 那个异常会被下面的 except 抓成
        # "清算失败"，于是一个"服务还没起来"的问题被报成"清算坏了"。
        vid = getattr(self, "_virtual_id", "") or S.VIRTUAL_ACCOUNT_ID
        try:
            acc = S.get_account(vid)
            if acc is None:
                acc = S.ensure_virtual_account(initial_cash=self.initial_cash)
            positions = self._positions_from_portfolio()
            if positions:
                S.save_positions(vid, positions)
            prices = {p["symbol"]: p.get("last_price") or 0.0 for p in positions}
            after = S.mark_prices(vid, prices)
            self.settles += 1
            self._publish("account.updated",
                          account_id=vid, reason="settle",
                          day=str(day or datetime.now().date().isoformat()))
            return {
                "ok": True,
                "settled": 1,
                "day": str(day or datetime.now().date().isoformat()),
                "account": after or acc,
                "positions": len(positions),
                "note": note,
            }
        except Exception as exc:
            logger.error(f"收盘清算失败: {exc}")
            return {"ok": False, "settled": 0, "error": str(exc)[:200]}

    @staticmethod
    def _positions_from_portfolio() -> List[Dict[str, Any]]:
        """试图从组合服务取当前持仓；取不到就返回空 —— **不要凭空编**。

        清算时保持沉默（不更新持仓）好过写一半错数据进去：
        持仓错了会直接导致"卖不掉"（T+1 规则按可卖数量拦），
        而这是实盘里最要命的一类故障。
        """
        try:
            from app.engine.service import get_engine

            eng = get_engine()
        except Exception:
            return []
        if eng is None:
            return []
        svc = None
        for getter in ("get_component", "service_by_capability"):
            fn = getattr(eng, getter, None)
            if not callable(fn):
                continue
            try:
                svc = fn(Cap.ACCOUNT_VIEW) if getter.endswith("capability") else fn("portfolio_service")
            except Exception:
                svc = None
            if svc is not None:
                break
        aggregate = getattr(svc, "aggregate", None)
        if not callable(aggregate):
            return []
        try:
            view = aggregate()
        except Exception as exc:
            logger.debug(f"读取组合聚合失败（本次不更新持仓）: {exc}")
            return []
        out: List[Dict[str, Any]] = []
        for row in (view.get("positions") or []):
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            out.append({
                "symbol": sym,
                "size": int(row.get("size") or 0),
                "sellable": int(row.get("size") or 0),
                "avg_price": float(row.get("avg_price") or 0.0),
                "last_price": float(row.get("last_price") or row.get("price") or 0.0),
            })
        return out

    # ============================================================
    # 对外视图
    # ============================================================
    def accounts(self) -> Dict[str, Any]:
        from app.account import store as S

        return S.summary()

    @property
    def tradable(self) -> bool:
        return bool(self._tradable)

    def session_info(self) -> Dict[str, Any]:
        return {
            "state": self._session_state,
            "tradable": bool(self._tradable),
            "since": self._session_since,
            "tradable_states": list(self._TRADABLE_STATES),
        }

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update({
            "virtual_account_id": getattr(self, "_virtual_id", ""),
            "tradable": self.tradable,
            "session": self.session_info(),
            "settles": self.settles,
            "fills_applied": self.fills_applied,
            "stats": dict(self.stats),
        })
        try:
            base["accounts"] = self.accounts()
        except Exception as exc:  # pragma: no cover
            base["accounts_error"] = str(exc)[:160]
        return base

    def health_check(self) -> tuple:
        if not self.enabled:
            return False, "账户服务已禁用"
        return True, f"账户正常 · 时段 {self._session_state or '未知'}"

    def _publish(self, event_name: str, **payload: Any) -> None:
        bus = getattr(self, "event_bus", None)
        if bus is None:
            return
        try:
            bus.publish(event_name, source="account_service", **payload)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"发布事件失败 {event_name}: {exc}")


__all__ = ["AccountService"]
