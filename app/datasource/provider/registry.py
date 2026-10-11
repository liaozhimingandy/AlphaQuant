#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : Provider 注册表：注册 → 按能力查 → 排序
#
#                排序规则（这条要能被一眼复述出来）：
#
#                    最终分 = 0.7 × 实测健康分 + 0.3 × 配置优先级
#
#                  * **健康分**（0~100）：成功率 60% + 延迟 25% + 新鲜度 15%，
#                    是**跑出来的**，比人写的配置更贴近此刻的网络状况；
#                  * **配置优先级**：初始大家都没数据（健康分都是 100），
#                    此时顺序完全由 config/providers.json 决定 —— 可预期；
#                  * **兜底源恒排最后**，且只在所有正常源都失败后才尝试（见 router）。
#
#                为什么不让配置优先级占更大权重：
#                  "被封的源还一直排第一" 是最浪费请求额度的失败模式。
#                  一个连续失败三次的源健康分会掉到 40 以下，
#                  0.7×40+0.3×90 = 55，会被 0.7×100+0.3×60 = 88 的正常源挤到后面。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from app.datasource.provider.base import (
    DataCapability,
    DataProvider,
    Route,
)
from app.datasource.routing.config import ProviderConfig, load_config
from app.datasource.provider.health import HealthBook
from app.common.logging import logger

#: 健康分 / 配置优先级 的合成权重
W_HEALTH: float = 0.7
W_PRIORITY: float = 0.3


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


