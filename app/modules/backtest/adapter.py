#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测适配器 —— 让 backtrader 能直接跑 ``app/strategy`` 里的自研策略。

## 为什么要这一层

历史上回测和实盘是两套平行的策略实现：

    app/research/strategies/*   基于 backtrader（bt.Strategy / bt.Indicator）
    app/strategy/*              自研运行时（IBaseStrategy / FactorContext）

同一条"MA 金叉"被写了两遍，于是出现了一个必然结果：
**回测跑出来的曲线，和实盘跑出来的不是同一套逻辑。**
回测再漂亮，也只能证明另一份代码曾经有效。

这一层就是用来终结这个问题的：回测不再有自己的策略实现，
而是把自研策略``包装``成 backtrader 认识的样子。

## 适配了什么

| 自研侧（app.modules.strategy）        | backtrader 侧        | 这一层做的事                     |
|---|---|---|
| ``on_bar(ctx, state)``         | ``next()``           | 每根 K 线构造 Bar → BarSeries → FactorContext |
| ``DecisionState``              | ``self.position``    | 把 bt 的持仓快照翻译成只读状态         |
| ``Signal(side=Side.BUY/SELL)`` | ``buy() / sell()``   | 信号 → 下单，并做 A 股整百股处理       |
| ``on_init/on_start/on_stop``   | 生命周期              | 对应转发，让策略知道回测何时开始结束   |

回测与实盘剩下的差异（撮合精度、滑点、复权口径）是**环境差异**，
不再是**代码差异** —— 这个区别很重要。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Type

import backtrader as bt

from app.common.logging import logger
from app.domain.series import BarSeries
from app.domain.types import Bar, Side, SignalSource
from app.modules.factor.context import FactorContext
from app.modules.strategy.state import DecisionState

#: 回测里给因子的历史窗口。够了：绝大多数技术指标用不到 500 根以外的历史。
_DEFAULT_LOOKBACK = 500

#: A 股最小交易单位
_LOT_SIZE = 100


class StrategyBridge(bt.Strategy):
    """把一个自研 :class:`~app.modules.strategy.base.IBaseStrategy` 实例跑在 backtrader 上。

    用法（通常由 :func:`create_bridged_strategy` 代劳）::

        cls = create_bridged_strategy("MaCrossStrategy", fast=5, slow=20)
        cerebro.addstrategy(cls)

    子类通过类属性拿到构造信息，而不是 backtrader 的 ``params`` ——
    后者要求每个参数都预先声明，与"策略参数由用户自由配置"的需求相冲突。
    """

    #: 自研策略在 :mod:`app.modules.strategy` 注册表里的名字
    strategy_name: str = ""
    #: 传给自研策略构造函数的参数
    strategy_params: Dict[str, Any] = {}
    #: 传给 FactorContext 的动态特征（新闻情感、LLM 评分等）；回测通常为空
    features: Dict[str, Any] = {}

    def __init__(self) -> None:
        super().__init__()
        self._impl = self._build_impl()
        self._series: Optional[BarSeries] = None
        self._task_id = self.strategy_name or "backtest"
        #: 上一根 K 线成交价，用于估算持仓成本
        if self._impl is not None:
            self._impl.on_init(task_id=self._task_id, symbol=self._symbol())

    # ------------------------------------------------------------------ 构造
    def _build_impl(self):
        # 延迟导入：app.modules.strategy 会在 import 时注册所有内置策略，
        # 没必要让回测链路在模块加载期就把整个策略层拉起来。
        from app.modules.strategy import create_strategy

        if not self.strategy_name:
            raise ValueError(
                "StrategyBridge 缺少 strategy_name："
                "请用 create_bridged_strategy(...) 生成子类，不要直接用基类"
            )
        return create_strategy(self.strategy_name, **dict(self.strategy_params))

    def _symbol(self) -> str:
        name = getattr(getattr(self.data, "_name", None), "__str__", None)
        return str(getattr(self.data, "_name", "") or "")

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        super().start()
        if self._impl is not None:
            self._impl.on_start()

    def stop(self) -> None:
        # 放在 try 里：策略自己的收尾逻辑出错不该吞掉回测结果
        try:
            if self._impl is not None:
                self._impl.on_stop()
        except Exception as exc:  # pragma: no cover
            logger.warning(f"策略 {self.strategy_name} on_stop 异常（忽略）: {exc}")
        super().stop()

    def notify_order(self, order) -> None:
        super().notify_order(order)
        if self._impl is not None:
            try:
                self._impl.on_order(order)
            except Exception as exc:  # pragma: no cover
                logger.debug(f"策略 {self.strategy_name} on_order 忽略: {exc}")

    # ------------------------------------------------------------------ 主循环
    def next(self) -> None:
        if self._impl is None:
            return
        self._append_bar()

        ctx = FactorContext(
            symbol=self._symbol(),
            series=self._series,
            features=dict(self.features),
            now=self._bar_datetime(),
        )
        state = self._snapshot_state()
        try:
            signal = self._impl.on_bar(ctx, state)
        except Exception as exc:
            # 算不出来就当没信号，但要留痕 —— 静默失败最难受
            logger.warning(f"策略 {self.strategy_name} 决策异常，本次跳过: {exc}")
            return
        if signal is None:
            return
        self._execute(signal.side, signal.reason, getattr(signal, "strength", 1.0))

    # ------------------------------------------------------------------ 内部
    def _bar_datetime(self) -> Optional[datetime]:
        try:
            return self.data.datetime.datetime(0)
        except Exception:  # pragma: no cover
            return None

    def _append_bar(self) -> None:
        """把当前 K 线追加到滑窗里。"""
        dt = self._bar_datetime() or datetime.min
        sym = self._symbol()
        bar = Bar(
            symbol=sym,
            dt=dt,
            open=float(self.data.open[0]),
            high=float(self.data.high[0]),
            low=float(self.data.low[0]),
            close=float(self.data.close[0]),
            volume=float(self.data.volume[0] or 0),
            amount=float(getattr(self.data, "amount", [0.0])[0] or 0.0),
        )
        if self._series is None:
            self._series = BarSeries(sym, maxlen=_DEFAULT_LOOKBACK)
        self._series.append(bar)

    def _snapshot_state(self) -> DecisionState:
        """把 backtrader 的账户/持仓翻译成自研策略认得的只读状态。"""
        pos = self.position
        size = int(getattr(pos, "size", 0) or 0)
        price = float(self.data.close[0])
        cash = float(self.broker.getcash())
        value = float(self.broker.getvalue())
        return DecisionState(
            task_id=self._task_id,
            symbol=self._symbol(),
            has_position=size != 0,
            position_size=size,
            avg_price=float(getattr(pos, "price", 0.0) or 0.0),
            last_price=price,
            cash=cash,
            equity=value,
            peak_equity=max(getattr(self, "_peak", value), value),
        )

    def _execute(self, side: Side, reason: str, strength: float = 1.0) -> None:
        buy_side = Side.BUY
        if side is buy_side:
            if self.position.size > 0:
                return  # 已持仓，A 股不做加仓
            size = self._sizing()
            if size <= 0:
                return
            self.buy(size=size)
        else:
            if self.position.size <= 0:
                return
            self.sell(size=abs(int(self.position.size)))
        logger.debug(
            f"[回测] {side.value} {self._symbol()} "
            f"strength={strength:.2f} reason={reason}"
        )

    def _sizing(self) -> int:
        """按可用现金算整百股数量，留 1% 手续费余量避免被资金校验拒单。"""
        price = float(self.data.close[0])
        if price <= 0:
            return 0
        usable = float(self.broker.getcash()) * 0.99
        lots = int(usable // (price * _LOT_SIZE))
        return lots * _LOT_SIZE


def create_bridged_strategy(
    name: str,
    params: Optional[Dict[str, Any]] = None,
    features: Optional[Dict[str, Any]] = None,
) -> Type[bt.Strategy]:
    """把一个自研策略包装成 backtrader 可直接 ``addstrategy`` 的类。

    :param name: 策略在 :mod:`app.modules.strategy` 里的注册名（如 ``MaCrossStrategy``）
    :param params: 传给策略构造函数的参数（如 ``{"fast": 5, "slow": 20}``）
    :param features: 注入给 FactorContext 的动态特征，回测里一般不用
    """
    return type(
        f"Bridged{name}",
        (StrategyBridge,),
        {
            "strategy_name": name,
            "strategy_params": dict(params or {}),
            "features": dict(features or {}),
        },
    )


__all__ = ["StrategyBridge", "create_bridged_strategy", "SignalSource"]
