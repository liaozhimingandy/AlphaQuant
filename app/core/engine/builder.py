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
    MarketCenterComponent,
    NewsCenterComponent,
    StrategyManagerComponent,
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
    #    回测模式默认不开——离线回放读的是历史快照，再去实时采集既没意义也拖慢启动。
    #    其余模式只在 market_mode=poll 时自动开启，理由很实在：
    #    poll 每隔一段时间读一次"最新行情"，但没人往库里写新数据的话，
    #    它读到的永远是同一批旧行——服务像在跑，行情其实从未更新。
    #    采集服务补的就是这个缺口。replay 的数据本来就是死的，不需要采集。
    collector_wanted = (str(m_mode).lower() == "poll") and run_mode != RunMode.BACKTEST
    if not settings.COLLECTOR_ENABLED:
        collector_wanted = False
    if with_collector is not None:
        collector_wanted = bool(with_collector)
    enable_collector = False
    if collector_wanted:
        collector = DataCollectorComponent(
            symbols=list(collector_symbols or settings.collector_symbol_list() or syms),
            interval=collector_interval or settings.COLLECTOR_INTERVAL,
            period=collector_period or settings.COLLECTOR_PERIOD,
            min_interval=settings.COLLECTOR_MIN_INTERVAL,
            config_path=collector_config,
        )
        engine.register_component(collector)
        enable_collector = True

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

    # 4) 策略中枢（持有全部任务）
    engine.register_component(StrategyManagerComponent(tasks=cfg.get("tasks") or []))

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

    # 6) 监控面板（可选）：内嵌 Twisted Web + 定时快照
    enable_monitor = settings.MONITOR_ENABLED if with_monitor is None else with_monitor
    if enable_monitor:
        engine.register_component(
            MonitorWebComponent(
                engine=engine,
                host=monitor_host or settings.MONITOR_HOST,
                port=monitor_port if monitor_port is not None else settings.MONITOR_PORT,
                snapshot_interval=monitor_interval,
                namespace=namespace,
            )
        )

    # 7) 重放"上次运行期间新增的任务"（改配置与运行时增删可以共存）
    if auto_load_runtime:
        try:
            engine.control.namespace = namespace
            engine.control.load_runtime_tasks()
        except Exception as exc:
            logger.warning(f"运行时任务重放失败（忽略）: {exc}")

    collector = engine.get_component("data_collector") if enable_collector else None
    collector_desc = ""
    if collector is not None:
        jobs = getattr(collector, "spec", None)
        n_jobs = len(getattr(jobs, "jobs", []) or [])
        collector_desc = f" | 采集服务={'开' if n_jobs else '开(无标的)'} ({n_jobs} 个任务)"

    logger.info(
        f"引擎装配完成 | 模式={run_mode.value} | 标的={syms if with_market else '（外部总线供给）'} | "
        f"行情模式={m_mode if with_market else 'off'} | 任务数={len(cfg.get('tasks') or [])} | "
        f"监控={'开' if enable_monitor else '关'}"
        + (f"({monitor_host or settings.MONITOR_HOST}:{monitor_port or settings.MONITOR_PORT})"
           if enable_monitor else "")
        + collector_desc
    )
    return engine


__all__ = ["build_engine", "load_task_config"]
