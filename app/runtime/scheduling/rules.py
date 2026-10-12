#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : rules.py
# @Description : 调度规则：把"发生了什么"翻译成"接下来要做什么"
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么规则要独立成表，而不是写死在调度器里
==========================================

硬编码的调度逻辑长这样::

    if event.event_type == "market.kline.updated":
        if ctx.collector_enabled:
            self.submit(factor_calculate, ...)

它能工作，但有三处代价：

  1. **不可观测**："系统里一共有哪些自动流转"要读遍调度器源码才知道；
  2. **不可配置**：想临时关掉"因子更新 → 策略运行"这条链，只能改代码；
  3. **不可测试**：规则与调度机制耦合，测一条规则要起一台引擎。

规则表把"什么事件 → 什么任务 → 什么条件"变成**数据**，
于是它可以：被列出来（面板/文档）、被开关（运维）、被单独测试（单测）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.common.logging import logger
from app.runtime.events.schema import EngineEvent
from app.runtime.events.types import TASK_QUEUE, Queue
from app.runtime.tasking.models import EngineTask, Priority

#: 条件回调：``(event, ctx) -> bool``。ctx 是调度器（可能为 None，便于单测）
ConditionFn = Callable[[EngineEvent, Any], bool]
#: 参数构造：``(event, ctx) -> Dict``
ParamsFn = Callable[[EngineEvent, Any], Dict[str, Any]]
#: 幂等键构造：``(event, ctx) -> str``
DedupFn = Callable[[EngineEvent, Any], str]


@dataclass
class Rule:
    """一条调度规则。

    :param event_type:   触发事件。**不支持通配** —— 规则必须可枚举，
                         一条规则只该对应一种明确的因果关系。
    :param task_type:    要生成的任务类型
    :param condition:    额外的前置判断（市场是否开盘、服务是否启用…）
    :param params_fn:    任务参数构造
    :param dedup_fn:     幂等键。不配则**不去重** —— 有些任务天然该每次都跑
                         （比如手工触发的采集），强行去重反而会让用户
                         点"立即采集"没反应。
    :param max_retries:  最大重试次数（下单类任务会被强制归零）
    """

    name: str
    event_type: str
    task_type: str
    description: str = ""

    condition: Optional[ConditionFn] = None
    params_fn: Optional[ParamsFn] = None
    dedup_fn: Optional[DedupFn] = None

    queue: str = ""
    priority: int = Priority.NORMAL
    max_retries: int = 0
    retry_interval: float = 5.0
    timeout_seconds: float = 60.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("规则必须有 name")
        if not self.event_type:
            raise ValueError(f"规则 {self.name} 缺少 event_type")
        if not self.task_type:
            raise ValueError(f"规则 {self.name} 缺少 task_type")
        if not self.queue:
            self.queue = TASK_QUEUE.get(self.task_type, Queue.DEFAULT)

    # ---------------- 判定 ----------------
    def should_run(self, event: EngineEvent, ctx: Any = None) -> bool:
        """是否该为这个事件建任务。条件回调抛错按**不跑**处理。

        为什么不抛出：一条规则的条件判断炸了（比如读配置失败），
        不该让同事件的其它规则也跟着失效，更不该让调度器崩掉。
        记日志 + 跳过，是这里唯一正确的处理。
        """
        if not self.enabled:
            return False
        if self.condition is None:
            return True
        try:
            return bool(self.condition(event, ctx))
        except Exception as exc:
            logger.error(f"规则 {self.name} 的条件判断异常，本次跳过: {exc}")
            return False

    # ---------------- 造任务 ----------------
    def build_task(self, event: EngineEvent, ctx: Any = None) -> EngineTask:
        params: Dict[str, Any] = {}
        if self.params_fn is not None:
            try:
                params = dict(self.params_fn(event, ctx) or {})
            except Exception as exc:
                logger.error(f"规则 {self.name} 构造参数失败，用空参数继续: {exc}")
        # 溯源字段由规则统一填，规则作者不用操心
        params.setdefault("__event_type__", event.event_type)

        dedup_key = ""
        if self.dedup_fn is not None:
            try:
                dedup_key = str(self.dedup_fn(event, ctx) or "")
            except Exception as exc:
                logger.error(f"规则 {self.name} 构造幂等键失败，本次不去重: {exc}")

        return EngineTask(
            task_type=self.task_type,
            params=params,
            queue=self.queue,
            priority=self.priority,
            max_retries=self.max_retries,
            retry_interval=self.retry_interval,
            timeout_seconds=self.timeout_seconds,
            dedup_key=dedup_key,
            source_event_id=event.event_id,
            source=event.source,
            correlation_id=event.correlation_id or event.event_id,
            causation_id=event.event_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "event_type": self.event_type,
            "task_type": self.task_type,
            "queue": self.queue,
            "priority": self.priority,
            "max_retries": self.max_retries,
            "enabled": self.enabled,
            "description": self.description,
            "has_condition": self.condition is not None,
            "has_dedup": self.dedup_fn is not None,
        }


