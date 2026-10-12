#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : defaults.py
# @Description : 内置调度规则：把"数据 → 因子 → 策略 → 风控 → 交易"串成一条链
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
这张表是系统里**全部自动流转**的唯一清单
==========================================

想回答"K 线到了之后系统会自动做什么"，读这张表就够了，
不用再去翻采集服务、因子服务、策略组件各自的源码。

编排原则（每一条都对应一类真实事故）
------------------------------------

1. **只有落库/修正才触发因子**：盘中的实时 bar（``market.bar.received``）
   直接驱动策略，不触发因子重算 —— 否则盘中每根 tick 都重算一次因子，
   因子缓存永远命中不了，CPU 全烧在重复计算上。
2. **下单链路不允许自动重试**：``trade.execute`` 的 ``max_retries`` 为 0，
   且 :data:`NON_RETRIABLE_TASK_TYPES` 会在 EngineTask 构造时再强制归零一次。
   网络超时后"到底发没发到券商"是未知状态，自动重试就是重复下单。
3. **长链路每一步都有幂等键**：同一批 K 线重复到达只产生一次因子任务；
   同一份因子快照只触发一次策略运行。缺了它，事件重投会变成级联放大。
4. **回测模式不跑实盘链**：``engine.ready → market.kline.collect`` 等规则
   带服务可用性条件，回测引擎没有这些服务时规则自动不生效，
   不需要另外维护一套"回测专用"的调度逻辑。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.runtime.events.schema import EngineEvent
from app.runtime.events.types import EventType, Pool, Queue, TaskType
from app.runtime.scheduling.rules import (
    ConditionFn,
    Rule,
    dedup_key,
    normalize_key,
    symbols_of,
)
from app.runtime.tasking.models import Priority


# =============================================================================
# 条件工具
# =============================================================================
def _component(ctx: Any, name: str) -> Optional[Any]:
    """从调度器上下文取组件。取不到一律返回 None（调度器可能没挂引擎，如单测）。"""
    engine = getattr(ctx, "engine", None)
    if engine is None:
        return None
    getter = getattr(engine, "get_component", None)
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:
        return None


def _flag(service: str, attr: Optional[str] = None, default: bool = True) -> ConditionFn:
    """条件：某服务的某个开关为真。

    :param attr: 服务上的属性/属性方法名（如 ``kline_enabled``）；
                 省略则只看组件 ``enabled``。
    """
    def _cond(event: EngineEvent, ctx: Any) -> bool:
        comp = _component(ctx, service)
        if comp is None:
            return default
        if not bool(getattr(comp, "enabled", True)):
            return False
        if attr is None:
            return True
        target = getattr(comp, attr, None)
        if callable(target):
            target = target()
        return default if target is None else bool(target)
    return _cond


def _mode_not(*modes: str) -> ConditionFn:
    """条件：运行模式**不是**这些值之一。"""
    banned = {m.upper() for m in modes}

    def _cond(event: EngineEvent, ctx: Any) -> bool:
        mode = getattr(ctx, "run_mode", None)
        value = getattr(mode, "value", mode)
        return str(value or "").upper() not in banned
    return _cond


def _has_symbols(event: EngineEvent, ctx: Any) -> bool:
    """条件：事件里确实带了标的。

    没有标的就跑一个"因子更新"任务，等于对全市场重算 ——
    所以宁可不跑（并在日志里说清楚）。
    """
    return bool(symbols_of(event, ctx))


def _category_in(*wanted: str) -> ConditionFn:
    """条件：``data.collection.requested`` 的 category 命中给定集合。

    空 category 视为命中（"没指定类别"= 走默认那条 K 线规则）。
    """
    wanted_set = {w.lower() for w in wanted}

    def _cond(event: EngineEvent, ctx: Any) -> bool:
        data = event.data or {}
        cats = data.get("categories")
        if isinstance(cats, (list, tuple)) and cats:
            return any(str(c).lower() in wanted_set for c in cats)
        cat = str(data.get("category") or "").lower()
        if not cat:
            return "" in wanted_set or "kline" in wanted_set
        return cat in wanted_set
    return _cond


def _not_executed(event: EngineEvent, ctx: Any) -> bool:
    """条件：这笔交易意图还没被执行过（防重复下单的最后一道闸）。"""
    return not bool((event.data or {}).get("executed"))


# =============================================================================
# 参数 / 幂等键 工具
# =============================================================================
def _kline_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    data = event.data or {}
    return {
        "symbols": symbols_of(event, ctx),
        "period": str(data.get("period") or ""),
        "start": str(data.get("start") or ""),
        "end": str(data.get("end") or ""),
        "rows": int(data.get("rows") or 0),
    }


