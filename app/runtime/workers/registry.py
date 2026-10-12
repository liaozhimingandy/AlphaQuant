#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 处理器注册表：任务类型 → 真正干活的函数
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
执行器怎么知道一个任务该调谁
============================

两个选择：

  A. 执行器里写 ``if task_type == "factor.calculate": factor_service.run(...)``
  B. 执行器查一张表，表里写着"这种任务归这个函数管"

选 B 的理由不是"更优雅"，而是：

  * **执行器不需要认识业务**：它只负责"取处理器 → 跑 → 回报状态"。
    它 import 不到因子服务、也不该 import —— 否则执行层与业务层又缠在一起；
  * **缺处理器是可见的**：查不到就明确报"无处理器"并进死信，
    而不是走到某个 else 分支悄悄什么都不做；
  * **可替换**：想给"因子计算"换个实现，改一行注册即可，
    不用动调度与执行链路。

处理器约定
----------

    def handler(task: EngineTask, ctx: Any) -> Any:

``ctx`` 是调度器（能拿到引擎与各服务）。返回值进 ``task.result``，
抛异常即失败（由调度器决定重试 / 死信）。
处理器必须**幂等**：同一个 dedup_key 的任务被重复执行时，
结果应当一致且无副作用。这一点在交易域尤其重要。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Mapping

from app.common.logging import logger
from app.runtime.tasking.models import EngineTask

#: 处理器签名
HandlerFn = Callable[[EngineTask, Any], Any]


class HandlerRegistry:
    """任务类型 → 处理器。"""

    def __init__(self) -> None:
        self._handlers: "Dict[str, HandlerFn]" = {}
        self._meta: "Dict[str, Dict[str, str]]" = {}

    # ---------------- 注册 ----------------
    def register(
        self,
        task_type: str,
        handler: HandlerFn,
        name: str = "",
        description: str = "",
        override: bool = False,
    ) -> None:
        if not callable(handler):
            raise ValueError(f"处理器必须可调用: {task_type}")
        key = str(task_type)
        if key in self._handlers and not override:
            raise ValueError(
                f"任务类型 {key} 已有处理器 {self._meta[key].get('name')}；"
                f"确需替换请传 override=True"
            )
        self._handlers[key] = handler
        self._meta[key] = {
            "name": name or getattr(handler, "__name__", "anonymous"),
            "description": description,
        }
        logger.debug(f"注册任务处理器: {key} → {self._meta[key]['name']}")

    def register_many(self, mapping: Mapping[str, HandlerFn]) -> None:
        for task_type, fn in mapping.items():
            self.register(task_type, fn, override=True)

    # ---------------- 查询 ----------------
    def get(self, task_type: str) -> Optional[HandlerFn]:
        return self._handlers.get(str(task_type))

    def has(self, task_type: str) -> bool:
        return str(task_type) in self._handlers

    def task_types(self) -> List[str]:
        return sorted(self._handlers.keys())

    def unregister(self, task_type: str) -> bool:
        if self._handlers.pop(str(task_type), None) is None:
            return False
        self._meta.pop(str(task_type), None)
        return True

    # ---------------- 可观测 ----------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "total": len(self._handlers),
            "handlers": [
                {"task_type": k, **self._meta.get(k, {})}
                for k in sorted(self._handlers.keys())
            ],
        }


__all__ = ["HandlerRegistry", "HandlerFn"]
