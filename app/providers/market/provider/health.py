#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : health.py
# @Description : 健康度 + 熔断器（**能力级**，不是数据源级）
#
#                为什么熔断键必须带上 capability：
#
#                    akshare 的 K 线被限流  ≠  akshare 的人气榜也用不了
#
#                东财 push2his 和 emappdata 是两套完全不同的风控面，
#                实测里就出现过"日线被拦、人气榜正常"。
#                如果把整个 akshare 判死，等于白白扔掉一个还能用的能力。
#                所以熔断器的键是 ``(provider, capability, route)``。
#
#                状态机（和电路熔断一个道理）：
#
#                    HEALTHY ──连续失败≥阈值──▶ OPEN ──冷却结束──▶ HALF_OPEN
#                       ▲                                            │
#                       └──────────成功◀──探测成功────────────────────┘
#                                          │
#                                       探测失败
#                                          ▼
#                                        OPEN（重新计时）
#
#                HALF_OPEN 只放**一个**探测请求进去。放两个的话，
#                源刚好在被封的边缘，两个请求一起失败会让它永远起不来。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 延迟评分的"满分线"：8 秒以上才扣到最低（最低也给 0.5，慢不等于坏）
_LATENCY_FULL_MS: float = 8000.0
#: 恢复半衰期（秒）：距**上次失败** 30 分钟，恢复度涨回一半
_RECOVERY_HALF_LIFE: float = 1800.0


class ProviderState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"
    HALF_OPEN = "half_open"

    @property
    def label(self) -> str:
        return {
            "healthy": "健康", "degraded": "降级",
            "open": "熔断中", "half_open": "半开探测",
        }[self.value]


@dataclass
class HealthStats:
    """一个路由的统计。用于**排序**，不用于判断死活（那是熔断器的活）。"""

    calls: int = 0
    ok: int = 0
    fail: int = 0
    neutral: int = 0                 # 无数据/不支持：不算成功也不算失败
    consecutive_fail: int = 0
    latency_ms: float = 0.0          # 延迟 EMA
    last_ok_at: float = 0.0
    last_fail_at: float = 0.0
    last_error: str = ""
    last_error_type: str = ""

    @property
    def success_rate(self) -> float:
        """成功率。没打过交道时给 1.0（**乐观起步**）——
        一个刚注册的源不该因为"还没证明自己"就被排到最后。"""
        total = self.ok + self.fail
        if total == 0:
            return 1.0
        return self.ok / total

    def recovery(self, now: Optional[float] = None) -> float:
        """恢复度 0~1：距**上次失败**过去多久（半衰期 30 分钟）。

        刻意用"距上次失败"而不是"距上次成功"：
        后者会让一个**一直好好干活**的源随着时间自己掉分，
        于是一个从未被验证过的源（满分 100）就能把它挤下去 ——
        表现为"配好的首选源莫名其妙换人了"。
        恢复度的职责只有一个：让**坏过的源**慢慢涨回来，而不是惩罚好源。
        """
        if self.last_fail_at <= 0:
            return 1.0
        age = max(0.0, (now if now is not None else time.time()) - self.last_fail_at)
        return 0.5 ** (age / _RECOVERY_HALF_LIFE)

    def latency_score(self) -> float:
        """延迟分 0.5~1。慢要扣分，但**慢不等于坏** —— 下限 0.5。"""
        if self.latency_ms <= 0:
            return 1.0
        return max(0.5, min(1.0, 1.0 - self.latency_ms / _LATENCY_FULL_MS))

    def score(self, now: Optional[float] = None) -> float:
        """综合健康分 0~100。

        三件事按经验权重合成：
          * 成功率 70% —— 最硬的指标，坏过一次就要付出明显代价；
          * 恢复度 20% —— 坏过的源慢慢涨回来（半衰期 30 分钟）；
          * 延迟 10% —— 慢会扣分，但权重刻意压低：
                        慢的源仍然可能是对的源，不该因为多花 1 秒就被换掉。
        """
        return round(100.0 * (0.70 * self.success_rate
                              + 0.20 * self.recovery(now)
                              + 0.10 * self.latency_score()), 2)

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        return {
            "calls": self.calls, "ok": self.ok, "fail": self.fail,
            "neutral": self.neutral,
            "success_rate": round(self.success_rate, 3),
            "consecutive_fail": self.consecutive_fail,
            "latency_ms": round(self.latency_ms, 1),
            "score": self.score(now),
            "last_ok_at": self.last_ok_at,
            "last_fail_at": self.last_fail_at,
            "last_error": self.last_error[:200],
            "last_error_type": self.last_error_type,
        }


