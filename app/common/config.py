#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @FileName    : config.py
# @Description : 全局配置：统一路径、数据库、日志、回测默认值
#               所有值均可通过环境变量覆盖，避免硬编码散落在业务代码里
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    """全局配置。

    约定：项目根目录 = 本文件往上三级（AlphaQuant/）。
    所有目录在首次访问时自动创建，避免 "日志写到 CWD" 这类依赖运行位置的坑。
    """

    # ---------------- 路径 ----------------
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    LOG_DIR: Path = BASE_DIR / "logs"
    OUTPUT_DIR: Path = BASE_DIR / "output"
    CSV_DIR: Path = DATA_DIR / "stock"
    CONFIG_DIR: Path = BASE_DIR / "config"

    # ---------------- 运行时产物目录 ----------------
    SNAPSHOT_DIR: Path = OUTPUT_DIR / "snapshots"   # 运行快照（每个 run 一个子目录）
    RUNTIME_DIR: Path = OUTPUT_DIR / "runtime"      # 运行时增删的任务，重启可重放
    SERVICES_DIR: Path = OUTPUT_DIR / "services"    # 多服务：各服务心跳/命令/状态
    BUS_DIR: Path = OUTPUT_DIR / "bus"              # 多服务：文件消息总线

    # ---------------- 数据库 ----------------
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", f"sqlite:///{DATA_DIR.as_posix()}/alphaquant.db"
    )

    # ---------------- 日志 ----------------
    # 级别：DEBUG / INFO / WARNING / ERROR
    #   INFO（默认）= 只记"重要信息"：生命周期、成交、风控否决、异常。
    #                逐根K线、缓存命中、指标中间值这类一律走 DEBUG，不进文件。
    #   DEBUG       = 调试模式，全部记录（含每根K线的决策明细）
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    # 文件可以比控制台更严（比如控制台 DEBUG 看得清、文件只留 INFO 省磁盘）
    LOG_FILE_LEVEL: str = os.getenv("LOG_FILE_LEVEL", "").upper()
    LOG_RETENTION: str = os.getenv("LOG_RETENTION", "30 days")
    # 单文件轮转上限，防止一次调试把磁盘写满
    LOG_ROTATION: str = os.getenv("LOG_ROTATION", "00:00")
    # 同一位置重复日志的最小间隔（秒）。0 = 不节流。
    # 长跑服务里"每根K线都告警一次"会让日志文件失去可读性。
    LOG_THROTTLE_WINDOW: float = _env_float("LOG_THROTTLE_WINDOW", 5.0)
    # 日志级别 < 该值的记录不落文件（默认与 LOG_FILE_LEVEL 联动）
    LOG_TRACE_KEEP: int = _env_int("LOG_TRACE_KEEP", 0)

    # ---------------- 数据源 ----------------
    # 逗号分隔，按顺序降级，例如 "baostock,akshare"
    DATA_SOURCE_PRIORITY: str = os.getenv("DATA_SOURCE_PRIORITY", "baostock,akshare")
    DATA_FETCH_MAX_RETRY: int = _env_int("DATA_FETCH_MAX_RETRY", 3)
    # 两次请求之间的最小间隔（秒），防止被数据源限流封 IP
    DATA_FETCH_MIN_INTERVAL: float = _env_float("DATA_FETCH_MIN_INTERVAL", 0.3)
    # 显式指定出口代理，例如 "http://127.0.0.1:7890"。
    # 为什么需要它而不只是依赖 HTTP_PROXY 环境变量：采集服务常以守护进程/
    # 计划任务方式启动，那种进程**不继承**你在 shell 里设的环境变量，
    # 于是直连东财被 RST，报"所有源都失败"，而你在命令行里手动跑却是好的。
    # 留空则按 HTTP_PROXY 环境变量，再不行就自动探测常见端口。
    DATA_PROXY: str = os.getenv("DATA_PROXY", "")
    # 是否允许自动探测本机常见代理端口（Clash/Shadowsocks/V2Ray 默认端口）。
    # 探测只在本机回环地址上做 TCP 连接，不发包，开销可忽略。
    DATA_PROXY_AUTODETECT: bool = _env_bool("DATA_PROXY_AUTODETECT", True)
    # 数据源初始权重覆盖，JSON 对象，例如 '{"mootdx":0.5,"tushare":2}'。
    # 不配就是全部 1（顺序完全由 DATA_SOURCE_PRIORITY 决定）；
    # 运行时会根据成败自动调整，并落盘到 data/source_weights.json。
    DATA_SOURCE_WEIGHTS: str = os.getenv("DATA_SOURCE_WEIGHTS", "")

    # ---------------- 数据校验服务（启动后体检 + 自动补全）----------------
    # 为什么要单开一个组件：库里缺一根K线，回测不会报错，只会**悄悄少一段行情**，
    # 策略信号因此漂移，而这种错根本没人会去查 SQL。
    DATA_GUARD_ENABLED: bool = _env_bool("DATA_GUARD_ENABLED", True)
    #: 组件启动后延迟多少秒跑首次体检。给采集服务留出首批入库时间，
    #: 也避免几个组件在启动瞬间同时打数据源。
    DATA_GUARD_START_DELAY: float = _env_float("DATA_GUARD_START_DELAY", 20.0)
    #: 体检周期（秒）。日线一天才变一次，一小时一次足够。
    DATA_GUARD_INTERVAL: float = _env_float("DATA_GUARD_INTERVAL", 3600.0)
    #: 每只标的回溯多少天找缺口。太大没意义（老缺口补不回来），太小查不出问题。
    DATA_GUARD_LOOKBACK_DAYS: int = _env_int("DATA_GUARD_LOOKBACK_DAYS", 120)
    #: 是否自动补采（关掉 = 只体检不修，先看报告再决定）
    DATA_GUARD_AUTO_REPAIR: bool = _env_bool("DATA_GUARD_AUTO_REPAIR", True)
    #: 单次体检最多查多少只标的（防止 5000 只全表扫描把启动卡住）
    DATA_GUARD_MAX_SYMBOLS: int = _env_int("DATA_GUARD_MAX_SYMBOLS", 50)
    #: 单次体检最多补多少只（控制联网请求量）
    DATA_GUARD_MAX_REPAIR: int = _env_int("DATA_GUARD_MAX_REPAIR", 20)
    #: 校验哪些数据表。逗号分隔，可选: daily / fund_flow / chip
    DATA_GUARD_CHECKS: str = os.getenv("DATA_GUARD_CHECKS", "daily,fund_flow,chip")

    # ---------------- 回测默认值 ----------------
    DEFAULT_CASH: float = _env_float("DEFAULT_CASH", 100000.0)
    DEFAULT_COMMISSION: float = _env_float("DEFAULT_COMMISSION", 0.0003)
    DEFAULT_SLIPPAGE_PERC: float = _env_float("DEFAULT_SLIPPAGE_PERC", 0.001)
    # 印花税：**只在卖出时收**。A 股现行万五（2023-08 起由千一减半）。
    # 它比佣金（万三）还贵，是卖出侧的主要成本 —— 漏掉它会让回测
    # 系统性高估策略收益（换手越高、周期越短，高估越严重）。
    DEFAULT_STAMP_DUTY: float = _env_float("DEFAULT_STAMP_DUTY", 0.0005)
    #: 单笔最低佣金（元）。券商普遍为 5 元，小额买入时不可忽略。
    MIN_COMMISSION: float = _env_float("MIN_COMMISSION", 5.0)
    #: 过户费率（双边，按成交金额）。沪深均收，现行十万分之一。
    TRANSFER_FEE: float = _env_float("TRANSFER_FEE", 0.00001)

    # ---------------- 引擎 ----------------
    # 回测模式下，调度器持续空闲多少秒后自动停止引擎（避免 reactor 永久阻塞）
    BACKTEST_IDLE_TIMEOUT: float = _env_float("BACKTEST_IDLE_TIMEOUT", 5.0)
    BACKTEST_AUTO_STOP: bool = _env_bool("BACKTEST_AUTO_STOP", True)

    # ---------------- 行情 ----------------
    MARKET_MODE: str = os.getenv("MARKET_MODE", "replay")       # replay / poll
    MARKET_INTERVAL: float = _env_float("MARKET_INTERVAL", 0.05)  # 秒
    MARKET_SYMBOLS: str = os.getenv("MARKET_SYMBOLS", "")        # 逗号分隔

    # ---------------- 新闻 ----------------
    NEWS_ENABLED: bool = _env_bool("NEWS_ENABLED", True)
    NEWS_INTERVAL: float = _env_float("NEWS_INTERVAL", 60.0)     # 采集间隔（秒）
    NEWS_SOURCES: str = os.getenv("NEWS_SOURCES", "")            # 逗号分隔的 RSS 地址
    NEWS_ANALYZER: str = os.getenv("NEWS_ANALYZER", "keyword")   # keyword / llm

    # ---------------- 大模型（可选，没配 key 就自动降级到规则版）----------------
    LLM_API_BASE: str = os.getenv("LLM_API_BASE", "https://api.openai.com/v1")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    LLM_TIMEOUT: float = _env_float("LLM_TIMEOUT", 20.0)
    LLM_MIN_CONFIDENCE: float = _env_float("LLM_MIN_CONFIDENCE", 0.5)

    # ---------------- 任务配置 ----------------
    TASK_CONFIG: str = os.getenv(
        "TASK_CONFIG", (BASE_DIR / "config" / "tasks.json").as_posix()
    )

    # ---------------- 运行时（进程级）配置 ----------------
    # 执行池 worker 数这类**建池时就固定**的参数放在这里。
    # 注意它 TASK_CONFIG 的分工：tasks.json 管"跑哪些策略"，
    # runtime.json 管"这台机器拿多少资源跑"。
    RUNTIME_CONFIG: str = os.getenv(
        "RUNTIME_CONFIG", (BASE_DIR / "config" / "runtime.json").as_posix()
    )

    # ---------------- 行情采集服务（常驻自动采集，替代手动执行）----------------
    # 采集编排配置：每个标的的数据粒度与采集频率都在这里改，不用动代码
    COLLECTOR_CONFIG: str = os.getenv(
        "COLLECTOR_CONFIG", (BASE_DIR / "config" / "collector.json").as_posix()
    )
    COLLECTOR_ENABLED: bool = _env_bool("COLLECTOR_ENABLED", True)
    # 默认采集频率：支持 "30s" / "5m" / "1h" / "1d"，裸数字按秒
    COLLECTOR_INTERVAL: str = os.getenv("COLLECTOR_INTERVAL", "5m")
    # 默认数据粒度：1d=日线；1/5/15/30/60=分钟线
    COLLECTOR_PERIOD: str = os.getenv("COLLECTOR_PERIOD", "1d")
    # 频率下限（秒），防止误配成 1s 把数据源打挂
    COLLECTOR_MIN_INTERVAL: float = _env_float("COLLECTOR_MIN_INTERVAL", 15.0)
    COLLECTOR_SYMBOLS: str = os.getenv("COLLECTOR_SYMBOLS", "")     # 逗号分隔

    # ---------------- 工作单元（一次性后台作业的并发上限）----------------
    # 面板上"立即采集 / 立即体检 / 立即回测"这类手动作业走工作单元，
    # 而不是无上限地 deferToThread —— 连点五次"立即体检"会同时发五轮
    # 补全请求，把数据源打到限流，连定时采集一起拖下水。
    WORKER_ENABLED: bool = _env_bool("WORKER_ENABLED", True)
    WORKER_CAPACITY: int = _env_int("WORKER_CAPACITY", 2)

    # ---------------- 组合服务（账户聚合 + 跨任务额度预留）----------------
    # 全账户"在途下单额度"上限（元）。不配时自动取聚合账户的可用现金，
    # 于是"下单占用的钱不能超过账户里有的钱"这条底线一行配置都不写也成立。
    # 显式配一个更小的数，等于给"同时在途的资金"再上一道闸。
    PORTFOLIO_BUDGET: Optional[float] = (
        float(os.getenv("PORTFOLIO_BUDGET")) if os.getenv("PORTFOLIO_BUDGET") else None
    )
    # 单笔预留的存活秒数。实盘订单可能永远收不到回报，没有 TTL 的预留会
    # 永久占住额度，症状是"跑着跑着就不下单了"且日志里没有任何错误。
    PORTFOLIO_RESERVE_TTL: float = float(os.getenv("PORTFOLIO_RESERVE_TTL", "180"))

    # ---------------- 监控面板（内嵌 Twisted Web，零额外进程）----------------
    MONITOR_ENABLED: bool = _env_bool("MONITOR_ENABLED", True)
    MONITOR_HOST: str = os.getenv("MONITOR_HOST", "127.0.0.1")
    MONITOR_PORT: int = _env_int("MONITOR_PORT", 8787)

    # ---------------- 运行快照 ----------------
    # 快照策略（**默认不按时间写盘**）：
    #   on_event  = 有"操作"才落盘（默认）。什么算操作：下单/成交/撤单/拒单、
    #               任务增删改暂停恢复、组件启停、控制指令、引擎启停。
    #               行情自己跳动、权益随行情浮动**不算操作** —— 那是同一份状态。→
    #               所以没人动它时，磁盘上不会多出一堆一模一样的快照。
    #   on_change = 定期检查内容指纹，真变了才落盘（比 on_event 多覆盖"状态自己漂移"）
    #   interval  = 到点就写，不管有没有变化（回放复盘想要完整时间轴时用）
    #   off       = 完全关闭快照（只要面板实时看，不要历史）
    MONITOR_SNAPSHOT_MODE: str = os.getenv("MONITOR_SNAPSHOT_MODE", "on_event").lower()
    # 快照开关。关掉后引擎不写任何快照文件（面板仍可用，读的是内存状态）
    MONITOR_SNAPSHOT_ENABLED: bool = _env_bool("MONITOR_SNAPSHOT_ENABLED", True)
    # 检查间隔（秒）。on_change 模式下这是"多久检查一次有没有变化"，不是"多久写一次"。
    # on_event 模式下不用它（那时由事件触发）。
    MONITOR_SNAPSHOT_INTERVAL: float = _env_float("MONITOR_SNAPSHOT_INTERVAL", 5.0)
    # 历史快照最多保留多少个（滚动淘汰）
    MONITOR_SNAPSHOT_KEEP: int = _env_int("MONITOR_SNAPSHOT_KEEP", 300)
    # 两次落盘之间的最小间隔（秒）。作用是**合并突发**：
    # 一次回放可能在一秒内产生上百笔订单，逐笔各写一份快照纯属浪费，
    # 攒够这个间隔写一份即可（内容已包含全部变化，不丢信息）。
    MONITOR_SNAPSHOT_MIN_GAP: float = _env_float("MONITOR_SNAPSHOT_MIN_GAP", 2.0)
    # on_event 模式下的抖动窗口（秒）：操作发生后再等这么久才落盘，
    # 把同一批操作合并成一份快照。0 = 操作后立刻写。
    MONITOR_SNAPSHOT_DEBOUNCE: float = _env_float("MONITOR_SNAPSHOT_DEBOUNCE", 1.0)

    # ---------------- 后台守护 ----------------
    PID_FILE: str = os.getenv("PID_FILE", (LOG_DIR / "alphaquant.pid").as_posix())
    STOP_FILE: str = os.getenv("STOP_FILE", (LOG_DIR / "alphaquant.stop").as_posix())
    DAEMON_LOG: str = os.getenv("DAEMON_LOG", (LOG_DIR / "service.out.log").as_posix())
    DAEMON_STOP_TIMEOUT: float = _env_float("DAEMON_STOP_TIMEOUT", 20.0)

    # ---------------- 多服务协作 ----------------
    SERVICES_CONFIG: str = os.getenv(
        "SERVICES_CONFIG", (BASE_DIR / "config" / "services.json").as_posix()
    )
    HUB_HOST: str = os.getenv("HUB_HOST", "127.0.0.1")
    HUB_PORT: int = _env_int("HUB_PORT", 8899)
    # 心跳超过多少秒没更新即判定服务失联（supervisor 据此重启）
    SERVICE_HEARTBEAT_TIMEOUT: float = _env_float("SERVICE_HEARTBEAT_TIMEOUT", 15.0)
    SERVICE_HEARTBEAT_INTERVAL: float = _env_float("SERVICE_HEARTBEAT_INTERVAL", 3.0)

    # ---------------- 交易落盘（订单/成交/权益 → SQLite）----------------
    # 这些数据天然是"关系型 + 要能按条件查"的（今天成交了什么、某任务权益曲线），
    # 塞进 JSON 文件就只能顺序读，越跑越难用。
    TRADE_PERSIST_ENABLED: bool = _env_bool("TRADE_PERSIST_ENABLED", True)
    # 权益曲线采样间隔（秒）。逐笔都存会让表膨胀得很快，而且曲线也不需要那个精度。
    EQUITY_SAMPLE_INTERVAL: float = _env_float("EQUITY_SAMPLE_INTERVAL", 30.0)
    # 写库批量大小与刷盘间隔（后台线程批量写，避免 trading 路径被 SQLite 阻塞）
    TRADE_FLUSH_BATCH: int = _env_int("TRADE_FLUSH_BATCH", 50)
    TRADE_FLUSH_INTERVAL: float = _env_float("TRADE_FLUSH_INTERVAL", 2.0)

    # ---------------- 扩展行情数据采集（资金流 / 筹码 / 涨停池 / 题材）----------------
    # 这类数据是"日频截面"的，和逐根K线不是一回事，所以单独一个采集组件。
    # 回测/回放模式默认不开：那段数据是死的，联网抓今天的截面数据没有意义。
    EXT_DATA_ENABLED: bool = _env_bool("EXT_DATA_ENABLED", True)
    #: 资金流/筹码的采集间隔（秒）。这类数据变化慢，5~10 分钟足够。
    EXT_DATA_INTERVAL: float = _env_float("EXT_DATA_INTERVAL", 600.0)
    #: 涨停池/题材这类"市场级"数据每天同步几次
    EXT_DATA_DAILY_INTERVAL: float = _env_float("EXT_DATA_DAILY_INTERVAL", 3600.0)
    #: 题材映射最多建多少个热门板块的成分（全建必然触发限流）
    EXT_DATA_THEME_TOP: int = _env_int("EXT_DATA_THEME_TOP", 20)
    #: 是否采集基本面（估值/财务）。季频数据，一天一次就够。
    EXT_DATA_FUNDAMENTAL: bool = _env_bool("EXT_DATA_FUNDAMENTAL", True)

    # ---------------- 关注标的（自选股的定时自动采集）----------------
    # 它和「股票池」不是一回事：股票池是**打分选出来**的结果（天天在变），
    # 关注标的是**人显式指定**的清单 —— 盯上了，就按各自的频率持续补数据。
    # 区别不在谁更重要，而在节拍归谁管：股票池是"到点重算一遍"，
    # 关注标的是"每只票各自到各自的点"。
    WATCHLIST_ENABLED: bool = _env_bool("WATCHLIST_ENABLED", True)
    #: 关注标的的**默认**采集频率（"1h" / "30m" / "1d"，裸数字按秒）。
    #: 单只标的可以在监控看板或 /api/watchlist/freq 里单独覆盖。
    WATCHLIST_INTERVAL: str = os.getenv("WATCHLIST_INTERVAL", "1h")
    #: 关注标的默认额外采集的类别（逗号分隔）。留空 = 跟随 cross 单元的默认类别。
    #: 注意 K 线与日频截面（资金流/筹码/基本面）**本来就包含在采集里**，
    #: 这里只是补充 lhb / margin / forecast / holders / rating / news 这类别。
    WATCHLIST_CATEGORIES: str = os.getenv("WATCHLIST_CATEGORIES", "")
    #: 扫描周期（秒）：多久**看一眼**有没有标的到点了。
    #: 这是"检查"间隔而不是采集频率 —— 别把它当成每只票的重采周期，
    #: 真正决定多久采一次的是单只标的自己的 freq（默认 = WATCHLIST_INTERVAL）。
    WATCHLIST_SWEEP_INTERVAL: float = _env_float("WATCHLIST_SWEEP_INTERVAL", 60.0)
    #: 单轮最多采多少只关注标的。这是**限流护栏**：
    #: 一次拉 200 只走全类别采集，足够把所有数据源一起送进黑名单，
    #: 然后连正常定时采集一起拖下水。没采到的下一轮自然补上。
    WATCHLIST_MAX_PER_ROUND: int = _env_int("WATCHLIST_MAX_PER_ROUND", 20)

    # ---------------- 扩展行情特征（资金流 / 筹码 / 题材 / 评分）----------------
    # 是否把这类日频截面数据注入因子上下文。关掉就是纯量价策略。
    # 离线回放（库里没有这些数据时）建议关掉，省掉每根K线一次装配尝试。
    EXT_FEATURES_ENABLED: bool = _env_bool("EXT_FEATURES_ENABLED", True)
    #: 特征缓存时间（秒）。打分要遍历候选池，不缓存会打爆数据库。
    EXT_FEATURE_TTL: float = _env_float("EXT_FEATURE_TTL", 300.0)

    # ---------------- 账户（资金账本 + 内置虚拟账户）----------------
    # 账户服务负责"钱记在哪儿"这件事：确保存在一个内置虚拟账户、
    # 维护它的余额与持仓快照、在收盘后固化当天的资金事实。
    ACCOUNT_ENABLED: bool = _env_bool("ACCOUNT_ENABLED", True)
    #: 虚拟账户的本金。**只在账户首次创建时生效**（之后改它不会改变已跑的钱），
    #: 否则"把本金改大"就变成了一种篡改历史收益率的手法。
    ACCOUNT_INITIAL_CASH: float = _env_float("ACCOUNT_INITIAL_CASH", 100_000.0)
    #: 交易时段轮询间隔（秒）。决定"的服务要花多久才发现开盘了"；
    #: 30 秒是个平衡点 —— 1 秒会白白空转，5 分钟则迟到半天。
    ACCOUNT_SESSION_POLL: float = _env_float("ACCOUNT_SESSION_POLL", 30.0)
    #: 虚拟账户走的网关/接入点。保持 simulated 是**刻意的**：
    #: 它保证这条链路永远不会把单子发到交易所。
    ACCOUNT_GATEWAY: str = os.getenv("ACCOUNT_GATEWAY", "simulated")
    ACCOUNT_ENDPOINT: str = os.getenv("ACCOUNT_ENDPOINT", "")

    # ---------------- 风控服务 ----------------
    # 关掉 = 所有信号直接放行且熔断失效。默认必须开：
    # 一个没有闸门的系统，"它最多会亏多少"是没有答案的。
    RISK_ENABLED: bool = _env_bool("RISK_ENABLED", True)

    # ---------------- 交易服务 ----------------
    # 交易服务**独立于实盘模式**：关模式下它只落库不连网关，
    # 所以"能不能订下令可追溯"与"敢不敢真下单"是两件事，各自有开关。
    TRADING_ENABLED: bool = _env_bool("TRADING_ENABLED", True)

    # ---------------- 实盘 ----------------
    # 默认撮合模式：simulated（本地模拟）/ live（真实券商网关）
    EXECUTION_MODE: str = os.getenv("EXECUTION_MODE", "simulated").lower()
    # 券商网关实现（按名字从执行层注册表找）。未配置时实盘模式拒绝启动，
    # 而不是"悄悄用模拟撮合假装在实盘"——那会让人以为已经接上券商了。
    BROKER_GATEWAY: str = os.getenv("BROKER_GATEWAY", "")
    # 实盘下单前置检查
    LIVE_MAX_ORDER_VALUE: float = _env_float("LIVE_MAX_ORDER_VALUE", 0.0)   # 0=不限
    LIVE_MAX_DAILY_TRADES: int = _env_int("LIVE_MAX_DAILY_TRADES", 0)       # 0=不限
    # 下单后多久没收到回报就认为丢了，去券商查一遍（秒）
    LIVE_ORDER_TIMEOUT: float = _env_float("LIVE_ORDER_TIMEOUT", 30.0)
    # 实盘启动时是否要求持仓与券商对账一致（对不上就拒绝启动）
    LIVE_STRICT_RECONCILE: bool = _env_bool("LIVE_STRICT_RECONCILE", True)
    # 涨跌停价格笼子：超出这个比例的下单直接拒绝（防止错误价格成交）
    LIVE_PRICE_LIMIT_PCT: float = _env_float("LIVE_PRICE_LIMIT_PCT", 0.02)

    # ---------------- 数据清洗 ----------------
    # 单根K线相对上一根的异常波动阈值（超过则标记为可疑，默认 21% ≈ 一个主板涨跌停）
    CLEAN_MAX_MOVE_PCT: float = _env_float("CLEAN_MAX_MOVE_PCT", 0.21)
    # 清洗策略：mark=只标记不丢 / drop=直接丢弃可疑数据
    CLEAN_ACTION: str = os.getenv("CLEAN_ACTION", "mark").lower()

    # ---------------- 用户自定义策略 ----------------
    # 用户策略目录。放进去的 .py 会被自动发现并注册，**回测链路与实盘链路都能用**。
    # 刻意放在工程根的 strategies/（而不是 app/ 里面）：升级框架时不会覆盖你的策略。
    USER_STRATEGY_DIR: str = os.getenv(
        "USER_STRATEGY_DIR", (BASE_DIR / "strategies").as_posix()
    )
    # 是否自动发现用户策略。测试环境建议关掉，避免外部文件混进断言。
    USER_STRATEGY_AUTOLOAD: bool = _env_bool("USER_STRATEGY_AUTOLOAD", True)

    # ---------------- 回测默认参数 ----------------
    # 初始资金/手续费/滑点/默认区间/默认策略等都在这个文件里改，不用动代码。
    # 优先级：命令行/面板显式传入 > 该配置文件 > 内置兜底值。
    BACKTEST_DEFAULTS_CONFIG: str = os.getenv(
        "BACKTEST_DEFAULTS_CONFIG", (BASE_DIR / "config" / "backtest.json").as_posix()
    )

    # ---------------- 券商接入点 ----------------
    # 接入点配置：命名接入点（网关类型 + 资金账号 + 连接参数 + 凭据环境变量名）。
    # 有了它，"接哪家券商、用哪个资金账号"就是配置问题，不是改代码。
    BROKER_ENDPOINTS_CONFIG: str = os.getenv(
        "BROKER_ENDPOINTS_CONFIG", (BASE_DIR / "config" / "brokers.json").as_posix()
    )
    # 默认使用哪个接入点（留空则用 brokers.json 里 default_endpoint 指定的那个）
    BROKER_ENDPOINT: str = os.getenv("BROKER_ENDPOINT", "")

    # ---------------- 数据源编排（Provider / Capability / Router）----------------
    # 谁启用、优先级多少、每种能力按什么顺序尝试、重试与熔断参数 —— 全在这个文件里。
    # 运行时实测健康分占 70%、这里配置的优先级占 30%，顺序会自动向"当前真能用的源"收敛。
    # 删掉文件或写错格式都只退化成内置默认值，不会让取数链起不来。
    PROVIDERS_CONFIG: str = os.getenv(
        "PROVIDERS_CONFIG", (BASE_DIR / "config" / "providers.json").as_posix()
    )

    @classmethod
    def collector_symbol_list(cls) -> list[str]:
        return [s.strip() for s in cls.COLLECTOR_SYMBOLS.split(",") if s.strip()]

    @classmethod
    def watchlist_category_list(cls) -> list[str]:
        """``WATCHLIST_CATEGORIES`` → 类别列表。留空返回 ``[]``（=跟随服务默认）。"""
        return [s.strip() for s in cls.WATCHLIST_CATEGORIES.split(",") if s.strip()]

    @classmethod
    def priority_list(cls) -> list[str]:
        return [s.strip() for s in cls.DATA_SOURCE_PRIORITY.split(",") if s.strip()]

    @classmethod
    def data_source_weights(cls) -> dict[str, float]:
        """数据源初始权重覆盖（``DATA_SOURCE_WEIGHTS``）。

        解析失败返回空 dict —— 配置写错不该让整个取数链起不来，
        退化成"全部权重 1"即可。
        """
        raw = (cls.DATA_SOURCE_WEIGHTS or "").strip()
        if not raw:
            return {}
        try:
            import json

            parsed = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in parsed.items():
            try:
                out[str(key).strip()] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        return out

    @classmethod
    def data_guard_check_list(cls) -> list[str]:
        return [s.strip() for s in cls.DATA_GUARD_CHECKS.split(",") if s.strip()]

    @classmethod
    def symbol_list(cls) -> list[str]:
        return [s.strip() for s in cls.MARKET_SYMBOLS.split(",") if s.strip()]

    @classmethod
    def news_source_list(cls) -> list[str]:
        return [s.strip() for s in cls.NEWS_SOURCES.split(",") if s.strip()]

    @classmethod
    def user_strategy_dirs(cls) -> list[Path]:
        """用户策略目录（可多个，按顺序发现；同名后者覆盖前者）。"""
        raw = os.getenv("USER_STRATEGY_DIRS", "")
        extra = [Path(p.strip()) for p in raw.split(",") if p.strip()]
        return [Path(cls.USER_STRATEGY_DIR), *extra]

    @classmethod
    def ensure_dirs(cls) -> None:
        """创建所有运行时目录。幂等，可重复调用。"""
        for d in (
            cls.DATA_DIR,
            cls.LOG_DIR,
            cls.OUTPUT_DIR,
            cls.CSV_DIR,
            cls.CONFIG_DIR,
            cls.SNAPSHOT_DIR,
            cls.RUNTIME_DIR,
            cls.SERVICES_DIR,
            cls.BUS_DIR,
        ):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
