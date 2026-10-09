#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : sizer.py
# @Description : 仓位计算：把"信号强度"翻译成"买多少股"
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from app.core.market.types import Account, Side
from app.utils.logger import logger


class IBaseSizer:
    """仓位计算器接口。"""

    name = "base"

    def size(
        self,
        side: Side,
        price: float,
        account: Account,
        strength: float = 1.0,
        position_size: int = 0,
        scale: float = 1.0,
    ) -> int:
        raise NotImplementedError


class FixedSizeSizer(IBaseSizer):
    """固定股数。最简单，也最容易把账户买穿——仅用于调试。

    注意：构造参数 ``size`` 存到 ``self.fixed_size``，**不能**存成 ``self.size``，
    否则会把同名的 :meth:`size` 方法覆盖成 int（这类属性遮蔽极难排查）。
    """
    name = "fixed"

    def __init__(self, size: int = 100, lot_size: int = 100, **kw) -> None:
        self.fixed_size = int(size)
        self.lot_size = max(1, int(lot_size))

    def size(self, side, price, account, strength=1.0, position_size=0, scale=1.0) -> int:
        if side == Side.SELL:
            return position_size if position_size > 0 else 0
        raw = self.fixed_size * scale
        return _to_lot(raw, self.lot_size)


class PercentEquitySizer(IBaseSizer):
    """按权益百分比买入：最常用，天然控制单笔风险敞口。

    size = 权益 * pct * 信号强度 * 风控缩放 / 价格，再向下取整到 lot。
    """
    name = "percent"

    def __init__(
        self,
        pct: float = 0.20,
        lot_size: int = 100,
        fee_rate: float = 0.0003,
        **kw,
    ) -> None:
        self.pct = float(pct)
        self.lot_size = max(1, int(lot_size))
        self.fee_rate = float(fee_rate)

    def size(self, side, price, account, strength=1.0, position_size=0, scale=1.0) -> int:
        if side == Side.SELL:
            return position_size if position_size > 0 else 0
        if price <= 0:
            return 0
        equity = account.cash  # 简化：权益≈可用现金+持仓市值由调用方保证
        budget = equity * self.pct * max(0.0, min(1.0, strength)) * max(0.0, scale)
        affordable = account.available / (price * (1 + self.fee_rate))
        raw = min(budget / (price * (1 + self.fee_rate)), affordable)
        return _to_lot(raw, self.lot_size)


class AllInSizer(IBaseSizer):
    """全额买入（回测常见口径）。"""
    name = "all_in"

    def __init__(self, lot_size: int = 100, fee_rate: float = 0.0003, **kw) -> None:
        self.lot_size = max(1, int(lot_size))
        self.fee_rate = float(fee_rate)

    def size(self, side, price, account, strength=1.0, position_size=0, scale=1.0) -> int:
        if side == Side.SELL:
            return position_size if position_size > 0 else 0
        if price <= 0:
            return 0
        affordable = account.available / (price * (1 + self.fee_rate))
        return _to_lot(affordable * max(0.0, scale), self.lot_size)


_SIZERS = {c.name: c for c in (FixedSizeSizer, PercentEquitySizer, AllInSizer)}


def build_sizer(spec: Optional[Any] = None, **defaults: Any) -> IBaseSizer:
    """从配置构造 sizer。spec 可以是字符串 / dict / 已构造实例 / None。"""
    if spec is None:
        spec = {"type": defaults.pop("sizer", "percent")}
    if isinstance(spec, IBaseSizer):
        return spec
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, Mapping):
        raise ValueError(f"sizer 配置必须是 dict/str，收到 {type(spec).__name__}")

    stype = str(spec.get("type") or spec.get("name") or "percent")
    params: Dict[str, Any] = dict(defaults)
    params.update(spec.get("params") or {})
    for k, v in spec.items():
        if k not in ("type", "name", "params"):
            params.setdefault(k, v)

    klass = _SIZERS.get(stype)
    if klass is None:
        logger.warning(f"未知 sizer: {stype}，回退到 percent")
        klass = PercentEquitySizer
    sizer = klass(**params)
    # 防御：构造参数若与 size 方法同名会把它覆盖成标量，这里提前炸掉而不是等到下单时
    if not callable(getattr(sizer, "size", None)):
        raise TypeError(
            f"{klass.__name__} 的 size 方法被实例属性遮蔽，"
            f"请检查构造参数是否与 size() 同名"
        )
    return sizer


def _to_lot(raw: float, lot_size: int) -> int:
    """向下取整到交易单位（A股 100 股一手）。"""
    if raw <= 0 or not _finite(raw):
        return 0
    lots = int(raw // lot_size)
    return lots * lot_size


def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


__all__ = [
    "IBaseSizer",
    "FixedSizeSizer",
    "PercentEquitySizer",
    "AllInSizer",
    "build_sizer",
]
