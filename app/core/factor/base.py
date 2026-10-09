#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : base.py
# @Description : 因子层基类：把行情/事件特征加工成一个"可比较的数值"
#               因子 ≠ 信号：因子只回答"现在这个值是多少"，不回答"该不该买"
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import abc
from typing import Any, Dict, Optional

from app.core.factor.context import FactorContext


class IBaseFactor(abc.ABC):
    """因子基类。

    约定：
      - ``name`` 为注册表唯一键
      - ``compute`` 返回 float / bool / None。**返回 None 表示"当前算不出来"**，
        规则层会把 None 当作"不满足"，而不是当作 0 —— 这一点很重要，
        否则指标预热期会被误判成强烈看空而触发交易。
    """

    name: str = ""
    #: 供文档/CLI 展示
    description: str = ""

    def __init__(self, **params: Any) -> None:
        self.params: Dict[str, Any] = params

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<{self.__class__.__name__} {self.name} {self.params}>"

    # ---------------- 子类实现 ----------------
    @abc.abstractmethod
    def compute(self, ctx: FactorContext) -> Optional[float]:
        raise NotImplementedError

    # ---------------- 兼容旧接口 ----------------
    def evaluate(self, ctx: FactorContext) -> bool:
        """布尔化求值。None 视为 False。"""
        v = self.compute(ctx)
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        return bool(v)
