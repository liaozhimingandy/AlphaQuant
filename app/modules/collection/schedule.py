#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : schedule.py
# @Description : 采集时间窗口：什么时候该去拉数据
#
#                为什么默认定成"开盘前 30 分钟 + 闭市 30 分钟后"：
#
#                  **盘前半小时（9:00）**：数据源此刻已经把上一交易日的数据
#                    整理完毕，而且还没进入竞价 —— 网络不挤、接口不限流。
#                    这一段用来补齐昨日漏采、预热当日标的。
#
#                  **盘中**：只采**分钟级**数据（日内会变）。
#                    日频截面数据（资金流/筹码/基本面/题材）盘中反复拉没有意义，
#                    一天一条的东西拉十次是纯浪费 —— 还会把自己送进限流名单。
#
#                  **闭市 30 分钟后（15:30）**：交易所与数据源需要时间结算，
#                    15:00 整去拉拿到的是**未定稿**的数据（当天的资金流、
#                    涨停池都还在变）。早了拿到脏数据，晚了白白等待 ——
#                    30 分钟是经验上比较稳的点，且可配置。
#
#                为什么必须可配置：
#                  不同数据源结算速度不同，用户的网络环境也不同。
#                  写死一个值，总有人拿到未定稿的数据而不知道为什么对不上账。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from app.common.config import settings
from app.common.logging import logger

#: 采集窗口配置放在采集配置文件里，不再单开一个文件 ——
#: 采集的"采什么、多久采、什么时候采"本来就是一件事，拆成两个文件
#: 只会让人改了一处忘了另一处。
SCHEDULE_KEY = "schedule"


@dataclass
class CollectionSchedule:
    """采集时间窗口。"""

    #: 盘前采集：开盘前多少分钟开始（默认 30 → 9:00）
    pre_open_minutes: int = 30
    #: 收盘后延迟多少分钟才去采"定稿数据"（默认 30 → 15:30）
    post_close_minutes: int = 30
    #: 盘前那一轮是否启用
    pre_open_enabled: bool = True
    #: 是否只在交易日采集
    trading_day_only: bool = True
    #: 盘中是否也采日频截面数据（默认**关**：一天一条的数据反复拉是浪费）
    intraday_enabled: bool = False
    #: 补充：允许在盘后窗口内重试的宽限时长（分钟）
    grace_minutes: int = 120

    def __post_init__(self) -> None:
        self.pre_open_minutes = max(0, min(120, int(self.pre_open_minutes)))
        self.post_close_minutes = max(0, min(180, int(self.post_close_minutes)))
        self.grace_minutes = max(0, min(480, int(self.grace_minutes)))

    # ------------------------------------------------------------------
    @property
    def pre_open_time(self) -> dtime:
        """盘前采集的开始时刻（如 9:00）。"""
        return (datetime.combine(datetime.today(), dtime(9, 30))
                - timedelta(minutes=self.pre_open_minutes)).time()

    @property
    def post_close_time(self) -> dtime:
        """盘后采集的开始时刻（如 15:30）。"""
        return (datetime.combine(datetime.today(), dtime(15, 0))
                + timedelta(minutes=self.post_close_minutes)).time()

    def slot_of(self, dt: Optional[datetime] = None, sessions=None) -> Optional[str]:
        """判断此刻属于哪个采集窗口。

        :return: ``"pre"`` 盘前 / ``"intraday"`` 盘中 / ``"post"`` 盘后定稿 /
                 ``None`` 此刻不该采集
        """
        from app.domain.calendar import MARKET_SESSIONS, get_calendar

        dt = dt or datetime.now()
        cal = get_calendar()
        if self.trading_day_only and not cal.is_trading_day(dt):
            return None
        sessions = sessions or MARKET_SESSIONS
        now = dt.time()
        open_at = sessions[0][0]
        close_at = sessions[-1][1]
        pre_from = (datetime.combine(dt.date(), open_at)
                    - timedelta(minutes=self.pre_open_minutes)).time()
        post_from = (datetime.combine(dt.date(), close_at)
                     + timedelta(minutes=self.post_close_minutes)).time()
        post_to = (datetime.combine(dt.date(), post_from)
                   + timedelta(minutes=self.grace_minutes)).time()

        if pre_from <= now < open_at:
            return "pre" if self.pre_open_enabled else None
        if now >= post_from:
            # 盘后窗口不是无限长：超过宽限时间就认为"今天已经错过了"，
            # 免得凌晨 3 点还在以为自己在采数据
            if self.grace_minutes and now > post_to:
                return None
            return "post"
        if open_at <= now < close_at:
            return "intraday" if self.intraday_enabled else None
        return None

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["pre_open_time"] = self.pre_open_time.strftime("%H:%M")
        d["post_close_time"] = self.post_close_time.strftime("%H:%M")
        return d

    @classmethod
    def from_dict(cls, raw: Any) -> "CollectionSchedule":
        if not isinstance(raw, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


def load_schedule(path: Optional[str] = None) -> CollectionSchedule:
    """从采集配置文件读 schedule 段。读不到就用默认值（不报错）。"""
    file = Path(path or settings.COLLECTOR_CONFIG)
    if not file.exists():
        return CollectionSchedule()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"采集配置解析失败（用默认时间窗口）: {exc}")
        return CollectionSchedule()
    if isinstance(raw, dict):
        return CollectionSchedule.from_dict(raw.get(SCHEDULE_KEY))
    return CollectionSchedule()


def save_schedule(sch: CollectionSchedule, path: Optional[str] = None) -> str:
    """把 schedule 段写回采集配置文件，**保留其它字段**。"""
    file = Path(path or settings.COLLECTOR_CONFIG)
    raw: Dict[str, Any] = {}
    if file.exists():
        try:
            got = json.loads(file.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                raw = got
        except Exception as exc:
            logger.error(f"采集配置解析失败，将重写: {exc}")
    payload = {k: v for k, v in asdict(sch).items()}
    payload["_comment"] = [
        "采集时间窗口（可在监控看板「设置」里改）。",
        "pre_open_minutes   开盘前多少分钟开始采（默认 30 → 9:00）",
        "post_close_minutes 收盘后延迟多少分钟采定稿数据（默认 30 → 15:30）",
        "                   早了拿到的是未定稿数据，晚了白白等待。",
        "intraday_enabled   盘中是否也采日频截面数据（默认关：一天一条的东西",
        "                   盘中反复拉是浪费，还会被限流）",
    ]
    raw[SCHEDULE_KEY] = payload
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(file)


_SCHEDULE: Optional[CollectionSchedule] = None


def get_schedule(reload: bool = False) -> CollectionSchedule:
    global _SCHEDULE
    if _SCHEDULE is None or reload:
        _SCHEDULE = load_schedule()
    return _SCHEDULE


def set_schedule(sch: CollectionSchedule, persist: bool = True) -> CollectionSchedule:
    global _SCHEDULE
    if persist:
        save_schedule(sch)
    _SCHEDULE = sch
    return sch


__all__ = [
    "SCHEDULE_KEY",
    "CollectionSchedule",
    "get_schedule",
    "load_schedule",
    "save_schedule",
    "set_schedule",
]
