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

from app.storage.engine import Base


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
# 写入路径全部走后台线程批量提交（见 repository/trade_repo.py）：
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

    名字带 Table 后缀是为了不和 :class:`app.portfolio.manager.TradeRecord`
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


# ===========================================================================
# 资金流 / 筹码 / 涨停 / 题材 / 综合评分
# ===========================================================================
# 为什么要单独一组表：
#   传统量价（开高低收量）只回答"价格走到哪了"，回答不了
#   "谁在买、买得急不急、套牢盘多不多、是不是当天热点"。
#   这几类数据**不是逐根K线**的，而是日频的截面数据 ——
#   它们天然该按 (symbol, date) 存，而不是塞进 BarSeries。
#
#   取不到数据时的约定：**表里没数据 = 该维度不可用**，
#   打分引擎会把它按"中性/不参与"处理并**明确标出来**，
#   绝不能用 0 顶替（0 分会被当成"很差"，那是假信号）。
# ===========================================================================


class FundFlow(Base):
    """个股资金流（日频）。

    ``main_net`` 主力净流入 = 超大单 + 大单净额。这是"大资金在往哪走"
    最直接的刻画。``main_net_pct`` 是它占成交额的比例 ——
    比绝对值好用得多：一只万亿成交的票净流入 1 亿和一只小票净流入 1 亿
    完全不是一回事。
    """

    __tablename__ = "fund_flow"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)
    close = Column(Float)
    pct_change = Column(Float)
    turnover = Column(Float)                 # 换手率
    amount = Column(Float)                   # 成交额
    main_net = Column(Float)                 # 主力净流入（超大单+大单）
    main_net_pct = Column(Float)             # 主力净流入 / 成交额
    super_large_net = Column(Float)          # 超大单净额
    large_net = Column(Float)                # 大单净额
    medium_net = Column(Float)               # 中单净额
    small_net = Column(Float)                # 小单净额
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uk_fundflow_symbol_date"),
        Index("ix_fundflow_date_main", "date", "main_net"),
    )


class ChipDistribution(Base):
    """筹码分布（日频）。

    ``profit_ratio`` 获利盘比例：当前价之下的筹码占比。
      高（>90%）意味着几乎人人浮盈 → **抛压重**；
      低（<10%）意味着普遍套牢 → 上方套牢盘密集，反弹压力大。
    ``concentration_*`` 筹码集中度：值越小越集中（主力控盘度高）。
    ``peak_price`` 主筹码峰价格：现价与它的距离刻画"是否处在密集成交区"。
    """

    __tablename__ = "chip_distribution"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)
    avg_cost = Column(Float)                 # 平均成本
    profit_ratio = Column(Float)             # 获利盘比例 0~1
    cost_90_low = Column(Float)
    cost_90_high = Column(Float)
    cost_70_low = Column(Float)
    cost_70_high = Column(Float)
    concentration_90 = Column(Float)         # 90% 筹码集中度（越小越集中）
    concentration_70 = Column(Float)
    peak_price = Column(Float)               # 主筹码峰对应价格
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uk_chip_symbol_date"),
    )


class LimitUpPool(Base):
    """涨停池（日频快照）。

    ``continuous`` 连板数、``seal_amount`` 封板资金、``broken_times`` 炸板次数
    是判断"涨停质量"的三个关键：封单越大越硬、炸板越少越稳、连板越多越有辨识度。
    """

    __tablename__ = "limitup_pool"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(64))
    pct_change = Column(Float)
    close = Column(Float)
    amount = Column(Float)
    seal_amount = Column(Float)              # 封板资金
    first_seal_at = Column(String(32))       # 首次封板时间
    last_seal_at = Column(String(32))
    broken_times = Column(Integer)           # 炸板次数
    limit_stat = Column(String(16))          # 如 "3/5"：5 天里涨停 3 次
    continuous = Column(Integer)             # 连板数
    industry = Column(String(64))            # 所属行业（题材线索）
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("date", "symbol", name="uk_limitup_date_symbol"),
        Index("ix_limitup_date_continuous", "date", "continuous"),
    )


class ThemeFlow(Base):
    """题材/板块表现与资金流（日频）。

    题材热度看的是"整个板块今天如何"，而不是单只票 ——
    一只票再强，如果它所在的板块整体在退潮，持续性通常不好。
    """

    __tablename__ = "theme_flow"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True, nullable=False)
    board = Column(String(64), index=True, nullable=False)   # 板块/题材名
    kind = Column(String(16), index=True)                    # concept / industry
    pct_change = Column(Float)
    turnover = Column(Float)
    main_net = Column(Float)
    up_count = Column(Integer)
    down_count = Column(Integer)
    leading_stock = Column(String(64))       # 领涨股（题材龙头）
    rank = Column(Integer)                   # 当日涨幅排名（越小越热）
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("date", "kind", "board", name="uk_theme_date_kind_board"),
    )


