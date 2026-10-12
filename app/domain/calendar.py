#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : calendar.py
# @Description : A 股交易日历：交易日判断、时段判断、下次开盘时间
#
#                为什么需要一个专门的日历模块（而不是"看是不是周末"）：
#                  闭市时还在按定时器请求数据，是最典型的一类浪费 ——
#                  请求发出去了、被限流风险占了、日志刷了一屏，
#                  拿回来的还是和昨天一模一样的数据。
#                  更糟的是这会掩盖真问题：日志里"一切正常"，
#                  但实际一天里 90% 的请求都是无意义的重复。
#
#                所以"什么时候能采"必须是**一等公民**，和"采什么、多久采一次"
#                平级，而不是散落在各个组件里的 if 判断。
#
#                节假日无法离线穷举（每年官方公告一次），所以分三层：
#                  1. 内置表：写死已公布的年份（准确、零依赖、离线可用）
#                  2. 配置文件：config/trade_calendar.json（联网刷新后写这里）
#                  3. 联网刷新：akshare 交易日历，失败就退回前两层
#                拿不到日历时**退化成"工作日即交易日"**，绝不因为日历缺失就不采数据。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.common.config import settings
from app.common.logging import logger

# ===========================================================================
# 交易时段
# ===========================================================================
#: A 股连续竞价时段
MARKET_SESSIONS: Tuple[Tuple[dtime, dtime], ...] = (
    (dtime(9, 30), dtime(11, 30)),
    (dtime(13, 0), dtime(15, 0)),
)

#: 开盘前多久算"盘前"（用于预热，不做数据采集）
PRE_OPEN_MINUTES = 30
#: 收盘后的缓冲区（数据源结算时间）：这段时间仍允许采，为了拿到收盘定稿数据
POST_CLOSE_MINUTES = 45

#: 状态枚举
STATE_PRE_OPEN = "pre_open"        # 盘前
STATE_OPEN = "open"                # 连续竞价中
STATE_LUNCH = "lunch"              # 午休
STATE_POST_CLOSE = "post_close"    # 收盘后缓冲（数据定稿中）
STATE_CLOSED = "closed"            # 当日未开市 / 已收市（超出缓冲）
STATE_HOLIDAY = "holiday"          # 非交易日（周末/法定节假日）

STATE_NAMES = {
    STATE_PRE_OPEN: "盘前",
    STATE_OPEN: "交易中",
    STATE_LUNCH: "午休",
    STATE_POST_CLOSE: "收盘结算中",
    # 早上 8 点和晚上 8 点都落在这里（一个是还没开、一个是已经收），
    # 所以叫"休市中"而不是"已收市"。
    STATE_CLOSED: "休市中",
    STATE_HOLIDAY: "非交易日",
}


# ===========================================================================
# 内置节假日（来自沪深北交易所公告的休市安排）
# ===========================================================================
# 只列**工作日里的休市日**（周末本来就休市，不必重复列）。
# 每年官方公告一次，拿到新一年的安排后补进这里即可。
#
# 2026 年（上交所/深交所/北交所 2025-12-22 通知）：
#   元旦 1/1(四)-1/3(六)，春节 2/15(日)-2/23(一)，清明 4/4(六)-4/6(一)，
#   劳动节 5/1(五)-5/5(二)，端午 6/19(五)-6/21(日)，
#   中秋 9/25(五)-9/27(日)，国庆 10/1(四)-10/7(三)
BUILTIN_HOLIDAYS: Dict[int, Tuple[str, ...]] = {
    2025: (
        "2025-01-01",                                           # 元旦
        "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春节
        "2025-02-03", "2025-02-04",
        "2025-04-04",                                           # 清明
        "2025-05-01", "2025-05-02", "2025-05-05",               # 劳动节
        "2025-06-02",                                           # 端午
        "2025-10-01", "2025-10-02", "2025-10-03",               # 国庆+中秋
        "2025-10-06", "2025-10-07", "2025-10-08",
    ),
    2026: (
        "2026-01-01", "2026-01-02",                             # 元旦
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",  # 春节
        "2026-02-20", "2026-02-23",
        "2026-04-06",                                           # 清明
        "2026-05-01", "2026-05-04", "2026-05-05",               # 劳动节
        "2026-06-19",                                           # 端午
        "2026-09-25",                                           # 中秋
        "2026-10-01", "2026-10-02", "2026-10-05",               # 国庆
        "2026-10-06", "2026-10-07",
    ),
}

