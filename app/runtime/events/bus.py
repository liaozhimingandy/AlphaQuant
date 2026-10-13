#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : bus.py
# @Description : 事件总线：进程内的**事实广播**通道
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
事件总线 vs 任务队列
====================

这两件事经常被合并成一个"消息队列"，然后系统就开始出问题：

  ====================  ==========================
  事件总线（本文件）     任务队列（tasking/store.py）
  ====================  ==========================
  传播**已经发生的事实**  承载**需要完成的工作**
  一对多广播            一对一领取（领走就没了）
  丢一条 = 少一次通知    丢一条 = 一件事没做
  不保证一定被处理       必须有终态（成功/失败/死信）
  处理同步、极快        处理异步、可能很慢
  ====================  ==========================

混在一起的典型后果：为了"不丢消息"给事件加上重试与确认，
于是"K 线到了"这条事实被重试三次，因子被重算三遍；
或者反过来，为了"处理得快"不给任务落库，重启后所有在途任务凭空消失。

本总线刻意保持**同步投递**：publish 返回时，所有订阅者都已跑完。
理由：订阅者做的事应该是"记账 / 打日志 / 往队列里塞一个任务"这类
瞬时操作，同步能让"事件顺序"这个最重要的性质得到保证。
真正耗时的活应该由订阅者转成任务丢给执行池 —— 这就是
"调度器订阅事件、生成任务"的意义。
"""
from __future__ import annotations

import inspect
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from app.common.logging import logger
from app.runtime.events.schema import EngineEvent
from app.runtime.events.types import (
    CANONICAL_TO_LEGACY,
    TASK_ECHO_EVENTS,
    canonical_event_type,
)


def _handler_label(handler: Callable[..., Any]) -> str:
    return getattr(handler, "__name__", repr(handler))


def _accepts_event(handler: Callable[..., Any]) -> bool:
    """处理器是否声明了 ``event`` 形参（或 ``**kwargs``）。

    新旧两种订阅风格要能共存：
      旧 ``def on_bar(bar=None, **kwargs)``  —— 只认展开后的关键字参数
      新 ``def on_event(event: EngineEvent)`` —— 想要完整信封（去重 ID / 因果链）

    判定结果按 handler 缓存：签名解析不便宜，而 publish 的频率可以很高
    （一根 K 线一次）。
    """
    cached = _ACCEPTS_EVENT_CACHE.get(handler)
    if cached is not None:
        return cached
    result = False
    try:
        sig = inspect.signature(handler)
        if "event" in sig.parameters:
            result = True
        else:
            result = any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
    except (TypeError, ValueError):
        # C 扩展函数 / 部分 builtin 取不到签名：按"只收关键字参数"处理，
        # 这是绝大多数旧订阅者的形态。
        result = False
    _ACCEPTS_EVENT_CACHE[handler] = result
    return result


_ACCEPTS_EVENT_CACHE: "Dict[Callable[..., Any], bool]" = {}


class EventBus:
    """全局事件总线：发布-订阅，解耦所有组件。

    相比重构前多出的四件事，每一件都对应一类真实故障：

    1. **事件去重**（``event_id``）—— 消息重投时不会让下游把同一件事做两遍；
    2. **通配订阅**（``market.*``）—— 否则想"监听整个域"要写十几个 subscribe；
    3. **新旧名同时投递** —— 事件名迁移期间不会静默丢订阅；
    4. **异常隔离 + 计数** —— 一个订阅者抛异常不能让后面的订阅者收不到事件，
       但也不能悄悄吞掉（以前只 log 不计数，于是"一直在报错"无人察觉）。
    """

    #: 事件名通配后缀
    WILDCARD = "*"

    def __init__(
        self,
        history_size: int = 200,
        dedup_size: int = 4096,
    ) -> None:
        self._receivers: "Dict[str, List[Callable[..., Any]]]" = defaultdict(list)
        self._lock = threading.RLock()
        #: 已见事件 ID（有界 LRU），用于消费去重
        self._seen: "OrderedDict[str, bool]" = OrderedDict()
        self._dedup_size = max(0, int(dedup_size))
        #: 最近事件历史（面板 / 排障用）
        self._history: "List[Dict[str, Any]]" = []
        self._history_size = max(0, int(history_size))
        self._match_cache: "Dict[str, Tuple[str, ...]]" = {}
        #: 按事件类型分别计数。只有总量（stats）时，"哪一类事件在暴涨"
        #: 这个问题答不了，而暴涨的那类往往就是出问题的那类。
        self._metrics: "Dict[str, Dict[str, Any]]" = defaultdict(self._new_metric)
        self.stats: Dict[str, int] = {
            "published": 0,
            "delivered": 0,
            "duplicate_dropped": 0,
            "handler_errors": 0,
        }

    @staticmethod
    def _new_metric() -> "Dict[str, Any]":
        return {
            "published": 0,
            "delivered": 0,
            "handler_errors": 0,
            "last_at": 0.0,
            "last_event_id": "",
        }

    # ============================================================
    # 订阅
    # ============================================================
    def subscribe(
        self,
        event_name: str,
        receiver: Callable[..., Any],
    ) -> None:
        """订阅事件。

        :param event_name: 精确事件名，或 ``域.*`` 通配，或 ``*``（全部）
        """
        if not callable(receiver):
            raise ValueError(f"订阅者必须可调用: {receiver!r}")
        with self._lock:
            receivers = self._receivers[str(event_name)]
            if receiver not in receivers:
                receivers.append(receiver)
                # 新订阅者可能让旧的匹配缓存失效（新增了一个通配 key）
                self._match_cache.clear()

    def subscribe_pattern(self, prefix: str, receiver: Callable[..., Any]) -> None:
        """订阅一整个域，如 ``subscribe_pattern("market", fn)``。"""
        self.subscribe(f"{prefix}.{self.WILDCARD}", receiver)

    def unsubscribe(self, event_name: str, receiver: Callable[..., Any]) -> None:
        with self._lock:
            receivers = self._receivers.get(str(event_name))
            if not receivers:
                return
            try:
                receivers.remove(receiver)
            except ValueError:
                return
            if not receivers:
                self._receivers.pop(str(event_name), None)
            self._match_cache.clear()

    def unsubscribe_all(self, receiver: Callable[..., Any]) -> int:
        """取消某个订阅者的全部订阅（组件销毁时用）。返回取消的条数。"""
        removed = 0
        with self._lock:
            for key in list(self._receivers.keys()):
                if receiver in self._receivers[key]:
                    self._receivers[key].remove(receiver)
                    removed += 1
                    if not self._receivers[key]:
                        self._receivers.pop(key, None)
            if removed:
                self._match_cache.clear()
        return removed

    def get_receiver(self) -> "Dict[str, List[Callable[..., Any]]]":
        """订阅表快照（旧接口名保留）。"""
        with self._lock:
            return {k: list(v) for k, v in self._receivers.items()}

    # ============================================================
    # 发布
    # ============================================================
    def publish(self, *args: Any, **kwargs: Any) -> EngineEvent:
        """发布事件。**同步**投递：返回时所有订阅者已执行完毕。

        兼容三种写法（迁移期间新旧代码会混着跑）::

            bus.publish(EventType.MARKET_KLINE_UPDATED, symbol="000001", rows=1)
            bus.publish(event_name="timer_3min", context={...})   # 旧写法
            bus.publish(EngineEvent.create("engine.ready", {...}))  # 新写法

        :return: 实际投递出去的 :class:`EngineEvent`
        """
        evt = self._coerce(*args, **kwargs)
        self.publish_event(evt)
        return evt

    def publish_event(self, event: EngineEvent) -> int:
        """投递一个已构造好的事件，返回触达的订阅者数量。"""
        if not isinstance(event, EngineEvent):
            raise TypeError(f"publish_event 只接受 EngineEvent，收到 {type(event).__name__}")

        if self._is_duplicate(event.event_id):
            self.stats["duplicate_dropped"] += 1
            logger.debug(f"事件重复投递，已丢弃: {event}")
            return 0

        with self._lock:
            self.stats["published"] += 1
            self._remember(event)
            metric = self._metrics[event.event_type]
            metric["published"] += 1
            metric["last_at"] = time.time()
            metric["last_event_id"] = event.event_id or ""

        delivered = 0
        for handler in self._handlers_for(event.event_type):
            try:
                self._invoke(handler, event)
                delivered += 1
                with self._lock:
                    self.stats["delivered"] += 1
                    self._metrics[event.event_type]["delivered"] += 1
            except Exception as exc:
                with self._lock:
                    self.stats["handler_errors"] += 1
                    self._metrics[event.event_type]["handler_errors"] += 1
                # 异常必须隔离：一个订阅者炸了，后面的订阅者仍要收到事件。
                # 但不静默 —— 计数进 stats，面板能看见"事件处理持续失败"。
                logger.error(
                    f"事件 {event.event_type} 的订阅者 {_handler_label(handler)} "
                    f"执行异常: {exc}",
                    exc_info=True,
                )
        return delivered

    # ---------------- 内部 ----------------
    def _coerce(self, *args: Any, **kwargs: Any) -> EngineEvent:
        """把各种调用形态统一成 EngineEvent。"""
        if args and isinstance(args[0], EngineEvent):
            evt = args[0]
            if kwargs:
                merged = dict(evt.data or {})
                merged.update(kwargs)
                evt.data = merged
            return evt

        name = str(args[0]) if args else ""
        if not name:
            name = str(kwargs.pop("event_name", "") or "")
        if not name:
            raise ValueError("publish 缺少事件名")

        # 载荷可能来自 ``data=``、``context=`` 或散装关键字参数。
        # 这里要**记住它原来挂在哪把钥匙上**：旧订阅者是照着
        # ``def h(context=None)`` 写的，把 EngineContext 塞进 ``value=``
        # 会让它们静默收到 None —— 这类"参数名漂移"的错最难查。
        payload = kwargs.pop("data", None)
        payload_key = "data" if payload is not None else ""
        if payload is None:
            payload = kwargs.pop("context", None)
            payload_key = "context" if payload is not None else ""
        source = str(kwargs.pop("source", "") or "")

        if isinstance(payload, Mapping):
            data: Dict[str, Any] = dict(payload)
        elif payload is None:
            data = {}
        else:
            data = {payload_key or "value": payload}
        # 其余关键字参数（如 order=... / task_id=...）并入载荷，
        # 这就是旧订阅者拿到的那批 kwargs。
        data.update(kwargs)

        return EngineEvent.create(event_type=name, data=data, source=source)

    def _invoke(self, handler: Callable[..., Any], event: EngineEvent) -> None:
        data = dict(event.data or {})
        # 载荷里已经有 ``event`` 键时**以载荷为准**：资讯服务发布的
        # ``event`` 是它自己的 NewsEvent，总线不该用 EngineEvent 顶掉它，
        # 否则老订阅者拿到错误类型。没有冲突时才注入事件信封。
        if "event" in data:
            handler(**data)
        elif _accepts_event(handler):
            handler(event=event, **data)
        else:
            handler(**data)

    def _handlers_for(self, event_type: str) -> "List[Callable[..., Any]]":
        """按事件名收集订阅者：精确 + 通配 + 新旧名映射，去重保序。"""
        with self._lock:
            cached = self._match_cache.get(event_type)
            if cached is not None:
                keys: Tuple[str, ...] = cached
            else:
                keys = self._compute_keys(event_type)
                self._match_cache[event_type] = keys
            seen: "Set[Callable[..., Any]]" = set()
            out: "List[Callable[..., Any]]" = []
            for key in keys:
                for h in self._receivers.get(key) or ():
                    if h not in seen:
                        seen.add(h)
                        out.append(h)
            return out

    def _compute_keys(self, event_type: str) -> "Tuple[str, ...]":
        """算出一次投递要查哪些订阅键。

        一次 ``market.kline.updated`` 可能要查：
          ``market.kline.updated``（精确）
          ``market.*``（域通配）
          ``*``（全局）
          ``market_kline_updated`` 等旧名（迁移兼容）
        """
        names = {event_type, canonical_event_type(event_type)}
        names |= set(CANONICAL_TO_LEGACY.get(canonical_event_type(event_type)) or ())
        keys: List[str] = []
        for registered in self._receivers.keys():
            if registered == self.WILDCARD:
                keys.append(registered)
                continue
            for name in names:
                if registered == name:
                    keys.append(registered)
                    break
                if registered.endswith(f".{self.WILDCARD}") and name.startswith(
                    registered[:-2] + "."
                ):
                    keys.append(registered)
                    break
        return tuple(dict.fromkeys(keys))

    def _is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        with self._lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = True
            while len(self._seen) > self._dedup_size:
                self._seen.popitem(last=False)
            return False

    def _remember(self, event: EngineEvent) -> None:
        if self._history_size <= 0:
            return
        self._history.append(event.to_dict())
        while len(self._history) > self._history_size:
            self._history.pop(0)

    # ============================================================
    # 可观测
    # ============================================================
    def recent(
        self,
        limit: int = 50,
        event_type: str = "",
    ) -> "List[Dict[str, Any]]":
        """最近事件（倒序保持时间顺序，最新的在末尾）。

        :param event_type: 只看这一类；空表示全部
        """
        with self._lock:
            items = self._history
            if event_type:
                wanted = str(event_type)
                items = [h for h in items if h.get("event_type") == wanted]
            return list(items[-max(0, int(limit)):])

    def event_metrics(self, limit: int = 0) -> "List[Dict[str, Any]]":
        """按事件类型分别统计。

        ``subscribers`` 是**实时**算的（不是发布时的快照）：
        订阅关系是会变的，"这个事件现在有没有人听"比
        "发布时有没有人听"重要得多 —— 后者正常而前者为 0，
        意味着链路在跑起来之后断了。
        """
        with self._lock:
            rows: "List[Dict[str, Any]]" = []
            for name, metric in self._metrics.items():
                rows.append({
                    "event_type": name,
                    "published": metric["published"],
                    "delivered": metric["delivered"],
                    "handler_errors": metric["handler_errors"],
                    "subscribers": len(self._handlers_for(name)),
                    "last_at": metric["last_at"],
                    "last_iso": (
                        datetime.fromtimestamp(metric["last_at"]).isoformat(
                            timespec="seconds"
                        )
                        if metric["last_at"] else ""
                    ),
                    "last_event_id": metric["last_event_id"],
                })
        rows.sort(key=lambda r: (-int(r["published"]), str(r["event_type"])))
        if limit and limit > 0:
            return rows[:int(limit)]
        return rows

    def event_detail(self, event_type: str) -> "Dict[str, Any]":
        """单类事件的指标 + 最近几条样例（排障时核对"到底发了什么"）。"""
        name = str(event_type or "")
        with self._lock:
            metric = dict(self._metrics.get(name) or self._new_metric())
            samples = [
                h for h in self._history if h.get("event_type") == name
            ][-10:]
        return {
            "event_type": name,
            "published": metric["published"],
            "delivered": metric["delivered"],
            "handler_errors": metric["handler_errors"],
            "subscribers": len(self._handlers_for(name)),
            "last_at": metric["last_at"],
            "last_iso": (
                datetime.fromtimestamp(metric["last_at"]).isoformat(timespec="seconds")
                if metric["last_at"] else ""
            ),
            "last_event_id": metric["last_event_id"],
            "samples": samples,
        }

    def known_event_types(self) -> "List[str]":
        with self._lock:
            return sorted(self._metrics.keys())

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "stats": dict(self.stats),
                "subscriptions": {k: len(v) for k, v in self._receivers.items()},
                "recent": list(self._history[-20:]),
                "types": self.event_metrics(20),
            }

    def is_echo_event(self, event_type: str) -> bool:
        """是否为"任务系统的回声"事件。调度器用它防止自己触发自己。"""
        return canonical_event_type(event_type) in TASK_ECHO_EVENTS


__all__ = ["EventBus"]
