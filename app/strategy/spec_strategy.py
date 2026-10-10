#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : spec_strategy.py
# @Description : 声明式回测策略：把 entry/exit 写成规则配置即可回测，零代码
#
#                动机：写一个 backtrader 策略类，门槛不低 —— 要懂 next()/notify_order
#                的生命周期、要自己管挂单与止损单（本项目的 ma_cross 里就有一段
#                "清理僵尸止损单"的血泪逻辑）。但大多数想法其实是
#                "金叉买、死叉卖" 这种**规则的组合**，根本不值得写一个类。
#
#                这个策略把项目已有的因子/规则层接到 backtrader 上：
#                  规则配置（dict/JSON） → 每根K线求值 → 买/卖
#                因此用户在面板或命令行里贴一段 JSON 就能回测，与实盘链路
#                用的是**同一套因子与规则**（结果可比，不会两边口径不同）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import backtrader as bt

from app.core.factor.context import FactorContext
from app.core.market.series import BarSeries
from app.core.market.types import Bar
from app.core.rule.base import IBaseRule
from app.core.rule.spec import build_rule
from app.utils.logger import logger

STRATEGY_NAME = "declarative"


_MA = lambda fast, slow: {  # noqa: E731
    "left": "ma", "right": "ma",
    "left_params": {"period": fast}, "right_params": {"period": slow},
}

#: 没给规则时用的默认（5/20 金叉买、死叉卖）。
#: 与 ma_cross 一样，策略本来就该有合理默认值 —— 但会在日志里说明
#: "现在跑的是默认规则"，避免用户以为自己写的规则生效了。
DEFAULT_SPEC: Dict[str, Any] = {
    "entry": {"cross_up": _MA(5, 20)},
    "exit": {"cross_down": _MA(5, 20)},
}