class RuleRegistry:
    """规则注册表：事件类型 → 规则列表。"""

    def __init__(self) -> None:
        self._rules: "Dict[str, List[Rule]]" = {}
        self._by_name: "Dict[str, Rule]" = {}
        self._match_cache: "Dict[str, Tuple[Rule, ...]]" = {}

    # ---------------- 注册 ----------------
    def register(self, rule: Rule) -> None:
        if rule.name in self._by_name:
            raise ValueError(f"重复的规则名: {rule.name}")
        self._by_name[rule.name] = rule
        self._rules.setdefault(rule.event_type, []).append(rule)
        self._match_cache.clear()

    def register_all(self, rules: Iterable[Rule]) -> None:
        for r in rules:
            self.register(r)

    def clear(self) -> None:
        self._rules.clear()
        self._by_name.clear()
        self._match_cache.clear()

    # ---------------- 查询 ----------------
    def match(self, event_type: str) -> "Tuple[Rule, ...]":
        """按事件类型取规则（含缓存）。"""
        cached = self._match_cache.get(event_type)
        if cached is not None:
            return cached
        matched = tuple(self._rules.get(event_type) or ())
        self._match_cache[event_type] = matched
        return matched

    def get(self, name: str) -> Optional[Rule]:
        return self._by_name.get(name)

    def all_rules(self) -> "List[Rule]":
        return list(self._by_name.values())

    def event_types(self) -> "List[str]":
        return sorted(self._rules.keys())

    # ---------------- 开关 ----------------
    def set_enabled(self, name: str, enabled: bool) -> bool:
        rule = self._by_name.get(name)
        if rule is None:
            return False
        rule.enabled = bool(enabled)
        logger.info(f"调度规则 {name} 已{'启用' if enabled else '停用'}")
        return True

    def snapshot(self) -> Dict[str, Any]:
        return {
            "total": len(self._by_name),
            "enabled": sum(1 for r in self._by_name.values() if r.enabled),
            "event_types": self.event_types(),
            "rules": [r.to_dict() for r in self.all_rules()],
        }


# =============================================================================
# 规则作者常用的小工具
# =============================================================================
def symbols_of(event: EngineEvent, ctx: Any = None, fallback: str = "") -> "List[str]":
    """从事件里取标的列表。

    三种来源，优先级从高到低：
      1. 事件的 ``symbols`` 字段；
      2. 事件的 ``symbol`` 字段（单只）；
      3. 调度器上下文的默认标的。

    没有第 3 项时返回空列表 —— **不要**在这里偷偷退回"全市场"。
    少了标的就用全市场跑一遍，是把一次小更新变成一次全量重算的经典事故。
    """
    data = event.data or {}
    raw = data.get("symbols")
    if isinstance(raw, (list, tuple)) and raw:
        return [str(s) for s in raw if s]
    one = data.get("symbol")
    if one:
        return [str(one)]
    if fallback:
        return [fallback]
    getter = getattr(ctx, "default_symbols", None)
    if callable(getter):
        try:
            return [str(s) for s in (getter() or [])]
        except Exception:
            return []
    return []


def dedup_key(*parts: Any) -> str:
    """拼一个可读的幂等键。空片段会被跳过。"""
    return ":".join(str(p) for p in parts if p not in (None, "", [], ()))


_WHITESPACE = re.compile(r"\s+")


def normalize_key(text: str) -> str:
    """把幂等键压成紧凑形式（去空白、转小写）。"""
    return _WHITESPACE.sub("", str(text or "")).lower()


__all__ = [
    "Rule", "RuleRegistry", "symbols_of", "dedup_key", "normalize_key",
    "ConditionFn", "ParamsFn", "DedupFn",
]
