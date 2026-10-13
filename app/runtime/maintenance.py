#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : maintenance.py
# @Description : 维护计划：日志清理 / 快照归档 / 定期校验 的开关、周期与参数
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
维护任务计划器
==============

为什么需要单独一层
------------------

重构后 ``system.maintenance.due`` 有三条规则（清理 / 归档 / 校验），
但**没有任何东西发布这个事件** —— 三条规则是死的，日志清理从来没跑过。
根因是"周期性维护"既不是行情事件也不是任务事件，它需要一个**时间源**。

这里的时间源就是本模块：

    scheduler 定时器 ──> system.maintenance.tick（每 30s）
                              │
                    MaintenancePlanner.tick()
                              │  哪个 job 到点了？
                              └─> system.maintenance.due {job, params}
                                        │
                              规则表（按 job 名过滤）──> 建任务 ──> maintenance 池

**计划器不发任务，只发"该干活了"这个事实**。建任务仍然归规则表管，
所以维护任务同样享有队列、幂等、重试、死信那一整套，和采集/因子任务一致。

为什么拆成 tick 和 due 两个事件
--------------------------------

一个事件也能做（tick 里直接带"该跑哪些"），但那样事件流里只能看到
"到点了"，看不出"到的是哪个点"。拆开之后运维能一眼分辨：
节拍在跑但没任务 = 计划没到点 / 被禁用；有 due 没任务 = 规则被关了。

安全默认
--------

清理与归档都**默认只报告不删**（处理器里 ``apply=False``）。
"定时任务自动删文件"是运维事故高发区：配错一天就删掉一周日志。
所以面板上要真的删，必须显式勾选，并且默认不勾。

六种周期
--------

维护任务该多久跑一次，答案是"看它是什么活"，所以周期有六档：

============  ==========================================  ==============
schedule      含义                                        用到的字段
============  ==========================================  ==============
``startup``   引擎**每次启动**跑一次（不是周期）            无
``interval``  每 N **分钟**（``interval_minutes``）或        两者之一
              N 小时（``interval_hours``）
``daily``     每天 ``HH:MM``                               ``at``
``weekly``    每周某天 ``HH:MM``                           ``weekday`` + ``at``
``monthly``   每月某日 ``HH:MM``                           ``day_of_month`` + ``at``
``cron``      cron 表达式（分 时 日 月 周）                 ``cron``
``once``      在指定时刻跑一次，跑完自动停用                  ``once_at``
============  ==========================================  ==============

三点值得说明：

* **间隔的下限按任务类别区分**（见 :func:`min_interval_seconds_for`）。
  原本是全局最小 1 小时，理由是"维护任务是扫日志目录这种活，做成分钟级
  只会让引擎一直在做无用功" —— 这个理由对维护任务依然成立，所以
  ``maintenance.*`` 仍是 1 小时。但业务任务（``pool.refresh`` 这类）
  的时效性恰恰是它的全部价值，"候选池十分钟刷新一次"要求分钟级。
  一刀切成小时会把业务憋死，一刀切成分钟会把维护放疯，于是分开。
* **任务清单是可以配的**。内置任务在 :data:`DEFAULT_JOBS`，
  但 ``config/maintenance.json`` 里写的其它条目同样会被加载 ——
  想让某件事周期跑，写一条配置即可，不必回来改这个模块。
  自定义任务经 :mod:`app.runtime.scheduling.defaults` 里那条通用规则
  落成任务，``task_type`` 就是它的路由依据。
* **``startup`` 存在的理由**：像"运行校验"这种活，它的价值就是
  "这次启动的环境是好的"，每小时再校验一遍既不增加信息量，
  又会在日志里堆一堆"正常"。它该在启动时跑，且只在启动时跑。

cron 是自己实现的（见 :mod:`app.runtime.cron`），支持 ``*`` / ``*/n`` /
``a-b`` / ``a,b`` / ``a-b/n``。**「日」与「周」是 OR 语义** ——
``0 3 1 * 1`` 是"每月 1 号**或**每周一"，不是"1 号且是周一"。

配置持久化在 ``config/maintenance.json``，改完立刻生效，重启后仍在。

面板「运行时 → 维护计划」可以改周期、增删任务，写在这里与写在UI里等价。
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.common.logging import logger
from app.runtime.cron import CronError, CronExpr, validate_cron
from app.runtime.events.types import EventType, TaskType

#: 计划器建议的检查节拍（秒）。调度器用它作为定时器间隔。
#: 30s 的代价可以忽略，且保证"每天 03:10"的误差不超过半分钟 ——
#: 维护窗口本来就允许分钟级误差，没必要为秒级精度做 cron 解析。
TICK_SECONDS: float = 30.0

#: 日任务的"错过宽限"（秒）。到点后这么久之内仍会补跑一次；
#: 超过就算错过，顺延明天。取 5 分钟：远比检查节拍宽（不会漏），
#: 又比"引擎晚启动一会儿就补跑一轮"窄（补跑维护任务收益极低）。
MISS_GRACE_SECONDS: float = 300.0

