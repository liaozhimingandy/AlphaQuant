#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : multi_factor_strategy.py
# @Description : 多因子策略（事件引擎链路）：把一组因子的加权分变成买卖决策
#
#                为什么需要它（已有 score 策略了）：
#                  score 策略消费的是**打分引擎**的结论（六维、面向选股，
#                  依赖资金流/题材/基本面这些日频截面数据）。
#                  而研究阶段产出的往往是"一组量价表达式因子"——
#                  它们不需要任何截面数据，只要K线就能算。
#                  让这批因子直接可交易，中间不该隔着打分引擎。
#
#                设计要点：
#                  1. **每个因子先各自标准化再加权**。
#                     动量是百分比、量能比是倍数，直接相加等于让量纲大的那个
#                     独占话语权 —— 这是多因子组合最经典的错。
#                  2. **默认用因子库里全部启用因子，等权**。
#                     先能用起来，再谈优化；权重可以从 `main.py factor fit`
#                     （Lasso）的结果拷进来。
#                  3. **因子算不出来时跳过它，并按剩余因子重新归一化权重**。
#                     而不是当成 0 分 —— 那会凭空制造一个看空信号。
#                  4. **总分缺失（一个因子都算不出来）时不动手**。
#
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.modules.factor.context import FactorContext
from app.domain.types import Side, Signal, SignalSource
from app.modules.strategy.base import IBaseStrategy
from app.modules.strategy.registry import register_strategy
from app.common.logging import logger

#: z-score 的截断区间：极端值不该主导总分（往往是数据毛刺）
Z_CLIP = 3.0
#: 标准化回看窗口
Z_WINDOW = 60


def _zscore_last(values: Any, window: int = Z_WINDOW) -> Optional[float]:
    """取序列最后一个值的滚动 z-score。"""
    try:
        import numpy as np

        arr = np.asarray(values, dtype=float)
    except Exception:
        return None
    if arr.size == 0 or math.isnan(arr[-1]):
        return None
    w = int(max(5, window))
    hist = arr[-w:]
    hist = hist[~_isnan(hist)]
    if hist.size < 5:
        return None
    mu = float(hist.mean())
    sd = float(hist.std())
    if sd == 0 or sd != sd:
        return 0.0
    z = (float(arr[-1]) - mu) / sd
    return max(-Z_CLIP, min(Z_CLIP, z))


def _isnan(arr) -> Any:
    import numpy as np

    return np.isnan(arr)


