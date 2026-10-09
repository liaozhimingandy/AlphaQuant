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
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_RETENTION: str = os.getenv("LOG_RETENTION", "30 days")

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
    # 快照落盘间隔（秒）
    MONITOR_SNAPSHOT_INTERVAL: float = _env_float("MONITOR_SNAPSHOT_INTERVAL", 5.0)
    # 历史快照最多保留多少个（滚动淘汰）
    MONITOR_SNAPSHOT_KEEP: int = _env_int("MONITOR_SNAPSHOT_KEEP", 300)

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
