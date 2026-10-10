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

    # ---------------- 回测默认值 ----------------
    DEFAULT_CASH: float = _env_float("DEFAULT_CASH", 100000.0)
    DEFAULT_COMMISSION: float = _env_float("DEFAULT_COMMISSION", 0.0003)
    DEFAULT_SLIPPAGE_PERC: float = _env_float("DEFAULT_SLIPPAGE_PERC", 0.001)
    DEFAULT_STAMP_DUTY: float = _env_float("DEFAULT_STAMP_DUTY", 0.0)

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

    @classmethod
    def collector_symbol_list(cls) -> list[str]:
        return [s.strip() for s in cls.COLLECTOR_SYMBOLS.split(",") if s.strip()]

    @classmethod
    def priority_list(cls) -> list[str]:
        return [s.strip() for s in cls.DATA_SOURCE_PRIORITY.split(",") if s.strip()]

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