def _as_spec(value: Any) -> Optional[Dict[str, Any]]:
    """把 dict / JSON 字符串 / 文件路径统一成规则 spec。"""
    if value is None or value == "" :
        return None
    if isinstance(value, dict):
        return value
    text = str(value).strip()
    if not text:
        return None
    # 看起来像路径就先当文件读；读不到再当 JSON 解析
    if text.endswith(".json") or "/" in text or "\\" in text:
        p = Path(text)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"规则文件 {text} 解析失败: {exc}") from exc
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError(
            f"无法解析规则配置（既不是文件也不是合法 JSON）: {text[:120]!r} … {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"规则配置必须是 JSON 对象，收到 {type(data).__name__}")
    return data


class DeclarativeStrategy(bt.Strategy):
    """由规则配置驱动的回测策略。

    参数（面板/命令行可直接传）::

        entry         开仓规则。dict / JSON 字符串 / .json 文件路径
        exit          平仓规则。同上
        entry_file    开仓规则文件（与 entry 二选一）
        exit_file     平仓规则文件
        spec_file     一次给全 {"entry": ..., "exit": ...} 的文件
        warmup        至少多少根K线之后才允许开仓（默认 0，靠因子预热自然拦）
        maxlen        滚动窗口长度（默认 500，够 250 周均线）
        lot_size      A 股一手股数，默认 100
        cash_buffer   单次开仓最多用可用资金的比例（默认 0.95）
        stop_loss_pct 止损比例，0 = 不止损（默认 0）
        printlog      是否打印每根K线的决策（默认 False）

    规则写法与实盘链路完全一致，例如::

        {"all": [
            {"cross_up": {"left": "ma", "right": "ma",
                          "left_params": {"period": 5}, "right_params": {"period": 20}}},
            {"factor": "ma_spread", "op": "gt", "value": 0.0,
             "params": {"fast": 5, "slow": 20}}
        ]}
    """

    STRATEGY_NAME = STRATEGY_NAME

    params = dict(
        entry=None,
        exit=None,
        spec=None,
        entry_file="",
        exit_file="",
        spec_file="",
        symbol="",
        warmup=0,
        maxlen=500,
        lot_size=100,
        cash_buffer=0.95,
        stop_loss_pct=0.0,
        printlog=False,
    )

    # ---------------------------------------------------------------
    def __init__(self) -> None:
        spec = self._load_spec()
        try:
            self.entry_rule: Optional[IBaseRule] = (
                build_rule(spec["entry"]) if spec.get("entry") else None
            )
            self.exit_rule: Optional[IBaseRule] = (
                build_rule(spec["exit"]) if spec.get("exit") else None
            )
        except Exception as exc:
            # 配置错了要在**开跑之前**炸掉。跑到一半才报错会让用户
            # 以为是策略逻辑问题，其实是配置写错了。
            raise ValueError(f"规则配置无法构造: {exc}") from exc

        if self.entry_rule is None and self.exit_rule is None:
            logger.warning(
                "声明式策略没有配置任何规则，改用默认规则（5/20 金叉买、死叉卖）。"
                "要跑自己的规则请传 spec / entry / exit（或 entry_file / spec_file）。"
            )
            spec = dict(DEFAULT_SPEC)
            self.entry_rule = build_rule(spec["entry"])
            self.exit_rule = build_rule(spec["exit"])

        self.symbol = str(self.p.symbol or getattr(self.data, "_name", "") or "SYMBOL")
        self.series = BarSeries(self.symbol, maxlen=int(self.p.maxlen))
        self._order = None
        self._buy_price: Optional[float] = None
        self._decisions = 0

    def _load_spec(self) -> Dict[str, Any]:
        spec: Dict[str, Any] = {}
        if self.p.spec:
            loaded = _as_spec(self.p.spec)
            if loaded:
                spec.update(loaded)
        if self.p.spec_file:
            loaded = _as_spec(str(self.p.spec_file))
            if loaded:
                spec.update(loaded)
        if self.p.entry_file:
            spec["entry"] = _as_spec(str(self.p.entry_file))
        if self.p.exit_file:
            spec["exit"] = _as_spec(str(self.p.exit_file))
        if self.p.entry:
            spec["entry"] = _as_spec(self.p.entry)
        if self.p.exit:
            spec["exit"] = _as_spec(self.p.exit)
        return spec

    # ---------------------------------------------------------------
    def _append_bar(self) -> None:
        dt = self.data.datetime.datetime(0)
        self.series.append(Bar(
            symbol=self.symbol,
            dt=dt,
            open=float(self.data.open[0]),
            high=float(self.data.high[0]),
            low=float(self.data.low[0]),
            close=float(self.data.close[0]),
            volume=float(self.data.volume[0] or 0.0),
        ))

    def _check(self, rule: Optional[IBaseRule]) -> bool:
        if rule is None:
            return False
        try:
            return bool(rule.is_satisfied(FactorContext(self.symbol, self.series)))
        except Exception as exc:
            # 单个因子算不出来（预热期数据不足最常见）不该中断整轮回测
            logger.debug(f"规则求值失败（按不触发处理）: {exc}")
            return False

    def _buy_size(self, price: float) -> int:
        if price <= 0:
            return 0
        cash = float(self.broker.getcash()) * float(self.p.cash_buffer)
        lot = max(1, int(self.p.lot_size))
        return max(0, int(cash / price / lot)) * lot

    def log(self, msg: str) -> None:
        if not self.p.printlog:
            return
        logger.info(f"[declarative] {self.data.datetime.date(0)} | {msg}")

    # ---------------------------------------------------------------
    def notify_order(self, order) -> None:
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status == order.Completed:
            if order.isbuy():
                self._buy_price = float(order.executed.price)
                self.log(f"买入 {abs(order.executed.size):.0f} 股 @ {order.executed.price:.3f}")
            else:
                self._buy_price = None
                self.log(f"卖出 {abs(order.executed.size):.0f} 股 @ {order.executed.price:.3f}")
        elif order.status in (order.Canceled, order.Rejected, order.Margin):
            self.log(f"订单未成交 | {order.getstatusname()}")
        self._order = None

    def next(self) -> None:
        self._append_bar()
        self._decisions += 1

        # 有单在外就不动：避免同一根K线里反复下单
        if self._order is not None:
            return
        if len(self.series) < max(2, int(self.p.warmup)):
            return

        price = float(self.data.close[0])

        if self.position:
            # 止损优先：它是保命的，不该排在策略判断后面
            if self.p.stop_loss_pct and self._buy_price:
                pnl = (price - self._buy_price) / self._buy_price
                if pnl <= -float(self.p.stop_loss_pct):
                    self.log(f"止损触发 | 浮亏 {pnl * 100:.2f}%")
                    self._order = self.close()
                    return
            if self._check(self.exit_rule):
                self.log(f"平仓信号 | {price:.3f}")
                self._order = self.close()
            return

        if self._check(self.entry_rule):
            size = self._buy_size(price)
            if size <= 0:
                self.log("开仓信号出现，但可用资金不足")
                return
            self.log(f"开仓信号 | {price:.3f} | {size} 股")
            self._order = self.buy(size=size)

    def stop(self) -> None:
        logger.info(
            f"[declarative] 回测结束 | 决策 {self._decisions} 根K线 | "
            f"期末权益 {self.broker.getvalue():,.2f}"
        )


__all__ = ["DeclarativeStrategy"]