class StockTheme(Base):
    """个股 ↔ 题材 的映射。

    单独一张表而不是每次现查：题材归属变化很慢，
    而打分是高频动作 —— 每次打分都去问一遍接口既不现实也容易被限流。
    """

    __tablename__ = "stock_theme"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True, nullable=False)
    board = Column(String(64), index=True, nullable=False)
    kind = Column(String(16), index=True)
    source = Column(String(32))              # 数据来源：concept_cons / limitup / manual
    date = Column(Date, index=True)
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("symbol", "board", name="uk_stocktheme_symbol_board"),
    )


class StockFundamental(Base):
    """个股基本面（估值 + 成长 + 质量）。

    为什么和资金流分开存：两者的**更新节奏完全不同**。
      资金流/筹码：日频，盘中就在变；
      估值：日频（随价格变），但一天一次足够；
      财务：季频，一份三季报要用到下一个报告期。
    硬塞一张表会导致"每次采都要把所有字段填一遍"，
    而季频字段九成时间都是空的。

    ``report_date`` 必须单独记：
      用"采集日"去标记一份三季报，回看时会误以为当时就知道了这个数据 ——
      那是最隐蔽的一类未来函数。
    """

    __tablename__ = "stock_fundamental"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)          # 数据对应日期
    report_date = Column(Date)                                # 财报报告期（季频）

    # ---- 估值 ----
    pe = Column(Float)                    # 市盈率（TTM）
    pb = Column(Float)                    # 市净率
    ps = Column(Float)                    # 市销率
    dividend_yield = Column(Float)        # 股息率 %
    market_cap = Column(Float)            # 总市值（元）
    float_market_cap = Column(Float)      # 流通市值（元）

    # ---- 成长 ----
    revenue_yoy = Column(Float)           # 营收同比 %
    profit_yoy = Column(Float)            # 净利同比 %

    # ---- 质量 ----
    roe = Column(Float)                   # 净资产收益率 %
    gross_margin = Column(Float)          # 毛利率 %
    net_margin = Column(Float)            # 净利率 %
    debt_ratio = Column(Float)            # 资产负债率 %
    bps = Column(Float)                   # 每股净资产

    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uk_fundamental_symbol_date"),
        Index("ix_fundamental_date", "date"),
    )


class StockScore(Base):
    """综合评分结果（日频，可回放）。

    存**各维度明细**而不只是总分：用户一定会问"为什么给它这么高分"，
    只留一个总分没法解释，也没法调权重。
    """

    __tablename__ = "stock_score"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    total_score = Column(Float, index=True)
    rank = Column(Integer)
    grade = Column(String(8))                # A/B/C/D
    dims_json = Column(Text)                 # 各维度得分
    reasons_json = Column(Text)              # 人话解释（加分/扣分原因）
    available_json = Column(Text)            # 哪些维度有数据（没数据的明确标出）
    source = Column(String(32))              # 候选池来源
    created_at = Column(DateTime, index=True)

    __table_args__ = (
        UniqueConstraint("date", "symbol", name="uk_score_date_symbol"),
        Index("ix_score_date_total", "date", "total_score"),
    )


class StockHotRank(Base):
    """人气榜快照（日频）。

    人气榜/资金流排名是**实时榜单**，之前每次选择候选池都要现场联网拉，
    代理一抽风就整页 0 条、且拉到的数据不落库、下次还得再拉。
    这里按 (date, symbol) 落库，刷新时先读库、库空再联网补拉，
    从而让"未开盘/代理间歇失败"时仍能拿到上一次的榜单。
    """

    __tablename__ = "stock_hot_rank"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(64))
    rank = Column(Integer)
    pct_change = Column(Float)
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("date", "symbol", name="uk_hot_date_symbol"),
    )


class StockFlowRank(Base):
    """资金流排名快照（日频，按主力净流入排序）。

    见 :class:`StockHotRank` 的说明：同样是为了"落库兜底"，
    让资金榜候选池在代理间歇失败时也能读到历史榜单。
    """

    __tablename__ = "stock_flow_rank"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(64))
    pct_change = Column(Float)
    main_net = Column(Float)
    main_net_pct = Column(Float)
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("date", "symbol", name="uk_flow_date_symbol"),
    )


class StockInfo(Base):
    """标的名称对照表（symbol -> name）。

    评分结果只存代码不存名称，"600042 是哪只票"根本看不出来。
    这里集中维护一份代码↔名称对照：全市场快照（``stock_zh_a_spot_em``）
    一次就能把全部 A 股名称灌进来，候选池同步时顺手也补。读取评分时
    按代码查名称回填，查不到就是"未知"（多半是脏数据/退市/幻想代码）。
    """

    __tablename__ = "stock_info"

    symbol = Column(String(20), primary_key=True)
    name = Column(String(64))
    updated_at = Column(DateTime)


