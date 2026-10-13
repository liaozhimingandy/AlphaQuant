#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : kinds.py
# @Description : 组件的四种"种类"。
#
#               之前引擎里 11 个组件是**平级**的：采集、行情、交易、策略、调度
#               全都挂在 engine._components 里，谁也不比谁特殊。结果是：
#                 - 看不出谁提供能力、谁消费能力；
#                 - 启停顺序只能靠 builder 里手写的注册顺序碰运气；
#                 - 组件之间互相找人全靠 engine.get_component("market_center")
#                   这种魔法字符串（全项目约 30 处），改名即崩。
#
#               这里把"组件"按**在引擎里扮演的角色**分成四类，
#               对应架构图：
#
#                   IBaseComponent
#                   ├── ServiceComponent    提供能力（被依赖）
#                   │     ├── MarketService
#                   │     ├── CollectionService
#                   │     ├── TradingService
#                   │     └── RiskService
#                   ├── StrategyComponent   消费能力，产出信号
#                   ├── WorkerComponent     消费能力，执行作业
#                   └── SchedulerComponent  决定"什么时候跑"
#
#               分类带来的硬收益：
#                 1. **依赖显式化**：服务声明 provides（我提供什么）与
#                    depends_on（我需要什么），引擎据此拓扑排序启停，
#                    不再依赖注册顺序这种隐式约定；
#                 2. **注入替代查找**：StrategyComponent / WorkerComponent
#                   通过 required_services 拿到服务实例，不再写魔法字符串；
#                 3. **可观测性**：面板可以按种类分组，一眼看出
#                    "服务层 4 个全绿，策略层 1 个在跑"。
#
#               注意：这四个类**不新增生命周期**。initialize/start/stop 的语义
#               完全沿用 IBaseComponent —— 它们只在该接口之上加"角色语义"。
#               这是刻意的设计：引擎主循环不需要为服务单独写一套启停逻辑。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from abc import abstractmethod
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Tuple

from app.engine.component import IBaseComponent
from app.common.logging import logger


# =============================================================================
# 1. 组件种类枚举
# =============================================================================
class ComponentKind(str, Enum):
    """组件的种类。面板按这个分组，启停顺序也按这个分层。

    ``str`` 混入是为了让快照 JSON 直接可序列化（``kind.value`` 即字符串）。
    """

    SERVICE = "service"      # 提供能力，被别人依赖
    STRATEGY = "strategy"    # 消费行情/风控，产出订单
    WORKER = "worker"        # 消费任务，执行作业
    SCHEDULER = "scheduler"  # 决定执行节拍
    PLAIN = "plain"          # 不属于以上四类（如监控面板这类观测者）

    @property
    def label(self) -> str:
        return {
            "service": "服务",
            "strategy": "策略",
            "worker": "工作单元",
            "scheduler": "调度",
            "plain": "组件",
        }[self.value]


#: 启停的**层级顺序**。数字小的先启动、后停止。
#: 调度先起来，服务紧接着，策略与工作单元最后 —— 它们要消费服务；
#: 停止时严格反序，先停消费方再停提供方，避免"还在读一个正在被拆掉的服务"。
#: PLAIN（监控面板这类观测者）排最后启动、最先停止，
#: 于是面板永远看不到"引擎正在散架"的中间态。
KIND_LAYER: Dict["ComponentKind", int] = {
    ComponentKind.SCHEDULER: 10,
    ComponentKind.SERVICE: 20,
    ComponentKind.STRATEGY: 30,
    ComponentKind.WORKER: 40,
    ComponentKind.PLAIN: 50,
}


