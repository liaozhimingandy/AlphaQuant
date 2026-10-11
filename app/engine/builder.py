#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : builder.py
# @Description : 引擎装配：把 tasks.json + settings 组装成一台可运行的引擎
#               这是"后台常驻服务"的入口，也是连接配置层与服务层的唯一地方
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

装配顺序的变迁
==============

**重构前**：注册顺序 == 启动顺序。于是 builder 里充满了这种注释::

    # 1) 采集必须在行情中心之前注册：它先起来把数据喂进库
    # 1.2) 数据校验放在采集**之后**：首检延后 20 秒是留给采集的
    # 3.5) 实盘网关必须在策略中枢之前：策略 on_start 会把任务挂到网关上

这些约束每一条都是真的，但它们**只存在于注释里**：
改装配代码的人稍不注意就打乱顺序，而乱序的后果不是报错，
是"挂载静默失败 / 体检把没采到的数据误判成缺口"。

**重构后**：约束被显式表达出来，由引擎自己算顺序：

  * 服务之间：``depends_on`` 声明 + 拓扑排序（成环直接报错）；
  * 服务与其它组件：种类层级（调度 → 服务 → 策略 → 工作单元 → 其它）；
  * 同层内部：注册顺序（只为了日志稳定，不影响正确性）。

所以下面这些 ``register_component`` 的**书写顺序只是可读性**，
改它不会改变行为 —— 这正是本次重构要的效果。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.common.config import settings
from app.engine.base import BaseQuantEngine
from app.domain.types import RunMode
from app.engine.component import (
    CollectionService,
    MarketService,
    NewsService,
    RiskService,
    SchedulerComponent,
    StrategyComponent,
    TradingService,
    WorkerComponent,
)
from app.monitoring.server import MonitorWebComponent
from app.engine.tasking.spec import parse_task_specs
from app.common.logging import logger

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
    with_ext_data: Optional[bool] = None,
    ext_symbols: Optional[List[str]] = None,
    ext_interval: Optional[float] = None,
    with_data_guard: Optional[bool] = None,
    data_guard_symbols: Optional[List[str]] = None,
    snapshot_enabled: Optional[bool] = None,
    snapshot_mode: str = "",
    snapshot_min_gap: Optional[float] = None,
    with_worker: Optional[bool] = None,
    worker_capacity: Optional[int] = None,
    risk_rules: Optional[List[Dict[str, Any]]] = None,
    **engine_kwargs: Any,
) -> BaseQuantEngine:
    """按配置装配一台引擎。

    组件分四层注册（**书写顺序不影响启停顺序**，顺序由依赖拓扑算出来）：

    ========  ============================================
    层        组件
    ========  ============================================
    调度      ``SchedulerComponent``
    服务      ``RiskService`` / ``CollectionService`` /
              ``MarketService`` / ``TradingService`` / ``NewsService``
    策略      ``StrategyComponent``
    工作单元  ``WorkerComponent``
    观测      ``MonitorWebComponent``
    ========  ============================================

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

    # ============================================================
    # 调度层
    # ============================================================
    engine.register_component(SchedulerComponent())

    # ============================================================
    # 服务层
    # ============================================================
    # 1) 风控服务（要在策略组件之前就绪：策略初始化时就把闸门发给任务运行时）
    risk_service = RiskService(rules=risk_rules or cfg.get("risk") or [])
    engine.register_component(risk_service)

    # 2) 采集服务：K 线采集 + 日频截面 + 完整性校验，三者合成一个服务。
    #
    #    **默认装配**（而不是"满足条件才装配"）。这两个概念要分清：
    #      - 「装配」= 服务挂进引擎，面板能看到它、能改频率、能手动补采
    #      - 「启用」= 定时器真的跑起来去拉数据
    #    以前是"不满足条件就不装配"，结果面板上直接显示"本引擎未装配采集服务"，
    #    用户既看不到采集状态，也没法手动补一次数据 —— 白白丢掉一整块能力。
    #
    #    启用条件：非纯回测模式，且 COLLECTOR_ENABLED 打开。
    #    纯回测（BACKTEST）只读历史，再去联网采集既没意义也拖慢启动，
    #    所以装配但不启用（面板会显示"已装配 · 回测模式下已停用"）。
    collector_enabled = settings.COLLECTOR_ENABLED and run_mode != RunMode.BACKTEST
    if with_collector is not None:
        collector_enabled = bool(with_collector)
    kline_reason = (
        "回测模式只读历史数据，不需要联网采集"
        if run_mode == RunMode.BACKTEST
        else ("采集服务已被显式关闭（--no-collector / COLLECTOR_ENABLED=false）"
              if not collector_enabled else "")
    )

    # 日频截面（资金流/筹码/涨停池/题材）：回测/回放模式默认不开 ——
    # 那段行情是死的，去抓"今天的涨停池"对回放没有任何意义，只会拖慢启动。
    want_ext = settings.EXT_DATA_ENABLED if with_ext_data is None else bool(with_ext_data)
    if run_mode == RunMode.BACKTEST and with_ext_data is None:
        want_ext = False

    # 完整性校验：纯回测默认不启用。回测读的是历史库，今天缺不缺跟它无关，
    # 而且回测跑完就自动停，体检往往还没开始就被杀了。
    guard_enabled = settings.DATA_GUARD_ENABLED and run_mode != RunMode.BACKTEST
    if with_data_guard is not None:
        guard_enabled = bool(with_data_guard)

    collection_service = CollectionService(
        symbols=syms,
        # K 线单元
        kline_symbols=list(collector_symbols or settings.collector_symbol_list() or syms),
        kline_interval=collector_interval or settings.COLLECTOR_INTERVAL,
        kline_period=collector_period or settings.COLLECTOR_PERIOD,
        kline_min_interval=settings.COLLECTOR_MIN_INTERVAL,
        kline_config_path=collector_config,
        kline_enabled=collector_enabled,
        # 日频截面单元
        cross_symbols=list(ext_symbols or settings.collector_symbol_list() or syms),
        cross_interval=ext_interval,
        cross_enabled=want_ext,
        # 完整性单元
        guard_symbols=list(data_guard_symbols or settings.collector_symbol_list() or syms),
        guard_enabled=guard_enabled,
        # 服务级
        disabled_reason=kline_reason,
    )
    # 采集服务要能推导标的（没显式指定时跟随引擎），故注入引擎引用。
    # 这是唯一的反向引用，且它只读 —— 服务不通过引擎调别的组件。
    collection_service.set_engine(engine)
    engine.register_component(collection_service)

    # 3) 行情服务
    #    with_market=False 用于"消费别的服务推过来的行情"的场景：
    #    策略服务自己不该再连行情源，否则会与总线上的 bar 重复驱动同一批策略。
    if with_market:
        engine.register_component(
            MarketService(
                symbols=syms,
                mode=m_mode,
                interval=m_interval,
                start=start,
                end=end,
                data_source=data_source,
            )
        )

    # 4) 交易服务（网关 + 落库合成一个）
    #    网关只在 LIVE 模式连真券商；落库在任何模式都工作
    #    （只读档位、模拟盘也要能留下审计记录）。
    enable_store = settings.TRADE_PERSIST_ENABLED if with_store is None else with_store
    trade = TradingService(
        gateway_name=broker_gateway or "",
        endpoint_id=broker_endpoint or "",
        gateway_enabled=(run_mode == RunMode.LIVE),
        store_enabled=enable_store,
        engine=engine,
    )
    engine.register_component(trade)

    # 5) 资讯服务（可选）
    enable_news = settings.NEWS_ENABLED if with_news is None else with_news
    if enable_news:
        news_cfg = cfg.get("news") or {}
        engine.register_component(
            NewsService(
                sources=build_news_sources(news_cfg),
                analyzers=build_news_analyzers(news_cfg),
                interval=float(news_cfg.get("interval") or settings.NEWS_INTERVAL),
            )
        )

    # ============================================================
    # 策略层
    # ============================================================
    sm = StrategyComponent(tasks=cfg.get("tasks") or [])
    sm.set_engine(engine)
    # 引擎的运行模式下沉给任务：任务 spec 没显式指定 run_mode 时跟随引擎。
    # 不做这件事的后果是 LIVE 起引擎而任务跑模拟撮合 —— 面板一切正常，
    # 但一笔真单都没发出去，用户以为在实盘。
    sm.runtime.run_mode = run_mode
    # 共用一个网关连接：券商不允许同账号多连接。
    # 用 lambda 延迟取值 —— 建任务的时刻在 initialize 阶段，
    # 那时网关对象才刚被交易服务创建出来。
    sm.runtime.gateway_provider = lambda: trade.gateway
    engine.register_component(sm)

    # ============================================================
    # 工作单元
    # ============================================================
    enable_worker = settings.WORKER_ENABLED if with_worker is None else bool(with_worker)
    if enable_worker:
        engine.register_component(
            WorkerComponent(capacity=worker_capacity)
        )

    # ============================================================
    # 观测层
    # ============================================================
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

    # ============================================================
    # 收尾连线
    # ============================================================
    # 8) 重放"上次运行期间新增的任务"（改配置与运行时增删可以共存）
    if auto_load_runtime:
        try:
            engine.control.namespace = namespace
            engine.control.load_runtime_tasks()
        except Exception as exc:
            logger.warning(f"运行时任务重放失败（忽略）: {exc}")

    # 重放/运行时新增的任务也要登记进数据库，否则"这轮跑了哪些任务"查不到
    if trade is not None:
        engine.control.on_task_added = trade.register_task

    # ---- 装配摘要日志 ----
    mmode = (snapshot_mode or settings.MONITOR_SNAPSHOT_MODE).lower()
    monitor_desc = ""
    if enable_monitor:
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
        + f" | 采集={'开' if collector_enabled else '停'}"
        f"（K线 {len(collection_service.kline_spec.jobs)} 个任务）"
        + (f" | 截面数据={'开' if want_ext else '关'}"
           f"({len(collection_service.cross_symbols)} 只 @ "
           f"{collection_service.cross_interval:.0f}s)" if want_ext else " | 截面数据=关")
        + (f" | 完整性校验=开({collection_service.guard_start_delay:.0f}s后首检，每"
           f"{collection_service.guard_interval:.0f}s)"
           if guard_enabled else " | 完整性校验=关")
        + f" | 落库={'开' if enable_store else '关'}"
        + f" | 券商网关={'开' if run_mode == RunMode.LIVE else '关（非 LIVE）'}"
        + (f" | 工作单元=开({worker_capacity or settings.WORKER_CAPACITY} 槽)"
           if enable_worker else " | 工作单元=关")
        + f" | 全局风控={len(risk_service.chain)} 条"
    )
    return engine


__all__ = ["build_engine", "load_task_config"]
