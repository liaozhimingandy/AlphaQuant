#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : types.py
# @Description : 事件 / 任务 / 队列 / 执行池 的**名字常量**。
#
#               为什么名字要集中定义：
#                 事件名是字符串，拼错不会报错，只会"安静地没人订阅"。
#                 这在事件驱动架构里是最难查的一类 bug —— 发布方以为发了，
#                 订阅方以为收不到是没发生，双方都觉得自己在正常工作。
#                 集中成常量后，拼错在 IDE / import 阶段就暴露。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
事件的六大分类
==============

把事件分成六类，而不是"每个函数一个事件"，是为了控制扇出：

  1. 生命周期  engine.*
  2. 数据      market.* / news.* / fundamental.*
  3. 计算      factor.* / strategy.* / universe.*
  4. 交易风控  risk.* / trade.*
  5. 任务执行  task.* / executor.*
  6. 运维异常  system.*

判断"该不该定义一个新事件"的标准：**有没有人会因为它而改变行为**。
"K 线采集完成"有人会用（触发因子），所以它该是事件；
"采集函数第 3 步执行完了"没有人关心，它只能是日志。
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Tuple


# =============================================================================
# 1. 事件类型（EventType）
# =============================================================================
class EventType:
    """全系统标准事件。

    命名约定 ``域.对象.动作``：``market.kline.updated`` 读作
    "行情域 / K线对象 / 发生了更新"。域前缀同时是通配订阅的前缀，
    所以 ``market.*`` 能一次订阅所有行情域事件。
    """

    # ---------------- 1. 系统生命周期 ----------------
    ENGINE_STARTED = "engine.started"
    ENGINE_READY = "engine.ready"
    ENGINE_STOPPING = "engine.stopping"
    ENGINE_STOPPED = "engine.stopped"
    ENGINE_RECOVERY_REQUESTED = "engine.recovery.requested"

    # ---------------- 2. 数据采集与数据质量 ----------------
    DATA_COLLECTION_REQUESTED = "data.collection.requested"
    #: 行情服务推过来的一根**实时** K 线（未落库）。
    #: 与 ``MARKET_KLINE_UPDATED``（已落库/已修正）刻意分开：
    #: 前者驱动策略，后者驱动因子与补数，混在一起会让因子在盘中反复重算。
    MARKET_BAR_RECEIVED = "market.bar.received"
    MARKET_TICK_RECEIVED = "market.tick.received"
    MARKET_KLINE_UPDATED = "market.kline.updated"
    MARKET_KLINE_GAP_DETECTED = "market.kline.gap_detected"
    MARKET_DATA_VALIDATED = "market.data.validated"
    #: 标的主表（代码/名称/**上市日期**/板块）被同步过了一次。
    #: 单独发一个事件，是因为主表是**很多判断的共同前提**：
    #: K线任务靠它跳过未上市标的、股票池靠它做板块剔除、
    #: 面板靠它显示"这票哪天上市"。它们不该各自去轮询数据库 ——
    #: 而一旦有人要轮询，就意味着"主表什么时候更新的"这个问题的答案
    #: 只能靠猜。
    STOCK_PROFILE_UPDATED = "stock.profile.updated"
    NEWS_ARTICLE_COLLECTED = "news.article.collected"
    NEWS_SENTIMENT_UPDATED = "news.sentiment.updated"
    FUNDAMENTAL_DATA_UPDATED = "fundamental.data.updated"

    # ---------------- 3. 因子与策略 ----------------
    FACTOR_CALCULATION_REQUESTED = "factor.calculation.requested"
    FACTOR_SNAPSHOT_UPDATED = "factor.snapshot.updated"
    STRATEGY_RUN_REQUESTED = "strategy.run.requested"
    STRATEGY_RUN_COMPLETED = "strategy.run.completed"
    STRATEGY_SIGNAL_GENERATED = "strategy.signal.generated"
    UNIVERSE_UPDATED = "universe.updated"

    # ---------------- 3.5 存量事件的规范名（迁移桥接）----------------
    #
    # 这些事件在重构前就以别的名字存在着（``snapshot_saved`` 等），
    # 它们有真实订阅者、也有真实业务含义，所以给它们各自的规范名，
    # 而不是硬塞进上面六类的某一格里 —— 硬塞会造成"两个不同的旧事件
    # 映射到同一个新事件"，于是一个事件会被投递给另一个事件的订阅者。
    COMPONENT_ENABLED = "component.enabled"
    COMPONENT_DISABLED = "component.disabled"
    SNAPSHOT_SAVED = "snapshot.saved"
    CONTROL_COMMAND = "control.command"

    # ---------------- 4. 交易与风险 ----------------
    RISK_CHECK_REQUESTED = "risk.check.requested"
    RISK_CHECK_APPROVED = "risk.check.approved"
    RISK_CHECK_REJECTED = "risk.check.rejected"
    TRADE_ORDER_REQUESTED = "trade.order.requested"
    #: 订单已创建（尚未送券商）。与 ``submitted`` 分开：
    #: 前者是本地状态，后者是"券商收下了"—— 幂等判断只认后者。
    TRADE_ORDER_CREATED = "trade.order.created"
    TRADE_ORDER_SUBMITTED = "trade.order.submitted"
    TRADE_ORDER_FILLED = "trade.order.filled"
    TRADE_ORDER_FAILED = "trade.order.failed"
    TRADE_ORDER_CANCELLED = "trade.order.cancelled"
    TRADE_POSITION_UPDATED = "trade.position.updated"
    TRADE_ACCOUNT_UPDATED = "trade.account.updated"
    RISK_LIMIT_BREACHED = "risk.limit.breached"
    # —— 交易时段 ——
    #: 这两个事件补上的正是 ``system.market.session.changed`` **不管**的那一层：
    #: 后者说的是"现在是盘前/盘中/收盘结算中"（客观时间），
    #: 这两个说的是"**我们的交易因此开始了/结束了**"（系统的主观动作）。
    #: 分开的必要性在于，两者之间还存在第三种状态 ——
    #: 市场开盘了，但我们的网关还没连上、虚拟账户还没就绪，
    #: 此刻"可以交易"是假的。把"能不能下单"表达成时间判断，
    #: 最终一定会有一天下单被拒却查不出为什么。
    TRADE_SESSION_OPENED = "trade.session.opened"
    TRADE_SESSION_CLOSED = "trade.session.closed"
    # —— 账户（库里的钱） ——
    #: ``trade.account.updated`` 是"券商回给我的账户又刷新了一次"；
    #: 这两个事件说的是**本地账户表**本身被改动（建账户 / 余额变了）。
    #: 这两件事在粒度上完全不同：前者一天能来几十次且只是"又刷新了一次"，
    #: 后者必须少而准 —— 它是资金去向的唯一本地审计线索。
    ACCOUNT_CREATED = "account.created"
    ACCOUNT_UPDATED = "account.updated"
    #: 熔断：一键停掉所有新的开仓。与 :attr:`RISK_CHECK_REJECTED` 的区别是
    #: 粒度 —— 后者是"这一单不许做的判决"，前者是"闸刀拉下来了"的状态变化，
    #: 后者每天可能几十次，前者一次就够了，且必须被人看见。
    RISK_HALTED = "risk.halted"
    RISK_RESUMED = "risk.resumed"

    # ---------------- 5. 任务与执行器 ----------------
    TASK_CREATED = "task.created"
    TASK_QUEUED = "task.queued"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_RETRY_SCHEDULED = "task.retry.scheduled"
    TASK_DEAD_LETTERED = "task.dead_lettered"
    EXECUTOR_REGISTERED = "executor.registered"
    EXECUTOR_HEARTBEAT_MISSED = "executor.heartbeat.missed"
    EXECUTOR_CAPACITY_CHANGED = "executor.capacity.changed"

    # ---------------- 5.5 存量"量化任务"事件（QuantTask 生命周期）----------------
    #
    # 与上面的 ``task.*`` 是**两个不同的对象**，不能共用命名空间：
    #   ``task.*`` = 引擎任务（EngineTask）——调度器由事件生成的、可重试的工作单元
    #   ``job.*``  = 量化任务（QuantTask）——"标的 + 策略 + 风控 + 账本"的运行实例
    #
    # 混在一起的后果很具体：调度器每建一个引擎任务，都会去触发策略中枢的
    # "任务新增"回调，而那个回调拿到的 task_id 是空的 —— 于是在日志里
    # 留下一条"挂载网关失败"的幽灵警告，排查时被它带偏。
    JOB_SUBMIT = "job.submit"
    JOB_ADDED = "job.added"
    JOB_REMOVED = "job.removed"
    JOB_STARTED = "job.started"
    JOB_STOPPED = "job.stopped"
    JOB_PAUSED = "job.paused"
    JOB_RESUMED = "job.resumed"

    # ---------------- 6. 系统异常与运维 ----------------
    SYSTEM_DEPENDENCY_UNAVAILABLE = "system.dependency.unavailable"
    SYSTEM_ENGINE_ERROR = "system.engine.error"
    SYSTEM_DATABASE_DEGRADED = "system.database.degraded"
    SYSTEM_EVENT_PUBLISH_FAILED = "system.event.publish.failed"
    SYSTEM_MARKET_SESSION_CHANGED = "system.market.session.changed"
    SYSTEM_DATA_STALENESS_DETECTED = "system.data.staleness.detected"
    #: 运维节拍。由维护计划器按固定间隔发出，计划器收到后自己判断
    #: "哪个维护任务到点了"，再发 :attr:`SYSTEM_MAINTENANCE_DUE`。
    #: 拆成两个事件是为了让"节拍"和"该干活了"在事件流里能分开看。
    SYSTEM_MAINTENANCE_TICK = "system.maintenance.tick"
    SYSTEM_MAINTENANCE_DUE = "system.maintenance.due"
    #: 启动恢复完成：报告"上次没干完的活捞回来多少"。
    #: 单独发一个事件而不是只打日志，是为了让"恢复"这件事能被审计 ——
    #: 静默恢复和静默丢弃在日志里长得差不多，在事件流里完全不同。
    SYSTEM_TASKS_RECOVERED = "system.tasks.recovered"
    #: 人工处置了一条死信（重投 / 丢弃）
    SYSTEM_DEAD_RESOLVED = "system.dead.resolved"
    #: 启动预检结论（环境与配置体检）
    SYSTEM_PREFLIGHT = "system.preflight"


