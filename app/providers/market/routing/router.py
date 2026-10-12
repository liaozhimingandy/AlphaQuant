#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : router.py
# @Description : Data Router —— 采集层真正做决策的地方
#
#                四层决策（和文档里那张图一一对应）：
#
#                    数据请求
#                       │
#                       ▼
#                  ① 能力匹配     没有 Provider 声明这个能力 → NOT_SUPPORTED（不是失败）
#                       │
#                       ▼
#                  ② 排序         0.7×实测健康分 + 0.3×配置优先级；兜底源恒最后
#                       │
#                       ▼
#                  ③ 熔断检查     (provider, capability, route) 级熔断 / 半开探测
#                       │
#                       ▼
#                  ④ 执行         重试（仅可重试类型）→ 成功 → 质量校验
#                       │
#                  ┌────┴────┐
#                成功       失败
#                  │          │
#              质量校验    错误分类 → 换下一个
#                  │
#             ┌────┴────┐
#           通过      不通过（QUALITY_ERROR → 计入健康度 → 换下一个）
#
#                两个容易做错、这里刻意处理了的细节：
#
#                1. **兜底源分两轮**：本地库兜底几乎不会失败，
#                   如果放在同一个列表里按分排序，它迟早会涨到队首，
#                   变成"明明有网却一直用旧数据"—— 这比拿不到数据更难发现。
#                   所以正常源全部失败之后，才轮到兜底源。
#                2. **参数按适配器签名过滤**：路由手里的参数是为"所有源"准备的，
#                   而每个适配器只认自己那几个（资金榜要 rank_kind、
#                   人气榜要 trade_date）。全量透传会直接 TypeError。
#                   这里按签名裁剪，加新源不用回头改调用方。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import inspect
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

import pandas as pd

from app.providers.market.provider.base import (
    COOLDOWN_SECONDS,
    DataCapability,
    ProviderError,
    ProviderErrorType,
    Route,
    as_provider_error,
)
from app.providers.market.provider.health import ProviderState
from app.providers.market.routing.quality import QualityReport, check
from app.providers.market.provider.registry import ProviderRegistry
from app.common.logging import logger, log_throttled

#: 网络层（可选）。akshare/baostock 自己发请求，域名健康度只能靠回写。
def _net():
    try:
        from app.providers.market.transport import net
        return net
    except Exception:
        return None


@dataclass
class RouteAttempt:
    """一次尝试的记录（成功/失败/跳过都记）。

    为什么要记"跳过"：日志里只看到"所有源都失败"是没法行动的，
    必须能看到"这个源是被熔断跳过的、那个源依赖的域名已被判死"。
    """

    route: str
    provider: str
    capability: str
    ok: bool = False
    skipped: bool = False
    attempts: int = 0
    ms: int = 0
    rows: Optional[int] = None
    error_type: str = ""
    error: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route, "provider": self.provider,
            "capability": self.capability, "ok": self.ok,
            "skipped": self.skipped, "attempts": self.attempts,
            "ms": self.ms, "rows": self.rows,
            "error_type": self.error_type, "error": self.error[:300],
            "note": self.note,
        }


@dataclass
class RouteResult:
    """一次取数的完整结果 —— 数据 + 它从哪来 + 质量如何。"""

    capability: DataCapability
    route: str
    provider: str
    data: pd.DataFrame
    attempts: List[RouteAttempt] = field(default_factory=list)
    ms: int = 0
    rows: int = 0
    quality: Optional[QualityReport] = None
    raw_id: str = ""
    fetched_at: float = field(default_factory=time.time)
    fallback_used: bool = False
    request: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, with_data: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "capability": self.capability.value,
            "route": self.route, "provider": self.provider,
            "rows": self.rows, "ms": self.ms,
            "raw_id": self.raw_id, "fetched_at": self.fetched_at,
            "fallback_used": self.fallback_used,
            "quality": self.quality.to_dict() if self.quality else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "request": {k: str(v) for k, v in (self.request or {}).items()},
        }
        if with_data and self.data is not None:
            out["data"] = self.data.to_dict("records")
        return out


