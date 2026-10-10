#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : builder.py
# @Description : 引擎装配：把 tasks.json + settings 组装成一台可运行的引擎
#               这是"后台常驻服务"的入口，也是连接配置层与组件层的唯一地方
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.engine.component import TaskSchedulerComponent
from app.core.engine.components import (
    DataCollectorComponent,
    LiveGatewayComponent,
    MarketCenterComponent,
    NewsCenterComponent,
    StrategyManagerComponent,
    TradingStoreComponent,
)
from app.core.engine.engine import BaseQuantEngine
from app.core.market.types import RunMode
from app.core.monitor.web import MonitorWebComponent
from app.core.task.spec import parse_task_specs
from app.utils.logger import logger

DEFAULT_TASK_FILE = "tasks.json"


def load_task_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载任务配置文件（JSON）。

    支持两种结构::

        {"tasks": [...]}          # 推荐
        [{...}, {...}]            # 也接受裸数组
    """
    file = Path(path or settings.TASK_CONFIG)
    if not file.exists():
        logger.warning(f"任务配置文件不存在: {file}，将以空任务启动")
        return {"tasks": []}
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"任务配置文件解析失败 {file}: {exc}") from exc

    if isinstance(raw, list):
        raw = {"tasks": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"任务配置必须是 object 或 array，收到 {type(raw).__name__}")
    raw.setdefault("tasks", [])
    return raw


def _symbols_of(cfg: Dict[str, Any]) -> List[str]:
    """从任务配置里推导需要订阅行情的标的（去重保序）。"""
    out: List[str] = []
    for spec in parse_task_specs(cfg.get("tasks")):
        for s in [spec.symbol, *(spec.watch_symbols or [])]:
            if s and s not in out:
                out.append(s)
    return out


def build_news_analyzers(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """构造分析器链：LLM 在前（若配了 key），规则版兜底。

    分析器是**链**：第一个能给出结论的就采用，后面的不再执行。
    配置里显式声明的分析器优先；没声明才用 settings 推导出来的默认分析器。
    """
    declared: List[Dict[str, Any]] = [dict(a) for a in (cfg.get("analyzers") or [])]
    if declared:
        return declared

    if settings.NEWS_ANALYZER == "llm":
        return [
            {
                "type": "llm",
                "params": {
                    "api_base": settings.LLM_API_BASE,
                    "api_key": settings.LLM_API_KEY,
                    "model": settings.LLM_MODEL,
                    "timeout": settings.LLM_TIMEOUT,
                },
            },
            {"type": "keyword"},  # LLM 失败时的兜底
        ]
    return [{"type": "keyword"}]


def build_news_sources(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """新闻源：配置文件优先，其次环境变量。"""
    sources: List[Dict[str, Any]] = [dict(s) for s in (cfg.get("sources") or [])]
    if not sources:
        urls = settings.news_source_list()
        if urls:
            sources.append(
                {
                    "type": "rss",
                    "urls": urls,
                    "keyword_map": cfg.get("keyword_map") or {},
                }
            )
    return sources


def build_engine(
    task_config: Optional[str] = None,
    mode: str = "",
    symbols: Optional[List[str]] = None,
    market_mode: str = "",
    market_interval: Optional[float] = None,
    start: str = "",
    end: str = "",
    data_source: str = "db",
    with_news: Optional[bool] = None,
    with_market: bool = True,
    with_monitor: Optional[bool] = None,
    monitor_host: str = "",
    monitor_port: Optional[int] = None,
    monitor_interval: Optional[float] = None,
    namespace: str = "default",
    auto_load_runtime: bool = True,
    with_collector: Optional[bool] = None,
    collector_interval: Any = None,
    collector_period: Any = None,
    collector_symbols: Optional[List[str]] = None,
    collector_config: Optional[str] = None,
    with_store: Optional[bool] = None,
    broker_gateway: str = "",
    broker_endpoint: str = "",
    snapshot_enabled: Optional[bool] = None,
    snapshot_mode: str = "",
    snapshot_min_gap: Optional[float] = None,
    **engine_kwargs: Any,
) -> BaseQuantEngine:
    """按配置装配一台引擎。

    组件注册顺序即启停顺序（停止时逆序）：
      行情中心 → 调度器 → 策略中枢 → 新闻中心 → 监控面板

    监控放最后注册，于是它**最先被停止**——先把面板关了再拆业务组件，
    不会出现"面板还在读一个正在被拆掉的引擎"。
    """
    settings.ensure_dirs()
    cfg = load_task_config(task_config)

    run_mode = RunMode((mode or cfg.get("mode") or "SIMULATE"))
    syms = list(symbols or settings.symbol_list() or _symbols_of(cfg))
    m_mode = str(market_mode or cfg.get("market_mode") or settings.MARKET_MODE)
    m_interval = float(
        market_interval
        if market_interval is not None
        else (cfg.get("market_interval") or settings.MARKET_INTERVAL)
    )
    start = str(start or cfg.get("start") or "")
    end = str(end or cfg.get("end") or "")

    engine_cfg: Dict[str, Any] = {
        "RUN_MODE": run_mode.value,
        "BACKTEST_IDLE_TIMEOUT": settings.BACKTEST_IDLE_TIMEOUT,
        "BACKTEST_AUTO_STOP": settings.BACKTEST_AUTO_STOP,
        **engine_kwargs,
    }

    engine = BaseQuantEngine.create(engine_cfg)

    # 1) 行情采集服务
    #    必须在行情中心之前注册：这样它先起来把数据喂进库，行情中心才有东西可读。
    #
    #    **默认装配**（而不是"满足条件才装配"）。这两个概念要分清：
    #      - 「装配」= 组件挂进引擎，面板能看到它、能改频率、能手动补采
    #      - 「启用」= 定时器真的跑起来去拉数据
    #    以前是"不满足条件就不装配"，结果面板上直接显示"本引擎未装配采集服务"，
    #    用户既看不到采集状态，也没法手动补一次数据 —— 白白丢掉一整块能力。
    #
    #    启用条件：非纯回测模式，且 COLLECTOR_ENABLED 打开。
    #    纯回测（BACKTEST）只读历史，再去联网采集既没意义也拖慢启动，
    #    所以装配但不启用（面板会显示"已装配 · 回测模式下已停用"，一目了然）。
    collector_enabled = settings.COLLECTOR_ENABLED and run_mode != RunMode.BACKTEST
    if with_collector is not None:
        collector_enabled = bool(with_collector)

    collector = DataCollectorComponent(
        symbols=list(collector_symbols or settings.collector_symbol_list() or syms),
        interval=collector_interval or settings.COLLECTOR_INTERVAL,
        period=collector_period or settings.COLLECTOR_PERIOD,
        min_interval=settings.COLLECTOR_MIN_INTERVAL,
        config_path=collector_config,
        enabled=collector_enabled,
        disabled_reason=(
            "回测模式只读历史数据，不需要联网采集"
            if run_mode == RunMode.BACKTEST
            else ("采集服务已被显式关闭（--no-collector / COLLECTOR_ENABLED=false）"
                  if not collector_enabled else "")
        ),
    )
    engine.register_component(collector)
    enable_collector = collector_enabled

    # 2) 行情中心
    #    with_market=False 用于"消费别的服务推过来的行情"的场景：
    #    策略服务自己不该再连行情源，否则会与总线上的 bar 重复驱动同一批策略。
    if with_market:
        engine.register_component(
            MarketCenterComponent(
                symbols=syms,
                mode=m_mode,
                interval=m_interval,
                start=start,
                end=end,
                data_source=data_source,
            )
        )

    # 3) 调度器（定时任务/异步任务基座）
    engine.register_component(TaskSchedulerComponent())

    # 3.5) 实盘网关（仅 LIVE 模式）
    #      必须在策略中枢之前注册：策略中枢 on_start 会把各任务挂到网关上，
    #      网关没就绪的话挂载会静默失败（回报就路由不回来了）。
    live_component = None
    if run_mode == RunMode.LIVE:
        live_component = LiveGatewayComponent(
            gateway_name=broker_gateway or "",
            endpoint_id=broker_endpoint or "",
        )
        engine.register_component(live_component)

    # 4) 策略中枢（持有全部任务）
    sm = StrategyManagerComponent(tasks=cfg.get("tasks") or [])
    sm.set_engine(engine)
    # 引擎的运行模式下沉给任务：任务 spec 没显式指定 run_mode 时跟随引擎。
    # 不做这件事的后果是 LIVE 起引擎而任务跑模拟撮合 —— 面板一切正常，
    # 但一笔真单都没发出去，用户以为在实盘。
    sm.runtime.run_mode = run_mode
    if live_component is not None:
        # 共用一个网关连接：券商不允许同账号多连接。
        # 用 lambda 延迟取值 —— 建任务的时刻在 initialize 阶段，
        # 那时网关对象才刚被 live_component 创建出来。
        sm.runtime.gateway_provider = lambda: live_component.gateway
    engine.register_component(sm)

    # 5) 新闻中心（可选）
    enable_news = settings.NEWS_ENABLED if with_news is None else with_news
    if enable_news:
        sources = build_news_sources(cfg.get("news") or {})
        engine.register_component(
            NewsCenterComponent(
                sources=sources,
                analyzers=build_news_analyzers(cfg.get("news") or {}),
                interval=float(
                    (cfg.get("news") or {}).get("interval") or settings.NEWS_INTERVAL
                ),
            )
        )

    # 6) 交易数据落库（默认开）：订单/成交/权益/重要事件 → SQLite
    #    放在监控面板之前注册：面板的 /api/db/* 一启动就要能查到数据。
    enable_store = settings.TRADE_PERSIST_ENABLED if with_store is None else with_store
    if enable_store:
        engine.register_component(
            TradingStoreComponent(engine=engine, enabled=True)
        )

    # 7) 监控面板（可选）：内嵌 Twisted Web + 定时快照
    enable_monitor = settings.MONITOR_ENABLED if with_monitor is None else with_monitor
    if enable_monitor:
        engine.register_component(
            MonitorWebComponent(
                engine=engine,
                host=monitor_host or settings.MONITOR_HOST,
                port=monitor_port if monitor_port is not None else settings.MONITOR_PORT,
                snapshot_interval=monitor_interval,
                snapshot_enabled=snapshot_enabled,
                snapshot_mode=snapshot_mode or "",
                snapshot_min_gap=snapshot_min_gap,
                namespace=namespace,
            )
        )

    # 8) 重放"上次运行期间新增的任务"（改配置与运行时增删可以共存）
    if auto_load_runtime:
        try:
            engine.control.namespace = namespace
            engine.control.load_runtime_tasks()
        except Exception as exc:
            logger.warning(f"运行时任务重放失败（忽略）: {exc}")

    # 重放/运行时新增的任务也要登记进数据库，否则"这轮跑了哪些任务"查不到
    store = engine.get_component("trading_store")
    if store is not None:
        engine.control.on_task_added = store.register_task

    collector_desc = ""
    collector = engine.get_component("data_collector")
    if collector is not None:
        jobs = len(getattr(getattr(collector, "spec", None), "jobs", []) or [])
        state = "启用" if collector.spec.enabled else "停用"
        collector_desc = f" | 采集={state}({jobs} 个任务)"
    store_desc = ""
    if store is not None:
        store_desc = f" | 落库={'开' if store.enabled else '关'}"
    monitor_desc = ""
    if enable_monitor:
        mmode = (snapshot_mode or settings.MONITOR_SNAPSHOT_MODE).lower()
        # 注意 snapshot_enabled 可能是 None（"用默认"），不能直接当假值用 ——
        # 否则日志会写"快照关"，而实际是开着的，排查时会被这条日志带偏。
        snap_on = (
            settings.MONITOR_SNAPSHOT_ENABLED if snapshot_enabled is None
            else bool(snapshot_enabled)
        ) and mmode != "off"
        monitor_desc = (
            f" | 监控=开({monitor_host or settings.MONITOR_HOST}:"
            f"{monitor_port if monitor_port is not None else settings.MONITOR_PORT})"
            f" | 快照={mmode}" if snap_on else " | 监控=开(快照关)"
        )

    logger.info(
        f"引擎装配完成 | 模式={run_mode.value} | 标的={syms if with_market else '（外部总线供给）'} | "
        f"行情模式={m_mode if with_market else 'off'} | 任务数={len(cfg.get('tasks') or [])}"
        + monitor_desc
        + collector_desc
        + store_desc
    )
    return engine


__all__ = ["build_engine", "load_task_config"]
