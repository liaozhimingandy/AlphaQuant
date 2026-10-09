#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 风控注册表 + 闸门链 RiskChain
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.core.factor.context import FactorContext
from app.core.market.types import Account, Signal
from app.core.risk.base import IBaseRiskRule, RiskVerdict
from app.core.strategy.state import DecisionState
from app.utils.logger import logger

_RISK_REGISTRY: Dict[str, type] = {}


def register_risk(cls: type) -> type:
    """装饰器：注册风控规则。"""
    name = getattr(cls, "name", "") or cls.__name__
    if not name:
        raise ValueError(f"风控类 {cls.__name__} 缺少 name")
    if name in _RISK_REGISTRY and _RISK_REGISTRY[name] is not cls:
        logger.warning(f"风控规则覆盖注册: {name}")
    _RISK_REGISTRY[name] = cls
    cls.name = name
    return cls


def get_risk(name: str) -> type:
    try:
        return _RISK_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"未注册的风控规则: {name!r}，可用: {sorted(_RISK_REGISTRY)}"
        ) from None


def create_risk(name: str, **params: Any) -> IBaseRiskRule:
    return get_risk(name)(**params)


def list_risks() -> List[str]:
    return sorted(_RISK_REGISTRY)


def _auto_register() -> None:
    """导入内置风控完成注册（延迟导入，避免循环依赖）。"""
    if _RISK_REGISTRY:
        return
    from app.core.risk import builtin  # noqa: F401


class RiskChain:
    """风控闸门链：任何一道否决就整体否决。

    与"过滤器"的关键区别：风控**不修改**信号方向，只会
      1. 否决（allowed=False）
      2. 缩放仓位（scale，取所有规则里最小的那个）
    """

    def __init__(self, rules: Optional[Iterable[IBaseRiskRule]] = None) -> None:
        self.rules: List[IBaseRiskRule] = list(rules or [])

    def add(self, rule: IBaseRiskRule) -> "RiskChain":
        self.rules.append(rule)
        return self

    def __len__(self) -> int:
        return len(self.rules)

    def check(
        self,
        signal: Signal,
        state: DecisionState,
        ctx: Optional[FactorContext] = None,
        account: Optional[Account] = None,
    ) -> RiskVerdict:
        """串行执行所有风控，返回首个否决或最终放行。"""
        scale = 1.0
        for rule in self.rules:
            try:
                verdict = rule.check(signal, state, ctx=ctx, account=account)
            except Exception as exc:  # 风控自身崩溃不能把系统打死 → 放行但留痕
                logger.error(f"风控 {rule.name} 执行异常，按放行处理: {exc}")
                continue
            if not verdict.allowed:
                return verdict
            scale = min(scale, verdict.scale)
        return RiskVerdict(True, rule="risk_chain", reason="全部通过", scale=scale)

    def to_dict(self) -> List[Dict[str, Any]]:
        return [
            {"name": r.name, "params": dict(getattr(r, "params", {}))}
            for r in self.rules
        ]


def build_risk_chain(spec: Optional[Iterable[Mapping[str, Any]]]) -> RiskChain:
    """从配置构造闸门链。

    格式::

        [
          {"type": "max_drawdown", "params": {"max_drawdown": 0.2}},
          {"type": "position_limit", "params": {"max_position_pct": 0.3}},
        ]
    """
    _auto_register()
    chain = RiskChain()
    for item in spec or []:
        if isinstance(item, IBaseRiskRule):
            chain.add(item)
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"风控配置必须是 dict，收到: {type(item).__name__}")
        rtype = str(item.get("type") or item.get("name"))
        params = dict(item.get("params") or {})
        # 允许把参数平铺在同级
        for k, v in item.items():
            if k not in ("type", "name", "params"):
                params.setdefault(k, v)
        chain.add(create_risk(rtype, **params))
    return chain


__all__ = [
    "register_risk",
    "get_risk",
    "create_risk",
    "list_risks",
    "RiskChain",
    "build_risk_chain",
]