def _factor_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    data = event.data or {}
    return {
        "symbols": symbols_of(event, ctx),
        "day": str(data.get("day") or data.get("date") or ""),
        "factor_set": list(data.get("factor_set") or []),
        "reason": event.event_type,
    }


def _factor_dedup(event: EngineEvent, ctx: Any) -> str:
    data = event.data or {}
    day = str(data.get("day") or data.get("date") or "")
    syms = ",".join(sorted(symbols_of(event, ctx)))
    return normalize_key(dedup_key(TaskType.FACTOR_CALCULATE, syms[:120], day))


def _strategy_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    data = event.data or {}
    return {
        "symbols": symbols_of(event, ctx),
        "task_ids": list(data.get("task_ids") or []),
        "strategy": str(data.get("strategy") or ""),
        "reason": event.event_type,
    }


def _signal_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    """信号 → 风控任务。信号对象直接放进 params 交给交易域处理。"""
    data = event.data or {}
    return {
        "signal": data.get("signal"),
        "task_id": str(data.get("task_id") or ""),
        "symbol": str(data.get("symbol") or (data.get("signal").symbol if data.get("signal") else "")),
        "side": str(data.get("side") or ""),
        "intention": data.get("intention") or data.get("order"),
    }


def _trade_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    data = event.data or {}
    return {
        "intention": data.get("intention") or data.get("order"),
        "task_id": str(data.get("task_id") or ""),
        "symbol": str(data.get("symbol") or ""),
        "side": str(data.get("side") or ""),
        "approved_size": data.get("approved_size") or data.get("size"),
        "risk_note": str(data.get("note") or data.get("reason") or ""),
    }


def _backfill_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    data = event.data or {}
    return {
        "symbols": symbols_of(event, ctx),
        "period": str(data.get("period") or ""),
        "start": str(data.get("start") or data.get("from") or ""),
        "end": str(data.get("end") or data.get("to") or ""),
    }


def _has_range(event: EngineEvent, ctx: Any) -> bool:
    """条件：缺口区间有效（起止齐全）。区间不全的补采只会采到错的东西。"""
    data = event.data or {}
    start = str(data.get("start") or data.get("from") or "")
    end = str(data.get("end") or data.get("to") or "")
    return bool(start and end) and _has_symbols(event, ctx)


def _news_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    """新闻事件 → 舆情分析任务参数。

    资讯服务发布 ``news_received`` 时把整条 :class:`NewsEvent` 挂在
    ``event`` 键上（里面带着 ``symbols`` 与可能的 ``analysis``），
    这里把它摊平，处理器才能一眼看出"这条新闻分析过没有" ——
    这是防止 新闻 → 舆情 → 新闻 死循环的关键。
    """
    data = event.data or {}
    envelope = data.get("event")
    analysis = data.get("analysis")
    symbols = symbols_of(event, ctx)
    if not symbols and envelope is not None:
        raw = getattr(envelope, "symbols", None)
        symbols = [str(s) for s in (raw or []) if s]
    if analysis is None and envelope is not None:
        analysis = getattr(envelope, "analysis", None)
    return {
        "news": data.get("news") or getattr(envelope, "news", None),
        "news_id": str(data.get("news_id") or data.get("id") or ""),
        "analysis": analysis,
        "symbols": symbols,
    }


def _universe_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    data = event.data or {}
    return {
        "symbols": symbols_of(event, ctx),
        "pool": str(data.get("pool") or ""),
        "reason": event.event_type,
    }


def _collection_params(event: EngineEvent, ctx: Any) -> Dict[str, Any]:
    data = event.data or {}
    return {
        "symbols": symbols_of(event, ctx),
        "categories": list(data.get("categories") or []),
        "category": str(data.get("category") or ""),
        "period": str(data.get("period") or ""),
    }


