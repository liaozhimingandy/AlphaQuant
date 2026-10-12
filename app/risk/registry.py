#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 风控注册表 + 闸门链 RiskChain
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from app.feature.context import FactorContext
from app.domain.types import Account, Signal
from app.risk.base import IBaseRiskRule, RiskVerdict
from app.strategy.state import DecisionState
from app.common.logging import logger

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
    from app.risk import builtin  # noqa: F401


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


#: A 股硬约束：这些不是"策略偏好"，是**市场的物理规则**。
#: 不拦的后果很具体 —— 涨停追买、跌停杀跌、当日买当日卖、卖空，
#: 每一笔都会被柜台拒单，却照样占用报单额度、污染审计日志，
#: 还会让本地账本以为自己建了仓，后续所有动作错位。
#:
#: 之所以要"强制"而不是靠用户配置：默认配置里没写这几条，
#: 而用户照默认配置上实盘时不会意识到自己缺了闸门。
#: 显式配过同名规则的不重复添加（以用户的参数为准）。
ASTOCK_HARD_RULES: Tuple[str, ...] = (
    "sellable_position",   # 不卖空、不超过可卖量
    "price_limit",         # 涨停不追买、跌停不追杀
    "t_plus_one",          # 当日买入不可当日卖出
)


def build_risk_chain(
    spec: Optional[Iterable[Mapping[str, Any]]] = None,
    enforce_astock: bool = False,
) -> RiskChain:
    """从配置构造闸门链。

    格式::

        [
          {"type": "max_drawdown", "params": {"max_drawdown": 0.2}},
          {"type": "position_limit", "params": {"max_position_pct": 0.3}},
        ]

    :param enforce_astock: 自动补上 :data:`ASTOCK_HARD_RULES`。
        实盘/模拟盘必须为 True —— 这几条是市场规则而非策略选择，
        漏掉它们不会产生"更激进的策略"，只会产生一堆必然废单。
        回测不要开：回测里 ``prev_close`` / ``is_same_day`` 常常缺失，
        开了会引入与实盘不一致的拦截口径（虽然规则本身会跳过，
        但多一层就多一处口径分歧）。
    """
    _auto_register()
    chain = RiskChain()
    seen: set = set()
    for item in spec or []:
        if isinstance(item, IBaseRiskRule):
            chain.add(item)
            seen.add(getattr(item, "name", ""))
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
        seen.add(rtype)

    if enforce_astock:
        for rtype in ASTOCK_HARD_RULES:
            if rtype in seen:
                continue      # 用户自己配过，用他的参数
            try:
                chain.add(create_risk(rtype))
            except Exception as exc:
                logger.error(f"A股硬约束 {rtype} 装配失败（已跳过）: {exc}")
    return chain


__all__ = [
    "register_risk",
    "get_risk",
    "create_risk",
    "list_risks",
    "RiskChain",
    "build_risk_chain",
]
