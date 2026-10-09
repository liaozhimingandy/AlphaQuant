#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : base.py
# @Description : 规则层基类：把"因子值"翻译成"是否满足"的布尔判断
#               规则可嵌套组合（All/Any/Not），从而让策略完全由配置拼出来
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import abc
from typing import Any, Dict, Optional

from app.core.factor.context import FactorContext


class IBaseRule(abc.ABC):
    """规则基类。

    ``is_satisfied(ctx)`` 是唯一的对外协议。
    所有规则必须**纯只读**——不能在规则里下单、改状态，否则组合后行为不可预测。
    """

    name: str = ""
    description: str = ""

    @abc.abstractmethod
    def is_satisfied(self, ctx: FactorContext) -> bool:
        raise NotImplementedError

    # ---------------- 调试 ----------------
    def explain(self, ctx: FactorContext) -> Dict[str, Any]:
        """返回判定明细，用于排查"为什么没触发"。"""
        return {"rule": self.name or self.__class__.__name__, "satisfied": self.is_satisfied(ctx)}

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<{self.__class__.__name__} {self.name}>"