class DataRouter:
    """按能力取数：匹配 → 排序 → 熔断 → 执行 → 校验 → 换源。"""

    def __init__(self, registry: Optional[ProviderRegistry] = None,
                 raw_store: Any = None, retries: Optional[int] = None,
                 backoff_base: Optional[float] = None,
                 quality_check: Optional[bool] = None,
                 raw_enabled: Optional[bool] = None):
        self.registry = registry or ProviderRegistry()
        self.raw_store = raw_store
        cfg = self.registry.config

        def _v(key: str, default: Any, override: Any) -> Any:
            return cfg.router_value(key, default) if override is None else override

        self.retries = int(_v("retries", 3, retries))
        self.backoff_base = float(_v("backoff_base", 0.5, backoff_base))
        self.quality_check = bool(_v("quality_check", True, quality_check))
        self._raw_enabled = bool(_v("raw_enabled", True, raw_enabled))
        self.events: Deque[Dict[str, Any]] = deque(maxlen=100)
        self._sleep: Callable[[float], None] = time.sleep

    # ---------------- ① 能力匹配 ----------------
    def resolve(self, cap: Any) -> List[Route]:
        """支持该能力的路由（已排序）。"""
        return self.registry.ranking(cap)

    # ---------------- 取数 ----------------
    def fetch(self, cap: Any, **params: Any) -> RouteResult:
        """取一批数据，返回 :class:`RouteResult`。

        :param params: 交给适配器的参数；``source="xxx"`` 为保留键，
            表示"只走这个路由"（排障 / 单测用）。
        :raises ProviderError: 所有路由都失败（``type`` 会是占比最高的失败类型）
        """
        capability = DataCapability.parse(cap)
        source = str(params.pop("source", "") or "").strip()
        #: 保留参数：单次调用覆盖重试次数（批量兜底场景会传 0，避免放大请求数）
        tries = params.pop("retries", None)
        tries = self.retries if tries is None else max(0, int(tries))

        net = _net()
        if net is not None:
            # 采集前先摘掉"僵尸代理端口"。第三方库自己发请求不走我们的传输层，
            # 只有从环境变量层摘掉才能救它们。
            try:
                net.sanitize_proxy()
            except Exception:
                pass

        routes = self.resolve(capability)
        if not routes:
            raise ProviderError(
                f"{capability.value}: 没有任何数据源声明该能力"
                f"（已注册能力: {sorted(self.registry.capabilities())}）",
                type=ProviderErrorType.NOT_SUPPORTED, capability=capability)

        if source:
            routes = [r for r in routes if r.name == source]
            if not routes:
                raise ProviderError(
                    f"未注册的路由 {source!r}；{capability.value} 可用: "
                    f"{[r.name for r in self.registry.routes_for(capability)]}",
                    type=ProviderErrorType.INVALID_REQUEST,
                    capability=capability, route=source)

        t_start = time.monotonic()
        attempts: List[RouteAttempt] = []

        # ---- 兜底源分两轮（见文件头注释） ----
        groups: Sequence[tuple] = (
            ([r for r in routes if not r.fallback], False),
            ([r for r in routes if r.fallback], True),
        )
        for group, is_fallback in groups:
            for route in group:
                result = self._try_route(route, capability, params, attempts,
                                         is_fallback, t_start, tries)
                if result is not None:
                    return result

        # ---- 全部用尽 ----
        detail = " | ".join(
            f"{a.route}: {a.note or a.error_type or a.error}"[:160] for a in attempts
        ) or "无可用路由"
        netish = sum(1 for a in attempts
                     if a.error_type in (ProviderErrorType.NETWORK_ERROR.value,
                                         ProviderErrorType.TIMEOUT.value))
        hint = ""
        try:
            from app.providers.market.compat.registry import network_hint
            hint = " || " + network_hint()
        except Exception:
            pass
        worst = self._worst_type(attempts)
        log_throttled(
            "WARNING",
            f"路由[{capability.value}] 失败：{len(routes)} 条路由已全部用尽 -> {detail}"
            + (f"（其中 {netish} 条是网络层失败）" if netish else "") + hint,
            key=f"route-exhausted-{capability.value}")
        raise ProviderError(
            f"{capability.value}: 所有数据源均失败 -> {detail}{hint}",
            type=worst, capability=capability)

    def fetch_frame(self, cap: Any, **params: Any) -> pd.DataFrame:
        """只要数据（不要血缘/质量报告）时的便捷写法。"""
        return self.fetch(cap, **params).data

    # ---------------- 单条路由 ----------------
    def _try_route(self, route: Route, capability: DataCapability,
                   params: Dict[str, Any], attempts: List[RouteAttempt],
                   is_fallback: bool, t_start: float,
                   retries: Optional[int] = None) -> Optional[RouteResult]:
        health = self.registry.health
        key = route.key

        # ③ 熔断检查
        breaker = health.breaker(key)
        if not breaker.begin():
            cb = breaker.to_dict()
            note = (f"熔断中(剩{int(cb['remaining'])}s)"
                    if cb["state"] == ProviderState.OPEN.value
                    else "半开探测已占用")
            attempts.append(RouteAttempt(
                route=route.name, provider=route.provider,
                capability=capability.value, skipped=True,
                note=note, error_type="", error=cb["last_reason"]))
            logger.info(f"路由[{capability.value}] · {route.name} {note}，跳过")
            return None

        # 域名级快速跳过：源自己不知道网络状况，路由器知道
        bad = self._blocked_hosts(route)
        if bad:
            attempts.append(RouteAttempt(
                route=route.name, provider=route.provider,
                capability=capability.value, skipped=True,
                note=f"依赖域名不可达({bad})"))
            logger.info(f"路由[{capability.value}] · {route.name} 依赖域名暂不可达"
                        f" -> {bad}，直接跳过")
            return None

        # ④ 执行
        bound = self._bind(route, params)
        last: Optional[ProviderError] = None
        tries = self.retries if retries is None else max(0, int(retries))
        for attempt in range(tries + 1):
            t0 = time.monotonic()
            try:
                df = route.handler(**bound)
            except BaseException as exc:  # noqa: BLE001 - 必须兜住第三方库的任何异常
                perr = as_provider_error(exc, provider=route.provider,
                                         capability=capability, route=route.name)
                if perr.retryable and attempt < tries:
                    wait = self._backoff(attempt)
                    logger.warning(
                        f"路由[{capability.value}] · {route.name} 第"
                        f"{attempt + 1}/{tries + 1} 次失败"
                        f"（{perr.type.value}·可重试）: {str(perr)[:110]}"
                        f" → {wait:.1f}s 后重试")
                    self._sleep(wait)
                    continue
                logger.info(
                    f"路由[{capability.value}] · {route.name} 失败"
                    f"（{perr.type.value}·不重试）: {str(perr)[:110]}")
                last = perr
                break

            # ---- 拿到了 ----
            ms = int((time.monotonic() - t0) * 1000)
            if df is None or (hasattr(df, "empty") and df.empty):
                last = ProviderError(
                    f"{route.name} 返回空数据", type=ProviderErrorType.NO_DATA,
                    provider=route.provider, capability=capability,
                    route=route.name)
                logger.info(f"路由[{capability.value}] · {route.name} 返回空数据，"
                            f"换下一个源")
                break

            # ---- 质量校验 ----
            quality = check(capability, df) if self.quality_check else None
            if quality is not None and not quality.ok:
                last = ProviderError(
                    f"{route.name} 数据校验不通过: {quality.summary()}",
                    type=ProviderErrorType.QUALITY_ERROR,
                    provider=route.provider, capability=capability,
                    route=route.name)
                logger.warning(
                    f"路由[{capability.value}] · {route.name} 取到 {len(df)} 行，"
                    f"但质量校验不通过（{quality.summary()}）→ 换下一个源")
                break

            # ---- 成功 ----
            rows = len(df)
            self._mark_hosts(route, True, "")
            health.record(key, True, ms=ms)
            raw_id = self._save_raw(route, capability, bound, df)
            result = RouteResult(
                capability=capability, route=route.name, provider=route.provider,
                data=df, attempts=attempts + [RouteAttempt(
                    route=route.name, provider=route.provider,
                    capability=capability.value, ok=True,
                    attempts=attempt + 1, ms=ms, rows=rows)],
                ms=int((time.monotonic() - t_start) * 1000), rows=rows,
                quality=quality, raw_id=raw_id, fallback_used=is_fallback,
                request=dict(bound))
            self.events.append({
                "capability": capability.value, "route": route.name,
                "provider": route.provider, "ok": True, "rows": rows,
                "ms": result.ms, "at": result.fetched_at,
                "fallback": is_fallback,
            })
            self._mirror(route, capability, True, ms=ms, rows=rows)
            logger.info(
                f"路由[{capability.value}] 成功 · **{route.name}**"
                f"（{route.provider}，第{attempt + 1}次尝试，{ms}ms，{rows} 行）"
                + ("【兜底源】" if is_fallback else ""))
            return result

        # ---- 该路由失败：分类 → 回写健康度 ----
        assert last is not None
        if last.penalize:
            # 解析失败时**不回写域名健康度**：响应其实拿到了，只是结构对不上。
            # 回写了就会把"接口改版"记成"域名不可达"，后续几批请求因域名
            # 冷却被整批跳过，症状看起来像断网（详见 registry.is_parse_error）。
            if last.type is not ProviderErrorType.PARSE_ERROR:
                self._mark_hosts(route, False, str(last))
            health.record(key, False, ms=0, error=str(last),
                          error_type=last.type.value,
                          extra_cooldown=COOLDOWN_SECONDS.get(last.type, 0.0))
        else:
            # 无数据 / 不支持 / 参数错误：源没坏，中性记录，不惩罚
            health.record(key, None, error=str(last), error_type=last.type.value)
        attempts.append(RouteAttempt(
            route=route.name, provider=route.provider, capability=capability.value,
            ok=False, attempts=tries + 1, error_type=last.type.value,
            error=str(last)[:300]))
        self._mirror(route, capability, False, error=str(last))
        return None

    # ---------------- 旧链路镜像（迁移期） ----------------
    @staticmethod
    def _mirror(route: Route, capability: DataCapability, ok: bool, *,
                ms: int = 0, rows: Optional[int] = None, error: str = "") -> None:
        """把本次采集**镜像**一份给旧注册表 ``sources.REGISTRY``。

        为什么需要这个：面板、候选池报告、数据校验报告都在读
        ``REGISTRY.used_sources() / recent_events()``（"这批数据来自哪个源"）。
        迁移期内如果新链路不留痕，这些地方会显示成"从未取到过数据"，
        反而比迁移前更不可观测。等旧链路彻底下线，删掉本方法即可。

        只镜像事件，**不镜像权重**：权重是旧链路自己的排序依据，
        新链路用健康分排序，两套数字叠在一起只会让人看不懂。
        """
        try:
            from app.providers.market.compat.registry import REGISTRY, FetchEvent

            spec = REGISTRY._specs.get(route.name)
            if spec is None:
                return
            kind = "daily" if capability is DataCapability.DAILY_KLINE \
                else capability.value
            REGISTRY.events.append(FetchEvent(
                kind=kind, source=route.name, ok=ok, attempts=1,
                rows=rows, ms=ms, error=error[:300]))
            if ok:
                spec.last_ok = time.time()
                spec.fail_count = 0
                spec.dead_until = 0.0
                spec.last_error = ""
            else:
                spec.last_error = error[:200]
        except Exception:
            pass

    # ---------------- 辅助 ----------------
    @staticmethod
    def _bind(route: Route, params: Dict[str, Any]) -> Dict[str, Any]:
        """按适配器签名裁剪参数。

        路由手里的参数是"给所有源的并集"（人气榜要 trade_date、
        资金榜要 rank_kind），全量透传会让只认 top 的适配器直接 TypeError。
        """
        try:
            sig = inspect.signature(route.handler)
        except (TypeError, ValueError):
            return dict(params)
        for p in sig.parameters.values():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                return dict(params)
        accepted = {name for name, p in sig.parameters.items()
                    if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  inspect.Parameter.KEYWORD_ONLY)}
        return {k: v for k, v in params.items() if k in accepted}

    def _backoff(self, attempt: int) -> float:
        return (2 ** attempt) * self.backoff_base + random.uniform(0, 0.3)

    def _blocked_hosts(self, route: Route) -> str:
        if not route.hosts:
            return ""
        net = _net()
        if net is None:
            return ""
        try:
            return net.blocked_reason(sorted(route.hosts))
        except Exception:
            return ""

    def _mark_hosts(self, route: Route, ok: bool, why: str) -> None:
        """回写域名健康度。

        必须的：akshare/baostock 在自己内部发请求，不走我们的传输层，
        网络层看不到那些失败。不回写，"域名级跳过"就只是个摆设。
        """
        if not route.hosts:
            return
        net = _net()
        if net is None:
            return
        for host in route.hosts:
            try:
                net.mark(host, ok, why[:200])
            except Exception:
                pass

    def _save_raw(self, route: Route, capability: DataCapability,
                  params: Dict[str, Any], df: pd.DataFrame) -> str:
        if not self._raw_enabled or self.raw_store is None:
            return ""
        try:
            return self.raw_store.save(
                provider=route.provider, capability=capability.value,
                route=route.name, params=params, frame=df) or ""
        except Exception as exc:
            logger.debug(f"Raw 层存档失败（忽略）: {exc}")
            return ""

    @staticmethod
    def _worst_type(attempts: List[RouteAttempt]) -> ProviderErrorType:
        """所有源都失败时，异常该标成什么类型 —— 取"最该让人警惕"的那个。"""
        types = [a.error_type for a in attempts if a.error_type]
        if not types:
            return ProviderErrorType.UNKNOWN
        for name in (ProviderErrorType.BLOCKED.value,
                     ProviderErrorType.RATE_LIMIT.value,
                     ProviderErrorType.AUTH_ERROR.value,
                     ProviderErrorType.NETWORK_ERROR.value,
                     ProviderErrorType.TIMEOUT.value,
                     ProviderErrorType.SERVER_ERROR.value,
                     ProviderErrorType.QUALITY_ERROR.value,
                     ProviderErrorType.PARSE_ERROR.value):
            if name in types:
                return ProviderErrorType(name)
        return ProviderErrorType(types[0])

    # ---------------- 可观测 ----------------
    def recent_events(self, n: int = 20) -> List[Dict[str, Any]]:
        return list(self.events)[-int(n):]

    def used_sources(self) -> Dict[str, str]:
        """每种能力最近一次**成功**使用的路由：{capability: route}。"""
        out: Dict[str, str] = {}
        for e in self.events:
            if e.get("ok"):
                out[str(e.get("capability"))] = str(e.get("route"))
        return out

    def reset(self) -> None:
        self.registry.reset()
        self.events.clear()


__all__ = ["DataRouter", "RouteAttempt", "RouteResult"]