# =============================================================================
# 规则表
# =============================================================================
def build_default_rules() -> List[Rule]:
    """内置规则清单。顺序即**同事件内的执行顺序**（列表顺序 = 建任务顺序）。"""
    return [
        # ---------------- 引擎就绪：把该跑的常规活都建出来 ----------------
        Rule(
            name="ready_recover",
            event_type=EventType.ENGINE_READY,
            task_type=TaskType.SYSTEM_RECOVER,
            queue=Queue.SYSTEM,
            priority=Priority.CRITICAL,
            max_retries=1,
            description="引擎就绪后检查是否有上次遗留的在途任务需要恢复",
        ),
        Rule(
            name="ready_collect_kline",
            event_type=EventType.ENGINE_READY,
            task_type=TaskType.MARKET_KLINE_COLLECT,
            condition=_flag("collection_service", "kline_enabled", default=False),
            params_fn=_kline_params,
            priority=Priority.HIGH,
            max_retries=2,
            retry_interval=10.0,
            timeout_seconds=600.0,
            description="引擎就绪后立刻采集一轮 K 线，避免刚启动时空窗",
        ),
        Rule(
            name="ready_integrity_check",
            event_type=EventType.ENGINE_READY,
            task_type=TaskType.DATA_INTEGRITY_CHECK,
            condition=_flag("collection_service", "guard_enabled", default=False),
            priority=Priority.LOW,
            max_retries=1,
            timeout_seconds=900.0,
            description="引擎就绪后做一次数据完整性体检",
        ),
        Rule(
            name="ready_maintenance_validate",
            event_type=EventType.ENGINE_READY,
            task_type=TaskType.MAINTENANCE_VALIDATE,
            priority=Priority.IDLE,
            timeout_seconds=300.0,
            description="启动后校验一次关键表/目录是否可写",
        ),

        # ---------------- 数据域 ----------------
        Rule(
            name="kline_gap_backfill",
            event_type=EventType.MARKET_KLINE_GAP_DETECTED,
            task_type=TaskType.MARKET_KLINE_BACKFILL,
            condition=_has_range,
            params_fn=_backfill_params,
            dedup_fn=lambda e, c: normalize_key(
                dedup_key(
                    TaskType.MARKET_KLINE_BACKFILL,
                    ",".join(sorted(symbols_of(e, c)))[:120],
                    (e.data or {}).get("start") or (e.data or {}).get("from"),
                    (e.data or {}).get("end") or (e.data or {}).get("to"),
                )
            ),
            priority=Priority.HIGH,
            max_retries=2,
            retry_interval=15.0,
            timeout_seconds=900.0,
            description="检测到 K 线缺口 → 补采该区间",
        ),
        Rule(
            name="kline_updated_factor",
            event_type=EventType.MARKET_KLINE_UPDATED,
            task_type=TaskType.FACTOR_CALCULATE,
            condition=_has_symbols,
            params_fn=_factor_params,
            dedup_fn=_factor_dedup,
            priority=Priority.NORMAL,
            max_retries=2,
            retry_interval=8.0,
            timeout_seconds=600.0,
            description="K 线落库/修正 → 重算相关标的的因子",
        ),
        Rule(
            name="news_collected_sentiment",
            event_type=EventType.NEWS_ARTICLE_COLLECTED,
            task_type=TaskType.SENTIMENT_ANALYZE,
            params_fn=_news_params,
            priority=Priority.NORMAL,
            max_retries=2,
            timeout_seconds=300.0,
            description="新闻入库 → 做情感/舆情分析",
        ),
        Rule(
            name="sentiment_updated_factor",
            event_type=EventType.NEWS_SENTIMENT_UPDATED,
            task_type=TaskType.FACTOR_CALCULATE,
            condition=_has_symbols,
            params_fn=_factor_params,
            dedup_fn=_factor_dedup,
            priority=Priority.NORMAL,
            max_retries=1,
            timeout_seconds=600.0,
            description="舆情评分变化 → 更新相关因子",
        ),
        Rule(
            name="fundamental_updated_factor",
            event_type=EventType.FUNDAMENTAL_DATA_UPDATED,
            task_type=TaskType.FACTOR_CALCULATE,
            condition=_has_symbols,
            params_fn=_factor_params,
            dedup_fn=_factor_dedup,
            priority=Priority.NORMAL,
            max_retries=1,
            timeout_seconds=600.0,
            description="财务数据更新 → 更新基本面因子",
        ),

        # ---------------- 因子 → 策略 ----------------
        Rule(
            name="factor_snapshot_strategy",
            event_type=EventType.FACTOR_SNAPSHOT_UPDATED,
            task_type=TaskType.STRATEGY_RUN,
            condition=_mode_not("BACKTEST"),
            params_fn=_strategy_params,
            dedup_fn=lambda e, c: normalize_key(
                dedup_key(
                    TaskType.STRATEGY_RUN,
                    ",".join(sorted(symbols_of(e, c)))[:120],
                    (e.data or {}).get("day") or (e.data or {}).get("date"),
                )
            ),
            priority=Priority.HIGH,
            max_retries=1,
            timeout_seconds=300.0,
            description="因子快照更新 → 运行依赖这些因子的策略（回测模式不自动跑）",
        ),
        Rule(
            name="strategy_run_requested",
            event_type=EventType.STRATEGY_RUN_REQUESTED,
            task_type=TaskType.STRATEGY_RUN,
            params_fn=_strategy_params,
            priority=Priority.HIGH,
            timeout_seconds=300.0,
            description="显式请求运行策略（面板/CLI 触发）",
        ),
        Rule(
            name="universe_updated_factor",
            event_type=EventType.UNIVERSE_UPDATED,
            task_type=TaskType.FACTOR_CALCULATE,
            condition=_has_symbols,
            params_fn=_universe_params,
            dedup_fn=_factor_dedup,
            priority=Priority.LOW,
            max_retries=1,
            timeout_seconds=900.0,
            description="股票池变化 → 给新进标的补算因子",
        ),

        # ---------------- 策略 → 风控 → 交易 ----------------
        Rule(
            name="signal_generated_risk",
            event_type=EventType.STRATEGY_SIGNAL_GENERATED,
            task_type=TaskType.RISK_EVALUATE,
            params_fn=_signal_params,
            priority=Priority.CRITICAL,
            max_retries=0,
            timeout_seconds=30.0,
            description="产生交易信号 → 必须先过风控（不重试：过期信号重试无意义）",
        ),
        Rule(
            name="risk_check_requested",
            event_type=EventType.RISK_CHECK_REQUESTED,
            task_type=TaskType.RISK_EVALUATE,
            params_fn=_signal_params,
            priority=Priority.CRITICAL,
            max_retries=0,
            timeout_seconds=30.0,
            description="显式请求风控评估",
        ),
        Rule(
            name="risk_approved_execute",
            event_type=EventType.RISK_CHECK_APPROVED,
            task_type=TaskType.TRADE_EXECUTE,
            condition=_not_executed,
            params_fn=_trade_params,
            priority=Priority.CRITICAL,
            max_retries=0,          # 下单永不自动重试，见文件头说明
            timeout_seconds=30.0,
            description="风控放行 → 下单（幂等由订单状态机保证，不靠重试）",
        ),

        # ---------------- 采集请求分发 ----------------
        Rule(
            name="collection_requested_kline",
            event_type=EventType.DATA_COLLECTION_REQUESTED,
            task_type=TaskType.MARKET_KLINE_COLLECT,
            condition=_category_in("kline", "bar", ""),
            params_fn=_collection_params,
            priority=Priority.HIGH,
            max_retries=2,
            timeout_seconds=600.0,
            description="请求采集 → K 线采集任务",
        ),
        Rule(
            name="collection_requested_fundamental",
            event_type=EventType.DATA_COLLECTION_REQUESTED,
            task_type=TaskType.FUNDAMENTAL_COLLECT,
            condition=_category_in("fundamental"),
            params_fn=_collection_params,
            priority=Priority.NORMAL,
            max_retries=2,
            timeout_seconds=900.0,
            description="请求采集 → 基本面采集任务",
        ),
        Rule(
            name="collection_requested_news",
            event_type=EventType.DATA_COLLECTION_REQUESTED,
            task_type=TaskType.NEWS_COLLECT,
            condition=_category_in("news"),
            params_fn=_collection_params,
            priority=Priority.NORMAL,
            max_retries=2,
            timeout_seconds=300.0,
            description="请求采集 → 新闻采集任务",
        ),

        # ---------------- 运维 ----------------
        Rule(
            name="maintenance_due_cleanup",
            event_type=EventType.SYSTEM_MAINTENANCE_DUE,
            task_type=TaskType.MAINTENANCE_LOG_CLEANUP,
            priority=Priority.IDLE,
            timeout_seconds=600.0,
            description="运维窗口到点 → 清理过期日志",
        ),
        Rule(
            name="maintenance_due_archive",
            event_type=EventType.SYSTEM_MAINTENANCE_DUE,
            task_type=TaskType.MAINTENANCE_ARCHIVE,
            priority=Priority.IDLE,
            timeout_seconds=1800.0,
            description="运维窗口到点 → 归档历史快照",
        ),
        Rule(
            name="recovery_requested",
            event_type=EventType.ENGINE_RECOVERY_REQUESTED,
            task_type=TaskType.SYSTEM_RECOVER,
            queue=Queue.SYSTEM,
            priority=Priority.CRITICAL,
            max_retries=1,
            description="检测到待恢复任务 → 建恢复任务",
        ),
    ]


__all__ = ["build_default_rules"]
