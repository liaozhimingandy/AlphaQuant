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
    MarketCenterComponent,
    NewsCenterComponent,
    StrategyManagerComponent,
)
from app.core.engine.engine import BaseQuantEngine
from app.core.market.types import RunMode
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
    **engine_kwargs: Any,
) -> BaseQuantEngine:
    """按配置装配一台引擎。

    组件注册顺序即启停顺序（停止时逆序）：
      行情中心 → 调度器 → 策略中枢 → 新闻中心
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

    # 1) 行情中心
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

    # 2) 调度器（定时任务/异步任务基座）
    engine.register_component(TaskSchedulerComponent())

    # 3) 策略中枢（持有全部任务）
    engine.register_component(StrategyManagerComponent(tasks=cfg.get("tasks") or []))

    # 4) 新闻中心（可选）
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

    logger.info(
        f"引擎装配完成 | 模式={run_mode.value} | 标的={syms} | "
        f"行情模式={m_mode} | 任务数={len(cfg.get('tasks') or [])}"
    )
    return engine


__all__ = ["build_engine", "load_task_config"]