@dataclass
class CircuitBreaker:
    """单个 (provider, capability, route) 的熔断器。"""

    threshold: int = 3
    cooldown: float = 300.0
    state: ProviderState = ProviderState.HEALTHY
    opened_at: float = 0.0
    open_count: int = 0              # 历史累计熔断次数
    consecutive_fail: int = 0        # 连续源级失败（成功即清零）
    probe_in_flight: bool = False
    #: 本次打开要用的冷却时长（限流/封禁会被设得更长，见 COOLDOWN_SECONDS）
    next_cooldown: float = 0.0
    last_reason: str = ""

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---------------- 查询 ----------------
    def remaining(self, now: Optional[float] = None) -> float:
        """距熔断结束还有多少秒（未熔断返回 0）。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.state not in (ProviderState.OPEN, ProviderState.HALF_OPEN):
                return 0.0
            cool = self.next_cooldown or self.cooldown
            return max(0.0, self.opened_at + cool - now)

    def allows(self, now: Optional[float] = None) -> bool:
        """现在**能不能**让它干活（不占坑）。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._maybe_half_open(now)
            if self.state is ProviderState.OPEN:
                return False
            if self.state is ProviderState.HALF_OPEN:
                return not self.probe_in_flight
            return True

    def begin(self, now: Optional[float] = None) -> bool:
        """申请一次执行权。HALF_OPEN 时只允许**一个**探测。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._maybe_half_open(now)
            if self.state is ProviderState.OPEN:
                return False
            if self.state is ProviderState.HALF_OPEN:
                if self.probe_in_flight:
                    return False
                self.probe_in_flight = True
            return True

    def _maybe_half_open(self, now: float) -> None:
        if (self.state is ProviderState.OPEN
                and now - self.opened_at >= (self.next_cooldown or self.cooldown)):
            self.state = ProviderState.HALF_OPEN
            self.probe_in_flight = False

    # ---------------- 回写 ----------------
    def finish_success(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self.state = ProviderState.HEALTHY
            self.consecutive_fail = 0
            self.opened_at = 0.0
            self.next_cooldown = 0.0
            self.probe_in_flight = False
            self.last_reason = ""

    def finish_failure(self, now: Optional[float] = None, reason: str = "",
                       extra_cooldown: float = 0.0) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self.last_reason = reason[:200]
            self.probe_in_flight = False
            if self.state is ProviderState.HALF_OPEN:
                # 探测失败 → 立刻回到 OPEN 并重新计时
                # （不重新计时的话，一个刚被封的源会每分钟来撞一次）
                self._open(now, extra_cooldown)
                return
            if self.state is ProviderState.OPEN:
                return                       # 已熔断，别重复计数
            self.consecutive_fail += 1
            self.state = ProviderState.DEGRADED
            if self.consecutive_fail >= self.threshold:
                self._open(now, extra_cooldown)

    def _open(self, now: float, extra_cooldown: float = 0.0) -> None:
        self.state = ProviderState.OPEN
        self.opened_at = now
        self.open_count += 1
        self.consecutive_fail = 0
        self.next_cooldown = max(float(self.cooldown), float(extra_cooldown or 0.0))

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.monotonic() if now is None else now
        with self._lock:
            return {
                "state": self.state.value,
                "state_label": self.state.label,
                "remaining": round(self.remaining(now), 1),
                "open_count": self.open_count,
                "threshold": self.threshold,
                "cooldown": round(self.next_cooldown or self.cooldown, 1),
                "last_reason": self.last_reason[:200],
            }


class HealthBook:
    """所有路由的健康台账。键 = ``(provider, capability, route)``。"""

    def __init__(self, threshold: int = 3, cooldown: float = 300.0):
        self.threshold = int(threshold)
        self.cooldown = float(cooldown)
        self._stats: Dict[Tuple[str, str, str], HealthStats] = {}
        self._breakers: Dict[Tuple[str, str, str], CircuitBreaker] = {}
        self._lock = threading.RLock()

    # ---------------- 取 ----------------
    def stats(self, key: Tuple[str, str, str]) -> HealthStats:
        with self._lock:
            return self._stats.setdefault(key, HealthStats())

    def breaker(self, key: Tuple[str, str, str]) -> CircuitBreaker:
        with self._lock:
            cb = self._breakers.get(key)
            if cb is None:
                cb = CircuitBreaker(threshold=self.threshold, cooldown=self.cooldown)
                self._breakers[key] = cb
            return cb

    def score(self, key: Tuple[str, str, str], now: Optional[float] = None) -> float:
        return self.stats(key).score(now)

    # ---------------- 写 ----------------
    def record(self, key: Tuple[str, str, str], ok: Optional[bool], *,
               ms: float = 0.0, error: str = "", error_type: str = "",
               extra_cooldown: float = 0.0) -> None:
        """记一次结果。

        :param ok: True=成功；False=源级失败（计入熔断）；
            **None=中性**（无数据/不支持该能力）：只记调用次数，
            既不算成功也不算失败 —— 惩罚"这只票没数据"会让好源被误杀。
        """
        now_wall = time.time()
        now_mono = time.monotonic()
        st = self.stats(key)
        st.calls += 1
        if ok is None:
            st.neutral += 1
            st.consecutive_fail = 0
            if error:
                st.last_error = error[:200]
                st.last_error_type = error_type
            return

        if ok:
            st.ok += 1
            st.consecutive_fail = 0
            st.last_ok_at = now_wall
            st.last_error = ""
            st.last_error_type = ""
            # EMA：新样本权重 0.3，既能跟上变化又不被单次抖动带跑
            st.latency_ms = (st.latency_ms * 0.7 + float(ms) * 0.3
                             if st.latency_ms else float(ms))
            self.breaker(key).finish_success(now_mono)
        else:
            st.fail += 1
            st.consecutive_fail += 1
            st.last_fail_at = now_wall
            st.last_error = error[:200]
            st.last_error_type = error_type
            # 连续失败计数与状态机都在熔断器里（HALF_OPEN 语义也在那儿）
            self.breaker(key).finish_failure(now_mono, error, extra_cooldown)

    # ---------------- 可观测 ----------------
    def snapshot(self) -> List[Dict[str, Any]]:
        now_wall = time.time()
        with self._lock:
            keys = sorted(set(self._stats) | set(self._breakers))
        out: List[Dict[str, Any]] = []
        for key in keys:
            provider, capability, route = key
            out.append({
                "provider": provider, "capability": capability, "route": route,
                **self.stats(key).to_dict(now_wall),
                "circuit": self.breaker(key).to_dict(),
            })
        return out

    def reset(self, key: Optional[Tuple[str, str, str]] = None) -> None:
        with self._lock:
            if key is None:
                self._stats.clear()
                self._breakers.clear()
                return
            self._stats.pop(key, None)
            self._breakers.pop(key, None)


__all__ = ["CircuitBreaker", "HealthBook", "HealthStats", "ProviderState"]