#: 事件分类目录。面板按它分组展示，文档按它组织。
EVENT_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "生命周期": (
        EventType.ENGINE_STARTED, EventType.ENGINE_READY,
        EventType.ENGINE_STOPPING, EventType.ENGINE_STOPPED,
        EventType.ENGINE_RECOVERY_REQUESTED,
    ),
    "数据与质量": (
        EventType.DATA_COLLECTION_REQUESTED, EventType.MARKET_BAR_RECEIVED,
        EventType.MARKET_TICK_RECEIVED, EventType.MARKET_KLINE_UPDATED,
        EventType.MARKET_KLINE_GAP_DETECTED, EventType.MARKET_DATA_VALIDATED,
        EventType.NEWS_ARTICLE_COLLECTED, EventType.NEWS_SENTIMENT_UPDATED,
        EventType.FUNDAMENTAL_DATA_UPDATED, EventType.STOCK_PROFILE_UPDATED,
    ),
    "因子与策略": (
        EventType.FACTOR_CALCULATION_REQUESTED, EventType.FACTOR_SNAPSHOT_UPDATED,
        EventType.STRATEGY_RUN_REQUESTED, EventType.STRATEGY_RUN_COMPLETED,
        EventType.STRATEGY_SIGNAL_GENERATED, EventType.UNIVERSE_UPDATED,
    ),
    "交易与风控": (
        EventType.RISK_CHECK_REQUESTED, EventType.RISK_CHECK_APPROVED,
        EventType.RISK_CHECK_REJECTED, EventType.TRADE_ORDER_REQUESTED,
        EventType.TRADE_ORDER_CREATED, EventType.TRADE_ORDER_SUBMITTED,
        EventType.TRADE_ORDER_FILLED, EventType.TRADE_ORDER_FAILED,
        EventType.TRADE_ORDER_CANCELLED, EventType.TRADE_POSITION_UPDATED,
        EventType.TRADE_ACCOUNT_UPDATED, EventType.RISK_LIMIT_BREACHED,
        EventType.TRADE_SESSION_OPENED, EventType.TRADE_SESSION_CLOSED,
        EventType.ACCOUNT_CREATED, EventType.ACCOUNT_UPDATED,
        EventType.RISK_HALTED, EventType.RISK_RESUMED,
    ),
    "任务与执行（引擎任务）": (
        EventType.TASK_CREATED, EventType.TASK_QUEUED,
        EventType.TASK_STARTED,
        EventType.TASK_COMPLETED, EventType.TASK_FAILED,
        EventType.TASK_RETRY_SCHEDULED, EventType.TASK_DEAD_LETTERED,
        EventType.EXECUTOR_REGISTERED, EventType.EXECUTOR_HEARTBEAT_MISSED,
        EventType.EXECUTOR_CAPACITY_CHANGED,
    ),
    "量化任务（存量）": (
        EventType.JOB_SUBMIT, EventType.JOB_ADDED, EventType.JOB_REMOVED,
        EventType.JOB_STARTED, EventType.JOB_STOPPED,
        EventType.JOB_PAUSED, EventType.JOB_RESUMED,
    ),
    "异常与运维": (
        EventType.SYSTEM_DEPENDENCY_UNAVAILABLE, EventType.SYSTEM_ENGINE_ERROR,
        EventType.SYSTEM_DATABASE_DEGRADED,
        EventType.SYSTEM_EVENT_PUBLISH_FAILED, EventType.SYSTEM_MARKET_SESSION_CHANGED,
        EventType.SYSTEM_DATA_STALENESS_DETECTED, EventType.SYSTEM_MAINTENANCE_DUE,
        EventType.SYSTEM_MAINTENANCE_TICK, EventType.SYSTEM_TASKS_RECOVERED,
        EventType.SYSTEM_DEAD_RESOLVED, EventType.SYSTEM_PREFLIGHT,
        EventType.COMPONENT_ENABLED, EventType.COMPONENT_DISABLED,
        EventType.SNAPSHOT_SAVED, EventType.CONTROL_COMMAND,
    ),
}

