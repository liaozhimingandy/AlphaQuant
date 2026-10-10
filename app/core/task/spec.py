#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : spec.py
# @Description : 任务的声明式配置 -> 任务实例
#               YAML / DB / API 都只产出 dict，由这里统一装配，改任务不用改代码
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from app.core.market.types import RunMode
from app.core.portfolio.sizer import build_sizer
from app.core.risk.registry import build_risk_chain
from app.core.strategy.spec import build_strategy
from app.core.task.task import QuantTask


@dataclass
class TaskSpec:
    """任务配置。一个任务 = 一个标的 + 一套策略 + 一套风控 + 一份资金。"""
    symbol: str
    strategy: Mapping[str, Any]
    task_id: str = ""
    name: str = ""
    risk: List[Mapping[str, Any]] = field(default_factory=list)
    sizer: Any = None
    initial_cash: float = 100_000.0
    commission: float = 0.0003
    slippage: float = 0.0005
    warmup_bars: int = 60
    max_hold_bars: int = 0
    #: 运行模式。"跟随引擎"是默认（空串），不是硬编码 SIMULATE ——
    #: 否则用 LIVE 起引擎时任务仍在模拟撮合，而看板上一切正常，
    #: 这是最危险的一类错配：用户以为在实盘，其实一笔真单都没发。
    run_mode: str = ""
    enabled: bool = True
    #: 关注的事件标的；留空表示只关注自己的 symbol
    watch_symbols: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "symbol": self.symbol,
            "strategy": dict(self.strategy),
            "risk": [dict(r) for r in self.risk],
            "sizer": self.sizer if isinstance(self.sizer, (str, dict, type(None))) else str(self.sizer),
            "initial_cash": self.initial_cash,
            "commission": self.commission,
            "slippage": self.slippage,
            "warmup_bars": self.warmup_bars,
            "max_hold_bars": self.max_hold_bars,
            "run_mode": self.run_mode,
            "enabled": self.enabled,
            "watch_symbols": list(self.watch_symbols),
            "meta": dict(self.meta),
        }


def _mode_value(run_mode: Any) -> str:
    """把引擎级运行模式归一成字符串（兼容 RunMode 枚举与裸字符串）。"""
    if run_mode is None or run_mode == "":
        return RunMode.SIMULATE.value
    return str(getattr(run_mode, "value", run_mode))


def build_task(spec: TaskSpec | Mapping[str, Any], gateway: Any = None,
               run_mode: Any = None) -> QuantTask:
    """把配置装配成可运行的任务。

    :param gateway: 实盘模式下由引擎注入的**共享**券商网关。
                    不传则各任务自建连接（只适合单机调试）。
    :param run_mode: 引擎级运行模式。任务 spec 没显式指定时就跟随它 ——
                     否则用 LIVE 起引擎、任务却悄悄跑模拟撮合，
                     而面板上一切正常，用户会以为自己在实盘。
    """
    if isinstance(spec, TaskSpec):
        cfg = spec
    elif isinstance(spec, Mapping):
        cfg = parse_task_spec(spec)
    else:
        raise ValueError(f"任务配置必须是 dict/TaskSpec，收到 {type(spec).__name__}")

    strategy = build_strategy(cfg.strategy)
    risk_chain = build_risk_chain(cfg.risk)
    task_id = cfg.task_id or f"{cfg.symbol}-{uuid.uuid4().hex[:6]}"

    return QuantTask(
        task_id=task_id,
        symbol=cfg.symbol,
        strategy=strategy,
        risk_chain=risk_chain,
        initial_cash=cfg.initial_cash,
        commission=cfg.commission,
        slippage=cfg.slippage,
        sizer=build_sizer(cfg.sizer),
        warmup_bars=cfg.warmup_bars,
        max_hold_bars=cfg.max_hold_bars,
        run_mode=RunMode(cfg.run_mode or _mode_value(run_mode)),
        meta={**cfg.meta, "name": cfg.name, "watch_symbols": list(cfg.watch_symbols)},
        gateway=gateway,
    )


def parse_task_spec(raw: Mapping[str, Any]) -> TaskSpec:
    """把 dict 解析成 TaskSpec，缺字段用默认值补齐。"""
    if not isinstance(raw, Mapping):
        raise ValueError(f"任务配置必须是 dict，收到 {type(raw).__name__}")

    symbol = str(raw.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("任务配置缺少 symbol")

    strategy = raw.get("strategy")
    if strategy is None:
        raise ValueError(f"任务 {symbol} 缺少 strategy 配置")
    if isinstance(strategy, str):
        strategy = {"type": strategy}

    return TaskSpec(
        symbol=symbol,
        strategy=strategy,
        task_id=str(raw.get("task_id") or ""),
        name=str(raw.get("name") or ""),
        risk=list(raw.get("risk") or []),
        sizer=raw.get("sizer"),
        initial_cash=float(raw.get("initial_cash", 100_000.0)),
        commission=float(raw.get("commission", 0.0003)),
        slippage=float(raw.get("slippage", 0.0005)),
        warmup_bars=int(raw.get("warmup_bars", 60)),
        max_hold_bars=int(raw.get("max_hold_bars", 0)),
        run_mode=str(raw.get("run_mode") or ""),
        enabled=bool(raw.get("enabled", True)),
        watch_symbols=list(raw.get("watch_symbols") or []),
        meta=dict(raw.get("meta") or {}),
    )


def parse_task_specs(raw: Any) -> List[TaskSpec]:
    """支持单条配置或多条配置（dict / list）。"""
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        items = raw.get("tasks") if "tasks" in raw else [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError(f"无法解析的任务配置: {type(raw).__name__}")

    specs: List[TaskSpec] = []
    for item in items:
        spec = parse_task_spec(item)
        if spec.enabled:
            specs.append(spec)
    return specs


__all__ = ["TaskSpec", "build_task", "parse_task_spec", "parse_task_specs"]