class ProviderRegistry:
    """所有 Provider 与 Route 的登记处。"""

    def __init__(self, config: Optional[ProviderConfig] = None,
                 health: Optional[HealthBook] = None):
        self._providers: Dict[str, DataProvider] = {}
        self._routes: Dict[str, Route] = {}
        self._order: List[str] = []            # route 注册顺序
        self._provider_order: List[str] = []   # provider 注册顺序
        self.config: ProviderConfig = config or load_config()
        self.health: HealthBook = health or HealthBook(
            threshold=int(self.config.router_value("circuit_threshold", 3)),
            cooldown=float(self.config.router_value("cooldown", 300.0)),
        )

    # ---------------- 配置 ----------------
    def reload_config(self, path: Optional[str] = None) -> ProviderConfig:
        """重读配置并**回写到已注册的路由**（热更新，不用重启）。"""
        self.config = load_config(path)
        self.health.threshold = int(self.config.router_value("circuit_threshold", 3))
        self.health.cooldown = float(self.config.router_value("cooldown", 300.0))
        for route in self._routes.values():
            route.enabled = self.config.enabled(route.provider)
        return self.config

    # ---------------- 注册 ----------------
    def register(self, provider: Any, *, enabled: Optional[bool] = None,
                 priority: Optional[int] = None) -> List[Route]:
        """注册一个 Provider（实例或类）。返回它带出来的 Route 列表。"""
        if isinstance(provider, type):
            provider = provider()
        if not isinstance(provider, DataProvider):
            raise TypeError(f"Provider 必须是 DataProvider 实例，收到 {type(provider)}")
        name = provider.name or type(provider).__name__.lower()
        if not name:
            raise ValueError("Provider 必须有 name")

        if priority is not None:
            provider.priority = int(priority)
        self._providers[name] = provider
        if name not in self._provider_order:
            self._provider_order.append(name)

        added: List[Route] = []
        for route in provider.routes():
            route.enabled = (self.config.enabled(route.provider)
                             if enabled is None else bool(enabled))
            self._routes[route.name] = route
            if route.name not in self._order:
                self._order.append(route.name)
            added.append(route)
        logger.debug(
            f"注册数据源 {name}，能力: "
            f"{sorted(c.value for c in provider.capabilities)}，"
            f"路由: {[r.name for r in added]}"
        )
        return added

    def add_route(self, route: Route) -> Route:
        """直接登记一条路由（**插件 / 临时源 / 单测**用）。

        正常路径是 ``register(provider)``，由 Provider 声明能力后自动带出路由；
        这里留给"只想临时加一条取数路径"的场合 —— 比如单测里造一个
        "必挂的源"验证换源逻辑，没必要为此写一个 Provider 类。
        """
        self._routes[route.name] = route
        if route.name not in self._order:
            self._order.append(route.name)
        return route

    def unregister(self, name: str) -> None:
        """按 Provider 名或 Route 名卸载。"""
        self._providers.pop(name, None)
        self._routes.pop(name, None)
        if name in self._order:
            self._order.remove(name)
        if name in self._provider_order:
            self._provider_order.remove(name)
        for rname in [r for r, route in self._routes.items()
                      if route.provider == name]:
            self._routes.pop(rname, None)
            if rname in self._order:
                self._order.remove(rname)

    # ---------------- 查询 ----------------
    def providers(self) -> List[DataProvider]:
        return [self._providers[n] for n in self._provider_order
                if n in self._providers]

    def provider(self, name: str) -> Optional[DataProvider]:
        return self._providers.get(name)

    def route(self, name: str) -> Optional[Route]:
        return self._routes.get(name)

    def routes(self) -> List[Route]:
        return [self._routes[n] for n in self._order if n in self._routes]

    def routes_for(self, cap: Any) -> List[Route]:
        """支持该能力且启用的路由（**未排序**）。"""
        capability = DataCapability.parse(cap)
        return [r for r in self.routes()
                if r.capability is capability and r.enabled]

    def ranking(self, cap: Any, now: Optional[float] = None) -> List[Route]:
        """按"最终分"降序排好的路由列表（兜底源排最后）。"""
        routes = self.routes_for(cap)
        if not routes:
            return []
        capability = DataCapability.parse(cap)
        order = self.config.route_order(capability.value)
        now = now or time.time()

        def _position(route: Route) -> int:
            """配置里写了位置就用配置，没写就排在配置之后（按注册顺序）。"""
            if route.name in order:
                return order.index(route.name)
            return len(order) + (self._order.index(route.name)
                                 if route.name in self._order else 999)

        def _priority(route: Route) -> int:
            """路由优先级（0~100）。

            **写进 routes 的按位置给分**（第 1 个 100、第 2 个 90 …）：
            配置顺序是人基于现场网络写的判断，在还没跑出健康分之前它说了算。
            没写进 routes 的（新加的源还没来得及配）用 provider 配置的优先级，
            并**封顶 50** —— 让它排在已配置的后面，避免"刚加的新源抢在前面"。
            """
            if route.name in order:
                return max(10, 100 - 10 * order.index(route.name))
            # 未列出：用 provider 配置的优先级（没配就用路由自带的），封顶 50
            return min(50, self.config.priority(route.provider, route.priority))

        def _rank(route: Route):
            return (1 if route.fallback else 0,
                    -round(self.final_score(route, now, order), 3),
                    _position(route))

        return sorted(routes, key=_rank)

    def final_score(self, route: Route, now: Optional[float] = None,
                    order: Optional[List[str]] = None) -> float:
        """路由的最终排序分 = 0.7×实测健康分 + 0.3×配置优先级。"""
        if order is None:
            order = self.config.route_order(route.capability.value)
        if route.name in order:
            priority = max(10, 100 - 10 * order.index(route.name))
        else:
            priority = min(50, self.config.priority(route.provider, route.priority))
        return (W_HEALTH * _clamp_score(self.health.score(route.key, now))
                + W_PRIORITY * _clamp_score(priority))

    def capabilities(self) -> Dict[str, List[str]]:
        """{能力: [路由名]} —— 回答"哪类数据有几个源能用"。"""
        out: Dict[str, List[str]] = {}
        for route in self.routes():
            out.setdefault(route.capability.value, []).append(route.name)
        return out

    # ---------------- 可观测 ----------------
    def snapshot(self) -> List[Dict[str, Any]]:
        """给面板 / CLI 用：每条路由的配置 + 健康 + 熔断状态。"""
        out: List[Dict[str, Any]] = []
        for route in self.routes():
            entry = route.to_dict()
            entry.update(self.health.stats(route.key).to_dict())
            entry["circuit"] = self.health.breaker(route.key).to_dict()
            entry["final_score"] = round(self.final_score(route), 2)
            out.append(entry)
        return out

    def health_snapshot(self) -> List[Dict[str, Any]]:
        return self.health.snapshot()

    def reset(self) -> None:
        """清空健康与熔断（测试 / 手动恢复时用）。配置保留。"""
        self.health.reset()


__all__ = ["ProviderRegistry", "W_HEALTH", "W_PRIORITY"]