#: 事件名 → 分类，用于日志与面板着色
EVENT_CATEGORY_OF: Dict[str, str] = {
    name: cat for cat, names in EVENT_CATEGORIES.items() for name in names
}

# -----------------------------------------------------------------------------
# 旧事件名 → 新事件名
#
# 重构前事件名是下划线风格（``bar_received``），散落在十几个文件里。
# 一次性全改风险太大（漏一处就是"发布方发了、订阅方收不到"），
# 所以这里保留映射：按旧名发布时，总线**同时**投递给新名的订阅者。
# 于是新旧代码可以混着跑，逐个迁移，不需要一次性的大爆炸式重构。
# -----------------------------------------------------------------------------
LEGACY_EVENT_ALIASES: Dict[str, str] = {
    "engine_started": EventType.ENGINE_STARTED,
    "engine_stopped": EventType.ENGINE_STOPPED,
    "bar_received": EventType.MARKET_BAR_RECEIVED,
    "tick_received": EventType.MARKET_TICK_RECEIVED,
    "data_collected": EventType.MARKET_KLINE_UPDATED,
    "news_received": EventType.NEWS_ARTICLE_COLLECTED,
    "news_analyzed": EventType.NEWS_SENTIMENT_UPDATED,
    "signal_generated": EventType.STRATEGY_SIGNAL_GENERATED,
    "signal_rejected": EventType.RISK_CHECK_REJECTED,
    "task_started": EventType.JOB_STARTED,
    "task_stopped": EventType.JOB_STOPPED,
    "task_paused": EventType.JOB_PAUSED,
    "task_resumed": EventType.JOB_RESUMED,
    "task_added": EventType.JOB_ADDED,
    "task_removed": EventType.JOB_REMOVED,
    "component_enabled": EventType.COMPONENT_ENABLED,
    "component_disabled": EventType.COMPONENT_DISABLED,
    "snapshot_saved": EventType.SNAPSHOT_SAVED,
    "control_command": EventType.CONTROL_COMMAND,
    "order_created": EventType.TRADE_ORDER_CREATED,
    "order_filled": EventType.TRADE_ORDER_FILLED,
    "order_cancelled": EventType.TRADE_ORDER_CANCELLED,
    "position_updated": EventType.TRADE_POSITION_UPDATED,
    "account_updated": EventType.TRADE_ACCOUNT_UPDATED,
    "component_error": EventType.SYSTEM_DEPENDENCY_UNAVAILABLE,
    "engine_error": EventType.SYSTEM_ENGINE_ERROR,
    "task_submit": EventType.JOB_SUBMIT,
}

