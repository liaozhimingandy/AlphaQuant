#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : spec.py
# @Description : 行情采集服务的配置模型：频率解析、交易时段判断、任务定义
#               这一层不依赖 Twisted —— "采什么、多久采一次、什么时候能采"
#               是纯策略问题，值得单独可测
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import settings
from app.data.datasource import normalize_symbol
from app.utils.logger import logger

# 频率单位：统一折算成秒
_FREQ_UNITS: Dict[str, float] = {
    "": 1.0, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
}

_FREQ_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")

#: 采集编排配置文件里的键名
DAILY_PERIOD = "1d"


def parse_frequency(value: Any, default: float = 300.0) -> float:
    """把用户写的频率统一成秒。

    支持 ``60``（裸数字=秒）、``"30s"``、``"5m"``、``"15min"``、``"1h"``、``"1d"``。
    写错就回退到默认值并告警，而不是让服务起不来 —— 采集失败了顶多数据旧，
    服务起不来是真的没有数据。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return float(default)
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else float(default)
    if isinstance(value, str):
        m = _FREQ_RE.match(value)
        if not m:
            logger.warning(f"无法解析采集频率 {value!r}，回退到 {default}s")
            return float(default)
        amount = float(m.group(1))
        unit = m.group(2).lower()
        if unit not in _FREQ_UNITS:
            logger.warning(f"未知频率单位 {unit!r}（来自 {value!r}），回退到 {default}s")
            return float(default)
        return amount * _FREQ_UNITS[unit]
    raise ValueError(f"频率必须是数字或字符串，收到 {type(value).__name__}")


def humanize_frequency(seconds: float) -> str:
    """秒 → 人话。用于面板展示与日志。"""
    s = float(seconds)
    if s < 60:
        return f"{s:g}s"
    if s < 3600:
        return f"{s / 60:g}m"
    if s < 86400:
        return f"{s / 3600:g}h"
    return f"{s / 86400:g}d"


def normalize_period(value: Any) -> str:
    """归一化数据粒度：``1d``=日线，其余为分钟周期（返回分钟数的字符串）。"""
    raw = str(value or DAILY_PERIOD).strip().lower()
    if raw in ("1d", "d", "day", "daily", "日线"):
        return DAILY_PERIOD
    raw = raw.rstrip("min")
    if raw not in ("1", "5", "15", "30", "60"):
        raise ValueError(
            f"不支持的采集粒度: {value!r} | 可用: 1d, 1, 5, 15, 30, 60"
        )
    return raw


def is_intraday(period: str) -> bool:
    return normalize_period(period) != DAILY_PERIOD


# ===========================================================================
# 交易时段
# ===========================================================================
# A股连续竞价时段。缓冲区是为了拿到"收盘后落定的那一根"：
# 15:00 之后数据源还需要一点时间结算，晚了就漏当天，早了数据没更新。
_SESSION_BUFFER_MINUTES = 30
A_SHARE_SESSIONS = (
    (dtime(9, 30), dtime(11, 30)),
    (dtime(13, 0), dtime(15, 0)),
)


def is_trading_day(dt: Optional[datetime] = None,
                   holidays: Optional[Sequence[str]] = None) -> bool:
    """周一~周五且不在自定义节假日里。

    法定节假日无法离线穷举（日历要联网维护），所以这里只认工作日 + 用户自己
    在配置里补的 ``holidays``；法定节日那天采集会拿到"和上一交易日一样"的数据，
    入库是幂等的，不会有副作用。
    """
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return False
    iso = dt.date().isoformat()
    return iso not in set(holidays or ())


def in_trading_session(dt: Optional[datetime] = None,
                       buffer_minutes: int = _SESSION_BUFFER_MINUTES) -> bool:
    """是否处在（含缓冲的）交易时段内。"""
    dt = dt or datetime.now()
    now = dt.time()
    buf = timedelta(minutes=max(0, int(buffer_minutes)))
    d = dt.date()
    for opening, closing in A_SHARE_SESSIONS:
        start = datetime.combine(d, opening) - buf
        end = datetime.combine(d, closing) + buf
        if start <= dt <= end:
            return True
    # 午休也属于交易日的活跃区间（数据源此刻仍在更新当日数据）
    return False


# ===========================================================================
# 采集任务
# ===========================================================================
@dataclass
class CollectJob:
    """一条采集任务：一个标的 × 一种数据粒度 × 一个频率。"""

    symbol: str
    period: str = DAILY_PERIOD          # 数据粒度：1d / 1 / 5 / 15 / 30 / 60
    interval: float = 300.0             # 采集频率（秒）
    adjust: str = "qfq"
    source: str = ""                    # 指定数据源，空=按全局优先级降级
    lookback_days: int = 30             # 日线增量窗口
    keep_days: int = 0                  # 分钟线保留天数，0=永久
    enabled: bool = True

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.period = normalize_period(self.period)
        self.interval = parse_frequency(self.interval, 300.0)

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.period}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "period": self.period,
            "interval": self.interval,
            "interval_human": humanize_frequency(self.interval),
            "adjust": self.adjust,
            "source": self.source,
            "lookback_days": int(self.lookback_days),
            "keep_days": int(self.keep_days),
            "enabled": bool(self.enabled),
        }


@dataclass
class CollectorSpec:
    """采集服务的完整配置。"""

    enabled: bool = True
    jobs: List[CollectJob] = field(default_factory=list)
    adjust: str = "qfq"
    source: str = ""
    #: True=只在交易时段采集 / False=全天候 / None=按粒度自动决定
    trading_hours_only: Optional[bool] = None
    #: 频率下限，防止用户写 1s 把数据源打挂（也防止自己被封 IP）
    min_interval: float = 15.0
    backfill_on_start: bool = True
    lookback_days: int = 30
    keep_days: int = 0
    holidays: List[str] = field(default_factory=list)
    config_path: str = ""

    def should_run(self, job: CollectJob, now: Optional[datetime] = None) -> bool:
        """这个任务此刻该不该跑。"""
        if not (self.enabled and job.enabled):
            return False
        enforce = self.trading_hours_only
        if enforce is None:
            # 自动：日内数据盘后才没意义；日线一天只会变一次，不限时段也能补到数据
            enforce = is_intraday(job.period)
        if not enforce:
            return True
        now = now or datetime.now()
        return is_trading_day(now, self.holidays) and in_trading_session(now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "adjust": self.adjust,
            "source": self.source,
            "trading_hours_only": self.trading_hours_only,
            "min_interval": self.min_interval,
            "backfill_on_start": self.backfill_on_start,
            "lookback_days": self.lookback_days,
            "keep_days": self.keep_days,
            "holidays": list(self.holidays),
            "jobs": [j.to_dict() for j in self.jobs],
        }


# ===========================================================================
# 配置加载
# ===========================================================================
_JOB_FIELDS = ("period", "interval", "adjust", "source", "lookback_days",
               "keep_days", "enabled")


def build_job(entry: Any, defaults: Optional[Dict[str, Any]] = None) -> Optional[CollectJob]:
    """从一个配置项构造任务。接受裸字符串（代码）或 dict。"""
    d = dict(defaults or {})
    if isinstance(entry, str):
        d["symbol"] = entry
    elif isinstance(entry, dict):
        sym = entry.get("symbol") or entry.get("code")
        if not sym:
            return None
        d["symbol"] = sym
        for k in _JOB_FIELDS:
            if k in entry:
                d[k] = entry[k]
    else:
        return None
    try:
        return CollectJob(**{k: v for k, v in d.items()
                             if k in CollectJob.__dataclass_fields__})
    except Exception as exc:
        logger.error(f"采集任务配置非法 {entry!r}: {exc}")
        return None


def load_collector_spec(
    path: Optional[str] = None,
    symbols: Optional[Sequence[str]] = None,
    interval: Any = None,
    period: Any = None,
    adjust: str = "",
    source: str = "",
    enabled: Optional[bool] = None,
    min_interval: Optional[float] = None,
    trading_hours_only: Optional[bool] = None,
    **_: Any,
) -> CollectorSpec:
    """加载采集配置：配置文件打底，命令行/API 参数覆盖。

    配置文件结构（``config/collector.json``）::

        {
          "enabled": true,
          "adjust": "qfq",
          "interval": "5m",            // 全局默认频率
          "period": "1d",              // 全局默认粒度
          "trading_hours_only": true,  // 只在交易时段采
          "min_interval": 15,          // 频率下限（秒）
          "backfill_on_start": true,   // 启动时先补一次
          "lookback_days": 30,         // 日线每次回看的更新窗口
          "holidays": ["2026-01-01"],
          "symbols": [
            "000001",                                   // 用全局默认
            {"symbol": "600000", "interval": "1m", "period": "1"}  // 单独高频
          ]
        }
    """
    file = Path(path or settings.COLLECTOR_CONFIG)
    raw: Dict[str, Any] = {}
    if file.exists():
        try:
            raw = json.loads(file.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.error(f"采集配置解析失败 {file}: {exc}（忽略，按默认值启动）")
            raw = {}
    elif file.parent.exists():
        logger.debug(f"无采集配置文件 {file}，按默认值启动")

    base: Dict[str, Any] = {
        "period": period or raw.get("period") or DAILY_PERIOD,
        "interval": parse_frequency(
            interval if interval is not None else raw.get("interval"), 300.0
        ),
        "adjust": adjust or raw.get("adjust") or "qfq",
        "source": source or raw.get("source") or "",
        "lookback_days": int(raw.get("lookback_days", 30)),
        "keep_days": int(raw.get("keep_days", 0)),
    }

    # 这里的合并语义必须讲清楚，否则会出现"明明在命令行指定了标的却没被采集"：
    #   1. 配置文件里的 symbols 是长期策略（含每个标的自己的频率/粒度），优先采用；
    #   2. 显式传入的 symbols 是"这批也要采"的补充意图，只补 diff，
    #      已经在同一标的同一粒度上配过的条目不被覆盖。
    # 两边合并而不是二选一，可以避免任何一种写法被静默丢弃。
    jobs: List[CollectJob] = []
    seen: set = set()
    for e in raw.get("symbols") or []:
        job = build_job(e, base)
        if job is not None and job.key not in seen:
            jobs.append(job)
            seen.add(job.key)
    for e in symbols or []:
        job = build_job(e, base)
        if job is not None and job.key not in seen:
            jobs.append(job)
            seen.add(job.key)

    spec = CollectorSpec(
        enabled=bool(raw.get("enabled", True)) if enabled is None else bool(enabled),
        jobs=jobs,
        adjust=base["adjust"],
        source=base["source"],
        trading_hours_only=(
            raw.get("trading_hours_only") if trading_hours_only is None
            else trading_hours_only
        ),
        min_interval=float(
            min_interval if min_interval is not None
            else raw.get("min_interval", 15.0)
        ),
        backfill_on_start=bool(raw.get("backfill_on_start", True)),
        lookback_days=base["lookback_days"],
        keep_days=base["keep_days"],
        holidays=[str(h) for h in (raw.get("holidays") or [])],
        config_path=str(file),
    )

    # 频率下限：这里做一次硬性夹紧，比在数据源层面被限流封 IP 划算
    for job in spec.jobs:
        if job.interval < spec.min_interval:
            logger.warning(
                f"{job.key} 采集频率 {humanize_frequency(job.interval)} 低于下限 "
                f"{humanize_frequency(spec.min_interval)}，已自动夹紧"
            )
            job.interval = spec.min_interval
    return spec


__all__ = [
    "CollectJob",
    "CollectorSpec",
    "DAILY_PERIOD",
    "A_SHARE_SESSIONS",
    "build_job",
    "humanize_frequency",
    "in_trading_session",
    "is_intraday",
    "is_trading_day",
    "load_collector_spec",
    "normalize_period",
    "parse_frequency",
]