class StockFactorValue(Base):
    """通用因子值（个股 × 日期 × 类别 × 指标名）。

    ============================================================
    为什么用"通用表"而不是每类数据一张表
    ============================================================
    要接的数据类别会不断加：龙虎榜、融资融券、业绩预告、股东户数、
    机构评级、研报、大宗交易、北向持股……
    每加一类就加一张表 + 一个仓储 + 一组查询方法，
    三个月后就是十几张表、十几套几乎一样的代码 —— 而它们的
    结构完全相同：**(标的, 日期, 指标名, 数值)**。

    所以统一成一张表，用 ``category`` 区分来源：
      - 新增一类数据 = 加一个获取函数，**不动表结构、不动仓储**
      - 查询是同一个入口，特征层能一次性把所有类别装配起来
      - 指标名是数据驱动的，界面上列得出"现在有哪些指标"

    代价是数值只能是 float（放不下的进 ``extra_json``），
    以及没有列级约束。对截面因子数据来说这是划算的交换。

    ``category`` 取值见 :data:`app.datasource.service.facade.CATEGORIES`。
    """

    __tablename__ = "stock_factor_value"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)
    #: 数据类别：lhb / margin / forecast / holders / rating / theme / flow …
    category = Column(String(32), index=True, nullable=False)
    #: 指标名：如 "lhb_net_buy"、"margin_balance"、"forecast_yoy"
    name = Column(String(64), index=True, nullable=False)
    value = Column(Float)
    #: 放不进单个 float 的补充信息（如机构名称、公告标题）
    extra_json = Column(Text)
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("symbol", "date", "category", "name",
                         name="uk_fvalue_sym_date_cat_name"),
        Index("ix_fvalue_date_cat", "date", "category"),
        Index("ix_fvalue_cat_name", "category", "name"),
    )


class MarketNews(Base):
    """舆情：新闻 / 公告 / 研报（带情感分）。

    与新闻中心（RSS 实时流）的分工：
      新闻中心负责"实时触发交易事件"，
      这里负责"沉淀成可回看、可回测的**特征**"。
    两者不是一回事 —— 实时那条链路不需要存三个月前的新闻，
    但因子研究需要。

    ``sentiment`` 由关键词规则或 LLM 产出，落在 -1~1。
    没有情感分的新闻也会存（value 为 None），因为"出现了多少条"
    本身就是关注度信号。
    """

    __tablename__ = "market_news"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True)      # 空 = 市场级
    date = Column(Date, index=True, nullable=False)
    title = Column(Text)
    #: news / notice / report / research
    kind = Column(String(16), index=True, default="news")
    source = Column(String(64))
    sentiment = Column(Float)                     # -1 ~ 1，None=未分析
    #: 热度/阅读/评论数（有则记，用于加权）
    heat = Column(Float)
    url = Column(Text)
    created_at = Column(DateTime)

    __table_args__ = (
        # 同一天同一标题只留一条（重复采集是幂等的前提）
        UniqueConstraint("symbol", "date", "kind", "title", name="uk_news_dedup"),
        Index("ix_news_date_kind", "date", "kind"),
    )


class MacroIndicator(Base):
    """宏观指标（市场级，非个股）。

    宏观不决定明天涨跌，但它决定**什么类型的策略在这个环境下能活**：
    利率上行周期里高估值成长股系统性承压，PMI 拐点往往领先周期股。
    所以它作为"环境开关"参与打分，而不是当择时信号。
    """

    __tablename__ = "macro_indicator"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True, nullable=False)
    #: 指标代码：cpi / ppi / pmi / m2 / shibor / usdcny / gdp …
    name = Column(String(32), index=True, nullable=False)
    value = Column(Float)
    #: 同比或环比（%），口径不统一就留空
    yoy = Column(Float)
    period = Column(String(16))                  # month / quarter
    source = Column(String(32))
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("date", "name", name="uk_macro_date_name"),
        Index("ix_macro_name_date", "name", "date"),
    )


class MarketBreadth(Base):
    """市场宽度/情绪（市场级，非个股）。

    "今天全市场是涨多还是跌多、有多少涨停、成交量多少" ——
    这是所有个股策略的**共同背景**。同一只票在普涨日和普跌日里
    同样的技术形态，胜率完全不同。
    """

    __tablename__ = "market_breadth"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True, unique=True, nullable=False)
    up_count = Column(Integer)                   # 上涨家数
    down_count = Column(Integer)                 # 下跌家数
    limit_up_count = Column(Integer)             # 涨停家数
    limit_down_count = Column(Integer)           # 跌停家数
    total_amount = Column(Float)                 # 两市成交额（元）
    #: -100 ~ 100，正=偏乐观
    sentiment = Column(Float)
    updated_at = Column(DateTime)