# -----------------------------------------------------------------------------
# 【必须是一对一】这条不变式由下面的检查强制。
#
# 曾经把 ``control_command`` 和 ``task_added`` 都映射到 ``task.created``，
# 结果调度器一发 ``task.created``，订阅 ``control_command`` 的控制面板
# 审计流就收到一条 ``action=""`` 的幽灵指令。这类"跨事件串扰"在代码里
# 完全看不出来，只在测试与面板上表现为莫名其妙的多一条记录。
#
# 所以：宁可多定义几个规范名，也不要让两个旧事件共享一个规范名。
# -----------------------------------------------------------------------------
if len(set(LEGACY_EVENT_ALIASES.values())) != len(LEGACY_EVENT_ALIASES):
    _dupes: Dict[str, List[str]] = {}
    for _legacy, _canon in LEGACY_EVENT_ALIASES.items():
        _dupes.setdefault(_canon, []).append(_legacy)
    _bad = {k: v for k, v in _dupes.items() if len(v) > 1}
    raise RuntimeError(
        f"旧事件名 → 新事件名 必须是单射，以下规范名被多个旧名共享: {_bad}"
    )

#: 新名 → 旧名集合（反向投递用）
CANONICAL_TO_LEGACY: Dict[str, FrozenSet[str]] = {}
for _legacy, _canon in LEGACY_EVENT_ALIASES.items():
    CANONICAL_TO_LEGACY.setdefault(_canon, set()).add(_legacy)