# ---------------- 周期种类 ----------------
SCHEDULE_STARTUP = "startup"      # 引擎启动时跑一次
SCHEDULE_INTERVAL = "interval"    # 每 N 小时
SCHEDULE_DAILY = "daily"          # 每天 HH:MM
SCHEDULE_WEEKLY = "weekly"        # 每周某天 HH:MM
SCHEDULE_MONTHLY = "monthly"      # 每月某日 HH:MM
SCHEDULE_CRON = "cron"            # cron 表达式
SCHEDULE_ONCE = "once"            # 指定时刻跑一次

SCHEDULE_KINDS: tuple = (
    SCHEDULE_STARTUP, SCHEDULE_INTERVAL, SCHEDULE_DAILY,
    SCHEDULE_WEEKLY, SCHEDULE_MONTHLY, SCHEDULE_CRON, SCHEDULE_ONCE,
)

SCHEDULE_LABELS: Dict[str, str] = {
    SCHEDULE_STARTUP: "引擎启动时",
    SCHEDULE_INTERVAL: "每 N 小时",
    SCHEDULE_DAILY: "每天",
    SCHEDULE_WEEKLY: "每周",
    SCHEDULE_MONTHLY: "每月",
    SCHEDULE_CRON: "Cron 表达式",
    SCHEDULE_ONCE: "指定时刻一次",
}

#: 间隔周期的最小粒度（小时）。见模块 docstring「六种周期」。
MIN_INTERVAL_HOURS: int = 1

#: 业务类任务的最小粒度（分钟）。相比上面的小时级，这里放开到分钟，
#: 因为"候选股票池十分钟刷新一次"这类需求本身就要求分钟级——
#: 它刷的是盘中资金流，隔一小时等于没刷。
#: 不能改成秒级：检查节拍是 30 秒（:data:`TICK_SECONDS`），
#: 比节拍还密的间隔既做不到也更没有意义。
MIN_INTERVAL_MINUTES: int = 1

#: 受"小时级"保护的类别前缀。扫日志目录那种活做成十分钟一次，
#: 除了让引擎一直在做无用功、在限流名单里越陷越深，没有任何收益。
MAINTENANCE_TASK_PREFIX: str = "maintenance."

#: 维护类任务的下限（秒）。请用统一入口 :func:`min_interval_seconds_for`，
#: 那里的判断才是完整的（含"非维护类按分钟"这一分支）。
_MAINTENANCE_FLOOR_SECONDS: float = 3600.0

#: ``day_of_month`` 的取值上限。封顶 28：29/30/31 在小月不存在，
#: 允许填的话"每月 31 号"会变成"有大月的月份才跑"，行为难以预期。
MAX_DAY_OF_MONTH: int = 28

#: 星期几的中文名，下标 = ``weekday``（1=周一 … 7=周日）
WEEKDAY_NAMES: tuple = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: ``once_at`` 的书写格式
ONCE_AT_FORMAT = "%Y-%m-%d %H:%M"


def min_interval_seconds_for(task_type: Any) -> float:
    """按任务类别给出间隔周期的下限（秒）。

    分两档的理由是这两类活的失败模式不同：

    * **维护类**（``maintenance.*``）跑密了只会浪费 CPU 和日志IO，
      而它对时效性毫无要求 —— 凌晨清理和上午清理没有区别；
    * **业务类**（如 ``pool.refresh``）的时效性恰恰是它的全部价值，
      隔一小时再刷，抓到的就是过期资金流。

    一刀切成小时会把业务类憋死，一刀切成分钟会把维护类放疯。
    """
    text = str(task_type or "")
    if text.startswith(MAINTENANCE_TASK_PREFIX):
        return _MAINTENANCE_FLOOR_SECONDS
    return float(MIN_INTERVAL_MINUTES * 60)


def _in_trading_window(now: Optional[datetime] = None) -> bool:
    """此刻是否值得为一个「仅交易时段」的任务联网。

    用 :meth:`TradeCalendar.is_open` 的语义（交易中 / 午休 / 收盘结算），
    而不是严格的连续竞价时段 —— 收盘后那半小时正是数据定稿的时候，
    恰恰是该刷一次的时候。

    日历本身读不出来时返回 ``True``（照常执行）：一次判断失败就让任务
    永久停摆是最难排查的故障，宁可多跑一次也不要静默不动。
    """
    try:
        from app.domain.calendar import get_calendar

        return bool(get_calendar().is_open(now))
    except Exception as exc:
        logger.debug(f"判断交易时段失败，按“值得执行”处理: {exc}")
        return True


def _norm_schedule(value: Any, default: str = SCHEDULE_DAILY) -> str:
    """归一化周期种类；不认识的一律回落，不让脏配置把任务变成"永不执行"。"""
    text = str(value or "").strip().lower()
    return text if text in SCHEDULE_KINDS else default


def _parse_hhmm(value: Any, default: str = "03:10") -> "tuple":
    """解析 ``HH:MM``。写错回落默认值 —— 一个时刻填错不该让任务消失。"""
    raw = str(value or "").strip()
    for sep in (":", "："):
        if sep in raw:
            left, _, right = raw.partition(sep)
            try:
                hour, minute = int(left.strip()), int(right.strip())
            except ValueError:
                return _parse_hhmm(default, "03:10")
            return min(max(hour, 0), 23), min(max(minute, 0), 59)
    # 只有 "3" 或 "0300" 这种
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 4:
        return min(int(digits[:2]), 23), min(int(digits[2:]), 59)
    if digits:
        return min(int(digits), 23), 0
    return _parse_hhmm(default, "03:10")