# =============================================================================
# 2. 服务能提供的能力（能力名常量）
#
#    用常量而不是裸字符串：写错能力名时 IDE / 静态检查能立刻发现，
#    而 "market.bar" 拼成 "market.bars" 只会在运行时静默查不到。
# =============================================================================
class Cap:
    # —— 行情 ——
    BAR_FEED = "market.bar_feed"        # 推送/回放 K 线
    SNAPSHOT_QUOTE = "market.quote"     # 取最新快照行情
    WATCHLIST = "market.watchlist"      # 动态增删订阅标的

    # —— 采集 ——
    COLLECT_KLINE = "collect.kline"     # 抓 K 线入库
    COLLECT_CROSS = "collect.cross"     # 抓日频截面（资金流/筹码/涨停池）
    DATA_INTEGRITY = "collect.integrity"  # 数据完整性与缺口补全
    WATCHLIST_COLLECT = "collect.watchlist"  # 关注标的：按标的各自的频率定时自动采

    # —— 交易 ——
    ORDER_ROUTE = "trade.route"         # 把订单路由到券商
    ACCOUNT_QUERY = "trade.account"     # 查资金/持仓
    RECONCILE = "trade.reconcile"       # 对账
    TRADE_PERSIST = "trade.persist"     # 订单/成交/权益落库

    # —— 风控 ——
    RISK_GATE = "risk.gate"             # 下单前拦截/放行/改量

    # —— 组合 ——
    ACCOUNT_VIEW = "portfolio.view"     # 账户级聚合视图（全账户资金/持仓/回撤）
    QUOTA_RESERVE = "portfolio.quota"   # 跨任务下单额度预留（治并发超限）

    # —— 资讯 ——
    NEWS_FEED = "news.feed"             # 新闻采集与分析


# =============================================================================
# 3. 服务重启策略
# =============================================================================
class RestartPolicy(str, Enum):
    """服务失败时的处理策略。

    服务与一次性任务的关键区别就在这里：服务是**常驻**的，
    所以它挂了要能自己爬起来，而不是把整台引擎拖死。
    """

    NEVER = "never"          # 不自动重启（默认，最安全）
    ON_ERROR = "on_error"    # 出错即重启
    ALWAYS = "always"        # 停止也重启（用于必须常驻的服务）

    @property
    def label(self) -> str:
        return {"never": "不重启", "on_error": "出错重启", "always": "常驻"}[self.value]