CANONICAL_TO_LEGACY = {k: frozenset(v) for k, v in CANONICAL_TO_LEGACY.items()}


def canonical_event_type(event_type: str) -> str:
    """把旧事件名折算成新事件名，新名原样返回。"""
    return LEGACY_EVENT_ALIASES.get(event_type, event_type)


# =============================================================================
# 2. 任务类型（TaskType）
#
#    事件类型与任务类型**不是一一对应**的：
#      一个事件可以不产生任务（如 task.started），
#      也可以产生多个任务（如 engine.ready 产生采集 + 体检 + 恢复三个任务），
#      多个事件也可以映射到同一种任务（缺口检测与手工请求都产生补采任务）。
#    把它们分开定义，正是为了容纳这三种情况。
# =============================================================================
class TaskType:
    #: 旧式"函数型任务"的兜底通道（面板/CLI 直接投递一个 callable）。
    #: 它没有业务语义，所以走默认池，不占任何业务池的槽位。
    LEGACY_JOB = "legacy.job"

    # —— 系统 ——
    SYSTEM_RECOVER = "system.recover"
    SNAPSHOT_PERSIST = "snapshot.persist"

    # —— 行情域 ——
    MARKET_KLINE_COLLECT = "market.kline.collect"
    MARKET_KLINE_BACKFILL = "market.kline.backfill"
    MARKET_QUOTE_SNAPSHOT = "market.quote.snapshot"
    DATA_INTEGRITY_CHECK = "data.integrity.check"
    # —— K 线的三班定时（见 app/modules/collection/pool_kline.py）——
    #: 盘中：刷候选池近况（每轮都采，数据在盘中只会越来越准）
    MARKET_KLINE_INTRADAY = "market.kline.intraday"
    #: 盘后：把当天那根 K 线补齐（先查再采，已有的一律不动）
    MARKET_KLINE_POST_CLOSE = "market.kline.post_close"
    #: 定期体检：查"该有数据的交易日为什么没有"，能补的当场补
    MARKET_KLINE_GAP_SCAN = "market.kline.gap_scan"

    # —— 研究域 ——
    NEWS_COLLECT = "news.collect"
    SENTIMENT_ANALYZE = "sentiment.analyze"
    FUNDAMENTAL_COLLECT = "fundamental.collect"
    FACTOR_CALCULATE = "factor.calculate"
    POOL_REFRESH = "pool.refresh"   # 股票池刷新：候选池来源＋资金流/筹码/基本面同步＋打分
    #: 标的主表同步：全市场股票列表 + **上市日期** + 板块分类
    STOCK_PROFILE_SYNC = "stock.profile.sync"

    # —— 采集域（手动触发，经调度器统一分配）——
    COLLECT_NOW = "collect.now"      # 全类别立即采集（指定标的）
    COLLECT_CROSS = "collect.cross"  # 定向采集（指定标的 × 指定类别）
    COLLECT_KLINE = "collect.kline"  # 立即补采 K 线

    # —— 策略域 ——
    STRATEGY_RUN = "strategy.run"
    UNIVERSE_RANK = "universe.rank"

    # —— 交易域 ——
    RISK_EVALUATE = "risk.evaluate"
    TRADE_EXECUTE = "trade.execute"
    #: 收盘清算：把"这一天的钱和票"固化到账户表（资金/市值/峰值权益）。
    #: 为什么是任务：它在写资金事实，失败必须能重跑、且必须幂等
    #: （同一天跑三次的结果必须和跑一次一样）。
    ACCOUNT_SETTLE = "account.settle"

    # —— 回测域 ——
    BACKTEST_RUN = "backtest.run"
    BACKTEST_SWEEP = "backtest.sweep"

    # —— 运维域 ——
    MAINTENANCE_LOG_CLEANUP = "maintenance.log_cleanup"
    MAINTENANCE_ARCHIVE = "maintenance.archive"
    MAINTENANCE_VALIDATE = "maintenance.validate"
    MAINTENANCE_DEAD_HISTORY = "maintenance.dead_history"


