#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : handlers.py
# @Description : 默认任务处理器：把任务类型接到**真实业务服务**上
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
这一层是"事件/任务机制"与"业务实现"之间的**适配器**
====================================================

执行池不知道什么是因子、什么是风控；业务服务不知道什么是队列、什么是重试。
这个文件负责把两边接起来，并且只做这一件事。

三条写作纪律
------------

1. **找不到服务就抛异常，不要静默返回成功**。
   "采集服务没装配"却返回 ``{"rows": 0}``，会让面板显示采集成功，
   而实际一条数据都没进来。抛异常 → 任务失败 → 死信里有明确原因。

2. **业务导入一律放在函数体内**。
   执行层如果被业务模块反向依赖（``app.modules.*`` 大多会 import
   ``app.runtime.*``），顶层导入必然成环。延迟导入同时让
   "只测事件链路、不装配业务"的单测跑得飞快。

3. **处理器必须幂等**。
   任务可能因为重试、事件重投、恢复扫描被执行多次。
   凡是"再执行一次结果就变了"的实现（重复下单、重复记账），都是 bug。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.common.logging import logger
from app.runtime.tasking.models import EngineTask
from app.runtime.workers.registry import HandlerRegistry


# =============================================================================
# 小工具
# =============================================================================
def _engine(ctx: Any) -> Any:
    return getattr(ctx, "engine", None)


def _component(ctx: Any, name: str) -> Optional[Any]:
    engine = _engine(ctx)
    if engine is None:
        return None
    getter = getattr(engine, "get_component", None)
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:
        return None


def _require(ctx: Any, name: str, what: str) -> Any:
    """取必需组件，**取不到就抛**。"""
    comp = _component(ctx, name)
    if comp is None:
        raise RuntimeError(
            f"{what}需要组件 {name!r}，但当前引擎没有装配它"
            f"（服务不存在 → 任务失败，而不是假装成功）"
        )
    return comp


def _publish(ctx: Any, event_type: str, **data: Any) -> None:
    """处理器完成后发布后续事件（推进工作流）。

    为什么由处理器发而不是调度器发：只有处理器知道**这件事真的做成了**。
    调度器在"任务成功"时统一发事件，会把"采集跑通了但一条数据都没采到"
    也当成成功往下推 —— 下游因子就会对着空气算一遍。
    """
    bus = getattr(ctx, "bus", None) or getattr(_engine(ctx), "event_bus", None)
    if bus is None:
        return
    source = str(data.pop("source", "") or "task_handler")
    logger.debug(f"处理器发布后续事件: {event_type}")
    bus.publish(event_type, source=source, **data)


def _symbols(task: EngineTask) -> List[str]:
    raw = (task.params or {}).get("symbols") or []
    return [str(s) for s in raw if s]


