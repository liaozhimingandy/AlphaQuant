#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : cron.py
# @Description : cron 表达式（5 段）解析与"下一次执行时刻"计算
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么自己写而不引 croniter
============================

项目守着"零新依赖"（只有 numpy / pandas）这条线，为一个 5 段表达式
引入一个第三方包不划算；而 cron 的语法子集很小，解析 + 求下一次
一共不到两百行，能被测试完整覆盖。

支持的语法
----------

::

    分  时  日  月  周
    *   *   *   *   *

每段的写法（可逗号并列多个）::

    *        该段全部取值
    */n      从最小值起每 n 个取一次
    a        单个值
    a-b      闭区间
    a-b/n    区间内每 n 个取一次
    a,b,c    并列

取值域：分 0-59、时 0-23、日 1-31、月 1-12、周 0-6（**0 = 周日**，
与标准 cron 一致）。

**日与周是 OR 语义**（Vixie cron 行为）：两段都受限时满足其一即可；
只有一段受限时按那一段；都不受限时每天都算。这条必须实现对，
否则 ``0 3 1 * 1``（每月 1 号或每周一）会被误判成"1 号且是周一"。

求下一次为什么不能逐分钟扫
--------------------------

``0 3 29 2 *``（2 月 29 日）四年才命中一次，逐分钟扫要跑两百万次。
这里按 **月 → 日 → 时 → 分** 逐层跳过整段，最坏情况是"日期永远不匹配"
时按天跳（五年约 1800 次），可以接受。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Set, Tuple

#: 五段的 (最小值, 最大值)，顺序 = 分 时 日 月 周
FIELD_BOUNDS: Tuple[Tuple[int, int], ...] = (
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 6),
)

#: 中文名，报错时告诉人错在哪一段
FIELD_NAMES: Tuple[str, ...] = ("分", "时", "日", "月", "周")

#: 找不到匹配时最多往前找多少天（约 5 年，足够覆盖 2 月 29 日）
DEFAULT_LIMIT_DAYS: int = 366 * 5


class CronError(ValueError):
    """cron 表达式写错了。

    单独一个类型的理由：调用方要能把"用户填错了"和"程序 bug"分开，
    前者给用户一句人话，后者才该炸栈。
    """


def _parse_field(text: str, lo: int, hi: int, name: str) -> List[int]:
    """解析一段，返回升序去重的取值列表。"""
    values: Set[int] = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            raise CronError(f"cron 的「{name}」段里有空项（多写了逗号？）")

        step = 1
        if "/" in part:
            base, _, raw_step = part.partition("/")
            raw_step = raw_step.strip()
            if not raw_step.isdigit() or int(raw_step) <= 0:
                raise CronError(
                    f"cron 的「{name}」段步长必须是正整数，收到 {part!r}"
                )
            step = int(raw_step)
            part = base.strip()
        else:
            part = part

        if part in ("", "*"):
            start, end = lo, hi
        elif "-" in part:
            left, _, right = part.partition("-")
            try:
                start, end = int(left.strip()), int(right.strip())
            except ValueError:
                raise CronError(f"cron 的「{name}」段区间写错了: {part!r}")
        else:
            try:
                start = int(part)
            except ValueError:
                raise CronError(
                    f"cron 的「{name}」段不是数字: {part!r}（合法 {lo}-{hi}）"
                )
            end = start

        if start < lo or end > hi:
            raise CronError(
                f"cron 的「{name}」段越界: {part!r}（合法 {lo}-{hi}）"
            )
        if start > end:
            raise CronError(
                f"cron 的「{name}」段区间倒置: {part!r}（左边不能大于右边）"
            )
        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"cron 的「{name}」段没有匹配到任何值")
    return sorted(values)