@register_strategy
class MultiFactorStrategy(IBaseStrategy):
    """多因子加权策略。

    配置示例（tasks.json）::

        "strategy": {
          "type": "multi_factor",
          "params": {
            "factors": [
              {"name": "mom_20", "weight": 1.0},
              {"name": "vol_ratio", "weight": 0.5}
            ],
            "entry": 0.55,
            "exit": -0.30
          }
        }

    ``factors`` 省略时用因子库里**全部启用**的因子（等权）。
    """

    name = "multi_factor"
    description = "多因子加权：把因子库里的因子标准化后加权求和"

    def __init__(
        self,
        factors: Optional[Sequence[Any]] = None,
        entry: float = 0.55,
        exit: float = -0.30,          # noqa: A002 —— 与配置字段同名，改了会读不到
        only_sources: str = "preset,user,mined",
        window: int = Z_WINDOW,
        reverse: bool = False,
        **params: Any,
    ) -> None:
        super().__init__(factors=factors, entry=entry, exit=exit, **params)
        self.entry = float(entry)
        self.exit = float(exit)
        self.window = int(window)
        self.reverse = bool(reverse)
        self.only_sources = str(only_sources or "")
        self.weights: Dict[str, float] = self._parse_weights(factors)
        if self.exit >= self.entry:
            raise ValueError(
                f"exit({self.exit}) 必须小于 entry({self.entry})，否则会刚买就卖"
            )

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_weights(factors: Optional[Sequence[Any]]) -> Dict[str, float]:
        """接受三种写法：["mom_20"] / [{"name":"mom_20","weight":2}] / {"mom_20":2}。"""
        out: Dict[str, float] = {}
        if not factors:
            return out
        if isinstance(factors, dict):
            for k, v in factors.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    out[str(k)] = 1.0
            return out
        for item in factors:
            if isinstance(item, str):
                out[item] = 1.0
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("factor") or "").strip()
                if not name:
                    continue
                try:
                    out[name] = float(item.get("weight", 1.0))
                except (TypeError, ValueError):
                    out[name] = 1.0
        return out

    def _default_weights(self) -> Dict[str, float]:
        """没配因子时用因子库里的（按来源过滤）。"""
        try:
            from app.modules.factor.library import get_library

            wanted = {s.strip() for s in self.only_sources.split(",") if s.strip()}
            specs = get_library().list()
            if wanted:
                specs = [s for s in specs if s.source in wanted]
            return {s.name: 1.0 for s in specs}
        except Exception as exc:
            logger.debug(f"读取因子库失败: {exc}")
            return {}

    # ------------------------------------------------------------------
    def composite(self, ctx: FactorContext) -> Optional[float]:
        """算加权综合分。**算不出的因子跳过并重分配权重**。"""
        weights = self.weights or self._default_weights()
        if not weights:
            return None

        num, den = 0.0, 0.0
        used: List[str] = []
        for name, w in weights.items():
            z = self._factor_z(name, ctx)
            if z is None:
                continue
            num += z * float(w)
            den += abs(float(w))
            used.append(name)
        if den <= 0 or not used:
            return None
        value = num / den
        return -value if self.reverse else value

    def _factor_z(self, name: str, ctx: FactorContext) -> Optional[float]:
        """单个因子 → 标准化分。

        表达式因子能拿到整条序列，所以做真正的滚动 z-score；
        其它因子（内置/截面）只能拿到当前值，退化成"原始值"，
        —— 那种情况请显式配权重与阈值，别指望它和其它因子可比。
        """
        expr = self._expr_of(name)
        if expr:
            try:
                from app.modules.factor.expr import evaluate

                arr = evaluate(expr, {
                    "open": ctx.series.open, "high": ctx.series.high,
                    "low": ctx.series.low, "close": ctx.series.close,
                    "volume": ctx.series.volume, "amount": ctx.series.amount,
                })
                return _zscore_last(arr, self.window)
            except Exception as exc:
                logger.debug(f"因子 {name} 序列计算失败: {exc}")
                return None
        try:
            from app.modules.factor.registry import create_factor, has_factor

            if not has_factor(name):
                return None
            v = create_factor(name).compute(ctx)
        except Exception as exc:
            logger.debug(f"因子 {name} 计算失败: {exc}")
            return None
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f

    @staticmethod
    def _expr_of(name: str) -> str:
        try:
            from app.modules.factor.registry import factor_registry

            klass = factor_registry().get(name)
            return getattr(klass, "_expr", "") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    def decide(
        self,
        ctx: FactorContext,
        state: Any,
        source: SignalSource = SignalSource.BAR,
        event: Any = None,
    ) -> Optional[Signal]:
        score = self.composite(ctx)
        if score is None:
            return None

        if not state.has_position:
            if score >= self.entry:
                # 强度按超出阈值的程度给，下游 sizer 据此定仓位
                span = max(0.2, abs(self.entry) + 0.5)
                strength = min(1.0, 0.5 + (score - self.entry) / (span * 2))
                return self.make_signal(
                    state, Side.BUY, source,
                    reason=f"多因子综合分 {score:+.2f} ≥ {self.entry:+.2f}",
                    strength=strength,
                    meta={"multi_factor": score},
                )
            return None

        if score <= self.exit:
            return self.make_signal(
                state, Side.SELL, source,
                reason=f"多因子综合分 {score:+.2f} ≤ {self.exit:+.2f}",
                strength=1.0,
                meta={"multi_factor": score},
            )
        return None


__all__ = ["MultiFactorStrategy"]
