#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : strategy.py
# @Description : 策略组件：引擎里承载 TaskRuntime 的那个组件
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

重构说明
========

由原 ``StrategyManagerComponent`` 改名而来（注册名 ``strategy_manager``
→ ``strategy``），并改继承 :class:`~app.engine.component.kinds.StrategyComponent`。

真正的改变不是名字，是**它拿依赖的方式**：

  * 以前：``engine.get_component("live_gateway")`` —— 字符串写死在方法体里。
    网关改个名就要全局搜一遍；搜漏了是"静默不挂载"：
    下单正常、成交回报路由不回来，账本永远不动，而面板一切正常。
  * 现在：``required_services = ("risk_service",)``，
    引擎在初始化前把服务实例注入 ``self.services``，缺失直接抛错。
    之所以是硬失败而不是降级：策略没有风控就是裸奔，
    "降级运行"在实盘里等于"照常下单但没人拦"。

:meth:`_attach_gateway` 改成按**能力**查找而不是按名字，
因为网关是可选的（只有 LIVE 模式才装配）：
有交易服务就挂上去，没有就什么都不做。
能力查找比名字查找更抗改名 —— 这正是注册表存在的意义。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from twisted.internet import defer

# 基类名与本项目要暴露的类名相同，故用别名导入。
# 不这么做的话 `class StrategyComponent(StrategyComponent)` 虽然能跑，
# 但读代码的人没法一眼分清哪个是基类哪个是实现。
from app.engine.component.kinds import Cap, StrategyComponent as StrategyBase
from app.engine.event import StandardEvents
from app.domain.types import Order, OrderStatus
from app.engine.tasking.runtime import TaskRuntime
from app.engine.tasking.spec import parse_task_specs
from app.common.logging import logger


