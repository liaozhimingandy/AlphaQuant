#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : defaults.py
# @Description : 回测默认参数：初始资金 / 手续费 / 滑点 / 默认区间 / 默认策略 …
#                全部来自 config/backtest.json，不散落在代码里
#
#                为什么单独抽一层：
#                  「这次回测用多少钱、算多少手续费」是会变的业务参数，
#                  不是代码常量。写死在 CLI 的 click 默认值里，
#                  面板那边就得再抄一份 —— 两份迟早不一致，
#                  于是"命令行跑出来"和"面板跑出来"结果不一样，还很难查。
#                  统一到一份配置，三处入口（CLI / 面板 / 作业 API）读同一个来源。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from app.core.config import settings
from app.utils.logger import logger


@dataclass
class BacktestDefaults:
    """回测默认参数。"""

    symbol: str = "000001"
    strategy: str = "ma_cross"
    strategy_params: Dict[str, Any] = field(default_factory=dict)

    start: str = "2020-01-01"
    #: 结束日期留空 = 取"今天"
    end: str = ""

    cash: float = 100_000.0
    commission: float = 0.0003
    slippage: float = 0.001
    adjust: str = "qfq"
    data_source: str = "auto"

    risk_free_rate: float = 0.0
    trading_days_per_year: int = 252

    print_log: bool = False
    plot: bool = False
    export: bool = False

    #: 配置文件路径（回测结果里会记录它，便于复现）
    source: str = ""

    # ------------------------------------------------------------------
    def resolved_end(self) -> str:
        return self.end or datetime.now().strftime("%Y-%m-%d")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "strategy_params": dict(self.strategy_params),
            "start": self.start,
            "end": self.resolved_end(),
            "cash": self.cash,
            "commission": self.commission,
            "slippage": self.slippage,
            "adjust": self.adjust,
            "data_source": self.data_source,
            "risk_free_rate": self.risk_free_rate,
            "trading_days_per_year": self.trading_days_per_year,
            "print_log": self.print_log,
            "plot": self.plot,
            "export": self.export,
            "source": self.source,
        }

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "BacktestDefaults":
        """读取默认参数。文件不存在/损坏时返回内置默认值（不是错误）。

        刻意"宽容失败"：默认值只是省事用的，读不到不该让回测跑不起来。
        """
        target = Path(path or settings.BACKTEST_DEFAULTS_CONFIG)
        d = cls(source=str(target))
        # 内置兜底值与 settings 保持一致（环境变量仍能改）
        d.cash = float(settings.DEFAULT_CASH)
        d.commission = float(settings.DEFAULT_COMMISSION)
        d.slippage = float(settings.DEFAULT_SLIPPAGE_PERC)
        if not target.exists():
            return d
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"回测默认参数解析失败（改用内置默认值）: {exc}")
            return d
        if not isinstance(raw, Mapping):
            logger.warning("回测默认参数必须是 JSON 对象，已忽略")
            return d
        return cls.from_dict(raw, source=str(target))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], source: str = "") -> "BacktestDefaults":
        d = cls(source=source)
        d.symbol = str(raw.get("symbol") or d.symbol)
        d.strategy = str(raw.get("strategy") or d.strategy)
        sp = raw.get("strategy_params")
        d.strategy_params = dict(sp) if isinstance(sp, Mapping) else {}
        d.start = str(raw.get("start") or d.start)
        d.end = str(raw.get("end") or "")
        d.cash = _num(raw.get("cash"), d.cash)
        d.commission = _num(raw.get("commission"), d.commission)
        d.slippage = _num(raw.get("slippage", raw.get("slippage_perc")), d.slippage)
        d.adjust = str(raw.get("adjust") or d.adjust)
        d.data_source = str(raw.get("data_source") or d.data_source)
        d.risk_free_rate = _num(raw.get("risk_free_rate"), d.risk_free_rate)
        d.trading_days_per_year = int(
            _num(raw.get("trading_days_per_year"), d.trading_days_per_year)
        )
        d.print_log = bool(raw.get("print_log", d.print_log))
        d.plot = bool(raw.get("plot", d.plot))
        d.export = bool(raw.get("export", d.export))
        return d

    # ------------------------------------------------------------------
    def merge(self, params: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """把默认值与显式入参合并。**显式值优先**，空值不覆盖默认。"""
        out = self.to_dict()
        for key, value in dict(params or {}).items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            out[key] = value
        out["end"] = str(out.get("end") or "").strip() or self.resolved_end()
        return out


def _num(value: Any, default: float) -> float:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = ["BacktestDefaults"]