# =============================================================================
# 3. 队列名（Queue）
#
#    队列是**逻辑分组**，不是进程。初期全部在同一进程内，
#    但每个队列有自己的并发上限，所以"回测跑满"不会把"实盘采集"挤死。
#    未来要拆进程时，队列名就是天然的 topic / routing key。
# =============================================================================
class Queue:
    SYSTEM = "system"
    MARKET = "market"
    RESEARCH = "research"
    STRATEGY = "strategy"
    TRADING = "trading"
    BACKTEST = "backtest"
    MAINTENANCE = "maintenance"
    DEFAULT = "default"

    ALL: Tuple[str, ...] = (
        SYSTEM, MARKET, RESEARCH, STRATEGY, TRADING, BACKTEST, MAINTENANCE, DEFAULT,
    )


# =============================================================================
# 4. 执行池（Pool）
#
#    每个池 = 一个**独立线程池 + 独立的并发上限 + 声明的任务能力**。
#
#    为什么不用一个公共线程池 + 总并发上限：
#      公共池里"回测"和"实盘采集"抢同一批线程。回测是 CPU/IO 密集且
#      一次跑几分钟的长任务，它会把池占满；此时行情采集排在队尾，
#      于是"K 线迟迟不来 → 策略不动 → 该止损的没止损"。
#      分池之后，回测再忙也只能用完 backtest 池自己的那一个槽位。
# =============================================================================
class Pool:
    SYSTEM = "system"
    MARKET = "market"        # 行情采集、K 线处理
    RESEARCH = "research"    # 新闻、舆情、因子计算
    STRATEGY = "strategy"    # 选股、策略运行、信号生成
    TRADING = "trading"      # 风控、订单与账户执行
    BACKTEST = "backtest"    # 历史回测、大规模参数实验
    MAINTENANCE = "maintenance"  # 日志清理、归档、定期校验
    DEFAULT = "default"      # 兜底池：只接没有业务归属的通用作业

    ALL: Tuple[str, ...] = (
        SYSTEM, MARKET, RESEARCH, STRATEGY, TRADING, BACKTEST, MAINTENANCE,
        DEFAULT,
    )

    #: 池名 → 队列名。一一对应，但**概念不同**：
    #: 队列是"活放在哪"，池是"谁有资格来领"。
    POOL_QUEUE: Dict[str, str] = {
        SYSTEM: Queue.SYSTEM,
        MARKET: Queue.MARKET,
        RESEARCH: Queue.RESEARCH,
        STRATEGY: Queue.STRATEGY,
        TRADING: Queue.TRADING,
        BACKTEST: Queue.BACKTEST,
        MAINTENANCE: Queue.MAINTENANCE,
        DEFAULT: Queue.DEFAULT,
    }

    #: 池名 → 默认并发上限。
    #:
    #: 数值不是随手定的：
    #:   - ``market`` 给 3：采集要并发（多标的），但数据源有限流，再多就是自杀；
    #:   - ``research`` 给 2：因子/舆情计算吃 CPU，给多了会把 reactor 拖慢；
    #:   - ``strategy`` 给 1：**必须串行**。两个策略同时改同一个账本，
    #:     权益曲线就不再是确定性的，回测也对不上；
    #:   - ``trading`` 给 1：**必须串行**。下单是幂等敏感操作，
    #:     并发下单在网络超时的情况下极易造成重复报单；
    #:   - ``backtest`` 给 1：长任务，本来就该排队，
    #:     而且回测绝不能挤占实盘资源；
    #:   - ``maintenance`` 给 1：慢活，不该抢任何人的资源。
    DEFAULT_CAPACITY: Dict[str, int] = {
        SYSTEM: 2,
        MARKET: 3,
        RESEARCH: 2,
        STRATEGY: 1,
        TRADING: 1,
        BACKTEST: 1,
        MAINTENANCE: 1,
        DEFAULT: 2,
    }

    #: 池名 → 该池声明能执行的任务类型（能力路由的依据）
    DEFAULT_CAPABILITIES: Dict[str, FrozenSet[str]] = {
        SYSTEM: frozenset({
            TaskType.SYSTEM_RECOVER, TaskType.SNAPSHOT_PERSIST,
        }),
    MARKET: frozenset({
        TaskType.MARKET_KLINE_COLLECT, TaskType.MARKET_KLINE_BACKFILL,
        TaskType.MARKET_QUOTE_SNAPSHOT, TaskType.DATA_INTEGRITY_CHECK,
        TaskType.COLLECT_NOW, TaskType.COLLECT_CROSS, TaskType.COLLECT_KLINE,
        TaskType.MARKET_KLINE_INTRADAY, TaskType.MARKET_KLINE_POST_CLOSE,
        TaskType.MARKET_KLINE_GAP_SCAN,
    }),
        RESEARCH: frozenset({
            TaskType.NEWS_COLLECT, TaskType.SENTIMENT_ANALYZE,
            TaskType.FUNDAMENTAL_COLLECT, TaskType.FACTOR_CALCULATE,
            TaskType.POOL_REFRESH, TaskType.STOCK_PROFILE_SYNC,
        }),
        STRATEGY: frozenset({
            TaskType.STRATEGY_RUN, TaskType.UNIVERSE_RANK,
        }),
        TRADING: frozenset({
            TaskType.RISK_EVALUATE, TaskType.TRADE_EXECUTE,
            TaskType.ACCOUNT_SETTLE,
        }),
        BACKTEST: frozenset({
            TaskType.BACKTEST_RUN, TaskType.BACKTEST_SWEEP,
        }),
        MAINTENANCE: frozenset({
            TaskType.MAINTENANCE_LOG_CLEANUP, TaskType.MAINTENANCE_ARCHIVE,
            TaskType.MAINTENANCE_VALIDATE,
        }),
        DEFAULT: frozenset({TaskType.LEGACY_JOB}),
    }

    #: 中文说明，面板与日志用
    LABEL: Dict[str, str] = {
        SYSTEM: "系统池",
        MARKET: "行情池",
        RESEARCH: "研究池",
        STRATEGY: "策略池",
        TRADING: "交易池",
        BACKTEST: "回测池",
        MAINTENANCE: "运维池",
        DEFAULT: "默认池",
    }