#: 日历缓存文件（联网刷新后写这里，之后离线也能用）
CALENDAR_FILE = "config/trade_calendar.json"


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class TradeCalendar:
    """交易日历。

    :param holidays: 额外休市日（工作日里的法定节假日）
    :param extra_sessions: 额外开市日（调休导致周末开盘时用，极少见但确实有）
    :param sessions: 连续竞价时段
    """

    def __init__(
        self,
        holidays: Optional[Iterable[str]] = None,
        extra_sessions: Optional[Iterable[str]] = None,
        sessions: Sequence[Tuple[dtime, dtime]] = MARKET_SESSIONS,
        pre_open_minutes: int = PRE_OPEN_MINUTES,
        post_close_minutes: int = POST_CLOSE_MINUTES,
        source: str = "builtin",
    ) -> None:
        self.sessions: Tuple[Tuple[dtime, dtime], ...] = tuple(
            (s, e) for s, e in (sessions or MARKET_SESSIONS)
        )
        self.pre_open_minutes = int(pre_open_minutes)
        self.post_close_minutes = int(post_close_minutes)
        self.source = str(source or "builtin")

        h: Set[date] = set()
        for year, days in BUILTIN_HOLIDAYS.items():
            for d in days:
                got = _as_date(d)
                if got:
                    h.add(got)
        for d in holidays or ():
            got = _as_date(d)
            if got:
                h.add(got)
        self.holidays: Set[date] = h

        e: Set[date] = set()
        for d in extra_sessions or ():
            got = _as_date(d)
            if got:
                e.add(got)
        self.extra_sessions: Set[date] = e

    # ===================================================================
    # 交易日
    # ===================================================================
    def is_trading_day(self, value: Any = None) -> bool:
        d = _as_date(value if value is not None else date.today())
        if d is None:
            return True          # 拿不到日期就别拦着采集
        if d in self.extra_sessions:
            return True
        if d in self.holidays:
            return False
        return d.weekday() < 5

    def last_trading_day(self, value: Any = None) -> date:
        """最近一个交易日（含当天）。"""
        d = _as_date(value if value is not None else date.today()) or date.today()
        for _ in range(15):      # 最长假期 10 天左右，15 足够
            if self.is_trading_day(d):
                return d
            d -= timedelta(days=1)
        return d

    def recent_trading_days(self, n: int = 20, until: Any = None) -> List[date]:
        """截至 ``until`` 的最近 n 个交易日（升序，最后一个是最近交易日）。"""
        out: List[date] = []
        d = self.last_trading_day(until)
        while len(out) < max(0, int(n)):
            out.append(d)
            d = self._prev_day(d)
        return sorted(out)

    def _prev_day(self, d: date) -> date:
        for _ in range(15):
            d -= timedelta(days=1)
            if self.is_trading_day(d):
                return d
        return d

    def _next_day(self, d: date) -> date:
        for _ in range(15):
            d += timedelta(days=1)
            if self.is_trading_day(d):
                return d
        return d

    # ===================================================================
    # 时段与状态
    # ===================================================================
    def state(self, dt: Optional[datetime] = None) -> str:
        """当前市场状态。见 :data:`STATE_NAMES`。"""
        dt = dt or datetime.now()
        if not self.is_trading_day(dt):
            return STATE_HOLIDAY
        now = dt.time()
        first_open, last_close = self.sessions[0][0], self.sessions[-1][1]

        if now < first_open:
            # 盘前窗口之外（比如早上 7 点）**不是**非交易日：今天照样开盘，
            # 只是还没到点。以前这里返回 HOLIDAY，于是每个交易日清晨
            # 日志都写"当前 非交易日"，看着像今天不开盘，是纯粹的误导。
            pre = (datetime.combine(dt.date(), first_open)
                   - timedelta(minutes=self.pre_open_minutes)).time()
            return STATE_PRE_OPEN if now >= pre else STATE_CLOSED
        if now >= last_close:
            post_end = (datetime.combine(dt.date(), last_close)
                        + timedelta(minutes=self.post_close_minutes))
            return STATE_POST_CLOSE if dt <= post_end else STATE_CLOSED

        for opening, closing in self.sessions:
            if opening <= now < closing:
                return STATE_OPEN
        return STATE_LUNCH        # 在两个时段之间 = 午休

    def is_open(self, dt: Optional[datetime] = None) -> bool:
        """此刻是否处在"值得采数据"的窗口（交易时段 + 收盘结算缓冲）。"""
        return self.state(dt) in (STATE_OPEN, STATE_LUNCH, STATE_POST_CLOSE)

    def in_session(self, dt: Optional[datetime] = None) -> bool:
        """是否在连续竞价时段内（不含午休与收盘后）。"""
        return self.state(dt) == STATE_OPEN

    # ===================================================================
    # 时间点推算
    # ===================================================================
    def next_open(self, dt: Optional[datetime] = None) -> Optional[datetime]:
        """下一次开盘时刻。当天还在盘前就返回今天 9:30。"""
        dt = dt or datetime.now()
        if self.is_trading_day(dt):
            first = datetime.combine(dt.date(), self.sessions[0][0])
            if dt < first:
                return first
        d = self._next_day(dt.date())
        return datetime.combine(d, self.sessions[0][0])

    def last_close(self, dt: Optional[datetime] = None) -> Optional[datetime]:
        """最近一次收盘时刻。"""
        dt = dt or datetime.now()
        d = self.last_trading_day(dt)
        close = datetime.combine(d, self.sessions[-1][1])
        return close if d < dt.date() else (close if dt >= close else None)

    def next_close(self, dt: Optional[datetime] = None) -> Optional[datetime]:
        dt = dt or datetime.now()
        if self.is_trading_day(dt):
            close = datetime.combine(dt.date(), self.sessions[-1][1])
            if dt < close:
                return close
        return datetime.combine(self._next_day(dt.date()), self.sessions[-1][1])

    # ===================================================================
    # 持久化 / 展示
    # ===================================================================
    def to_dict(self) -> Dict[str, Any]:
        dt = datetime.now()
        nxt = self.next_open(dt)
        return {
            "source": self.source,
            "state": self.state(dt),
            "state_name": STATE_NAMES.get(self.state(dt), ""),
            "is_trading_day": self.is_trading_day(dt),
            "is_open": self.is_open(dt),
            "last_trading_day": self.last_trading_day(dt).isoformat(),
            "next_open": nxt.isoformat(timespec="minutes") if nxt else None,
            "sessions": [[s.strftime("%H:%M"), e.strftime("%H:%M")]
                         for s, e in self.sessions],
            "holiday_count": len(self.holidays),
            "extra_session_count": len(self.extra_sessions),
        }

    def save(self, path: Optional[str] = None) -> str:
        file = Path(path or settings.BASE_DIR / CALENDAR_FILE)
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": "交易日历缓存。由 main.py calendar refresh 联网生成；"
                        "手工改也可以，格式是 ISO 日期列表（只需列工作日里的休市日）。",
            "source": self.source,
            "holidays": sorted(d.isoformat() for d in self.holidays),
            "extra_sessions": sorted(d.isoformat() for d in self.extra_sessions),
        }
        file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(file)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "TradeCalendar":
        """读缓存文件；没有就用内置表。

        缓存文件优先于内置表 —— 它可能是联网校准过的，更贴近当年实际安排。
        """
        file = Path(path or settings.BASE_DIR / CALENDAR_FILE)
        if file.exists():
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return cls(
                        holidays=raw.get("holidays") or (),
                        extra_sessions=raw.get("extra_sessions") or (),
                        source=str(raw.get("source") or "cache"),
                    )
            except Exception as exc:
                logger.warning(f"交易日历缓存解析失败（用内置表）: {exc}")
        return cls(source="builtin")