def _next_monthly(base: datetime, dom: int, hour: int, minute: int) -> datetime:
    """下一个月的 ``dom`` 日 ``HH:MM``。

    ``datetime.replace`` 一次替换全部字段（year/month/day 同时生效），
    ``dom`` 封顶 28，所以不会构造出 2 月 30 日这种不存在的日期。
    """
    year, month = ((base.year + 1, 1) if base.month == 12
                   else (base.year, base.month + 1))
    return base.replace(year=year, month=month, day=dom,
                        hour=hour, minute=minute, second=0, microsecond=0)


@dataclass
class MaintenanceJob:
    """一个维护任务的定义 + 运行态。

    周期相关的字段**按种类生效**，其余留着不参与计算（切换周期时不用重填）。
    面板据此做联动显示：选"每 N 小时"就只显示小时框。

    :param interval_hours: ``interval`` 用的小时数，最小 :data:`MIN_INTERVAL_HOURS`
    :param interval_minutes: ``interval`` 用的分钟数。**优先于** ``interval_hours``。
                             业务任务（刷股票池这类）真正需要的是它，
                             下限见 :func:`min_interval_seconds_for`
    :param trading_hours_only: 只在交易时段到期。休市期间不触发也**不记失败** ——
                             它不是"这轮没做成"，是"这轮根本不该发生"
    :param at: ``daily`` / ``weekly`` 用的 ``HH:MM``（24 小时制）
    :param weekday: ``weekly`` 用的星期（1=周一 … 7=周日）
    :param cron: ``cron`` 用的 5 段表达式
    :param once_at: ``once`` 用的 ``YYYY-MM-DD HH:MM``
    :param interval_sec: **已废弃**，只为兼容早期配置，读入时换算成小时
    :param params: 直接透传给任务处理器（``retention_days`` / ``apply`` …）
    """

    name: str
    task_type: str
    label: str
    enabled: bool = False
    schedule: str = SCHEDULE_DAILY
    at: str = "03:10"
    weekday: int = 1
    day_of_month: int = 1
    interval_hours: int = 24
    cron: str = ""
    once_at: str = ""
    #: 0 = 未设置，回落到 :attr:`interval_hours`
    interval_minutes: int = 0
    trading_hours_only: bool = False
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # 早期配置用的是秒。留着它只为兼容读旧文件，新写出的配置不再有它。
    interval_sec: int = 0

    # ---- 运行态（不落盘）----
    last_run_at: float = 0.0        # epoch 秒
    last_result: str = ""
    runs: int = 0
    errors: int = 0

    # ---------------- 周期 ----------------
    @property
    def kind(self) -> str:
        """归一化后的周期种类（脏配置也不会得到陌生值）。"""
        return _norm_schedule(self.schedule)

    @property
    def is_startup(self) -> bool:
        return self.kind == SCHEDULE_STARTUP

    def cron_expr(self) -> Optional[CronExpr]:
        """解析好的 cron；不是 cron 种类或写错时返回 None。"""
        if self.kind != SCHEDULE_CRON:
            return None
        try:
            return CronExpr.parse(self.cron)
        except CronError:
            return None

    def cron_error(self) -> str:
        """cron 写错的原因；没错返回空串。面板直接显示。"""
        if self.kind != SCHEDULE_CRON:
            return ""
        return validate_cron(self.cron)

    def parse_once_at(self) -> Optional[datetime]:
        if self.kind != SCHEDULE_ONCE:
            return None
        raw = str(self.once_at or "").strip()
        if not raw:
            return None
        for fmt in (ONCE_AT_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def interval_hours_value(self) -> int:
        """:data:`MIN_INTERVAL_HOURS` 兜底的小时数。"""
        return max(MIN_INTERVAL_HOURS, int(self.interval_hours or MIN_INTERVAL_HOURS))

    def min_interval_seconds(self) -> float:
        """本任务允许的最密间隔（秒）。"""
        return min_interval_seconds_for(self.task_type)

    def interval_seconds_value(self) -> float:
        """``interval`` 周期的实际秒数。

        分钟优先于小时：``interval_minutes`` 是用户在面板上为**这个任务**
        显式填的粒度，而 ``interval_hours`` 有默认值 24，只有在没填分钟时
        才该轮到它说话。反过来会让"配了 10 分钟"被默认 24 小时盖掉。
        """
        floor = self.min_interval_seconds()
        minutes = int(self.interval_minutes or 0)
        if minutes > 0:
            return max(float(minutes * 60), floor)
        return max(float(self.interval_hours_value() * 3600), floor)

    def next_run_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """下次执行时刻。

        返回 ``None`` 有两种含义，调用方要分得开（面板显示也不同）：

        * **禁用** —— 没启用就没有下次；
        * **没有计划中的下次** —— ``startup``（只在启动时跑）
          和已错过的 ``once``。这类不是"永不执行"，是"不在周期里"。
        """
        if not self.enabled:
            return None
        base = now or datetime.now()
        kind = self.kind

        if kind == SCHEDULE_STARTUP:
            return None

        if kind == SCHEDULE_INTERVAL:
            if self.last_run_at:
                # 用秒而不是小时：这是"十分钟刷新一次"能落地的地方。
                # 换成 timedelta(hours=...) 会把任何亚小时粒度抹成 0。
                return (datetime.fromtimestamp(self.last_run_at)
                        + timedelta(seconds=self.interval_seconds_value()))
            # 没跑过 → 下一次检查就跑（而不是等满一个周期，
            # 否则刚启用的任务要干等 N 小时才第一次执行）
            return base

        if kind == SCHEDULE_CRON:
            expr = self.cron_expr()
            return expr.next_after(base) if expr else None

        if kind == SCHEDULE_ONCE:
            target = self.parse_once_at()
            if target is None:
                return None
            # 跑过了 → 没有下次（调用方会把它停用，这里只是防御）
            if self.last_run_at and self.last_run_at >= target.timestamp():
                return None
            if target > base:
                return target
            # 刚过 → 宽限内还算数；超过宽限就是真错过了，
            # 补跑一个"上周三该跑的校验"没有任何意义。
            if (base - target).total_seconds() <= MISS_GRACE_SECONDS:
                return target
            return None

        # ---- daily / weekly / monthly ----
        hour, minute = _parse_hhmm(self.at)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if kind == SCHEDULE_WEEKLY:
            target += timedelta(days=self.weekday_value() - base.isoweekday())
            step = timedelta(days=7)
        elif kind == SCHEDULE_MONTHLY:
            dom = self.day_of_month_value()
            target = target.replace(day=dom)
            step = None   # 月长不定，"下一轮"不能用固定 timedelta
        else:
            step = timedelta(days=1)
        if step is None:
            # monthly：同一结构（已跑过/没到/宽限内/错过），只是"下一轮"
            # 要重新算——下个月同一天，而不是加固定天数。
            if self.last_run_at and self.last_run_at >= target.timestamp():
                return _next_monthly(base, dom, hour, minute)
            if target > base:
                return target
            if (base - target).total_seconds() <= MISS_GRACE_SECONDS:
                return target
            return _next_monthly(base, dom, hour, minute)
        # 这个点已经跑过 → 下一周/明天
        if self.last_run_at and self.last_run_at >= target.timestamp():
            return target + step
        # 还没到 → 就是它
        if target > base:
            return target
        # 刚过（一个宽限窗口内）→ **现在就跑**。
        # 这个分支不能省：检查是每 30s 一拍，"03:10" 实际会在
        # 03:10:15 附近被看到，若一律判成"过了就顺延明天"，
        # 日任务将永远触发不了。宽限之外才算真的错过，等下个周期。
        if (base - target).total_seconds() <= MISS_GRACE_SECONDS:
            return target
        return target + step

    def weekday_value(self) -> int:
        return min(max(int(self.weekday or 1), 1), 7)

    def day_of_month_value(self) -> int:
        """每月几号（1-:data:`MAX_DAY_OF_MONTH`）。29-31 在小月不存在，封顶 28。"""
        return min(max(int(self.day_of_month or 1), 1), MAX_DAY_OF_MONTH)

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        # startup 不参与周期检查：它由引擎启动显式触发。
        # 让它也走 is_due 会导致"每次 tick 都跑一遍"（next_run_at 返回 None
        # 时不会触发，但返回 base 就会），语义必须在这里截断。
        if self.is_startup:
            return False
        base = now or datetime.now()
        # 「仅交易时段」的任务在休市时不到期。
        # 跳过不会累积欠账 —— interval 的下次时刻是 last_run_at + 间隔，
        # 固定只会算出一次，所以开盘那一刻刷一轮（正是想要的），
        # 而不会把夜里欠的十几次一次性补跑出来把数据源打进限流名单。
        # 另外休市跳过不计入 errors：它不是"这轮没做成"，是"这轮不该发生"。
        if self.trading_hours_only and not _in_trading_window(base):
            return False
        nxt = self.next_run_at(base)
        return nxt is not None and nxt <= base

    # ---------------- 序列化 ----------------
    def to_dict(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        nxt = self.next_run_at(now)
        kind = self.kind
        missing = ""
        if kind == SCHEDULE_CRON and not self.cron:
            missing = "还没填 cron 表达式"
        elif kind == SCHEDULE_ONCE and self.parse_once_at() is None:
            missing = f"时刻格式应为 {ONCE_AT_FORMAT}"
        elif kind == SCHEDULE_CRON:
            missing = self.cron_error()
        return {
            "name": self.name,
            "task_type": self.task_type,
            "label": self.label,
            "enabled": self.enabled,
            "schedule": kind,
            "schedule_label": SCHEDULE_LABELS.get(kind, kind),
            "at": self.at,
            "weekday": self.weekday_value(),
            "weekday_label": WEEKDAY_NAMES[self.weekday_value() - 1],
            "day_of_month": self.day_of_month_value(),
            "interval_hours": self.interval_hours_value(),
            "interval_minutes": int(self.interval_minutes or 0),
            "trading_hours_only": bool(self.trading_hours_only),
            "interval_seconds": self.interval_seconds_value(),
            "min_interval_seconds": self.min_interval_seconds(),
            "cron": self.cron,
            "cron_text": self.cron_expr().describe() if self.cron_expr() else "",
            "once_at": self.once_at,
            "params": dict(self.params),
            "description": self.description,
            "config_error": missing,
            "runs": self.runs,
            "errors": self.errors,
            "last_run_at": self.last_run_at,
            "last_run_iso": (
                datetime.fromtimestamp(self.last_run_at).isoformat(timespec="seconds")
                if self.last_run_at else ""
            ),
            "next_run_at": nxt.timestamp() if nxt else None,
            "next_run_iso": nxt.isoformat(timespec="seconds") if nxt else "",
            "next_run_text": (
                nxt.isoformat(timespec="seconds") if nxt
                else ("引擎启动时" if kind == SCHEDULE_STARTUP else "—")
            ),
            # 「仅交易时段」的任务在休市期间"看起来已到期却不执行"，
            # 不把原因摆出来，面板上就是一个永远卡住的"待执行"。
            "waiting_market": bool(
                self.enabled and not self.is_startup
                and self.trading_hours_only
                and not _in_trading_window(now or datetime.now())
            ),
            "last_result": self.last_result,
        }

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any], default: "MaintenanceJob") -> "MaintenanceJob":
        schedule = _norm_schedule(raw.get("schedule"), default.kind)

        # 旧配置只有 interval_sec（秒）。换算成小时时**向上取整**：
        # 3600 秒 = 1 小时没问题，而 5400 秒（1.5h）向下取整会得到 1 小时，
        # 等于悄悄把周期缩短 —— 维护任务跑太频繁比跑少了更烦人。
        hours = raw.get("interval_hours")
        if hours is None or str(hours).strip() == "":
            legacy_sec = int(raw.get("interval_sec") or 0)
            hours = (
                max(MIN_INTERVAL_HOURS, int(math.ceil(legacy_sec / 3600.0)))
                if legacy_sec else default.interval_hours
            )

        job = cls(
            name=name,
            task_type=str(raw.get("task_type") or default.task_type),
            label=str(raw.get("label") or default.label),
            enabled=bool(raw.get("enabled", default.enabled)),
            schedule=schedule,
            at=str(raw.get("at") or default.at),
            weekday=int(raw.get("weekday") or default.weekday),
            day_of_month=int(raw.get("day_of_month") or default.day_of_month),
            interval_hours=int(hours or MIN_INTERVAL_HOURS),
            cron=str(raw.get("cron") or default.cron),
            once_at=str(raw.get("once_at") or default.once_at),
            interval_minutes=int(raw.get("interval_minutes") or default.interval_minutes or 0),
            trading_hours_only=bool(raw.get("trading_hours_only", default.trading_hours_only)),
            params=dict(raw.get("params") or default.params),
            description=str(raw.get("description") or default.description),
        )
        return job