#: 任务类型 → 默认队列。能力路由的**默认表**，可被配置覆盖。
TASK_QUEUE: Dict[str, str] = {
    TaskType.LEGACY_JOB: Queue.DEFAULT,
    TaskType.SYSTEM_RECOVER: Queue.SYSTEM,
    TaskType.SNAPSHOT_PERSIST: Queue.SYSTEM,
    TaskType.MARKET_KLINE_COLLECT: Queue.MARKET,
    TaskType.MARKET_KLINE_BACKFILL: Queue.MARKET,
    TaskType.MARKET_QUOTE_SNAPSHOT: Queue.MARKET,
    TaskType.DATA_INTEGRITY_CHECK: Queue.MARKET,
    TaskType.MARKET_KLINE_INTRADAY: Queue.MARKET,
    TaskType.MARKET_KLINE_POST_CLOSE: Queue.MARKET,
    TaskType.MARKET_KLINE_GAP_SCAN: Queue.MARKET,
    TaskType.NEWS_COLLECT: Queue.RESEARCH,
    TaskType.SENTIMENT_ANALYZE: Queue.RESEARCH,
    TaskType.FUNDAMENTAL_COLLECT: Queue.RESEARCH,
    TaskType.FACTOR_CALCULATE: Queue.RESEARCH,
    TaskType.POOL_REFRESH: Queue.RESEARCH,
    TaskType.STOCK_PROFILE_SYNC: Queue.RESEARCH,
    TaskType.COLLECT_NOW: Queue.MARKET,
    TaskType.COLLECT_CROSS: Queue.MARKET,
    TaskType.COLLECT_KLINE: Queue.MARKET,
    TaskType.STRATEGY_RUN: Queue.STRATEGY,
    TaskType.UNIVERSE_RANK: Queue.STRATEGY,
    TaskType.RISK_EVALUATE: Queue.TRADING,
    TaskType.TRADE_EXECUTE: Queue.TRADING,
    TaskType.ACCOUNT_SETTLE: Queue.TRADING,
    TaskType.BACKTEST_RUN: Queue.BACKTEST,
    TaskType.BACKTEST_SWEEP: Queue.BACKTEST,
    TaskType.MAINTENANCE_LOG_CLEANUP: Queue.MAINTENANCE,
    TaskType.MAINTENANCE_ARCHIVE: Queue.MAINTENANCE,
    TaskType.MAINTENANCE_VALIDATE: Queue.MAINTENANCE,
}


