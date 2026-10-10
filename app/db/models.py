#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 15:14
# @FileName    : models.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    Index,
    Text,
    UniqueConstraint
)

from app.db.database import Base


class StockDaily(Base):

    __tablename__ = "stock_daily"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "date",
            name="uk_symbol_date"
        ),
    )


class StockMinute(Base):
    """分钟线/日内K线。

    为什么和日线分开一张表：
      1. 数据量差两个数量级（1 分钟线一年约 4.8 万条/标的），
         混在 stock_daily 里会让"日线回测"的每次扫描都变慢；
      2. 分钟线的生命周期完全不同——通常只保留最近若干个交易日，
         需要按 symbol+period 整段清理，分表才删得干净。

    ``period`` 用字符串存（'1'/'5'/'15'/'30'/'60'），与数据源的参数保持一致，
    避免以后加 2 分钟、10 分钟线时还要迁移表结构。
    """

    __tablename__ = "stock_minute"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    period = Column(String(8), nullable=False)
    dt = Column(DateTime, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "period",
            "dt",
            name="uk_symbol_period_dt"
        ),
    )

# ===========================================================================
# 交易与运行记录
# ===========================================================================
# 为什么这些要进 SQLite 而不是继续写 JSON/JSONL：
#   「今天这个任务成交了几笔」「某任务的权益曲线怎么走的」
#   「所有被风控否决的订单里哪种规则最多」
# —— 这些问题的答案天然是"按条件查一批行"，JSON 只能顺序读。
# 一旦要按 task_id + 日期过滤、要聚合、要 join，文件方案就崩了。
#
# 写入路径全部走后台线程批量提交（见 repository/trading_repository.py）：
# 交易主链路绝不能被 SQLite 的 fsync 阻塞。
# ===========================================================================


class RunRecord(Base):
    """一次引擎运行的记录。run_id 是主键，方便所有子表关联。"""

    __tablename__ = "run_record"

    run_id = Column(String(64), primary_key=True)
    mode = Column(String(16), index=True)          # BACKTEST / SIMULATE / LIVE
    namespace = Column(String(64), index=True, default="default")
    status = Column(String(16), index=True)        # RUNNING / STOPPED / ERROR
    started_at = Column(DateTime, index=True)
    stopped_at = Column(DateTime)
    host = Column(String(64))
    pid = Column(Integer)
    note = Column(Text)


class TaskRecord(Base):
    """任务在某个 run 里的登记信息（策略、参数、初始资金、风控链）。"""

    __tablename__ = "task_record"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), index=True, nullable=False)
    task_id = Column(String(64), index=True, nullable=False)
    symbol = Column(String(20), index=True)
    strategy = Column(String(64))
    run_mode = Column(String(16))
    initial_cash = Column(Float)
    status = Column(String(16))
    spec_json = Column(Text)                        # 任务完整 spec，便于复现
    created_at = Column(DateTime, index=True)

    __table_args__ = (
        UniqueConstraint("run_id", "task_id", name="uk_run_task"),
    )


class OrderRecord(Base):
    """订单生命周期。

    注意**状态会更新**（PENDING → SUBMITTED → FILLED/PARTIAL/CANCELLED），
    所以 order_id 唯一，靠 upsert 覆盖，而不是 append 流水。
    这样才能查"当前还有哪些挂着没成交的单"。
    """

    __tablename__ = "order_record"

    order_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), index=True)
    task_id = Column(String(64), index=True)
    symbol = Column(String(20), index=True)
    side = Column(String(8))                        # BUY / SELL
    size = Column(Integer)
    price = Column(Float)
    status = Column(String(16), index=True)
    filled_size = Column(Integer, default=0)
    filled_price = Column(Float, default=0.0)
    reason = Column(Text)
    reject_reason = Column(Text)
    source = Column(String(32))
    created_at = Column(DateTime, index=True)
    updated_at = Column(DateTime)

    __table_args__ = (
        Index("ix_order_task_created", "task_id", "created_at"),
        Index("ix_order_run_status", "run_id", "status"),
    )


class TradeRecordTable(Base):
    """成交明细。一次成交一行，append-only，不更新。

    名字带 Table 后缀是为了不和 :class:`app.core.portfolio.manager.TradeRecord`
    这个领域对象重名——两者含义接近但层次不同。
    """

    __tablename__ = "trade_record"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), index=True)
    task_id = Column(String(64), index=True)
    order_id = Column(String(64), index=True)
    symbol = Column(String(20), index=True)
    side = Column(String(8))
    size = Column(Integer)
    price = Column(Float)
    fee = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    cash_after = Column(Float)
    position_after = Column(Integer)
    reason = Column(Text)
    source = Column(String(32))
    dt = Column(DateTime, index=True)


class EquityPoint(Base):
    """权益曲线采样点。

    刻意**不逐笔存**：曲线看的是趋势，逐笔的精度既没人看也把表撑爆。
    采样间隔由 EQUITY_SAMPLE_INTERVAL 控制（默认 30 秒）。
    """

    __tablename__ = "equity_point"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), index=True)
    task_id = Column(String(64), index=True)
    symbol = Column(String(20))
    equity = Column(Float)
    cash = Column(Float)
    position_size = Column(Integer)
    market_value = Column(Float)
    drawdown = Column(Float)
    dt = Column(DateTime, index=True)

    __table_args__ = (
        Index("ix_equity_task_dt", "task_id", "dt"),
    )


class BacktestResult(Base):
    """一次回测的结果摘要。

    只存指标和曲线，不存逐笔明细（那些在 order/trade/equity 表里，
    用 run_id 关联即可）。这样看板上拉列表时不用扫大表。
    """

    __tablename__ = "backtest_result"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), index=True, nullable=False)
    task_id = Column(String(64), index=True)
    symbol = Column(String(20), index=True)
    strategy = Column(String(64), index=True)
    start_date = Column(String(16))
    end_date = Column(String(16))
    initial_cash = Column(Float)
    final_equity = Column(Float)
    total_return = Column(Float)
    max_drawdown = Column(Float)
    sharpe = Column(Float)
    trade_count = Column(Integer)
    win_rate = Column(Float)
    metrics_json = Column(Text)                     # 完整指标，便于后续加字段
    equity_json = Column(Text)                      # 降采样后的曲线
    created_at = Column(DateTime, index=True)


class SystemEvent(Base):
    """重要系统事件（采集结果、风控否决、组件启停、异常）。

    与 loguru 日志的分工：
      - 日志文件：给人看的、带上下文的自由文本，按天轮转、会被清理
      - 这张表：给程序查的、结构化的"关键事实"，可长期保留、可聚合

    只落**重要**事件（配合日志分级策略），不是 log 的镜像。
    """

    __tablename__ = "system_event"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), index=True)
    ts = Column(DateTime, index=True)
    level = Column(String(16), index=True)
    category = Column(String(32), index=True)       # collect / risk / order / engine / error
    task_id = Column(String(64), index=True)
    symbol = Column(String(20), index=True)
    message = Column(Text)
    detail_json = Column(Text)