@dataclass(frozen=True)
class CronExpr:
    """解析好的 cron 表达式。

    :param text: 原始文本（面板回显用）
    :param dom_limited / dow_limited: 「日」「周」是否被限制。
        这两段是 OR 语义的前提，必须记住原文是不是 ``*`` ——
        只看解析出的取值集合也能判断（全集 = 没限制），
        但记下来更直白，也避免"全集"的定义随月份漂移。
    """

    minutes: Tuple[int, ...]
    hours: Tuple[int, ...]
    doms: Tuple[int, ...]
    months: Tuple[int, ...]
    dows: Tuple[int, ...]
    text: str = ""
    dom_limited: bool = False
    dow_limited: bool = False

    # ---------------- 解析 ----------------
    @classmethod
    def parse(cls, text: str) -> "CronExpr":
        """解析 5 段 cron 表达式，写错抛 :class:`CronError`。"""
        raw = str(text or "").strip()
        if not raw:
            raise CronError("cron 表达式为空")
        parts = raw.split()
        if len(parts) != 5:
            raise CronError(
                f"cron 表达式必须是 5 段（分 时 日 月 周），收到 {len(parts)} 段: {raw!r}"
            )
        parsed = [
            _parse_field(parts[i], FIELD_BOUNDS[i][0], FIELD_BOUNDS[i][1],
                         FIELD_NAMES[i])
            for i in range(5)
        ]
        minutes, hours, doms, months, dows = parsed
        return cls(
            minutes=tuple(minutes),
            hours=tuple(hours),
            doms=tuple(doms),
            months=tuple(months),
            dows=tuple(dows),
            text=raw,
            dom_limited=set(doms) != set(range(1, 32)),
            dow_limited=set(dows) != set(range(0, 7)),
        )

    # ---------------- 判定 ----------------
    def _day_matches(self, dt: datetime) -> bool:
        """日 / 周 的 OR 语义。"""
        if self.dom_limited and self.dow_limited:
            return dt.day in self.doms or self._dow(dt) in self.dows
        if self.dom_limited:
            return dt.day in self.doms
        if self.dow_limited:
            return self._dow(dt) in self.dows
        return True

    @staticmethod
    def _dow(dt: datetime) -> int:
        """Python 的周一=1…周日=7 换算成 cron 的 周日=0…周六=6。"""
        return dt.isoweekday() % 7

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._day_matches(dt)
        )

    # ---------------- 求下一次 ----------------
    def next_after(
        self,
        now: datetime,
        limit_days: int = DEFAULT_LIMIT_DAYS,
        inclusive: bool = False,
    ) -> Optional[datetime]:
        """> ``now`` 的第一个匹配时刻。

        :param inclusive: True 时 ``now`` 本身匹配也算（"现在该不该跑"）
        :return: 找不到返回 None（比如 ``0 0 30 2 *``，2 月 30 日不存在）
        """
        cur = now.replace(second=0, microsecond=0)
        if not inclusive:
            cur += timedelta(minutes=1)
        if inclusive and self.matches(cur):
            return cur

        limit = cur + timedelta(days=max(1, int(limit_days)))
        while cur < limit:
            if cur.month not in self.months:
                # 跳到下个月 1 号 00:00（+32 天必进下月，再归一到 1 号）
                cur = (cur.replace(day=1, hour=0, minute=0)
                       + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(cur):
                cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cur.hour not in self.hours:
                cur = (cur + timedelta(hours=1)).replace(minute=0)
                continue
            if cur.minute not in self.minutes:
                cur += timedelta(minutes=1)
                continue
            return cur
        return None

    # ---------------- 人话 ----------------
    def describe(self) -> str:
        """给面板用的一句话说明；说不清就回原文。"""
        every_min = len(self.minutes) == 60
        every_hour = len(self.hours) == 24
        every_day = not self.dom_limited and not self.dow_limited
        every_month = len(self.months) == 12

        if every_min and every_hour and every_day and every_month:
            return "每分钟"

        step = self._step_of(self.minutes, 60)
        if step and every_hour and every_day and every_month:
            return f"每 {step} 分钟"

        # 小时不要求整除 24：``0 */5 * * *`` 是 0/5/10/15/20 时，
        # 说"每 5 小时"比甩一句原始表达式有用得多。
        step = self._step_of(self.hours, 24, strict=False)
        if step and len(self.minutes) == 1 and every_day and every_month:
            mm = self.minutes[0]
            return (
                f"每 {step} 小时整点" if mm == 0
                else f"每 {step} 小时的第 {mm} 分"
            )

        if (
            len(self.minutes) == 1 and len(self.hours) == 1
            and every_day and every_month
        ):
            return f"每天 {self.hours[0]:02d}:{self.minutes[0]:02d}"

        if (
            len(self.minutes) == 1 and len(self.hours) == 1
            and self.dow_limited and not self.dom_limited and every_month
        ):
            names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
            days = "、".join(names[d] for d in self.dows)
            return f"每周 {days} {self.hours[0]:02d}:{self.minutes[0]:02d}"

        if (
            len(self.minutes) == 1 and len(self.hours) == 1
            and self.dom_limited and not self.dow_limited and every_month
        ):
            days = "、".join(f"{d} 号" for d in self.doms)
            return f"每月 {days} {self.hours[0]:02d}:{self.minutes[0]:02d}"

        return self.text or "（空）"

    @staticmethod
    def _step_of(values: Sequence[int], full: int, strict: bool = True) -> int:
        """判断这一段是不是"每 N 个"的均匀模式，是则返回步长，否则 0。

        :param strict: True 时还要求**刚好铺满整圈**（分钟段必须 60/步长
            个值，否则 ``1,2,3`` 会被说成"每 1 分钟"）。小时段用宽松模式，
            因为 24 不能被 5 整除，而 ``*/5`` 确实就是"每 5 小时"。
        """
        seq = list(values)
        if len(seq) < 2:
            return 0
        step = seq[1] - seq[0]
        if step <= 0:
            return 0
        if seq != list(range(seq[0], seq[0] + step * len(seq), step)):
            return 0
        if strict and len(seq) * step != full:
            return 0
        return step


def describe_cron(text: str) -> str:
    """解析并描述；写错时返回错误文本（面板直接显示）。"""
    try:
        return CronExpr.parse(text).describe()
    except CronError as exc:
        return str(exc)


def validate_cron(text: str) -> str:
    """校验表达式。返回空串表示合法，否则返回错误原因。"""
    try:
        CronExpr.parse(text)
        return ""
    except CronError as exc:
        return str(exc)


__all__ = ["CronExpr", "CronError", "describe_cron", "validate_cron"]