def _legacy_adjust(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把"还是旧内置默认值"的那几条配置迁到新默认值。

    只迁**没被改过**的配置。判定依据是它还精确等于旧的默认值
    （运行校验曾经是 ``interval`` + ``interval_sec=3600``）——
    用户改过周期、填过 cron 的一律不动，绝不替用户做决定。

    不做这件事的后果：用户从没手动改过维护计划，但面板保存过一次
    （比如只勾了"真的删除"），那份旧配置就会把已经改成"启动时执行"
    的默认值又拽回"每 1 小时"，于是用户提的需求根本没生效。
    """
    if name != "validate" or not isinstance(cfg, dict):
        return cfg
    if _norm_schedule(cfg.get("schedule")) != SCHEDULE_INTERVAL:
        return cfg
    if int(cfg.get("interval_sec") or 0) != 3600:
        return cfg
    if str(cfg.get("cron") or "").strip():
        return cfg
    migrated = dict(cfg)
    migrated["schedule"] = SCHEDULE_STARTUP
    return migrated


def known_task_types() -> List[str]:
    """可被周期调度的任务类型清单。

    来源是 :class:`TaskType` 枚举本身而不是某份手工白名单 ——
    手工清单必然滞后于代码（新加一种任务忘了补，面板上就永远选不到），
    而枚举是"系统里存在哪些任务"的唯一事实来源。
    """
    try:
        from app.runtime.events.types import TaskType
    except Exception:
        return []
    out: List[str] = []
    for attr in dir(TaskType):
        if attr.startswith("_"):
            continue
        value = getattr(TaskType, attr, None)
        if isinstance(value, str) and value and "." in value and value not in out:
            out.append(value)
    return sorted(out)


def _job_from_config(name: str, cfg: Dict[str, Any]) -> Optional[MaintenanceJob]:
    """把配置里的一条自定义任务读成 :class:`MaintenanceJob`。

    缺 ``task_type`` 的条目直接跳过：它是唯一决定"这条配置要干什么"的
    字段，缺了它后面无论怎么兜底都是在猜用户的意图。

    未知 ``task_type`` **不拦**。处理器有可能在别处动态注册（插件、
    面板），在这里拦死等于把"配置先落盘、处理器后注册"这种完全正常的
    顺序判成非法。没有处理器的任务会在调度器里落成 ``no_handler`` 并被
    计数 —— 那是有据可查的失败，比静默丢弃强得多。
    """
    if not isinstance(cfg, dict):
        return None
    task_type = str(cfg.get("task_type") or "").strip()
    if not task_type:
        logger.warning(f"自定义定时任务 {name} 缺少 task_type，已跳过")
        return None
    known = known_task_types()
    if known and task_type not in known:
        logger.warning(
            f"自定义定时任务 {name} 的 task_type={task_type!r} 不在已知类型里；"
            f"仍会加载，但若最终没有对应处理器，执行时会记为 no_handler"
        )
    default = MaintenanceJob(
        name=name,
        task_type=task_type,
        label=str(cfg.get("label") or name),
        enabled=False,
        # 自定义任务的默认周期：既然是用户主动配的定时任务，
        # 「间隔」比「每天凌晨」更像他想要的表达。
        schedule=SCHEDULE_INTERVAL,
    )
    return MaintenanceJob.from_dict(name, cfg, default)


#: 内置维护任务。**全部默认不删东西**，只报告。
DEFAULT_JOBS: Dict[str, MaintenanceJob] = {
    "log_cleanup": MaintenanceJob(
        name="log_cleanup",
        task_type=TaskType.MAINTENANCE_LOG_CLEANUP,
        label="日志清理",
        enabled=True,
        schedule="daily",
        at="03:10",
        params={"retention_days": 7, "apply": False},
        description="扫描并清理超过保留天数的日志文件；未勾选『真的删除』时只列出清单",
    ),
    "archive": MaintenanceJob(
        name="archive",
        task_type=TaskType.MAINTENANCE_ARCHIVE,
        label="快照归档",
        enabled=False,
        schedule="daily",
        at="03:40",
        params={"before_days": 30, "apply": False},
        description="把超过天数的运行快照移入 archive/；默认关闭，需先确认归档目标",
    ),
    "validate": MaintenanceJob(
        name="validate",
        task_type=TaskType.MAINTENANCE_VALIDATE,
        label="运行校验",
        enabled=True,
        # 只在**引擎启动时**跑一次。它的价值就是"这次启动的环境是好的"，
        # 每小时再校验一遍既不增加信息量，又会在日志里堆一堆"正常"。
        schedule=SCHEDULE_STARTUP,
        params={},
        description="引擎启动时校验日志目录、数据目录与数据库可用性（只读，不删任何东西）",
    ),
    "dead_history": MaintenanceJob(
        name="dead_history",
        task_type=TaskType.MAINTENANCE_DEAD_HISTORY,
        label="死信历史清理",
        enabled=True,
        schedule="daily",
        at="04:10",
        # apply 默认 True：它清的**只是已处置**的死信处置记录（数据库行，
        # 不是文件），未处置的一条不动 —— 那是排障依据。
        # 这类清理"只报告不删"没有意义，报告一个数量谁也用不上。
        params={"retention_days": 30, "keep": 200, "apply": True},
        description="清理已处置超过保留期的死信处置历史（未处置的不动）；对照 Rhapsody 的通知事件历史清理",
    ),
}


class MaintenancePlanner:
    """维护计划：读配置 → 判断到点 → 发 ``system.maintenance.due``。"""

    def __init__(
        self,
        publish: Optional[Callable[..., Any]] = None,
        path: Optional[str] = None,
    ) -> None:
        self._publish = publish
        self._path = Path(path) if path else self.default_path()
        self._lock = threading.RLock()
        self._jobs: Dict[str, MaintenanceJob] = {}
        self._load()
        self._last_tick: float = 0.0
        self._ticks: int = 0
        self._fired: int = 0

    # ---------------- 配置 ----------------
    @staticmethod
    def default_path() -> Path:
        try:
            from app.common.config import settings

            return Path(settings.CONFIG_DIR) / "maintenance.json"
        except Exception:
            return Path("config") / "maintenance.json"

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        raw: Dict[str, Any] = {}
        try:
            if self._path.exists():
                parsed = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw = parsed.get("jobs") or parsed
        except Exception as exc:
            logger.warning(
                f"读取维护配置 {self._path} 失败（{type(exc).__name__}: {exc}），使用内置默认"
            )
            raw = {}

        with self._lock:
            self._jobs = {
                name: MaintenanceJob.from_dict(
                    name, _legacy_adjust(name, raw.get(name) or {}), default
                )
                for name, default in DEFAULT_JOBS.items()
            }
            unknown = [k for k in raw if k not in DEFAULT_JOBS and isinstance(raw[k], dict)]
            if unknown:
                logger.info(f"维护配置里读到 {len(unknown)} 个自定义任务: {unknown}")
                loaded: Dict[str, MaintenanceJob] = {}
                for name in unknown:
                    job = _job_from_config(name, raw[name])
                    if job is not None:
                        loaded[name] = job
                self._jobs.update(loaded)

    def save(self) -> None:
        with self._lock:
            payload = {
                "_note": "维护任务计划（面板『运行时 → 维护计划』写入，保存即生效）",
                "jobs": {
                    name: {
                        "task_type": j.task_type,
                        "label": j.label,
                        "enabled": j.enabled,
                        "schedule": j.kind,
                        "at": j.at,
                        "weekday": j.weekday_value(),
                        "day_of_month": j.day_of_month_value(),
                        "interval_hours": j.interval_hours_value(),
                        "interval_minutes": int(j.interval_minutes or 0),
                        "trading_hours_only": bool(j.trading_hours_only),
                        "cron": j.cron,
                        "once_at": j.once_at,
                        "params": dict(j.params),
                        "description": j.description,
                        # 标记"这条不是内置任务"，便于将来区分对待；
                        # 删掉不影响加载，但有了它，排障时能一眼看出来源。
                        "custom": name not in DEFAULT_JOBS,
                    }
                    for name, j in self._jobs.items()
                },
            }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(f"保存维护配置失败: {exc}")
            raise

    # ---------------- 改配置 ----------------
    def update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """整体更新配置（面板一次提交整张表）。

        :param payload: ``{"jobs": {name: {...}}}`` 或 ``{name: {...}}``
        :return: 更新后的 :meth:`snapshot`
        """
        raw = (payload or {}).get("jobs")
        if not isinstance(raw, dict):
            raw = dict(payload or {})
        changed: List[str] = []
        created: List[str] = []
        with self._lock:
            for name, cfg in raw.items():
                if not isinstance(cfg, dict):
                    continue
                if name in self._jobs:
                    cur = self._jobs[name]
                    new = MaintenanceJob.from_dict(name, cfg, cur)
                    # 运行态要留着 —— 改一下周期不该把"上次什么时候跑的"清空，
                    # 否则每次保存完立刻就会补跑一轮。
                    new.last_run_at = cur.last_run_at
                    new.last_result = cur.last_result
                    new.runs = cur.runs
                    new.errors = cur.errors
                    self._jobs[name] = new
                    changed.append(name)
                    continue
                # 走到这里说明是**新增**：面板提交了库里还没有的任务名。
                # 以前这里直接 continue —— 于是"新增定时任务"永远无法通过表
                # 单落地，只能手改配置文件再重启，这正是要让配置可配的意义。
                born = _job_from_config(name, cfg)
                if born is None:
                    continue
                self._jobs[name] = born
                created.append(name)
        self.save()
        if changed:
            logger.info(f"维护计划已更新: {changed}")
        if created:
            logger.info(f"维护计划新增任务: {created}")
        return self.snapshot()

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            job = self._jobs.get(str(name or ""))
            if job is None:
                return False
            job.enabled = bool(enabled)
        self.save()
        return True

    # ---------------- 增删任务 ----------------
    def add_job(self, name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """新增一条定时任务。

        :param name: 任务名（配置里的 key，也是日志里的标识）
        :param cfg:  任务定义，至少要有 ``task_type``
        :return:     ``{"ok": bool, "error": str, "job": {...}}``

        内置任务不能同名覆盖：它们的行为有一部分写死在代码里
        （比如"归档"默认不启用是有意为之），让同名配置悄悄改掉语义，
        排障时会变成一个无法解释的谜。
        """
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "任务名不能为空"}
        if not isinstance(cfg, dict):
            return {"ok": False, "error": "任务定义必须是对象"}
        with self._lock:
            if name in DEFAULT_JOBS:
                return {"ok": False, "error": f"{name} 是内置任务，不能新建同名任务"}
            job = _job_from_config(name, cfg)
            if job is None:
                return {"ok": False, "error": "缺少 task_type（它决定这条配置要做什么）"}
            self._jobs[name] = job
        self.save()
        logger.info(f"新增定时任务 {name}（{job.task_type}）")
        return {"ok": True, "job": job.to_dict()}

    def remove_job(self, name: str) -> Dict[str, Any]:
        """删除一条自定义定时任务。

        内置任务删不掉：它们是引擎自保的一部分（日志清理、死信回收），
        真不需要可以停用，但让它从表里消失会导致"谁在清理日志"失去答案。
        """
        name = str(name or "").strip()
        with self._lock:
            if name in DEFAULT_JOBS:
                return {"ok": False, "error": f"{name} 是内置任务，只能停用不能删除"}
            if self._jobs.pop(name, None) is None:
                return {"ok": False, "error": f"没有这个任务: {name}"}
        self.save()
        logger.info(f"已删除定时任务 {name}")
        return {"ok": True, "removed": name}

    # ---------------- 触发 ----------------
    def bind(self, publish: Callable[..., Any]) -> None:
        """注入事件发布函数（引擎启动时调度器调用）。"""
        self._publish = publish

    def _emit(self, job: MaintenanceJob, reason: str) -> bool:
        """发一个 ``system.maintenance.due`` 事件。**失败算没跑**。"""
        if self._publish is None:
            job.errors += 1
            job.last_result = "事件总线未就绪（引擎还没启动？）"
            logger.warning(f"维护任务 {job.name} 无法触发：事件总线未就绪")
            return False
        try:
            self._publish(
                EventType.SYSTEM_MAINTENANCE_DUE,
                job=job.name,
                task_type=job.task_type,
                params=dict(job.params),
                reason=reason,
            )
        except Exception as exc:
            job.errors += 1
            job.last_result = f"发布事件失败: {exc}"
            logger.error(f"发布维护事件失败 {job.name}: {exc}")
            return False
        job.last_run_at = time.time()
        job.runs += 1
        job.last_result = f"已触发（{reason}）"
        # 一次性任务跑完就停用。不停用的话它会在下一拍再跑一遍：
        # next_run_at 对已跑过的 once 返回 None，看似安全，
        # 但"停用"是要写回配置并显示在面板上的事实 ——
        # 一个显示"启用"却永远不会再跑的任务比"已停用"更让人困惑。
        if job.kind == SCHEDULE_ONCE:
            job.enabled = False
            job.last_result = f"已触发（{reason}），一次性任务已自动停用"
            try:
                self.save()
            except Exception as exc:  # pragma: no cover
                logger.warning(f"一次性维护任务 {job.name} 停用后写盘失败: {exc}")
        return True

    def tick(self, now: Optional[datetime] = None) -> List[str]:
        """检查一遍所有计划，给到点的发事件。

        :return: 本次触发的 job 名列表
        """
        base = now or datetime.now()
        fired: List[str] = []
        with self._lock:
            self._ticks += 1
            self._last_tick = time.time()
            for job in self._jobs.values():
                if not job.is_due(base):
                    continue
                if self._emit(job, "scheduled"):
                    fired.append(job.name)
        if fired:
            logger.info(f"维护窗口到点: {fired}")
            self._fired += len(fired)
        return fired

    def run_startup(self) -> List[str]:
        """跑一遍所有「引擎启动时」类型的任务。

        与 :meth:`tick` 完全分开的两条路径：tick 是"按周期到点了"，
        这里是"进程刚起来"。分开的理由是它们失败时的含义不同 ——
        启动任务失败意味着"这次启动的环境没被校验过"，
        而周期任务失败只是"这一轮没做成"。

        它不看 ``enabled`` 以外的任何条件，也不受周期影响：
        startup 的语义就是"每次启动一次"。

        :return: 本次触发的 job 名列表
        """
        fired: List[str] = []
        with self._lock:
            for job in self._jobs.values():
                if not job.enabled or not job.is_startup:
                    continue
                if self._emit(job, "startup"):
                    fired.append(job.name)
        if fired:
            logger.info(f"启动维护任务已触发: {fired}")
            self._fired += len(fired)
        return fired

    def run_now(self, name: str) -> Dict[str, Any]:
        """立刻跑一次（面板「立即执行」）。

        与 :meth:`tick` 的区别：它**不看周期、不看开关** ——
        "手动立即执行"被 enabled=false 挡住是最让人困惑的设计。
        """
        job = self._jobs.get(str(name or ""))
        if job is None:
            return {"ok": False, "error": f"没有这个维护任务: {name}",
                    "known": sorted(self._jobs)}
        ok = self._emit(job, "manual")
        return {
            "ok": ok,
            "job": job.name,
            "task_type": job.task_type,
            "params": dict(job.params),
            "error": "" if ok else (job.last_result or "发布失败"),
        }

    # ---------------- 观测 ----------------
    def jobs(self) -> List[MaintenanceJob]:
        with self._lock:
            return list(self._jobs.values())

    def snapshot(self) -> Dict[str, Any]:
        base = datetime.now()
        with self._lock:
            jobs = [j.to_dict(base) for j in self._jobs.values()]
        return {
            "jobs": jobs,
            "enabled": sum(1 for j in jobs if j["enabled"]),
            "total": len(jobs),
            "tick_seconds": TICK_SECONDS,
            "ticks": self._ticks,
            "fired": self._fired,
            "last_tick": self._last_tick,
            "last_tick_iso": (
                datetime.fromtimestamp(self._last_tick).isoformat(timespec="seconds")
                if self._last_tick else ""
            ),
            "config_path": str(self._path),
            # 面板要靠这些常量渲染下拉框，**不能在前台硬编码** ——
            # 否则后台加了一种周期，面板上永远选不到。
            "kinds": [
                {"value": k, "label": SCHEDULE_LABELS[k]} for k in SCHEDULE_KINDS
            ],
            "weekday_names": list(WEEKDAY_NAMES),
            "min_interval_hours": MIN_INTERVAL_HOURS,
            "min_interval_minutes": MIN_INTERVAL_MINUTES,
            # 前端靠它判断某个任务能配多密。维护类仍受小时保护，
            # 所以这个值**按任务而异**，不是一个全局常量。
            "maintenance_floor_seconds": _MAINTENANCE_FLOOR_SECONDS,
            "once_at_format": ONCE_AT_FORMAT,
            "startup_jobs": [j.name for j in self._jobs.values() if j.is_startup],
            # 新增任务时的可选类型。从这里取而不是前端硬编码 ——
            # 否则后台新增一种任务类型，面板的下拉框里永远选不到。
            "task_types": known_task_types(),
            "builtin_jobs": sorted(DEFAULT_JOBS),
        }


__all__ = [
    "MaintenanceJob",
    "MaintenancePlanner",
    "DEFAULT_JOBS",
    "TICK_SECONDS",
    "MISS_GRACE_SECONDS",
    "SCHEDULE_KINDS",
    "SCHEDULE_LABELS",
    "SCHEDULE_STARTUP",
    "SCHEDULE_INTERVAL",
    "SCHEDULE_DAILY",
    "SCHEDULE_WEEKLY",
    "SCHEDULE_MONTHLY",
    "SCHEDULE_CRON",
    "SCHEDULE_ONCE",
    "MIN_INTERVAL_HOURS",
    "MIN_INTERVAL_MINUTES",
    "MAINTENANCE_TASK_PREFIX",
    "min_interval_seconds_for",
    "MAX_DAY_OF_MONTH",
    "WEEKDAY_NAMES",
]
