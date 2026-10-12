#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : schema.py
# @Description : EngineEvent —— 事件总线上流动的**唯一**消息形态
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
事件是"已经发生的事实"
======================

这句话决定了 EngineEvent 的几个设计：

  * **不可变意图**：发出去的事件不该被订阅方改写（``data`` 传进来是引用，
    我们不承诺深拷贝，但约定俗成谁都不改）。要改就发一个新事件，
    并用 ``causation_id`` 指向原事件 —— 这样因果链是显式记录下来的，
    而不是靠猜。
  * **时间用本地朴素时间**：项目里 datetime 全是 naive（``datetime.now()``），
    这里也跟着用。混用 aware 与 naive 会在比较时抛
    ``TypeError: can't compare offset-naive and offset-aware datetimes``，
    而这种错误只在"恰好跨了时区边界"的数据上复现，极难排查。
  * **零依赖**：用 dataclass 而不是 pydantic —— 本项目严守 numpy/pandas
    之外的零新依赖，标准库完全够表达这个结构。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from app.runtime.events.types import canonical_event_type

#: 事件结构的版本。未来改字段时靠它做兼容读取，而不是靠 try/except 猜。
SCHEMA_VERSION = 1


@dataclass
class EngineEvent:
    """事件总线传递的标准消息。

    :param event_type:      事件类型，取 :class:`EventType` 常量
    :param data:            业务载荷
    :param source:          谁发出的（组件名 / 服务名 / ``engine``）
    :param event_id:        事件唯一 ID，用于**消费去重**
    :param occurred_at:     事件发生时刻
    :param correlation_id:  关联一条完整业务流程（K线采集 → 因子 → 策略 → 下单
                            这一整条链共享同一个 correlation_id，
                            于是"这单是哪个 K 线触发的"可以一路追回去）
    :param causation_id:    直接导致本事件的那个事件 ID
    :param schema_version:  结构版本
    """

    event_type: str
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: datetime = field(default_factory=datetime.now)
    correlation_id: str = ""
    causation_id: str = ""
    schema_version: int = SCHEMA_VERSION

    # ---------------- 构造助手 ----------------
    @classmethod
    def create(
        cls,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
        source: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        event_id: str = "",
    ) -> "EngineEvent":
        """显式构造。``event_type`` 会折算成规范名（旧名 → 新名）。"""
        return cls(
            event_type=canonical_event_type(event_type),
            data=dict(data or {}),
            source=source,
            event_id=event_id or uuid.uuid4().hex,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def child(
        self,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
        source: str = "",
    ) -> "EngineEvent":
        """派生一个**由本事件导致**的新事件。

        因果关系在这里被固化下来：子事件的 ``causation_id`` 指向本事件，
        ``correlation_id`` 继承本事件。所以事后复盘时，
        "哪条新闻导致了哪个因子更新导致了哪个信号"是一条能查出来的链，
        而不是散落在日志里靠时间戳对齐。
        """
        return EngineEvent.create(
            event_type=event_type,
            data=data,
            source=source or self.source,
            correlation_id=self.correlation_id or self.event_id,
            causation_id=self.event_id,
        )

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "source": self.source,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "schema_version": self.schema_version,
            "data": dict(self.data or {}),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EngineEvent":
        occurred = raw.get("occurred_at")
        if isinstance(occurred, str):
            try:
                occurred_at = datetime.fromisoformat(occurred)
            except ValueError:
                occurred_at = datetime.now()
        elif isinstance(occurred, datetime):
            occurred_at = occurred
        else:
            occurred_at = datetime.now()
        return cls(
            event_type=canonical_event_type(str(raw.get("event_type") or "")),
            data=dict(raw.get("data") or {}),
            source=str(raw.get("source") or ""),
            event_id=str(raw.get("event_id") or uuid.uuid4().hex),
            occurred_at=occurred_at,
            correlation_id=str(raw.get("correlation_id") or ""),
            causation_id=str(raw.get("causation_id") or ""),
            schema_version=int(raw.get("schema_version") or SCHEMA_VERSION),
        )

    # ---------------- 便捷读取 ----------------
    def get(self, key: str, default: Any = None) -> Any:
        return (self.data or {}).get(key, default)

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        return f"<EngineEvent {self.event_type} #{self.event_id[:8]} src={self.source}>"


__all__ = ["EngineEvent", "SCHEMA_VERSION"]
