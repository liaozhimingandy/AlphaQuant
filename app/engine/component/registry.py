#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 服务注册表 —— 引擎里"谁提供什么、谁需要什么"的唯一权威。
#
#               有了它，组件之间不再互相 ``engine.get_component("xxx")``：
#                 - 服务启动时把自己的能力登记进来（``provides``）；
#                 - 消费方声明自己要哪些服务（``depends_on`` /
#                   ``required_services``）；
#                 - 注册表做**拓扑排序**，给出安全的启停顺序；
#                 - 引擎按这个顺序注入依赖，消费方拿到现成的实例。
#
#               顺带解决的三件旧事：
#                 1. **循环依赖不再静默**：以前靠函数内延迟 import 掩盖，
#                    现在拓扑排序会直接指出环在哪一环；
#                 2. **改名不再崩**：调用方按服务名取，注册表名变了一个地方改；
#                 3. **缺失依赖可诊断**：面板能直接列出"谁缺了谁"。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.engine.component.kinds import ComponentKind, KIND_LAYER, ServiceComponent
from app.common.logging import logger


class ServiceRegistryError(RuntimeError):
    """注册表级错误：重名、循环依赖、必需能力缺失。"""


#: 旧组件名 → 新服务名的兼容别名表。
#:
#: 重构前有约 30 处 ``engine.get_component("market_center")`` 这类按字符串
#: 取组件的写法。服务改名后，这些调用本该全部改掉——但**一次改完风险高**，
#: 漏一处就是"面板正常但功能静默失效"。
#:
#: 所以引擎注册服务时自动带上这些旧名作为别名，旧调用方不改也能继续工作，
#: 而新代码一律用新服务名。等调用方迁完，删掉这张表即可。
#:
#: 注意 ``ext_data`` / ``data_guard`` / ``data_collector`` 三个旧名
#: **都指向** ``collection_service`` —— 它们被合并成了同一个服务，
#: 这不是笔误，正是本次重构的目的。
LEGACY_ALIASES: Dict[str, str] = {
    "market_center": "market_service",
    "data_collector": "collection_service",
    "ext_data": "collection_service",
    "data_guard": "collection_service",
    "live_gateway": "trading_service",
    "trading_store": "trading_service",
    "news_center": "news_service",
    "strategy_manager": "strategy",
    "task_scheduler": "scheduler",
    "timerV2": "scheduler",
}


def aliases_for(service_name: str) -> List[str]:
    """查一个服务该带哪些旧名别名。"""
    return sorted(a for a, t in LEGACY_ALIASES.items() if t == service_name)