# ===========================================================================
# 单例与刷新
# ===========================================================================
_CALENDAR: Optional[TradeCalendar] = None


def get_calendar(reload: bool = False) -> TradeCalendar:
    """全局日历（懒加载）。"""
    global _CALENDAR
    if _CALENDAR is None or reload:
        _CALENDAR = TradeCalendar.load()
    return _CALENDAR


def set_calendar(cal: TradeCalendar) -> None:
    global _CALENDAR
    _CALENDAR = cal


def refresh_from_network(save: bool = True) -> Dict[str, Any]:
    """联网拉交易日历并写缓存。

    失败时**不动现有日历** —— 日历缺失会导致该采的时候不采，
    那比"多采了几次"严重得多。所以宁可用旧的/内置的，也不要清空。
    """
    try:
        from app.datasource.service import facade as MX
    except Exception as exc:
        return {"ok": False, "error": f"数据模块不可用: {exc}"}
    try:
        days = MX.fetch_trade_dates()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    if not days:
        return {"ok": False, "error": "数据源未返回交易日历"}

    trading = {_as_date(d) for d in days}
    trading.discard(None)
    if not trading:
        return {"ok": False, "error": "交易日历为空"}

    # 从"交易日全集"反推休市日：范围内的工作日里不在交易日集合中的，就是休市日
    lo, hi = min(trading), max(trading)
    holidays: List[str] = []
    d = lo
    while d <= hi:
        if d.weekday() < 5 and d not in trading:
            holidays.append(d.isoformat())
        d += timedelta(days=1)
    extra = sorted(x.isoformat() for x in trading if x.weekday() >= 5)

    cal = TradeCalendar(holidays=holidays, extra_sessions=extra,
                        source="network")
    path = cal.save() if save else ""
    set_calendar(cal)
    return {
        "ok": True, "path": path, "range": [lo.isoformat(), hi.isoformat()],
        "trading_days": len(trading), "holidays": len(holidays),
        "extra_sessions": len(extra),
    }


__all__ = [
    "CALENDAR_FILE",
    "MARKET_SESSIONS",
    "STATE_CLOSED",
    "STATE_HOLIDAY",
    "STATE_LUNCH",
    "STATE_NAMES",
    "STATE_OPEN",
    "STATE_POST_CLOSE",
    "STATE_PRE_OPEN",
    "BUILTIN_HOLIDAYS",
    "TradeCalendar",
    "get_calendar",
    "refresh_from_network",
    "set_calendar",
]