# =============================================================================
# 4. ServiceComponent —— 提供能力的一方
# =============================================================================
class ServiceComponent(IBaseComponent):
    """服务组件：**对外提供能力**的常驻组件。

    与 StrategyComponent / WorkerComponent 的本质区别：
      服务是**被依赖**的，它自己不主动消费别人的输出。
      一个组件只要开始写着 ``engine.get_component("xxx")`` 去找别人，
      它就多半不该是 ServiceComponent —— 那是消费方的特征。

    子类需要声明三件事（都有安全默认值，不写也能跑）：

    .. code-block:: python

        class MarketService(ServiceComponent):
            name = "market_service"
            provides = {Cap.BAR_FEED, Cap.WATCHLIST}
            depends_on = ()            # 服务之间也可以有依赖
            restart_policy = RestartPolicy.ON_ERROR

    **依赖注入**：引擎在 ``initialize`` 阶段按 ``depends_on`` 把依赖
    解析好并塞进 ``self.services``，子类在 ``on_initialize`` 里直接取用，
    不用再碰引擎对象。这让服务可以脱离引擎单独测试——
    手工 new 一个、塞几个桩对象进去就能测。
    """

    kind: ComponentKind = ComponentKind.SERVICE

    #: 我对外提供哪些能力（:class:`Cap` 常量集合）
    provides: FrozenSet[str] = frozenset()

    #: 我依赖哪些**服务名**（不是能力名）。引擎据此拓扑排序。
    depends_on: Tuple[str, ...] = ()

    #: 失败时怎么办
    restart_policy: RestartPolicy = RestartPolicy.NEVER

    def __init__(self) -> None:
        super().__init__()
        #: 依赖注入容器：{服务名: 服务实例}。由引擎在 initialize 前填好。
        self.services: Dict[str, Any] = {}
        #: 服务自身的可观测计数器（面板展示），子类自行维护
        self.service_stats: Dict[str, int] = {}

    # ---------------- 依赖注入 ----------------
    def bind_services(self, resolved: Dict[str, Any]) -> None:
        """引擎把解析好的依赖塞进来。**不要**在子类里重写。

        缺失的依赖不抛异常，只记 warning —— 一个可选依赖缺失
        不应该让整个引擎起不来（比如没配券商时交易服务仍应能启动，
        只是不能下单）。
        """
        self.services = dict(resolved or {})
        missing = [n for n in self.depends_on if n not in self.services]
        if missing:
            logger.warning(
                f"服务 {self.name} 的依赖未就绪: {missing}"
                f"（将以降级模式运行）"
            )

    def need(self, service_name: str) -> Any:
        """取一个必需依赖。**拿不到就抛**，让问题立刻暴露。

        为什么不用 ``self.services.get(...)`` 静默降级：
        静默降级的典型后果是"面板一切正常，但一笔单都没发出去"——
        这是实盘系统最贵的一类 bug。
        """
        svc = self.services.get(service_name)
        if svc is None:
            raise RuntimeError(
                f"服务 {self.name} 缺少必需依赖 {service_name!r}；"
                f"请在 depends_on 中声明它"
            )
        return svc

    def optional(self, service_name: str) -> Any:
        """取一个可选依赖，拿不到返回 None（用于"有则更好"的能力增强）。"""
        return self.services.get(service_name)

    # ---------------- 能力 ----------------
    def has_capability(self, capability: str) -> bool:
        return capability in self.provides

    def capabilities(self) -> List[str]:
        return sorted(self.provides)

    # ---------------- 可观测 ----------------
    def snapshot(self) -> Dict[str, Any]:
        """默认服务快照：能力 + 依赖 + 重启策略。

        子类应 ``super().snapshot()`` 后补自己的业务字段，
        这样面板对任何服务都有统一的"身份证"信息。
        """
        return {
            "kind": self.kind.value,
            "capabilities": self.capabilities(),
            "depends_on": list(self.depends_on),
            "restart_policy": self.restart_policy.value,
            "dependencies_ready": sorted(self.services.keys()),
            "service_stats": dict(self.service_stats),
        }

    def health_check(self) -> Tuple[bool, str]:
        """服务的健康 = 自己活着 **且** 必需依赖都在。

        只报自己活着是不够的：行情服务 RUNNING 但它依赖的采集服务挂了，
        面板上行情那格还是绿的，而行情其实是死的。
        """
        if not self.enabled:
            return True, "disabled"
        if self.state != "RUNNING":
            return False, self.state
        missing = [n for n in self.depends_on if n not in self.services]
        if missing:
            return False, f"依赖缺失: {','.join(missing)}"
        return True, "RUNNING"


# =============================================================================
# 5. StrategyComponent —— 消费能力、产出信号的一方
# =============================================================================
class StrategyComponent(IBaseComponent):
    """策略组件：**消费服务、产出订单**。

    它是引擎里"做决定"的那一层。特征：
      - 输入来自服务（行情服务推 bar、风控服务给裁决）；
      - 输出是订单事件，交给交易服务执行；
      - **自己不提供任何能力**给别人用 —— 所以它没有 provides。

    ``required_services`` 是**硬依赖**：缺一个就起不来。
    这与 ServiceComponent 的可选依赖不同——策略没有行情就是瞎子，
    降级运行毫无意义，不如直接失败让人来修。
    """

    kind: ComponentKind = ComponentKind.STRATEGY

    #: 必需的服务名。引擎启动时校验，缺失直接抛错（fail fast）。
    required_services: Tuple[str, ...] = ()

    #: 可选的服务名。声明了就注入，没装配就不给（消费方自己判断 None）。
    #: 与 required_services 的区别是**缺了不报错**——组合服务是典型例子：
    #: 没有它照样能交易，只是少了"跨任务额度预留"这道保护。
    optional_services: Tuple[str, ...] = ()

    def __init__(self) -> None:
        super().__init__()
        self.services: Dict[str, Any] = {}

    def bind_services(self, resolved: Dict[str, Any]) -> None:
        self.services = dict(resolved or {})
        missing = [n for n in self.required_services if n not in self.services]
        if missing:
            # 注意这里是 raise 而不是 warning —— 见类文档的理由
            raise RuntimeError(
                f"策略组件 {self.name} 缺少必需服务: {missing}"
            )

    def service(self, service_name: str) -> Any:
        if service_name not in self.services:
            raise RuntimeError(
                f"组件 {self.name} 未获得服务 {service_name!r}；"
                f"请在 required_services 中声明"
            )
        return self.services[service_name]

    # ---------------- 子类要实现的业务钩子 ----------------
    @abstractmethod
    def on_bar(self, bar: Any, **kwargs: Any) -> None:
        """行情输入。由子类驱动自己的任务/策略。"""
        raise NotImplementedError

    def on_signal_rejected(self, order: Any, reason: str = "") -> None:
        """订单被风控驳回时的回调（默认只记日志）。"""
        logger.info(
            f"[{self.name}] 订单被驳回 | {getattr(order, 'order_id', '?')} | {reason}"
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "required_services": list(self.required_services),
            "optional_services": list(self.optional_services),
            "services_bound": sorted(self.services.keys()),
        }