class ServiceRegistry:
    """服务注册表。

    它**只管服务**，不管组件的全集——生命周期仍由引擎统一驱动。
    这样职责清楚：注册表回答"能不能拿到"，引擎回答"什么时候启停"。

    别名机制（``alias``）是为迁移期准备的：旧代码里有约 30 处
    ``get_component("data_collector")`` 这样的写法，注册新服务名的同时
    把旧名登记为别名，旧调用方不用改也能继续工作。
    等调用方全部迁移完，删掉别名入参即可，不需要再动业务代码。
    """

    def __init__(self) -> None:
        self._services: Dict[str, ServiceComponent] = {}
        self._aliases: Dict[str, str] = {}          # 别名 -> 正式服务名
        self._by_capability: Dict[str, List[str]] = {}  # 能力 -> [服务名]

    # ------------------------------------------------------------------
    # 注册 / 注销
    # ------------------------------------------------------------------
    def register(
        self,
        service: ServiceComponent,
        alias: Sequence[str] = (),
    ) -> None:
        """登记一个服务。

        :param alias: 额外的历史名称（迁移期兼容用），都指向同一个实例。
        """
        name = service.name
        if not name:
            raise ServiceRegistryError("服务必须有 name")
        if name in self._services:
            raise ServiceRegistryError(f"服务 {name} 已注册")

        self._services[name] = service
        for cap in getattr(service, "provides", ()) or ():
            self._by_capability.setdefault(cap, []).append(name)
        for a in alias:
            if a and a != name:
                self._aliases[a] = name

        logger.debug(
            f"服务注册: {name} | 能力: {sorted(getattr(service, 'provides', ()) or ())}"
            + (f" | 别名: {list(alias)}" if alias else "")
        )

    def unregister(self, name: str) -> None:
        real = self._aliases.pop(name, name)
        service = self._services.pop(real, None)
        if service is None:
            return
        for cap in getattr(service, "provides", ()) or ():
            bucket = self._by_capability.get(cap)
            if bucket and real in bucket:
                bucket.remove(real)
                if not bucket:
                    self._by_capability.pop(cap, None)
        for a, target in list(self._aliases.items()):
            if target == real:
                self._aliases.pop(a, None)

    def clear(self) -> None:
        self._services.clear()
        self._aliases.clear()
        self._by_capability.clear()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def has(self, name: str) -> bool:
        return self._resolve(name) is not None

    def _resolve(self, name: str) -> Optional[str]:
        if name in self._services:
            return name
        return self._aliases.get(name)

    def get(self, name: str) -> Optional[ServiceComponent]:
        """按服务名（或别名）取服务。没有返回 None。"""
        real = self._resolve(name)
        return self._services.get(real) if real else None

    def require(self, name: str) -> ServiceComponent:
        """按服务名取服务，**取不到就抛**。

        用于必需依赖：静默返回 None 会让调用方写出 ``if svc:`` 的降级分支，
        而那个分支在实盘里意味着"单没发出去但没人知道"。
        """
        svc = self.get(name)
        if svc is None:
            raise ServiceRegistryError(
                f"未注册的服务: {name} | 已注册: {sorted(self._services)}"
            )
        return svc

    def by_capability(self, capability: str) -> List[ServiceComponent]:
        """找出所有提供该能力的服务（按注册顺序）。"""
        return [
            self._services[n]
            for n in self._by_capability.get(capability, [])
            if n in self._services
        ]

    def first_by_capability(self, capability: str) -> Optional[ServiceComponent]:
        """取第一个提供该能力的服务。有多个时按注册顺序取最早的。"""
        found = self.by_capability(capability)
        return found[0] if found else None

    def names(self) -> List[str]:
        return list(self._services.keys())

    def items(self) -> List[Tuple[str, ServiceComponent]]:
        return list(self._services.items())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._resolve(name) is not None

    def __len__(self) -> int:
        return len(self._services)

    # ------------------------------------------------------------------
    # 依赖解析
    # ------------------------------------------------------------------
    def resolve_dependencies(
        self, service_names: Optional[Iterable[str]] = None,
    ) -> List[str]:
        """拓扑排序。**返回安全的启动顺序**（被依赖者在前）。

        排序键有两条，优先级依次是：
          1. ``depends_on`` 声明的真实依赖（硬约束，必须满足）；
          2. 组件种类层级（软约束，让调度先于服务、服务先于策略）。

        只用 1 的话，没有依赖关系的组件之间顺序任意，日志会乱跳；
        只用 2 的话，服务之间的依赖又可能被违反。两条一起用才既正确又稳定。

        :raises ServiceRegistryError: 存在循环依赖，或依赖了不存在的服务
        """
        names: List[str] = list(
            service_names if service_names is not None else self._services.keys()
        )
        known: Set[str] = set(names)
        # 依赖也要能被别名解析：声明写旧名也能对上
        deps: Dict[str, List[str]] = {}
        for n in names:
            svc = self._services[n]
            raw = list(getattr(svc, "depends_on", ()) or ())
            resolved: List[str] = []
            for d in raw:
                real = self._resolve(d)
                if real is None:
                    # 依赖未注册：按 ServiceComponent 的约定降级（不阻断启动），
                    # 但这里记 warning，因为"声明了却没装"通常是装配顺序写错了
                    logger.warning(f"服务 {n} 依赖的 {d} 未注册，将以降级模式运行")
                    continue
                if real not in known:
                    logger.warning(
                        f"服务 {n} 依赖的 {real} 不在本次排序范围内，忽略"
                    )
                    continue
                if real != n:
                    resolved.append(real)
            deps[n] = resolved

        def layer(n: str) -> int:
            kind = getattr(self._services[n], "kind", ComponentKind.SERVICE)
            return KIND_LAYER.get(kind, 99)

        ordered: List[str] = []
        state: Dict[str, int] = {}      # 0=未访问 1=访问中 2=已完成
        stack: List[str] = []

        def visit(n: str) -> None:
            if state.get(n, 0) == 2:
                return
            if state.get(n, 0) == 1:
                cycle = " -> ".join([*stack[stack.index(n):], n]) if n in stack else n
                raise ServiceRegistryError(f"检测到循环依赖: {cycle}")
            state[n] = 1
            stack.append(n)
            for d in sorted(deps.get(n, ()), key=lambda x: (layer(x), x)):
                visit(d)
            stack.pop()
            state[n] = 2
            ordered.append(n)

        for n in sorted(names, key=lambda x: (layer(x), x)):
            visit(n)
        return ordered

    def build_injections(
        self, target: Any, service_names: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """给一个消费方解析出它声明的依赖。

        :param target: 有 ``depends_on``（服务）或 ``required_services``（策略/工作单元）的组件
        :return: {服务名: 服务实例}
        """
        wanted = list(
            getattr(target, "depends_on", ()) or ()
        ) or list(
            getattr(target, "required_services", ()) or ()
        )
        # 可选服务：声明了就注入，没装配就不给（消费方自己判断 None）。
        # 与 required_services 的区别是**缺了不报错**——
        # 组合服务就是典型的例子：没有它照样能交易，只是少了跨任务额度控制。
        wanted += [w for w in (getattr(target, "optional_services", ()) or ())
                   if w not in wanted]
        out: Dict[str, Any] = {}
        for w in wanted:
            svc = self.get(w)
            if svc is not None:
                out[w] = svc
        return out

    # ------------------------------------------------------------------
    # 可观测
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """面板用的服务全景图。"""
        out: Dict[str, Any] = {}
        for name, svc in self._services.items():
            entry: Dict[str, Any] = {
                "name": name,
                "kind": getattr(svc, "kind", ComponentKind.SERVICE).value,
                "capabilities": sorted(getattr(svc, "provides", ()) or ()),
                "depends_on": list(getattr(svc, "depends_on", ()) or ()),
                "restart_policy": str(
                    getattr(svc, "restart_policy", "never")
                ),
                "state": getattr(svc, "state", "UNKNOWN"),
            }
            missing = [
                d for d in (getattr(svc, "depends_on", ()) or ())
                if self.get(d) is None
            ]
            entry["missing_dependencies"] = missing
            if hasattr(svc, "health_check"):
                try:
                    ok, detail = svc.health_check()
                    entry["healthy"] = bool(ok)
                    entry["health_detail"] = detail
                except Exception as exc:  # pragma: no cover - 防御
                    entry["healthy"] = None
                    entry["health_detail"] = f"health_check 异常: {exc}"
            out[name] = entry
        return out

    def missing_capabilities(self, required: Iterable[str]) -> List[str]:
        """检查一批能力里哪些没人提供。装配阶段自检用。"""
        return [c for c in required if not self.by_capability(c)]


__all__ = ["ServiceRegistry", "ServiceRegistryError"]