# =============================================================================
# 系统域
# =============================================================================
def handle_system_recover(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """恢复上次遗留的在途任务。

    只把**未终结**的任务重新入队。终态任务不重跑 ——
    重启一次就把今天所有成功的采集重做一遍，既浪费配额又可能引入重复数据。
    """
    store = getattr(ctx, "store", None)
    if store is None:
        return {"recovered": 0, "note": "无任务存储，跳过恢复"}
    resumed = 0
    for pending in store.non_terminal():
        if pending.task_id == task.task_id:
            continue
        if store.requeue(pending):
            resumed += 1
    if resumed:
        logger.info(f"恢复扫描：重新入队 {resumed} 个未终结任务")
    return {"recovered": resumed}


def handle_snapshot_persist(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """立刻落一份运行快照。"""
    monitor = _component(ctx, "monitor")
    flush = getattr(monitor, "flush", None)
    if not callable(flush):
        return {"persisted": False, "note": "监控组件不在场"}
    flush()
    return {"persisted": True}


# =============================================================================
# 行情域
# =============================================================================
def handle_kline_collect(task: EngineTask, ctx: Any) -> Any:
    """K 线采集。返回采集服务的 Deferred（执行池会等它落地）。"""
    svc = _require(ctx, "collection_service", "K 线采集")
    if not bool(getattr(svc, "kline_enabled", False)):
        raise RuntimeError("K 线采集单元未启用，任务无法执行")
    symbols = _symbols(task)
    symbol = symbols[0] if len(symbols) == 1 else ""
    return svc.collect_now(symbol)


def handle_kline_backfill(task: EngineTask, ctx: Any) -> Any:
    """按区间补采。

    公开接口只支持"按标的补一轮"，区间参数进日志与结果供核对；
    真正的区间裁剪在采集服务内部按缺口表处理。这里不假装有更细的能力。
    """
    from twisted.internet import defer

    svc = _require(ctx, "collection_service", "K 线补采")
    if not bool(getattr(svc, "kline_enabled", False)):
        raise RuntimeError("K 线采集单元未启用，任务无法执行")
    params = task.params or {}
    symbols = _symbols(task)
    if not symbols:
        return {"rows": 0, "note": "没有指定标的，跳过补采"}

    deferreds = [svc.collect_now(sym) for sym in symbols]
    if len(deferreds) == 1:
        return deferreds[0]

    def _combine(results: Any) -> Dict[str, Any]:
        rows = 0
        errors: List[str] = []
        for ok, item in results:
            if not ok:
                errors.append(str(item))
                continue
            if isinstance(item, dict):
                rows += int(item.get("rows") or 0)
                errors.extend(str(e) for e in (item.get("errors") or []))
        return {
            "rows": rows,
            "symbols": len(symbols),
            "period": str(params.get("period") or ""),
            "range": [str(params.get("start") or ""), str(params.get("end") or "")],
            "errors": errors,
        }

    d = defer.DeferredList(deferreds, consumeErrors=True)
    d.addCallback(_combine)
    return d


def handle_integrity_check(task: EngineTask, ctx: Any) -> Any:
    """数据完整性体检。"""
    svc = _require(ctx, "collection_service", "数据体检")
    symbols = _symbols(task) or None
    return svc.check_now(symbols)


# =============================================================================
# 研究域
# =============================================================================
def handle_news_collect(task: EngineTask, ctx: Any) -> Any:
    """新闻采集。"""
    svc = _require(ctx, "news_service", "新闻采集")
    poll = getattr(svc, "poll_now", None)
    if not callable(poll):
        raise RuntimeError(
            "资讯服务缺少 poll_now()，无法由任务驱动采集"
        )
    return poll()


def handle_sentiment_analyze(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """舆情分析。

    **幂等且不成环**：资讯服务在轮询时已经内联分析过一次，
    这里只在"拿到一条尚未分析的新闻"时才真的分析，并推送
    ``news.sentiment.updated``。若载荷里已经带分析结果，直接跳过 ——
    否则会形成 新闻 → 舆情 → 新闻 的死循环。
    """
    params = task.params or {}
    news = params.get("news")
    if news is None:
        return {"analyzed": False, "note": "无新闻对象"}
    if params.get("analysis") is not None:
        return {"analyzed": False, "note": "载荷已带分析结果，跳过重复分析"}

    svc = _component(ctx, "news_service")
    center = getattr(svc, "news_center", None)
    analyze = getattr(center, "analyze", None)
    if not callable(analyze):
        return {"analyzed": False, "note": "资讯服务不在场或无分析器"}

    analysis = analyze(news)
    symbols = _symbols(task)
    _publish(
        ctx,
        "news.sentiment.updated",
        news=news,
        analysis=analysis,
        symbols=symbols,
        source="sentiment_handler",
    )
    return {"analyzed": True, "symbols": symbols}


def handle_fundamental_collect(task: EngineTask, ctx: Any) -> Any:
    """基本面（估值 + 财务）定向采集。"""
    svc = _require(ctx, "collection_service", "基本面采集")
    symbols = _symbols(task)
    categories = list((task.params or {}).get("categories") or []) or ["fundamental"]
    return svc.collect_categories(symbols, categories)


def handle_factor_calculate(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """因子更新。

    因子是本项目的**惰性计算 + TTL 缓存**结构（``FeatureProvider``），
    所以"重算因子"在实现上就是"让相关标的的缓存失效"，
    下一次读取时自然会用最新数据重算。
    这比主动全量重算省得多，也天然幂等（失效十次和失效一次结果相同）。
    """
    from app.modules.factor.features import get_provider

    params = task.params or {}
    symbols = _symbols(task)
    provider = get_provider()
    if not symbols:
        provider.invalidate()
        count = -1  # -1 表示"全部"
    else:
        for sym in symbols:
            provider.invalidate(sym)
        count = len(symbols)

    day = str(params.get("day") or "")
    _publish(
        ctx,
        "factor.snapshot.updated",
        symbols=symbols,
        day=day,
        source="factor_handler",
    )
    return {"invalidated": count, "day": day, "reason": params.get("reason") or ""}


def handle_pool_refresh(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """刷新股票池：拉候选 → 同步资金流/筹码/基本面 → 多维度打分 → 落库。

    为什么是"任务"而不是面板直接调
    --------------------------------
    之前股票池刷新是面板直接 ``StockPool().refresh()`` 在一个线程里跑，
    只写了 control audit 一条内存记录 —— 既不进任务明细、也不进死信、
    引擎重启也无从恢复。这与回测曾经的问题一致，现已对齐修法：
    经调度器分配为 ``pool.refresh`` 任务，面板「任务明细」看得见、
    失败可人工重投、崩溃可恢复。

    处理器约定
    ----------
    同步跑（已站在研究池工作线程上，不要再开线程）。
    候选池为空/剔除后为空属于"跑完了但没结果"，**返回结果**而非抛异常，
    否则一个常态会无谓地进死信；真正的网络异常在 ``refresh`` 内被记录、
    这里重新抛出让任务失败并进入错误队列。
    """
    from app.modules.universe.pool import StockPool

    params = dict(task.params or {})
    source = str(params.get("source") or "") or None
    limit = params.get("limit")
    do_sync = params.get("sync")
    symbols = list(params.get("symbols") or []) or None
    exclusions = params.get("exclusions")
    force = params.get("force")

    pool = StockPool()
    try:
        report = pool.refresh(
            source=source,
            limit=int(limit) if limit else None,
            do_sync=None if do_sync is None else bool(do_sync),
            symbols=symbols,
            exclusions=exclusions if isinstance(exclusions, dict) else None,
            force=bool(force),
        )
    except Exception as exc:  # 真实采集/打分异常 → 任务失败 → 死信
        raise RuntimeError(f"股票池刷新失败: {exc}") from exc

    if report.get("error"):
        # 候选池为空等"无结果"态：任务标记成功，结论写进 result 供面板读取
        return {"ok": False, "error": report["error"], "report": report}
    return {"ok": True, "report": report}


def handle_collect_now(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """全类别立即采集（指定标的）：量价 K 线 + 日频截面 + 舆情/宏观。

    与 :func:`handle_pool_refresh` 同样的"走任务"理由：之前面板直接
    ``deferToThread(collect_now)``，既不进任务明细、也不进错误队列。
    现经调度器分配为 ``collect.now`` 任务，面板看得见、失败可重投。

    存储后端故障（``StorageError``）来自 ``run_full_collection`` 内部、
    不被这里捕获 → 任务判失败 → 进死信 → 可重放。
    """
    from app.modules.collection.service import run_full_collection

    params = dict(task.params or {})
    syms = list(params.get("symbols") or []) or None
    cats = list(params.get("categories") or []) or None
    return run_full_collection(syms, cats)


def handle_collect_cross(task: EngineTask, ctx: Any) -> Any:
    """定向采集：指定标的 × 指定类别（基本面/资金流/筹码/龙虎榜…）。

    委托给采集服务的 ``collect_categories``（它自带并发护栏与进度上报），
    返回 Deferred —— 处理器返回 Deferred 时，执行池会等它落地再判完成，
    不会"函数返回了就当成成功"。存储故障已被服务内 ``StorageError`` 上浮，
    这里不再吞，任务会进错误队列。
    """
    svc = _require(ctx, "collection_service", "定向采集")
    syms = _symbols(task)
    cats = list((task.params or {}).get("categories") or ["fundamental"])
    return svc.collect_categories(syms, cats)


def handle_collect_kline(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """立即补采 K 线（指定标的，空 = 跟随采集服务配置）。

    复用采集服务既有的 ``_run_kline_forced``，不另写一遍联网逻辑。
    """
    from app.domain.symbol import normalize_symbol

    svc = _require(ctx, "collection_service", "K线补采")
    symbol = str((task.params or {}).get("symbol") or "")
    jobs = [
        j for j in getattr(svc, "kline_spec", None).jobs or []
        if j.enabled and (not symbol or j.symbol == normalize_symbol(symbol))
    ]
    if not jobs:
        raise RuntimeError(f"没有匹配的采集任务: {symbol or '*'}")
    return svc._run_kline_forced(jobs)


# =============================================================================
# 策略域
# =============================================================================
def _latest_bar(symbol: str) -> Optional[Any]:
    """从本地库读该标的的最后一根日线。

    只读库不联网（``source="db"``）：策略运行任务是"用现有数据做一次决策"，
    不是"先去把数据补齐"。补数有专门的补采任务，两者混在一起会让
    "这次决策用了哪些数据"变得不可复现。
    """
    from datetime import datetime, timedelta

    from app.data.service import MarketDataService
    from app.modules.market.service import df_to_bars

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        df = MarketDataService.load(symbol, start, end, source="db")
    except Exception as exc:
        logger.debug(f"读取 {symbol} 最新 K 线失败: {exc}")
        return None
    if df is None or getattr(df, "empty", True):
        return None
    bars = df_to_bars(symbol, df.tail(1))
    return bars[0] if bars else None


def handle_strategy_run(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """运行策略。

    用**最新一根 K 线**驱动目标任务做一次决策。两处保护：

      * **同根 bar 只驱动一次**：``bar.dt`` 不比任务已有的最后一根新，就跳过。
        否则"因子更新触发一次、手工又触发一次"会把同一根 K 线喂两遍，
        序列长度、成交笔数、净值曲线全部失真；
      * **只对指定标的/任务生效**：没有 task_ids 也没有 symbols 时按
        "不跑"处理。宁可不跑，也不要悄悄把全场策略都跑一遍。
    """
    sm = _require(ctx, "strategy", "策略运行")
    runtime = getattr(sm, "runtime", None)
    if runtime is None:
        raise RuntimeError("策略中枢没有任务运行时")

    params = task.params or {}
    wanted_ids = [str(x) for x in (params.get("task_ids") or []) if x]
    symbols = set(_symbols(task))

    targets: List[Any] = []
    if wanted_ids:
        for tid in wanted_ids:
            t = runtime.get(tid)
            if t is not None:
                targets.append(t)
    elif symbols:
        targets = [t for t in runtime if getattr(t, "symbol", "") in symbols]
    else:
        return {"ran": 0, "note": "未指定任务或标的，按不跑处理"}

    ran, skipped, orders = 0, 0, []
    for t in targets:
        symbol = str(getattr(t, "symbol", "") or "")
        bar = _latest_bar(symbol)
        if bar is None:
            skipped += 1
            continue
        last = getattr(getattr(t, "series", None), "last", None)
        last_dt = getattr(last, "dt", None)
        bar_dt = getattr(bar, "dt", None)
        if last_dt is not None and bar_dt is not None and bar_dt <= last_dt:
            skipped += 1
            continue
        # 任务状态是 domain 层的 TaskState，这里按**值**比较而不是按类型：
        # 执行层不该为了让一次判断成立就去 import 业务枚举。
        status = getattr(t, "status", None)
        if status is not None and str(getattr(status, "value", status)).upper() != "RUNNING":
            skipped += 1
            continue
        try:
            order = t.on_bar(bar)
        except Exception as exc:
            logger.error(f"任务 {getattr(t, 'task_id', '?')} 运行失败: {exc}", exc_info=True)
            skipped += 1
            continue
        ran += 1
        if order is not None:
            orders.append(getattr(order, "order_id", ""))

    return {"ran": ran, "skipped": skipped, "orders": orders}


# =============================================================================
# 交易域
# =============================================================================
def handle_risk_evaluate(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """风控评估：把一个交易信号/意图过一遍全局风控链。

    产出 ``risk.check.approved`` 或 ``risk.check.rejected``，
    由规则表决定要不要继续生成下单任务。
    """
    params = task.params or {}
    signal = params.get("signal")
    if signal is None:
        raise RuntimeError(
            "风控评估缺少 params.signal（交易信号），无法评估"
        )
    svc = _component(ctx, "risk_service")
    if svc is None:
        raise RuntimeError("风控服务未装配，无法评估交易意图")

    from app.modules.strategy.state import DecisionState

    task_id = str(params.get("task_id") or "")
    state = None
    sm = _component(ctx, "strategy")
    runtime = getattr(sm, "runtime", None) if sm else None
    if runtime is not None and task_id:
        owner = runtime.get(task_id)
        state = getattr(owner, "state", None)
    if state is None:
        state = DecisionState()

    verdict = svc.check(signal, state, ctx=None, account=None)
    allowed = bool(getattr(verdict, "allowed", False))
    scale = float(getattr(verdict, "scale", 1.0) or 1.0)

    if allowed:
        _publish(
            ctx,
            "risk.check.approved",
            signal=signal,
            task_id=task_id,
            symbol=str(getattr(signal, "symbol", "") or ""),
            side=str(getattr(getattr(signal, "side", None), "value", "") or ""),
            scale=scale,
            note=getattr(verdict, "reason", ""),
            source="risk_handler",
        )
    else:
        _publish(
            ctx,
            "risk.check.rejected",
            signal=signal,
            task_id=task_id,
            symbol=str(getattr(signal, "symbol", "") or ""),
            rule=getattr(verdict, "rule", ""),
            reason=getattr(verdict, "reason", ""),
            source="risk_handler",
        )
    return {
        "allowed": allowed,
        "rule": getattr(verdict, "rule", ""),
        "reason": getattr(verdict, "reason", ""),
        "scale": scale,
    }


def handle_trade_execute(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """下单执行。

    **不做自动重试**（EngineTask 构造时会被强制归零），
    幂等由订单状态机与额度预留保证，不靠重试。

    注意这里会**再过一次风控**（TOCTOU 防护）：从"风控说可以"
    到"真正报单"之间账户状态可能已经变了，只看之前的裁决就下单，
    是并发下超限的常见来源。
    """
    params = task.params or {}
    signal = params.get("signal")
    task_id = str(params.get("task_id") or "")
    if signal is None:
        raise RuntimeError("下单任务缺少 params.signal，拒绝执行")
    if not task_id:
        raise RuntimeError("下单任务缺少 params.task_id，无法归属到具体任务")

    sm = _component(ctx, "strategy")
    runtime = getattr(sm, "runtime", None) if sm else None
    owner = runtime.get(task_id) if runtime is not None else None
    if owner is None:
        raise RuntimeError(f"找不到任务 {task_id}，订单无法归属")

    executor = getattr(owner, "execute_approved", None)
    if not callable(executor):
        raise RuntimeError(
            "任务运行时未实现 execute_approved()，"
            "交易意图无法脱离策略链路独立执行"
        )
    order = executor(signal, approved_size=params.get("approved_size"))
    return {
        "executed": order is not None,
        "order_id": getattr(order, "order_id", ""),
        "symbol": str(getattr(order, "symbol", "") or ""),
    }


# =============================================================================
# 运维域
# =============================================================================
def handle_maintenance_validate(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """校验关键目录与数据库是否可用。

    这一步看起来不起眼，但它回答的是"这台引擎能不能安全地跑下去"：
    目录不可写 → 快照落不了盘；库连不上 → 采集全是空转。
    与其等出问题再查，不如启动后先自检一次。
    """
    from pathlib import Path

    from app.common.config import settings

    checks: Dict[str, Any] = {}
    for label, path in (
        ("output", getattr(settings, "OUTPUT_DIR", "")),
        ("log", getattr(settings, "LOG_DIR", "")),
        ("data", getattr(settings, "DATA_DIR", "")),
    ):
        if not path:
            checks[label] = "未配置"
            continue
        try:
            p = Path(str(path))
            p.mkdir(parents=True, exist_ok=True)
            checks[label] = "可写" if p.is_dir() else "不存在"
        except Exception as exc:
            checks[label] = f"不可写: {exc}"

    try:
        from app.data.engine import get_engine

        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(_text("SELECT 1"))
        checks["database"] = "可连接"
    except Exception as exc:
        checks["database"] = f"不可连接: {exc}"

    bad = {k: v for k, v in checks.items() if "不可" in str(v) or "不存在" in str(v)}
    return {"checks": checks, "healthy": not bad, "problems": bad}


def _text(sql: str) -> Any:
    from sqlalchemy import text

    return text(sql)


def handle_log_cleanup(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """日志清理。**默认只报告不删除**。

    删除是不可逆操作，而"定时任务自动删文件"是运维事故的高发区。
    所以默认 ``apply=False``：先告诉你哪些文件超期了，确认无误后
    显式传 ``apply=True`` 才真的删，且**只动本项目自己的日志目录**。
    """
    from datetime import datetime, timedelta
    from pathlib import Path

    from app.common.config import settings

    params = task.params or {}
    # 保留期支持"小时/天"两种写法（面板的 Hours/Days 切换写 retention_hours）：
    # retention_hours 优先，旧的 retention_days 仍然认（×24 换算）。
    # 这里**不做 max(1,..) 夹逼**："保留 0"是配错的危险信号，
    # 该被拒绝而不是被悄悄换成 1 —— 换算夹逼等于替错误配置兜底。
    raw_hours = params.get("retention_hours")
    if raw_hours is not None:
        retention_hours = int(raw_hours)
        retention_days = round(retention_hours / 24.0, 2)
    else:
        raw_retention = params.get("retention_days")
        retention_days = 7 if raw_retention is None else int(raw_retention)
        retention_hours = retention_days * 24
    apply_delete = bool(params.get("apply"))
    # 单次最多删多少。没有上限的话，一个配错的保留天数会在一拍之内
    # 把整个日志目录清空 —— 而那一拍里你连"正在删"的日志都看不到。
    max_files = int(params.get("max_files") or 500)
    root = Path(str(getattr(settings, "LOG_DIR", "") or ""))
    if not root or not root.is_dir():
        return {"removed": 0, "note": "日志目录不存在或不可读"}
    if retention_hours < 1:
        return {"removed": 0, "note": f"保留期 {retention_hours} 小时过小，已拒绝执行（至少 1 小时）"}

    cutoff = datetime.now() - timedelta(hours=retention_hours)
    stale, freed = [], 0
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        size = f.stat().st_size
        stale.append({"path": str(f), "mtime": mtime.isoformat(), "size": size})
        freed += size

    removed = 0
    if apply_delete:
        for item in stale[:max_files]:
            try:
                Path(item["path"]).unlink()
                removed += 1
            except Exception as exc:
                logger.warning(f"删除过期日志失败 {item['path']}: {exc}")

    return {
        "retention_hours": retention_hours,
        "retention_days": retention_days,
        "stale": len(stale),
        "removed": removed,
        "freed_bytes": freed,
        "apply": apply_delete,
        "max_files": max_files,
        "truncated": len(stale) > max_files,
        "sample": stale[:10],
    }


def handle_archive(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """快照归档：把过期快照**移进** ``snapshots/archive/``。

    默认只统计不移动（理由同日志清理：移动也是不可逆的批量文件操作）。
    确认后传 ``apply=True`` —— 目标是同一个 output 目录下的 archive/，
    **不跨盘、不删除**，所以撤销只需把文件挪回去。

    归档区本身在扫描时会被排除，否则第二次运行会把已归档的文件
    再"归档"一遍（原地移动），报告上看到的数量会莫名其妙。
    """
    import shutil
    from datetime import datetime, timedelta
    from pathlib import Path

    from app.common.config import settings

    params = task.params or {}
    # 同日志清理：before_hours（面板 Hours/Days 切换）优先，before_days 兼容；
    # 不做夹逼 —— "保留 0"是危险配置，拒绝而不是悄悄换算。
    raw_hours = params.get("before_hours")
    if raw_hours is not None:
        before_hours = int(raw_hours)
        before_days = round(before_hours / 24.0, 2)
    else:
        raw_days = params.get("before_days")
        before_days = 30 if raw_days is None else int(raw_days)
        before_hours = before_days * 24
    apply_move = bool(params.get("apply"))
    max_files = int(params.get("max_files") or 1000)

    base = Path(str(getattr(settings, "OUTPUT_DIR", "") or ""))
    root = Path(str(getattr(settings, "SNAPSHOT_DIR", "") or (base / "snapshots")))
    if not root.is_dir():
        return {"archived": 0, "note": "快照目录不存在"}
    if before_hours < 1:
        return {"archived": 0, "note": f"归档保留期 {before_hours} 小时过小，已拒绝执行（至少 1 小时）"}

    archive_root = root / "archive"
    cutoff = datetime.now() - timedelta(hours=before_hours)
    old = []
    for f in root.rglob("*.json"):
        if archive_root in f.parents:      # 已归档的不重复处理
            continue
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                old.append({"path": str(f), "size": f.stat().st_size})
        except OSError:
            continue

    moved, moved_bytes, failed = 0, 0, []
    if apply_move:
        for item in old[:max_files]:
            src = Path(item["path"])
            try:
                rel = src.relative_to(root)
            except ValueError:
                continue
            dst = archive_root / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1
                moved_bytes += int(item["size"])
            except Exception as exc:
                failed.append({"path": str(src), "error": str(exc)})

    return {
        "before_hours": before_hours,
        "before_days": before_days,
        "candidates": len(old),
        "archived": moved,
        "moved_bytes": moved_bytes,
        "failed": failed[:10],
        "apply": apply_move,
        "max_files": max_files,
        "target": str(archive_root),
        "note": "" if apply_move else "只统计未移动（默认）；勾选『真的归档』后才会移入 archive/",
        "sample": old[:10],
    }


def handle_dead_history_cleanup(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """清理死信**处置历史**（对照 Rhapsody 的 Cleanup notification event history）。

    与日志/归档清理的安全取向不同：这里默认 ``apply=True`` 真删。
    原因是清的对象不一样 —— 它只清**已处置**的死信记录（数据库行、
    有 ``keep`` 条下限保护），还挂在错误队列里等人处理的死信一条不动；
    而"只报告不删"对一个计数报表没有任何价值。

    :param retention_days: 处置完超过 N 天的才清（默认 30）
    :param keep: 无论多老，最新 N 条永远保留（默认 200）
    """
    journal = getattr(getattr(ctx, "store", None), "journal", None)
    if journal is None or not getattr(journal, "enabled", False):
        return {"pruned": 0, "note": "任务流水账未启用，没有可清理的处置历史"}

    params = task.params or {}
    raw_days = params.get("retention_days")
    days = 30 if raw_days is None else int(raw_days)
    keep = max(50, int(params.get("keep") or 200))   # 下限保护：别把历史清光
    if days < 1:
        return {"pruned": 0, "note": f"保留期 {days} 天过小，已拒绝执行（至少 1 天）"}

    apply_delete = bool(params.get("apply", True))
    if not apply_delete:
        # 报告模式：只数不清。参数与 prune 完全一致。
        prunable = journal.count_prunable_dead_history(
            older_than_days=float(days), keep=keep
        )
        return {
            "retention_days": days, "keep": keep, "prunable": prunable,
            "pruned": 0, "apply": False,
            "note": "只统计未删除；勾选『真的执行』后才会清理",
        }

    pruned = journal.prune_dead_history(older_than_days=float(days), keep=keep)
    return {
        "retention_days": days, "keep": keep,
        "pruned": pruned, "prunable": pruned, "apply": True,
        "note": f"已清理 {pruned} 条已处置的旧记录",
    }


def handle_backtest_run(task: EngineTask, ctx: Any) -> Dict[str, Any]:
    """跑一次历史回测（长任务，占 ``backtest`` 池唯一的槽位）。

    两个必须遵守的点：

    **同步阻塞，不在处理器里再开线程。**
    处理器已经站在池的工作线程上了。再开一个线程的结果有三重伤害：
    绕开 ``backtest`` 池 capacity=1 的并发上限、任务在回测跑完前就被
    标记成功、以及把池线程空转着等一个没人管的子线程。

    **幂等。**
    回测会把结果写进 ``backtest_result`` 表，重跑一次就多一条记录。
    任务可能因为重试、事件重投、崩溃恢复被再执行一次，所以这里用
    ``run_id`` 做去重键：同一个任务重投时直接返回上次的结果，不重复落库。
    回测本来就是确定性的（同参数同数据 → 同结果），幂等不会丢信息。
    """
    from app.monitoring.api.backtest import get_backtest_service

    svc = get_backtest_service()
    params = dict(task.params or {})
    symbol = str(params.get("symbol") or "").strip()
    if not symbol:
        # 抛异常而不是返回空结果：让任务失败进死信，死信里写着"缺 symbol"
        raise ValueError("回测任务缺少 symbol")

    # 幂等键绑定 task_id：重试/重投是同一个任务，不是"用户又跑了一次"
    run_id = f"bt_{task.task_id}"
    existing = svc.find_result(run_id)
    if existing is not None:
        logger.info(f"回测任务 {task.task_id} 已有结果，跳过重复执行（幂等）")
        return {
            "job_id": "", "run_id": run_id, "result_id": existing.get("id"),
            "symbol": symbol, "summary": existing.get("metrics") or {},
            "idempotent": True,
        }

    job = svc.submit(params, execute=False, run_id=run_id)
    svc.run_job(job.job_id)

    if job.status != "DONE":
        # run_job 内部吞掉了异常写进 job.error，这里翻出来让任务真的失败
        raise RuntimeError(job.error or f"回测未成功完成（状态 {job.status}）")

    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "result_id": job.result_id,
        "symbol": symbol,
        "summary": job.summary or {},
        "idempotent": False,
    }


# =============================================================================
# 注册
# =============================================================================
#: 任务类型 → (处理器, 说明)
DEFAULT_HANDLERS: Dict[str, Dict[str, Any]] = {
    "system.recover": {
        "fn": handle_system_recover,
        "description": "恢复上次遗留的在途任务",
    },
    "snapshot.persist": {
        "fn": handle_snapshot_persist,
        "description": "立刻落一份运行快照",
    },
    "market.kline.collect": {
        "fn": handle_kline_collect,
        "description": "K 线采集（委托采集服务）",
    },
    "market.kline.backfill": {
        "fn": handle_kline_backfill,
        "description": "按标的补采 K 线",
    },
    "data.integrity.check": {
        "fn": handle_integrity_check,
        "description": "数据完整性体检",
    },
    "news.collect": {
        "fn": handle_news_collect,
        "description": "新闻采集",
    },
    "sentiment.analyze": {
        "fn": handle_sentiment_analyze,
        "description": "舆情评分（已分析则跳过，防成环）",
    },
    "fundamental.collect": {
        "fn": handle_fundamental_collect,
        "description": "基本面定向采集",
    },
    "factor.calculate": {
        "fn": handle_factor_calculate,
        "description": "因子重算（失效缓存 + 推送快照事件）",
    },
    "pool.refresh": {
        "fn": handle_pool_refresh,
        "description": "刷新股票池（候选池来源＋资金流/筹码/基本面同步＋多维度打分）",
    },
    "collect.now": {
        "fn": handle_collect_now,
        "description": "全类别立即采集（指定标的）",
    },
    "collect.cross": {
        "fn": handle_collect_cross,
        "description": "定向采集（指定标的 × 指定类别）",
    },
    "collect.kline": {
        "fn": handle_collect_kline,
        "description": "立即补采 K 线",
    },
    "strategy.run": {
        "fn": handle_strategy_run,
        "description": "用最新 K 线驱动目标策略做一次决策",
    },
    "risk.evaluate": {
        "fn": handle_risk_evaluate,
        "description": "全局风控评估并推送裁决事件",
    },
    "trade.execute": {
        "fn": handle_trade_execute,
        "description": "风控放行后下单（不自动重试）",
    },
    "maintenance.validate": {
        "fn": handle_maintenance_validate,
        "description": "校验日志/数据目录与数据库可用性",
    },
    "maintenance.log_cleanup": {
        "fn": handle_log_cleanup,
        "description": "日志清理（默认只报告，apply=True 才删）",
    },
    "maintenance.archive": {
        "fn": handle_archive,
        "description": "快照归档统计（默认不移动）",
    },
    "maintenance.dead_history": {
        "fn": handle_dead_history_cleanup,
        "description": "清理已处置的死信处置历史（未处置的不动）",
    },
    "backtest.run": {
        "fn": handle_backtest_run,
        "description": "历史回测（长任务，占 backtest 池唯一槽位，幂等）",
    },
}

#: 已声明但**尚未接业务实现**的任务类型。
#: 刻意不给它们注册处理器：查不到处理器会明确进死信并写明原因，
#: 好过一个"跑成功但什么都没做"的空壳实现。
UNIMPLEMENTED_TASK_TYPES = (
    "universe.rank",      # 需要接 app.modules.universe 的选股流程
    "backtest.sweep",     # 大规模参数实验，应由运维显式发起
    "market.quote.snapshot",
)
# 注：``backtest.run`` 曾经在这一栏里，理由是"建议显式调用而非事件驱动"。
# 但"显式调用"这条路是自己另起一套（面板直接调 BacktestService），
# 结果就是回测既不在任务明细里、失败也不进死信、崩溃了无从恢复。
# 现已注册处理器：显式提交照样可以，只是提交之后交给调度器分配。


def register_default_handlers(registry: HandlerRegistry) -> int:
    """把默认处理器装进注册表。返回注册数量。"""
    count = 0
    for task_type, meta in DEFAULT_HANDLERS.items():
        registry.register(
            task_type,
            meta["fn"],
            name=meta["fn"].__name__,
            description=meta["description"],
            override=True,
        )
        count += 1
    return count


__all__ = [
    "DEFAULT_HANDLERS",
    "UNIMPLEMENTED_TASK_TYPES",
    "register_default_handlers",
]