# =============================================================================
# 6. WorkerComponent —— 执行作业的一方
# =============================================================================
class WorkerComponent(IBaseComponent):
    """工作单元：**领活干活**的组件。

    与策略组件的区别：策略是"到点就该做决定"，
    工作单元是"有活就干，干完继续等"。它没有自己的节拍，
    节拍来自调度组件或外部队列。

    ``capacity`` 是并发槽位上限。它不是可选的装饰：
    采集类工作单元不设上限时，几十只票同时开抓会把数据源打到限流，
    然后所有请求一起超时——限流本身反而成了故障放大器。
    """

    kind: ComponentKind = ComponentKind.WORKER

    #: 并发槽位。<=0 表示不限制。
    capacity: int = 1

    def __init__(self) -> None:
        super().__init__()
        self.services: Dict[str, Any] = {}
        self._inflight: int = 0

    def bind_services(self, resolved: Dict[str, Any]) -> None:
        self.services = dict(resolved or {})

    # ---------------- 槽位管理 ----------------
    @property
    def inflight(self) -> int:
        return self._inflight

    def has_slot(self) -> bool:
        """还有没有空槽位。"""
        if self.capacity <= 0:
            return True
        return self._inflight < self.capacity

    def acquire(self) -> bool:
        """占一个槽位。占不到返回 False（调用方应稍后重试，不要硬闯）。"""
        if not self.has_slot():
            return False
        self._inflight += 1
        return True

    def release(self) -> None:
        if self._inflight > 0:
            self._inflight -= 1

    @abstractmethod
    def claim(self, job: Any) -> bool:
        """领一个作业。返回 True 表示接下了。"""
        raise NotImplementedError

    def is_busy(self) -> bool:
        """引擎空闲看门狗用：还有活在手上就不算空闲。"""
        return self._inflight > 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "capacity": self.capacity,
            "inflight": self._inflight,
            "services_bound": sorted(self.services.keys()),
        }


# =============================================================================
# 7. SchedulerComponent —— 决定执行节拍的一方
# =============================================================================
class SchedulerComponent(IBaseComponent):
    """调度组件：决定**什么时候**该跑什么。

    它是引擎的"心脏"。服务/策略/工作单元都只管"做什么"，
    什么时候做由它说了算。

    契约只有两条，任何调度实现都必须满足：
      * ``is_idle`` —— 没有待办、也没有在跑的，引擎才能判定空闲退出；
      * ``submit``  —— 往队列里放活。
    """

    kind: ComponentKind = ComponentKind.SCHEDULER

    def __init__(self) -> None:
        super().__init__()
        self.services: Dict[str, Any] = {}

    def bind_services(self, resolved: Dict[str, Any]) -> None:
        self.services = dict(resolved or {})

    # ---------------- 契约 ----------------
    @property
    def is_idle(self) -> bool:
        """是否空闲。默认按"没在跑"处理，子类应重写为真实队列判断。"""
        return True

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        """提交一个待调度项。默认实现抛错——调度器必须提供这个能力。"""
        raise NotImplementedError(
            f"{type(self).__name__} 必须实现 submit()"
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "idle": bool(self.is_idle),
        }


__all__ = [
    "ComponentKind",
    "KIND_LAYER",
    "Cap",
    "RestartPolicy",
    "ServiceComponent",
    "StrategyComponent",
    "WorkerComponent",
    "SchedulerComponent",
]