#: 这些**任务类型**默认不允许自动重试。
#:
#: 下单能不能重试，取决于"第一次到底发没发到券商"。网络超时后我们只知道
#: "没收到回执"，不知道"券商收没收到"。盲目重试是最经典的重复下单来源，
#: 所以重试必须由订单状态机（先查单、再决定）来管，而不是由通用调度器来管。
NON_RETRIABLE_TASK_TYPES: FrozenSet[str] = frozenset({
    TaskType.TRADE_EXECUTE,
})


#: 这些**事件**是"任务系统的回声"——由任务执行过程自己发出。
#: 调度规则**绝不能**订阅它们去创建新任务，否则 task.started → 任务 →
#: task.started → 任务 …… 立刻形成无限循环。
#: 调度器在处理事件时会先查这张表并短路。
TASK_ECHO_EVENTS: FrozenSet[str] = frozenset({
    EventType.TASK_CREATED, EventType.TASK_QUEUED,
    EventType.TASK_STARTED,
    EventType.TASK_COMPLETED, EventType.TASK_FAILED,
    EventType.TASK_RETRY_SCHEDULED, EventType.TASK_DEAD_LETTERED,
    EventType.EXECUTOR_REGISTERED, EventType.EXECUTOR_HEARTBEAT_MISSED,
    EventType.EXECUTOR_CAPACITY_CHANGED,
})


__all__ = [
    "EventType", "EVENT_CATEGORIES", "EVENT_CATEGORY_OF",
    "LEGACY_EVENT_ALIASES", "CANONICAL_TO_LEGACY", "canonical_event_type",
    "TaskType", "Queue", "Pool", "TASK_QUEUE",
    "NON_RETRIABLE_TASK_TYPES", "TASK_ECHO_EVENTS",
]