class StrategyComponent(StrategyBase):
    """多任务调度中枢。

    它是引擎里唯一持有 :class:`TaskRuntime` 的组件，职责边界很清晰：
      - **输入**：BAR_RECEIVED（行情通道）、NEWS_ANALYZED（事件通道）
      - **输出**：ORDER_FILLED / SIGNAL_REJECTED / POSITION_UPDATED

    这样"一个引擎跑 N 个任务"对引擎本身是透明的——引擎只知道有这个组件，
    不需要理解什么是任务、什么是策略。
    """

    name = "strategy"

    #: 硬依赖。缺了就起不来（见模块文档的理由）
    required_services = ("risk_service",)

    #: 软依赖。缺了只是没有跨任务额度预留，不影响交易本身。
    optional_services = ("portfolio_service",)

    #: 已发布订单事件的去重集合上限
    _PUBLISHED_MAX = 8192

    def __init__(self, tasks: Optional[List[Any]] = None, **kwargs: Any) -> None:
        super().__init__()
        self.runtime = TaskRuntime()
        self._task_configs: List[Any] = list(tasks or [])
        self.runtime.set_order_sink(self._on_order)
        #: (order_id, status, filled_size) 去重集合，保证订单事件幂等
        self._published: set = set()

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        # 全局风控闸门：把它交给任务运行时，于是每个任务下单前
        # 都会先过一遍引擎级的熔断与全局规则。
        # 用 provider 而不是直接塞服务对象，理由同网关——
        # 任务可能在服务完全就绪前就被创建。
        risk = self.services.get("risk_service")
        if risk is not None:
            self.runtime.risk_gate_provider = lambda: risk.gate

        # 组合服务（可选）：一边把额度预留器交给运行时，让每个任务下单前
        # 先占住额度；一边把 runtime 交给它，它要据此聚合全账户口径。
        # 双向都是 provider —— 服务和 runtime 互相都比对方建得早。
        pf = self.services.get("portfolio_service")
        if pf is not None:
            self.runtime.quota_provider = lambda: pf
            pf.attach_runtime(lambda: self.runtime)
            logger.info("策略中枢已接入组合服务 | 额度预留与账户聚合已启用")

        cfg = self.component_config or {}
        raw = cfg.get("tasks") or self._task_configs
        self.load_tasks(raw)

        self.subscribe_event(StandardEvents.BAR_RECEIVED, self._on_bar)
        self.subscribe_event(StandardEvents.NEWS_ANALYZED, self._on_news)
        # 实盘：任务建好后要挂到网关上，让成交回报能路由回来。
        # 用事件解耦，策略组件不需要知道交易服务存不存在。
        self.subscribe_event(StandardEvents.TASK_ADDED, self._on_task_added)
        logger.info(f"策略中枢已订阅行情/新闻通道 | 任务数: {len(self.runtime)}")

    def _on_task_added(self, task: Any = None, task_id: str = "", **kwargs: Any) -> None:
        if task is not None:
            self._attach_gateway(task)

    def _attach_gateway(self, task: Any) -> None:
        """把新任务挂到交易服务上（服务不存在时这一步自然什么都不做）。"""
        engine = getattr(self, "_engine", None)
        if engine is None:
            return
        gw = self._resolve_gateway(engine)
        if gw is None:
            return
        try:
            gw.attach(task)
        except Exception as exc:
            logger.error(f"任务 {getattr(task, 'task_id', '?')} 挂载交易网关失败: {exc}")

    @staticmethod
    def _resolve_gateway(engine: Any) -> Any:
        """按**能力**找交易服务，而不是按名字。

        按名字找的话，服务一改名这里就静默返回 None ——
        表现为"下单正常但回报永不回来"，是最难查的一类故障。
        按能力找，改名只影响注册表一处。
        """
        registry = getattr(engine, "services", None)
        if registry is not None:
            svc = registry.first_by_capability(Cap.ORDER_ROUTE)
            return svc
        # 退化路径：老引擎没有注册表时按名字兜底
        return engine.get_component("trading_service")

    def set_engine(self, engine: Any) -> None:
        self._engine = engine

    def load_tasks(self, raw: Any) -> List[str]:
        """从配置装载任务。返回任务ID列表。"""
        specs = parse_task_specs(raw)
        ids: List[str] = []
        for spec in specs:
            try:
                task = self.runtime.add_from_spec(spec)
                ids.append(task.task_id)
            except Exception as exc:
                logger.error(f"任务装载失败 {spec.symbol}: {exc}", exc_info=True)
        return ids

    def on_start(self) -> defer.Deferred:
        # 先挂网关再启动任务：装载阶段（initialize）进来的任务不会触发
        # TASK_ADDED 事件，只能在这里补挂。漏挂的后果是成交回报到达时
        # "无法归属"被整批丢弃 —— 下单成功但账本永远不动。
        for task in list(self.runtime):
            self._attach_gateway(task)
        self.runtime.start_all()
        if len(self.runtime) == 0:
            logger.warning("策略中枢没有任何任务，引擎将空转")
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        self.runtime.stop_all()
        return defer.succeed(None)

    # ---------------- 通道一：行情 ----------------
    def on_bar(self, bar=None, **kwargs) -> None:
        """``StrategyComponent`` 的抽象钩子实现。"""
        self._on_bar(bar=bar, **kwargs)

    def _on_bar(self, bar=None, **kwargs) -> None:
        if bar is None:
            return
        # 只驱动，不在这里发订单事件 —— TaskRuntime 内部对每个产出的订单
        # 都会走一次 order_sink（见 _emit），返回值是同一批对象。
        # 以前两处都发，于是每个订单被发布两次：
        # 落库后成交记录直接翻倍，胜率/成交笔数全部算错。
        self.runtime.on_bar(bar)

    # ---------------- 通道二：新闻 ----------------
    def _on_news(self, news=None, analysis=None, event=None, **kwargs) -> None:
        if news is None:
            return
        self.runtime.on_news(news, analysis)

    # ---------------- 订单回流 ----------------
    def _on_order(self, order: Order) -> None:
        self._publish_order(order)

    def _publish_order(self, order: Order) -> None:
        """订单**每一个状态**都要发事件。

        以前只在 FILLED / REJECTED 时发，于是"挂单中"和"已撤单/部分成交"
        这两类状态在审计上完全不可见 —— 而实盘最容易出问题的恰好是它们
        （撤不掉的单、只成交了一半的单）。落库服务靠这些事件重建订单生命周期。

        幂等：同一个订单的同一个 (状态, 累计成交量) 只发一次。
        订单状态是单向推进的，所以这个组合天然唯一；
        重发只会让下游把同一笔成交记两遍（胜率直接失真）。
        """
        key = (order.order_id, order.status, int(order.filled_size or 0))
        if key in self._published:
            return
        self._published.add(key)
        if len(self._published) > self._PUBLISHED_MAX:
            # 有界集合：长跑服务不能因为记"发过什么"而无限吃内存
            for old in list(self._published)[: self._PUBLISHED_MAX // 4]:
                self._published.discard(old)

        status = order.status
        if status == OrderStatus.PENDING:
            self.event_bus.publish(
                StandardEvents.ORDER_CREATED, order=order, task_id=order.task_id
            )
        elif status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            self.event_bus.publish(
                StandardEvents.ORDER_FILLED, order=order, task_id=order.task_id
            )
            self.event_bus.publish(
                StandardEvents.POSITION_UPDATED, order=order, task_id=order.task_id
            )
        elif status == OrderStatus.CANCELLED:
            self.event_bus.publish(
                StandardEvents.ORDER_CANCELLED, order=order, task_id=order.task_id
            )
        elif status == OrderStatus.REJECTED:
            self.event_bus.publish(
                StandardEvents.SIGNAL_REJECTED,
                order=order,
                reason=order.reject_reason,
                task_id=order.task_id,
            )

    # ---------------- 可观测 ----------------
    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update(self.runtime.snapshot())
        return base

    def summary(self) -> str:
        return self.runtime.summary()

    def __len__(self) -> int:
        return len(self.runtime)


__all__ = ["StrategyComponent"]
