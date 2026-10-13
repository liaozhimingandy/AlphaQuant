#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : marketx.py
# @Description : 扩展行情数据：资金流 / 筹码分布 / 涨停池 / 题材板块 / 人气榜
#
#                传统量价（开高低收量）只回答"价格走到哪了"，回答不了
#                "谁在买、买得急不急、套牢盘多不多、是不是当天热点"。
#                这个文件负责把这些**截面数据**取回来。
#
#                两个容易踩坑、这里专门处理了的点：
#
#                1. **akshare 的列名会随版本变**。用精确列名取值的代码，
#                   升级一次依赖就全线崩。所以这里一律用**关键词匹配**找列
#                   （`_pick`），找不到就返回空 —— 宁可"这个字段没数据"，
#                   也不要 KeyError 把整只票的采集带崩。
#                2. **金额字段带单位**。"1.23亿" / "3456万" / "--" 都是常态，
#                   直接 float() 必炸。统一走 `_num()` 解析。
#
#                取不到数据时**抛 MarketXError 而不是返回空表** ——
#                "没数据"和"接口失败"是两件事，混为一谈会让上层误以为
#                "今天确实没有资金流入"。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import os
import random
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from app.common.config import settings
from app.domain.symbol import normalize_symbol
from app.providers.market.provider import bootstrap as _bootstrap
from app.providers.market.provider.base import ProviderError
from app.providers.market.service.data_service import data_service
from app.providers.market.compat.registry import (
    REGISTRY,
    DataUnavailable,
    SourceError,
    is_transient,
)
from app.common.logging import logger, log_throttled

try:  # akshare 是这些接口的唯一来源，缺了就整体不可用
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None  # type: ignore[assignment]


class MarketXError(RuntimeError):
    """扩展行情数据获取失败（网络、接口变更、返回空等）。"""


_LAST_CALL: Dict[str, float] = {}

#: 限速/熔断/快照缓存都是**跨线程共享**的可变状态。
#: 采集全部跑在 ``deferToThread`` 的线程池里（同一时刻可能有 N 只票在取数），
#: 没有这把锁时 N 个线程会读到同一个 ``_LAST_CALL[key]``，
#: 算出相同的 ``wait`` 并各自 sleep，然后**同一瞬间一起发请求** ——
#: 限速与熔断在并发下形同虚设，仍然会被源端 RST / 封 IP。
_RATE_LOCK = threading.Lock()


#: 被限流后**临时**上调的间隔（秒），按 limit_key 独立。
#: ``DATA_FETCH_MIN_INTERVAL`` 是"平时"的节奏（默认 0.3s），
#: 但东财对高频访问会直接 RST —— 这时再按 0.3s 打就是持续撞墙：
#: 一輪 60 只票会被限流打掉大半，且每次被拒后下一只票又从 0.3s 重新开始。
#: 所以检测到限流式失败就把间隔翻倍，成功后回落。
_THROTTLE_GAP: Dict[str, float] = {}
_THROTTLE_GAP_MAX = 5.0


def _throttle_gap(key: str) -> float:
    base = float(settings.DATA_FETCH_MIN_INTERVAL or 0.0)
    return max(base, float(_THROTTLE_GAP.get(key) or 0.0))


def _throttle_penalize(key: str) -> None:
    """撞到限流/连接被重置：把该类接口的间隔翻倍，别继续硬撞。"""
    base = float(settings.DATA_FETCH_MIN_INTERVAL or 0.0)
    with _RATE_LOCK:
        cur = max(base, float(_THROTTLE_GAP.get(key) or 0.0))
        _THROTTLE_GAP[key] = min(max(cur * 2.0, 0.5), _THROTTLE_GAP_MAX)


def _throttle_relax(key: str) -> None:
    """一次成功后回落一档（不直接清零，避免立刻又打满）。"""
    with _RATE_LOCK:
        if key in _THROTTLE_GAP:
            _THROTTLE_GAP[key] = max(0.0, _THROTTLE_GAP[key] / 2.0)


def _throttle(key: str = "marketx") -> None:
    """同类接口的限速。被限流封 IP 是最常见也最难恢复的故障。

    用**预约时刻**而不是「读上次时间 → 自己 sleep」：

    后者在多线程下会让 N 个线程读到同一个 ``last``、算出同样的 wait、
    睡醒后同时打出去（限速失效）。预约时刻的做法是：在锁内领一个
    **属于自己的发车时刻** ``slot``，锁外睡到那个时刻。后到的线程领到
    更晚的 slot，天然排成一列，间隔就是 gap。锁内绝不 sleep，
    否则会把整个线程池串行卡在临界区上。
    """
    gap = _throttle_gap(key)
    if gap <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        slot = max(now, _LAST_CALL.get(key, 0.0) + gap)
        _LAST_CALL[key] = slot
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


# ===========================================================================
# 批量熔断：别让"一个源挂了"变成"整批票各重试一遍"
# ===========================================================================
# 场景：筹码这类接口是**逐票调用**的，一輪要打 60+ 次。
# 源若真的不通（被限流/域名不可达），每只票都会各自重试 3 次并指数退避 ——
# 一輪下来是 200 次徒劳请求，既拖慢同步，又把自己往"封得更死"里推。
# 所以：连续 N 次同因失败就**断开**该接口，本批剩余票直接跳过，
# 冷却期结束再试一次（源恢复了能自动回来）。
_BATCH_BREAKER: Dict[str, Dict[str, Any]] = {}
_BATCH_BREAKER_THRESHOLD = 3       # 连续失败几次就断开
_BATCH_BREAKER_COOLDOWN = 300.0    # 断开后冷却 5 分钟


def _breaker_open(key: str) -> Optional[str]:
    """熔断中返回原因文本；未熔断（或冷却已过）返回 None。"""
    with _RATE_LOCK:
        st = _BATCH_BREAKER.get(key)
        if not st:
            return None
        until = float(st.get("until") or 0.0)
        if until <= 0:
            # 已经在累计失败、但还没到断开阈值 —— 此时**不能**把计数清掉，
            # 否则每次进来都从头数，永远到不了阈值（熔断形同虚设）。
            return None
        if time.monotonic() >= until:
            _BATCH_BREAKER.pop(key, None)   # 冷却结束，放它再试一次
            return None
        return str(st.get("why") or "连续失败")


def _breaker_record(key: str, ok: bool, why: str = "") -> None:
    """记录一次调用结果。成功即清零；失败累计到阈值就断开。

    必须持锁：``fails += 1`` 是读改写，并发下会丢计数，
    表现是"明明失败了很多次，熔断却迟迟不触发"。
    """
    with _RATE_LOCK:
        if ok:
            _BATCH_BREAKER.pop(key, None)
            return
        st = _BATCH_BREAKER.setdefault(key, {"fails": 0, "until": 0.0, "why": ""})
        st["fails"] = int(st.get("fails") or 0) + 1
        st["why"] = why or "连续失败"
        if int(st["fails"]) >= _BATCH_BREAKER_THRESHOLD and not st.get("until"):
            st["until"] = time.monotonic() + _BATCH_BREAKER_COOLDOWN
            fails = st["fails"]
            detail = str(why)[:80]
        else:
            fails = detail = None
    if fails is not None:
        # 告警放锁外：log_throttled 内部有自己的状态，没必要扩大临界区
        log_throttled(
            "WARNING",
            f"[{key}] 连续失败 {fails} 次，"
            f"{_BATCH_BREAKER_COOLDOWN / 60:.0f} 分钟内跳过该接口"
            f"（源疑似不可用，整批重试只会加剧限流）：{detail}",
            key=f"breaker-{key}",
        )


def _breaker_guard(key: str, call: Callable[[], pd.DataFrame],
                   what: str = "") -> pd.DataFrame:
    """``call()`` 的熔断包装：熔断中直接抛，否则调用并按瞬时错误记账。

    只对**瞬时错误**（连接被重置/超时）累计熔断计数 ——
    "这只票没数据"是数据问题，不该让整批票跟着跳过。
    """
    why = _breaker_open(key)
    if why:
        raise SourceError(f"{what or key} 熔断中（{why}）")
    try:
        df = call()
    except Exception as exc:                      # 网络类异常可能不是 MarketXError
        if is_transient(exc):
            _breaker_record(key, False, f"{type(exc).__name__}: {str(exc)[:60]}")
        raise
    _breaker_record(key, True)
    return df


def _require_akshare() -> None:
    if ak is None:
        raise MarketXError("未安装 akshare，无法获取资金流/筹码/涨停池数据")


# ===========================================================================
# 通用解析工具
# ===========================================================================
def _pick(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    """按关键词找列名。优先完全匹配，其次包含匹配。"""
    if df is None or df.empty:
        return None
    cols = {str(c): str(c) for c in df.columns}
    lowered = {c.lower(): c for c in cols}
    for cand in candidates:
        key = str(cand).lower()
        if key in lowered:
            return lowered[key]
    for cand in candidates:
        key = str(cand).lower()
        for low, original in lowered.items():
            if key in low:
                return original
    return None


_UNIT = {"万": 1e4, "亿": 1e8, "千": 1e3}


def _num(value: Any) -> Optional[float]:
    """把 "1.23亿" / "-3,456万" / "--" / None 统一成 float。无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            v = float(value)
        except Exception:
            return None
        return None if v != v else v  # NaN → None
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in ("", "-", "--", "None", "nan", "NaN"):
        return None
    mult = 1.0
    for suffix, factor in _UNIT.items():
        if text.endswith(suffix):
            mult = factor
            text = text[: -len(suffix)]
            break
    try:
        v = float(text) * mult
    except ValueError:
        return None
    return None if v != v else v


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value)
        return ts.date()
    except Exception:
        return None


def _market_of(symbol: str) -> str:
    """沪深市场标识，资金流接口需要单独传。"""
    code = normalize_symbol(symbol)
    return "sh" if code.startswith(("6", "9")) else "sz"


# 真正的判定逻辑放在 sources.py（注册表也要用），这里保留原名做兼容。
_is_retryable = is_transient


def _call(name: str, *args: Any, limit_key: str = "marketx",
          retry: Optional[int] = None, **kwargs: Any) -> pd.DataFrame:
    """调一次 akshare 接口，统一限速 + 指数退避重试 + 错误包装。

    重试只针对**瞬时网络类**错误（见 `_is_retryable`），退避 = 0.5·2^(n-1) + 抖动，
    避免和上游限流/代理抽风撞车。

    :param retry: 重试次数。`None` = 取配置 `DATA_FETCH_MAX_RETRY`（默认 3）；
        传 `0` = 只调一次不重试。**经由数据源注册表调用时传 0** ——
        注册表会在源级做重试，两层都重试会把 3 次放大成 9 次请求。
    """
    _require_akshare()
    fn = getattr(ak, name, None)
    if fn is None:
        raise MarketXError(f"akshare 没有接口 {name}（版本过低或已改名）")

    max_retry = int(settings.DATA_FETCH_MAX_RETRY or 0) if retry is None else int(retry)
    max_retry = max(0, max_retry)
    last_exc: Optional[Exception] = None
    for attempt in range(max_retry + 1):
        if attempt:
            # 指数退避：0.5s、1s、2s …… + 0~0.3s 抖动，错开重试节奏
            backoff = (2 ** (attempt - 1)) * 0.5 + random.uniform(0, 0.3)
            logger.warning(
                f"{name} 第 {attempt}/{max_retry} 次重试"
                f"（前因 {type(last_exc).__name__}: {str(last_exc)[:80]}），"
                f"{backoff:.1f}s 后继续"
            )
            time.sleep(backoff)
        _throttle(limit_key)
        try:
            df = fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if _is_retryable(exc):
                # 瞬时失败多半是限流：拉长**后续**同类请求的间隔。
                # 重试循环内的退避只管这一只票，不管下一只。
                _throttle_penalize(limit_key)
                if attempt < max_retry:
                    continue
            raise MarketXError(f"{name} 调用失败: {type(exc).__name__}: {exc}") from exc
        if df is None or (hasattr(df, "empty") and df.empty):
            # 空数据：非交易日 / 接口变更 —— 重试无意义，直接上报
            raise MarketXError(f"{name} 返回空数据（非交易日？接口变更？）")
        _throttle_relax(limit_key)
        return df
    # 理论上走不到（循环内已 raise），保险兜底
    raise MarketXError(
        f"{name} 调用失败: {type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def _registry_retry() -> int:
    """经由注册表调用 akshare 接口时的重试次数 —— 恒为 0。

    注册表会在**源级**重试（见 ``SourceRegistry.fetch``），
    这里再重试一层就会把 3 次放大成 3×3=9 次请求，白白加重限流。
    所以"重试 3 次后换下一个源"由注册表统一负责，底层只管调一次、把错误报清楚。
    """
    return 0


# ===========================================================================
# 资金流
# ===========================================================================
def _parse_fund_flow(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """把 akshare 逐日资金流原始表解析成统一结构。"""
    col_date = _pick(df, ["日期", "date"])
    col_close = _pick(df, ["收盘价", "close"])
    col_pct = _pick(df, ["涨跌幅", "pct"])
    col_main = _pick(df, ["主力净流入-净额", "主力净流入净额", "主力净额"])
    col_main_pct = _pick(df, ["主力净流入-净占比", "主力净占比"])
    col_super = _pick(df, ["超大单净流入-净额", "超大单净额"])
    col_large = _pick(df, ["大单净流入-净额", "大单净额"])
    col_medium = _pick(df, ["中单净流入-净额", "中单净额"])
    col_small = _pick(df, ["小单净流入-净额", "小单净额"])
    col_turnover = _pick(df, ["换手率"])
    col_amount = _pick(df, ["成交额", "成交金额"])
    if col_date is None:
        raise MarketXError("资金流数据缺少日期列，接口可能已变更")
    if col_main is None:
        raise MarketXError("资金流数据缺少主力净流入列，接口可能已变更")

    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        d = _to_date(r.get(col_date))
        if d is None:
            continue
        main = _num(r.get(col_main))
        amount = _num(r.get(col_amount)) if col_amount else None
        pct = _num(r.get(col_main_pct)) if col_main_pct else None
        # 净占比缺失时用"净额 / 成交额"补 —— 这个比例比绝对金额更可比
        if pct is None and main is not None and amount:
            pct = main / amount
        rows.append({
            "symbol": code,
            "date": d,
            "close": _num(r.get(col_close)) if col_close else None,
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
            "main_net": main,
            "main_net_pct": pct,
            "super_large_net": _num(r.get(col_super)) if col_super else None,
            "large_net": _num(r.get(col_large)) if col_large else None,
            "medium_net": _num(r.get(col_medium)) if col_medium else None,
            "small_net": _num(r.get(col_small)) if col_small else None,
            "turnover": _num(r.get(col_turnover)) if col_turnover else None,
            "amount": amount,
            "updated_at": datetime.now(),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        raise MarketXError("资金流数据解析后为空")
    return out.sort_values("date").reset_index(drop=True)


# 资金流多源兜底的状态
_FF_PRIMARY_DEAD_UNTIL = 0.0                     # 主源(东财 push2his)熔断到期时刻
# "kind:日期" -> (全市场快照, 到期时刻)
# 成功快照缓存久一点（全市场翻 100+ 页很贵）；
# **失败**只负缓存 5 分钟 —— 否则一次瞬时故障会让当天再也拿不到数据，
# 源恢复了也回不来。
_FF_SNAPSHOT_CACHE: Dict[str, Tuple[pd.DataFrame, float]] = {}
_FF_SNAPSHOT_TTL_OK = 1800.0
_FF_SNAPSHOT_TTL_FAIL = 300.0
#: "kind:日期" -> 正在取这张快照的线程的通知句柄。
#: 没有它会出现**缓存击穿**：这个接口一次要翻 100+ 页，整批 68 只票
#: 在同一个瞬间发现缓存 miss，然后一起翻 —— 68 × 100 页直接把源打挂，
#: 也正好绕过了上面的限速（限速只管间隔，管不住"同时 miss"）。
_FF_INFLIGHT: Dict[str, threading.Event] = {}


def _ff_snapshot(kind: str) -> pd.DataFrame:
    """全市场资金流快照（东财另一个可用的 push2 域名），按自然日缓存。

    `stock_individual_fund_flow`（逐日历史）走 push2his 域名，常被本机代理/限流拦截；
    而 `stock_fund_flow_individual`（全市场排名，含 即时/3日/5日/10日 主力净额）
    走 push2 域名，实测在本机代理下可用。用它兜底，至少补回 资金链 维度
    最关键的 `main_net_pct`。
    """
    key = f"{kind}:{date.today().isoformat()}"

    # ---- 缓存查询 / 抢占取数权（都在锁内，临界区极短）----
    with _RATE_LOCK:
        hit = _FF_SNAPSHOT_CACHE.get(key)
        if hit is not None and time.monotonic() < hit[1]:
            return hit[0]  # 可能是空表 —— 即上一次失败的"负缓存"
        ev = _FF_INFLIGHT.get(key)
        if ev is None:
            ev = threading.Event()
            _FF_INFLIGHT[key] = ev
            owner = True
        else:
            owner = False

    # ---- 非属主：等属主取完，别自己再翻一遍 ----
    if not owner:
        ev.wait(timeout=120.0)
        with _RATE_LOCK:
            hit = _FF_SNAPSHOT_CACHE.get(key)
        return hit[0] if hit is not None else pd.DataFrame()

    # ---- 属主：真正去取（锁外，不阻塞别人读缓存）----
    try:
        # retries=0：这里本身就在"资金流源"的重试循环里，
        # 再让注册表重试一遍会把请求数翻倍（每个视角 100+ 页，很贵）。
        df = fetch_fund_flow_rank(kind=kind, retries=0)
    except Exception as exc:
        # 负缓存：这个接口一次要翻 100 多页（几秒），
        # 不缓存失败结果的话，整批 68 只票会各自重试一遍，直接拖垮同步。
        logger.debug(f"资金流快照({kind})失败，5 分钟内不再重试: {exc}")
        df = pd.DataFrame()
    ttl = _FF_SNAPSHOT_TTL_OK if not df.empty else _FF_SNAPSHOT_TTL_FAIL
    try:
        with _RATE_LOCK:
            _FF_SNAPSHOT_CACHE[key] = (df, time.monotonic() + ttl)
            _FF_INFLIGHT.pop(key, None)
    finally:
        ev.set()   # 唤醒所有等待者（哪怕取数异常也要唤醒，否则它们干等 120s）
    return df


def _ff_anchor_day() -> date:
    """快照兜底合成序列的"最新一天"锚点。

    快照接口只给 即时/3日/5日/10日 的净额，**不带日期**，所以日期要自己造。
    直接用 `date.today()` 会在周末/节假日造出"非交易日的数据行" ——
    周日刷新一下，库里就多出周日的资金流，进而把"最新评分日"顶成周日，
    列表和刷新口径全乱。所以锚到**最近一个交易日**。
    """
    try:
        from app.domain.calendar import TradeCalendar
        return TradeCalendar().last_trading_day()
    except Exception as exc:  # 日历不可用也要能降级
        logger.debug(f"交易日历不可用，退回最近工作日: {exc}")
        d = date.today()
        while d.weekday() >= 5:  # 5=周六 6=周日
            d -= timedelta(days=1)
        return d


def _ff_span_days(anchor: date, max_off: int) -> List[date]:
    """从锚点日往前取 ``max_off + 1`` 个**交易日**（升序，最后一个是锚点）。

    快照只给"即时/3日/5日/10日"四个视角的净额、不带日期，所以序列的日期
    要自己造。锚点已经锚到最近交易日了，但**往前推的时候也必须按交易日推**：
    直接 ``anchor - timedelta(days=off)`` 会把周末和节假日也算进去，
    于是库里多出"周六的资金流"，把"最新评分日"顶成周六，
    列表口径和刷新判断跟着一起乱。
    """
    try:
        from app.domain.calendar import TradeCalendar
        got = TradeCalendar().recent_trading_days(n=max_off + 1, until=anchor)
        if got:
            return got
    except Exception as exc:      # 日历不可用也要能降级
        logger.debug(f"交易日历不可用，按自然日跳过周末: {exc}")
    out: List[date] = []
    d = anchor
    while len(out) <= max_off:
        out.append(d)
        d -= timedelta(days=1)
        while d.weekday() >= 5:   # 5=周六 6=周日
            d -= timedelta(days=1)
    return sorted(out)


def _fund_flow_from_snapshot(code: str, days: Optional[int]) -> pd.DataFrame:
    """从全市场快照反推单只票资金流序列（降级模式）。

    快照只有 即时/3日/5日/10日 四个视角的主力净额，没有逐日明细，
    所以这里**合成**一条 4 点短序列：用四个视角的净额近似成趋势，
    让 资金链 维度的 net_days/accel/pulse 不至于完全失效；
    核心的 `main_net_pct`（即时）则精确取自快照。

    这是一条**降级路径**，主源（逐日真实明细）可用时不会走到这里，
    所以序列里的值是近似的：多周期视角是区间累计值，已摊成区间日均
    （见 ``windows``），且只有"即时"那天有涨跌幅与净占比。
    """
    code = normalize_symbol(code)
    kinds = ["即时", "3日", "5日", "10日"]
    frames: Dict[str, pd.DataFrame] = {}
    for k in kinds:
        try:
            frames[k] = _ff_snapshot(k)
        except Exception as exc:
            logger.debug(f"资金流快照({k})失败: {exc}")
    # 只保留真正拿到数据的视角（空表 = 该视角本轮失败的负缓存）
    ok_frames = {k: v for k, v in frames.items() if v is not None and not v.empty}
    if not ok_frames:
        # 一个视角都没拿到 = 源不可用（网络/代理级问题），不是"这只票没数据"
        raise MarketXError("资金流快照不可用（全部视角均失败）")

    # 注意：fetch_fund_flow_rank 已把列归一化为
    # [symbol, name, pct_change, main_net, main_net_pct]，
    # 所以这里两套列名（归一化 + 原始中文）都要认。
    # 注意：DataFrame 不能直接用 `or` 取兜底（会触发 truth value 歧义报错）
    latest = ok_frames.get("即时")
    if latest is None:
        latest = next(iter(ok_frames.values()))
    row0 = None
    if latest is not None and not latest.empty:
        col_code = _pick(latest, ["symbol", "代码", "股票代码", "code"])
        if col_code is not None:
            sub = latest[latest[col_code].apply(lambda v: normalize_symbol(v) == code)]
            if not sub.empty:
                row0 = sub.iloc[0]
    if row0 is None:
        raise MarketXError(f"资金流快照中未找到 {code}")
    main = _num(row0.get(_pick(latest, ["main_net", "主力净流入-净额", "主力净额", "净额"])))
    main_pct = _num(row0.get(_pick(latest, ["main_net_pct", "主力净流入-净占比", "净占比"])))
    pct_change = _num(row0.get(_pick(latest, ["pct_change", "涨跌幅"])))

    today = _ff_anchor_day()  # 锚到最近交易日，别在周末造出非交易日数据行
    offsets = {"即时": 0, "3日": 2, "5日": 4, "10日": 9}
    # 各视角的**统计窗口天数**。3日/5日/10日 给的是区间**累计**净额，
    # 不是单日净额 —— 不摊开的话，"10 天累计 5 亿"会被当成某一天流入 5 亿，
    # 与"即时"的单日值差一到两个数量级，下游算 accel/pulse 时彻底失真。
    # 摊成"区间日均"后四个点落在同一量级上，趋势可用（仍是近似，但可比）。
    windows = {"即时": 1, "3日": 3, "5日": 5, "10日": 10}
    # 往前推也要按交易日推，见 _ff_span_days 的说明
    span = _ff_span_days(today, max(offsets.values()))
    rows: List[Dict[str, Any]] = []
    for k, off in offsets.items():
        fr = frames.get(k)
        if fr is None or fr.empty:  # 空表 = 该视角本轮失败的负缓存
            continue
        if k not in ok_frames:
            continue
        col_code = _pick(fr, ["symbol", "代码", "股票代码", "code"])
        col_main = _pick(fr, ["main_net", "主力净流入-净额", "主力净额", "净额"])
        if col_code is None or col_main is None:
            continue
        sub = fr[fr[col_code].apply(lambda v: normalize_symbol(v) == code)]
        if sub.empty:
            continue
        val = _num(sub.iloc[0].get(col_main))
        win = windows.get(k, 1)
        # 累计净额 → 区间日均（见 windows 的说明）
        if val is not None and win > 1:
            val = val / win
        rows.append({
            "symbol": code,
            "date": span[len(span) - 1 - off] if len(span) > off
                    else span[0],
            "close": None,
            "pct_change": pct_change if off == 0 else None,
            "main_net": val,
            "main_net_pct": main_pct if off == 0 else None,
            "super_large_net": None, "large_net": None, "medium_net": None,
            "small_net": None, "turnover": None, "amount": None,
            "updated_at": datetime.now(),
        })
    rows.sort(key=lambda x: x["date"])
    out = pd.DataFrame(rows)
    if out.empty:
        raise MarketXError("资金流快照兜底解析后为空")
    if days and len(out) > days:
        out = out.tail(int(days)).reset_index(drop=True)
    return out


def _to_tushare_code(symbol: str) -> str:
    code = normalize_symbol(symbol)
    if code.endswith((".SH", ".SZ", ".BJ")):
        return code
    return f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"


def _fund_flow_tushare(symbol: str, days: Optional[int] = None) -> pd.DataFrame:
    """Tushare moneyflow 兜底源（不依赖东财，独立 API）。

    需配置环境变量 TUSHARE_TOKEN 且安装 tushare（免费注册约 120 积分，
    moneyflow 接口需 2000 积分）。字段映射：net_mf_amount→main_net(元)；
    四档买卖额之和→成交额，net_mf_amount/成交额→main_net_pct。

    异常语义（注册表靠它决定要不要熔断）：
      * ``DataUnavailable`` —— 没配 token / 没装库 / 接口返回空：源没坏，跳过即可；
      * ``SourceError``     —— 调用失败（网络、鉴权、积分不足）：源坏了，计入熔断。
    """
    code = normalize_symbol(symbol)
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise DataUnavailable("未配置 TUSHARE_TOKEN，跳过 Tushare 资金流兜底")
    try:
        import tushare as ts
    except Exception:
        raise DataUnavailable("未安装 tushare，无法用其作资金流兜底源")
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=int(days or 60) * 2 + 10)).strftime("%Y%m%d")
    try:
        pro = ts.pro_api(token)
        df = pro.moneyflow(ts_code=_to_tushare_code(code), start_date=start, end_date=end)
    except Exception as exc:
        raise SourceError(f"Tushare moneyflow 调用失败: {type(exc).__name__}: {exc}") from exc
    if df is None or df.empty:
        raise DataUnavailable("Tushare moneyflow 返回空")
    col_date = _pick(df, ["trade_date"])
    col_net = _pick(df, ["net_mf_amount"])
    if col_date is None or col_net is None:
        raise MarketXError("Tushare moneyflow 缺少必要列")
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        d = _to_date(r.get(col_date))
        if d is None:
            continue
        net = _num(r.get(col_net))
        amt = sum(
            _num(r.get(c)) or 0.0
            for c in ["buy_sm_amount", "sell_sm_amount", "buy_md_amount",
                      "sell_md_amount", "buy_lg_amount", "sell_lg_amount",
                      "buy_elg_amount", "sell_elg_amount"]
        )
        pct = net / amt if (net is not None and amt) else None
        rows.append({
            "symbol": code,
            "date": d,
            "close": None, "pct_change": None,
            "main_net": net,
            "main_net_pct": pct,
            "super_large_net": (_num(r.get("buy_elg_amount")) or 0) - (_num(r.get("sell_elg_amount")) or 0),
            "large_net": (_num(r.get("buy_lg_amount")) or 0) - (_num(r.get("sell_lg_amount")) or 0),
            "medium_net": (_num(r.get("buy_md_amount")) or 0) - (_num(r.get("sell_md_amount")) or 0),
            "small_net": (_num(r.get("buy_sm_amount")) or 0) - (_num(r.get("sell_sm_amount")) or 0),
            "turnover": None,
            "amount": amt or None,
            "updated_at": datetime.now(),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        raise MarketXError("Tushare moneyflow 解析后为空")
    out = out.sort_values("date").reset_index(drop=True)
    if days and len(out) > days:
        out = out.tail(int(days)).reset_index(drop=True)
    return out


def _fund_flow_akshare(symbol: str, days: Optional[int] = None) -> pd.DataFrame:
    """akshare 源：主源(逐日 push2his) + 全市场快照兜底 + 主源熔断。

    这里有两级熔断，粒度不同，别混为一谈：
      * **主源级**（本函数内的 `_FF_PRIMARY_DEAD_UNTIL`）：push2his 这一个域名
        被代理拦了，就别让整批 68 只票各自重试 3 次，直接走快照兜底；
      * **源级**（注册表里的 `SourceRegistry`）：akshare 整源（含快照）都不可用
        时才熔断，交给注册表去切 Tushare。

    异常语义：快照里查不到这只票 → ``DataUnavailable``（源没坏）；
    主源和快照都拿不到 → ``SourceError``（源坏了）。
    """
    global _FF_PRIMARY_DEAD_UNTIL
    code = normalize_symbol(symbol)
    now = time.monotonic()
    if now >= _FF_PRIMARY_DEAD_UNTIL:
        try:
            df = _call("stock_individual_fund_flow", stock=code,
                       market=_market_of(code), limit_key="fundflow",
                       retry=_registry_retry())
            parsed = _parse_fund_flow(df, code)
            if not parsed.empty:
                return parsed.tail(int(days)).reset_index(drop=True) if days else parsed
        except MarketXError as exc:
            if is_transient(exc):
                # 主源(东财 push2his)疑似被代理/网络拦截：熔断 10 分钟，
                # 同批次剩余票直接走快照兜底
                _FF_PRIMARY_DEAD_UNTIL = time.monotonic() + 600
                log_throttled("DEBUG",
                              "资金流主源(push2his)疑似被代理/网络拦截，10 分钟内切快照兜底",
                              key="ff-primary-dead")
            # 主源失败 → 快照兜底
    try:
        return _fund_flow_from_snapshot(code, days)
    except MarketXError as exc:
        if "未找到" in str(exc):
            raise DataUnavailable(str(exc)) from exc
        raise SourceError(f"akshare 资金流不可用: {exc}") from exc


def fetch_fund_flow(symbol: str, days: Optional[int] = None) -> pd.DataFrame:
    """个股历史资金流（日频）。

    **不再自己写 if/else 切源** —— 走 `app.providers.market.compat.registry.REGISTRY`：
    按 `DATA_SOURCE_PRIORITY` 顺序依次尝试所有注册了 `fund_flow` 能力的源，
    第一个失败就自动换下一个（源级故障还会熔断该源一段时间）。

    内置已注册：akshare（主源 push2his 逐日 + 全市场快照兜底）、
    tushare（moneyflow，需 TUSHARE_TOKEN）。

    :return: 列 = [date, close, pct_change, main_net, main_net_pct,
             super_large_net, large_net, medium_net, small_net, turnover, amount]
    """
    try:
        return data_service.get_fund_flow_df(symbol, days=days)
    except ProviderError as exc:
        # 统一成 marketx 的对外错误类型，保持既有调用方的契约
        raise MarketXError(str(exc)) from exc


# ===========================================================================
# 内置数据源注册
# ===========================================================================
# 在这里注册 = 注册到引擎里。引擎本来就 import 了 marketx，
# 所以新增一个源只需要：写适配器 + 下面加一行 register，不用改引擎代码。
REGISTRY.register("akshare", supports=["fund_flow"], fetch=_fund_flow_akshare,
                  hosts=["push2his.eastmoney.com"])
REGISTRY.register("tushare", supports=["fund_flow"], fetch=_fund_flow_tushare)


#: 视角别名 -> akshare ``stock_fund_flow_individual`` 认的规范名。
#:
#: 这个接口**只认** ``即时`` / ``3日排行`` / ``5日排行`` / ``10日排行`` 四个值，
#: 内部是 ``if symbol == "3日排行": ... else: 即时`` 的等值判断 —— 传 ``"3日"``
#: 会静默落到 else 分支：用**即时**的 URL 拉回 10 列数据，
#: 却按多周期的 7 个列名去赋值，于是必然抛
#: ``ValueError: Length mismatch: Expected axis has 10 elements, new values have 7``。
#: 表现为"资金榜 3/5/10 日视角天天挂、只剩即时"，且错误被记成 parse_error，
#: 看起来像东财改版，实际是参数名差一个"排行"二字。
_FLOW_RANK_KIND_ALIASES: Dict[str, str] = {
    "即时": "即时", "今日": "即时", "实时": "即时", "当日": "即时",
    "3日排行": "3日排行", "3日": "3日排行", "3": "3日排行",
    "5日排行": "5日排行", "5日": "5日排行", "5": "5日排行",
    "10日排行": "10日排行", "10日": "10日排行", "10": "10日排行",
}


def _flow_rank_kind(kind: Any) -> str:
    """把各种写法的视角归一成 akshare 认的规范名，认不出就退回"即时"。"""
    text = str(kind or "即时").strip()
    return _FLOW_RANK_KIND_ALIASES.get(text, "即时")


def _flow_rank_akshare(rank_kind: str = "即时",
                       top: Optional[int] = None) -> pd.DataFrame:
    """资金榜源：全市场资金流排名（`stock_fund_flow_individual`）。

    参数叫 ``rank_kind`` 而不是 ``kind``：注册表 ``fetch(kind, ...)`` 的
    第一个位置参数已经叫 kind，适配器再叫 kind 会撞名。

    注意两个视角的**返回列不一样**，列候选必须两套都认：
      * ``即时``：10 列，含 ``成交额``，能自算净占比；
      * ``3/5/10日排行``：7 列，只有 ``资金流入净额(元)``，**没有成交额**，
        净占比算不出来，留 None（上层合成序列时该项缺失是可以接受的，
        总比整个视角拿不到强）。
    """
    df = _call("stock_fund_flow_individual",
               symbol=_flow_rank_kind(rank_kind),
               limit_key="fundflow", retry=_registry_retry())
    col_code = _pick(df, ["代码", "股票代码", "code"])
    col_name = _pick(df, ["名称", "股票简称"])
    # 即时视角叫"涨跌幅"，多周期视角叫"阶段涨跌幅"
    col_pct = _pick(df, ["涨跌幅", "阶段涨跌幅"])
    col_main = _pick(df, ["主力净流入-净额", "主力净额", "净额",
                          "资金流入净额(元)", "资金流入净额"])
    col_main_pct = _pick(df, ["主力净流入-净占比", "净占比"])
    col_amount = _pick(df, ["成交额", "成交金额"])
    if col_code is None:
        raise MarketXError("资金流排名缺少代码列")

    rows = []
    for _, r in df.iterrows():
        sym = normalize_symbol(r.get(col_code))
        if not sym:
            continue
        main = _num(r.get(col_main)) if col_main else None
        amount = _num(r.get(col_amount)) if col_amount else None
        pct = _num(r.get(col_main_pct)) if col_main_pct else None
        # 东财这个排名接口只给"净额/成交额"，**没有**净占比列。
        # 占比恒为 None 会让 资金链 维度整列失效，
        # 所以这里用"净额 / 成交额"补（与逐日接口同款兜底）。
        if pct is None and main is not None and amount:
            pct = main / amount
        rows.append({
            "symbol": sym,
            "name": str(r.get(col_name) or "") if col_name else "",
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
            "main_net": main,
            "main_net_pct": pct,
        })
    out = pd.DataFrame(rows).drop_duplicates(subset=["symbol"]).reset_index(drop=True)
    if out.empty:
        raise DataUnavailable("资金流排名解析后为空")
    out = out.sort_values("main_net", ascending=False).reset_index(drop=True)
    if top:
        out = out.head(int(top))
    return out


REGISTRY.register("akshare_flow", supports=["flow_rank"], fetch=_flow_rank_akshare,
                  hosts=["push2.eastmoney.com"])


def fetch_fund_flow_rank(kind: str = "即时", top: Optional[int] = None,
                         retries: Optional[int] = None) -> pd.DataFrame:
    """全市场资金流排名（当日）。`kind` 可传 "即时"/"3日"/"5日"/"10日"。

    同样走 `REGISTRY`：重试 N 次 → 换源 → 全挂告警，日志会写明用了哪个源。

    :param retries: 覆盖重试次数。批量兜底场景（见 ``_ff_snapshot``）传 0，
        避免外层已在重试时再放大请求数。
    """
    try:
        return data_service.get_flow_rank_df(kind, top=top, retries=retries)
    except ProviderError as exc:
        raise MarketXError(str(exc)) from exc


# ===========================================================================
# 筹码分布
# ===========================================================================
def fetch_chip(symbol: str, adjust: str = "") -> pd.DataFrame:
    """筹码分布（日频）。

    `stock_cyq_em` 返回每交易日的：获利比例、平均成本、
    90%/70% 成本区间与集中度。主筹码峰 `peak_price` 由 90% 区间的
    加权中点近似 —— 严格的筹码峰需要密度曲线，接口只给区间，
    这里用区间中点 + 集中度修正，够用于"现价离密集区多远"的判断。
    """
    code = normalize_symbol(symbol)
    # 走熔断包装：源不通时，整批剩余票直接跳过，而不是各自重试 3 次
    df = _breaker_guard(
        "chip",
        lambda: _call("stock_cyq_em", symbol=code, adjust=adjust,
                      limit_key="chip"),
        what="筹码分布",
    )

    col_date = _pick(df, ["日期", "date"])
    col_profit = _pick(df, ["获利比例", "获利盘"])
    col_avg = _pick(df, ["平均成本"])
    col_90l = _pick(df, ["90成本-低", "90成本区间-低", "90成本低"])
    col_90h = _pick(df, ["90成本-高", "90成本区间-高", "90成本高"])
    col_90c = _pick(df, ["90集中度"])
    col_70l = _pick(df, ["70成本-低"])
    col_70h = _pick(df, ["70成本-高"])
    col_70c = _pick(df, ["70集中度"])
    if col_date is None:
        raise MarketXError("筹码分布缺少日期列")

    rows = []
    for _, r in df.iterrows():
        d = _to_date(r.get(col_date))
        if d is None:
            continue
        lo = _num(r.get(col_90l)) if col_90l else None
        hi = _num(r.get(col_90h)) if col_90h else None
        peak = None
        if lo is not None and hi is not None and hi >= lo:
            # 区间中点作为主峰位置；集中度越小说明越集中，峰越"尖"
            peak = (lo + hi) / 2.0
        rows.append({
            "symbol": code,
            "date": d,
            "profit_ratio": _num(r.get(col_profit)) if col_profit else None,
            "avg_cost": _num(r.get(col_avg)) if col_avg else None,
            "cost_90_low": lo,
            "cost_90_high": hi,
            "cost_70_low": _num(r.get(col_70l)) if col_70l else None,
            "cost_70_high": _num(r.get(col_70h)) if col_70h else None,
            "concentration_90": _num(r.get(col_90c)) if col_90c else None,
            "concentration_70": _num(r.get(col_70c)) if col_70c else None,
            "peak_price": peak,
            "updated_at": datetime.now(),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        raise MarketXError("筹码分布解析后为空")
    return out.sort_values("date").reset_index(drop=True)


# ===========================================================================
# 涨停池
# ===========================================================================
def _zt_pool_one(day: date) -> pd.DataFrame:
    """拉单个交易日的涨停池并完成解析。"""
    df = _call("stock_zt_pool_em", date=day.strftime("%Y%m%d"),
               limit_key="zt", retry=1)
    return _parse_limitup_pool(df, day)


def fetch_limitup_pool(trade_date: Optional[str] = None) -> pd.DataFrame:
    """涨停池。`trade_date` 格式 YYYYMMDD，留空取**最近有数据的交易日**。

    为什么留空时要回退而不是只试今天：涨停池是**收盘后**才落定的数据，
    开盘日一早去取当天，接口返回的必然是空 —— 这不是接口变更，也不是
    数据源坏了。以前直接把"当日为空"抛成错误，于是每个交易日早上
    固定报一次失败，而库里其实已经有上一交易日的数据了。

    所以：从今天往前最多找 3 个交易日，取第一个有数据的；
    都没有才是真的没数据（``DataUnavailable``，调用方按"暂无"跳过）。
    """
    if trade_date:
        df = _call("stock_zt_pool_em", date=str(trade_date), limit_key="zt")
        return _parse_limitup_pool(df, _to_date(trade_date) or date.today())

    from app.domain.calendar import get_calendar

    cal = get_calendar()
    errors: List[str] = []
    # recent_trading_days 升序，倒着走 = 最近的先试
    for day in reversed(cal.recent_trading_days(3)):
        try:
            return _zt_pool_one(day)
        except MarketXError as exc:
            errors.append(f"{day}: {str(exc)[:60]}")
            continue
    raise DataUnavailable(
        f"涨停池最近 3 个交易日均无数据（{'；'.join(errors)}）"
    )


def _parse_limitup_pool(df: pd.DataFrame, d: date) -> pd.DataFrame:
    """解析涨停池返回（列名可能随 akshare 版本变化，全部按关键词找）。"""

    col_code = _pick(df, ["代码", "股票代码"])
    col_name = _pick(df, ["名称", "股票简称"])
    col_pct = _pick(df, ["涨跌幅"])
    col_close = _pick(df, ["最新价"])
    col_amount = _pick(df, ["成交额"])
    col_seal = _pick(df, ["封板资金"])
    col_first = _pick(df, ["首次封板时间"])
    col_last = _pick(df, ["最后封板时间"])
    col_broken = _pick(df, ["炸板次数"])
    col_stat = _pick(df, ["涨停统计"])
    col_cont = _pick(df, ["连板数", "连板"])
    col_ind = _pick(df, ["所属行业"])
    if col_code is None:
        raise MarketXError("涨停池缺少代码列")

    rows = []
    for _, r in df.iterrows():
        sym = normalize_symbol(r.get(col_code))
        if not sym:
            continue
        rows.append({
            "date": d,
            "symbol": sym,
            "name": str(r.get(col_name) or "") if col_name else "",
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
            "close": _num(r.get(col_close)) if col_close else None,
            "amount": _num(r.get(col_amount)) if col_amount else None,
            "seal_amount": _num(r.get(col_seal)) if col_seal else None,
            "first_seal_at": str(r.get(col_first) or "") if col_first else "",
            "last_seal_at": str(r.get(col_last) or "") if col_last else "",
            "broken_times": int(_num(r.get(col_broken)) or 0) if col_broken else 0,
            "limit_stat": str(r.get(col_stat) or "") if col_stat else "",
            "continuous": int(_num(r.get(col_cont)) or 0) if col_cont else 0,
            "industry": str(r.get(col_ind) or "") if col_ind else "",
            "updated_at": datetime.now(),
        })
    out = pd.DataFrame(rows).drop_duplicates(subset=["date", "symbol"]).reset_index(drop=True)
    if out.empty:
        raise MarketXError("涨停池解析后为空")
    return out


# ===========================================================================
# 题材 / 板块
# ===========================================================================
def fetch_boards(kind: str = "concept") -> pd.DataFrame:
    """板块（题材/行业）当日表现。`kind` = concept / industry。"""
    name = "stock_board_concept_name_em" if kind == "concept" else "stock_board_industry_name_em"
    df = _call(name, limit_key="board")

    col_board = _pick(df, ["板块名称", "板块", "行业", "名称"])
    col_pct = _pick(df, ["涨跌幅"])
    col_turnover = _pick(df, ["换手率"])
    col_up = _pick(df, ["上涨家数"])
    col_down = _pick(df, ["下跌家数"])
    col_lead = _pick(df, ["领涨股票"])
    col_rank = _pick(df, ["排名"])
    if col_board is None:
        raise MarketXError(f"{name} 缺少板块名列")

    d = date.today()
    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        board = str(r.get(col_board) or "").strip()
        if not board:
            continue
        rows.append({
            "date": d,
            "board": board,
            "kind": kind,
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
            "turnover": _num(r.get(col_turnover)) if col_turnover else None,
            "up_count": int(_num(r.get(col_up)) or 0) if col_up else None,
            "down_count": int(_num(r.get(col_down)) or 0) if col_down else None,
            "leading_stock": str(r.get(col_lead) or "") if col_lead else "",
            # 接口自带的"排名"就是按涨幅排的，直接用；没有就用行号兜底
            "rank": int(_num(r.get(col_rank)) or (i + 1)) if col_rank else (i + 1),
            "updated_at": datetime.now(),
        })
    out = pd.DataFrame(rows).drop_duplicates(subset=["board"]).reset_index(drop=True)
    if out.empty:
        raise MarketXError("板块数据解析后为空")
    return out


def fetch_board_fund_flow(kind: str = "concept") -> pd.DataFrame:
    """板块资金流。`kind` = concept / industry。"""
    name = "stock_fund_flow_concept" if kind == "concept" else "stock_fund_flow_industry"
    df = _call(name, symbol="即时", limit_key="board")

    col_board = _pick(df, ["行业", "板块", "名称"])
    col_pct = _pick(df, ["涨跌幅"])
    col_net = _pick(df, ["净额", "主力净流入-净额"])
    if col_board is None or col_net is None:
        raise MarketXError(f"{name} 返回字段不完整")

    rows = []
    for _, r in df.iterrows():
        board = str(r.get(col_board) or "").strip()
        if not board:
            continue
        rows.append({
            "board": board,
            "kind": kind,
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
            "main_net": _num(r.get(col_net)),
        })
    return pd.DataFrame(rows).drop_duplicates(subset=["board"]).reset_index(drop=True)


def fetch_concept_cons(board: str) -> pd.DataFrame:
    """概念板块成分股。"""
    df = _call("stock_board_concept_cons_em", symbol=str(board), limit_key="board")
    col_code = _pick(df, ["代码", "股票代码"])
    col_name = _pick(df, ["名称", "股票简称"])
    col_pct = _pick(df, ["涨跌幅"])
    if col_code is None:
        raise MarketXError("概念成分股缺少代码列")
    rows = []
    for _, r in df.iterrows():
        sym = normalize_symbol(r.get(col_code))
        if not sym:
            continue
        rows.append({
            "symbol": sym,
            "name": str(r.get(col_name) or "") if col_name else "",
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
            "board": str(board),
        })
    return pd.DataFrame(rows).drop_duplicates(subset=["symbol"]).reset_index(drop=True)


# ===========================================================================
# 人气榜 / 全市场快照
# ===========================================================================
def _parse_hot_rank(df: pd.DataFrame) -> pd.DataFrame:
    """把任意"热门/人气"榜单解析成统一结构 [symbol, name, rank, pct_change]。

    各源列名各不相同（东财"当前排名"、雪球"关注人数"……），这里统一宽松匹配，
    匹配不到就留空 —— **排名缺失不影响入选**，只影响排序粒度。
    """
    col_code = _pick(df, ["代码", "股票代码", "symbol", "code"])
    col_name = _pick(df, ["股票名称", "股票简称", "名称", "name"])
    col_rank = _pick(df, ["当前排名", "排名", "关注人数", "rank"])
    col_pct = _pick(df, ["涨跌幅", "pct_change"])
    if col_code is None:
        raise MarketXError("人气榜缺少代码列")
    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        sym = normalize_symbol(r.get(col_code))
        if not sym:
            continue
        rows.append({
            "symbol": sym,
            "name": str(r.get(col_name) or "") if col_name else "",
            "rank": int(_num(r.get(col_rank)) or 0) if col_rank else i + 1,
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
        })
    return pd.DataFrame(rows).drop_duplicates(subset=["symbol"]).reset_index(drop=True)


def _hot_rank_akshare_em(top: Optional[int] = None) -> pd.DataFrame:
    """人气榜源 1：东财人气榜（`stock_hot_rank_em`，push2 域名）。"""
    df = _call("stock_hot_rank_em", limit_key="hot", retry=_registry_retry())
    out = _parse_hot_rank(df)
    if out.empty:
        raise DataUnavailable("东财人气榜解析后为空")
    return out.head(int(top)) if top else out


def _hot_rank_xueqiu(top: Optional[int] = None) -> pd.DataFrame:
    """人气榜源 2：雪球关注热度（`stock_hot_follow_xq`）。

    与东财**不同域名**，东财被代理拦时它可能是唯一还能通的源。
    """
    df = _call("stock_hot_follow_xq", "最热门", limit_key="hot",
               retry=_registry_retry())
    out = _parse_hot_rank(df)
    if out.empty:
        raise DataUnavailable("雪球关注热度解析后为空")
    return out.head(int(top)) if top else out


def _hot_rank_spot(top: Optional[int] = None) -> pd.DataFrame:
    """人气榜源 3（最后兜底）：全市场快照按成交额降序。

    严格说这不是"人气榜"，但**成交额前列 = 市场关注度前列**，
    在所有人气榜源都挂掉时，用它保住候选池不至于空 —— 会在日志里标注降级。
    """
    df = _call("stock_zh_a_spot_em", limit_key="spot", retry=_registry_retry())
    col_code = _pick(df, ["代码", "股票代码"])
    col_name = _pick(df, ["名称", "股票简称"])
    col_amount = _pick(df, ["成交额"])
    col_pct = _pick(df, ["涨跌幅"])
    if col_code is None:
        raise DataUnavailable("全市场快照缺少代码列，无法做人气兜底")
    rows = []
    for _, r in df.iterrows():
        sym = normalize_symbol(r.get(col_code))
        if not sym:
            continue
        rows.append({
            "symbol": sym,
            "name": str(r.get(col_name) or "") if col_name else "",
            "amount": _num(r.get(col_amount)) if col_amount else None,
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        raise DataUnavailable("全市场快照解析后为空")
    out = out.sort_values("amount", ascending=False).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    out = out[["symbol", "name", "rank", "pct_change"]]
    return out.head(int(top)) if top else out


# 人气榜的**可用**源：只走 emappdata（实测稳定），名称/涨跌幅用腾讯批量报价补
from app.providers.market.compat.hot_sources import _hot_rank_emapp  # noqa: E402  注册用


# 人气榜的源：**emapp_hot 排第一**，因为它走的是实测稳定的 emappdata 域名。
# akshare 那个同名函数内部还要再请求一次 push2（被代理拦死），等于自带一条死路；
# akshare_spot 压在 82.push2 上，也是同一个死路。三者都声明了 hosts，
# 域名一旦被网络层判死就直接跳过，不再各等一遍超时。
# 想调整顺序，把源名写进 settings.DATA_SOURCE_PRIORITY 即可（见 docs/DATA_SOURCES.md）。
REGISTRY.register("emapp_hot", supports=["hot_rank"], fetch=_hot_rank_emapp,
                  hosts=["emappdata.eastmoney.com", "qt.gtimg.cn"])
REGISTRY.register("akshare_em", supports=["hot_rank"], fetch=_hot_rank_akshare_em,
                  hosts=["push2.eastmoney.com"])
REGISTRY.register("xueqiu_hot", supports=["hot_rank"], fetch=_hot_rank_xueqiu,
                  hosts=["stock.xueqiu.com"])
REGISTRY.register("akshare_spot", supports=["hot_rank"], fetch=_hot_rank_spot,
                  hosts=["82.push2.eastmoney.com"])


def fetch_hot_rank(top: Optional[int] = None) -> pd.DataFrame:
    """人气榜（热度）。排名的绝对值意义不大，**排名变化**才有意义。

    走 `REGISTRY`：东财人气榜 → 雪球关注热度 → 全市场快照（成交额代理），
    每个源先重试 `DATA_FETCH_MAX_RETRY` 次，都失败才换下一个，
    全挂时注册表会打 WARNING 告警并抛出带各源原因的异常。
    """
    try:
        return data_service.get_hot_rank_df(top=top)
    except ProviderError as exc:
        raise MarketXError(str(exc)) from exc


def fetch_spot() -> pd.DataFrame:
    """全市场即时快照（股票池全市场初筛用）。"""
    df = _call("stock_zh_a_spot_em", limit_key="spot")
    col_code = _pick(df, ["代码", "股票代码"])
    col_name = _pick(df, ["名称", "股票简称"])
    col_price = _pick(df, ["最新价"])
    col_pct = _pick(df, ["涨跌幅"])
    col_amount = _pick(df, ["成交额"])
    col_turnover = _pick(df, ["换手率"])
    if col_code is None:
        raise MarketXError("全市场快照缺少代码列")
    rows = []
    for _, r in df.iterrows():
        sym = normalize_symbol(r.get(col_code))
        if not sym:
            continue
        rows.append({
            "symbol": sym,
            "name": str(r.get(col_name) or "") if col_name else "",
            "close": _num(r.get(col_price)) if col_price else None,
            "pct_change": _num(r.get(col_pct)) if col_pct else None,
            "amount": _num(r.get(col_amount)) if col_amount else None,
            "turnover": _num(r.get(col_turnover)) if col_turnover else None,
        })
    return pd.DataFrame(rows).drop_duplicates(subset=["symbol"]).reset_index(drop=True)


# ===========================================================================
# 交易日历
# ===========================================================================
def fetch_trade_dates() -> List[str]:
    """全部交易日（YYYY-MM-DD 升序）。

    一年只变一次的东西，联网拉一次写进本地缓存就够（见
    ``app.domain.calendar.refresh_from_network``）。
    拉不到就抛异常交给调用方降级 —— **绝不返回"空日历"**，
    那会让所有日子都变成非交易日，从此再也不采数据了。
    """
    ak = _require_akshare()
    df = ak.tool_trade_date_hist_sina()
    col = _pick(df, ["trade_date"])
    if col is None:
        col = df.columns[0]
    out: List[str] = []
    for v in df[col].tolist():
        d = _to_date(v)
        if d:
            out.append(str(d))
    return sorted(set(out))


# ===========================================================================
# 基本面（估值 / 成长 / 质量）
# ===========================================================================
#: 乐咕估值接口名。akshare 1.18 之后**整个接口被移除**（不是改名），
#: 所以走之前必须先 hasattr 探测，不能假设它存在。
_LEGACY_VALUATION_API = "stock_a_indicator_lg"
#: 百度估值（``stock_zh_valuation_baidu``）支持的指标 → 输出字段。
#: 实测只有这三项能用（"市销率/股息率/流通市值" 在接口内部返回 None）。
_BAIDU_VAL_INDICATORS: Tuple[Tuple[str, str], ...] = (
    ("市盈率(TTM)", "pe"),
    ("市净率", "pb"),
    ("总市值", "market_cap"),
)
#: 百度"总市值"的单位是**亿元**，库里统一存元
_BAIDU_MV_UNIT = 1e8


def _valuation_legacy(symbol: str, code: str) -> Dict[str, Any]:
    """乐咕乐股估值（老 akshare）。字段最全：PE/PB/PS/股息率/总市值/流通市值。"""
    df = _call(_LEGACY_VALUATION_API, symbol=code, limit_key="valuation")
    col_date = _pick(df, ["trade_date", "日期"])
    col_pe = _pick(df, ["pe", "pe_ttm"])
    col_pb = _pick(df, ["pb"])
    col_ps = _pick(df, ["ps_ttm", "ps"])
    col_dv = _pick(df, ["dv_ratio", "股息率"])
    col_mv = _pick(df, ["total_mv", "总市值"])        # 单位：万元
    col_cmv = _pick(df, ["circ_mv", "流通市值"])
    if df.empty:
        raise MarketXError(f"估值数据为空: {symbol}")
    df = df.sort_values(by=col_date) if col_date else df
    last = df.iloc[-1]
    return {
        "symbol": normalize_symbol(symbol),
        "date": _to_date(last.get(col_date)) if col_date else None,
        "pe": _num(last.get(col_pe)) if col_pe else None,
        "pb": _num(last.get(col_pb)) if col_pb else None,
        "ps": _num(last.get(col_ps)) if col_ps else None,
        "dividend_yield": _num(last.get(col_dv)) if col_dv else None,
        # 源数据单位是万元，统一成元存库 —— 单位混用是最难查的一类错
        "market_cap": (_num(last.get(col_mv)) or 0) * 10000 if col_mv else None,
        "float_market_cap": (_num(last.get(col_cmv)) or 0) * 10000 if col_cmv else None,
    }


def _valuation_baidu(symbol: str, code: str) -> Dict[str, Any]:
    """百度估值兜底（akshare 1.18+ 乐咕接口被移除后的替代）。

    一次只能取一个指标，所以这里按 `pe / pb / 总市值` 各调一次，
    取各自序列的最后一条。**单个指标拿不到不整体失败** —— 少一个 PS
    比整个估值维度全空要好得多。
    """
    out: Dict[str, Any] = {
        "symbol": normalize_symbol(symbol), "date": None,
        "pe": None, "pb": None, "ps": None, "dividend_yield": None,
        "market_cap": None, "float_market_cap": None,
    }
    last_date: Optional[date] = None
    errors: List[str] = []
    for indicator, field in _BAIDU_VAL_INDICATORS:
        try:
            df = _call("stock_zh_valuation_baidu", symbol=code,
                       indicator=indicator, period="近一年",
                       limit_key="valuation", retry=1)
        except MarketXError as exc:
            errors.append(f"{indicator}: {str(exc)[:60]}")
            continue
        col_date = _pick(df, ["date", "日期"])
        col_val = _pick(df, ["value", "值"])
        if col_val is None:
            errors.append(f"{indicator}: 返回缺少数值列")
            continue
        df = df.sort_values(by=col_date) if col_date else df
        last = df.iloc[-1]
        value = _num(last.get(col_val))
        if value is not None and field == "market_cap":
            value = value * _BAIDU_MV_UNIT      # 亿元 → 元
        out[field] = value
        if col_date:
            last_date = _to_date(last.get(col_date)) or last_date
    out["date"] = last_date

    if out["pe"] is None and out["pb"] is None and out["market_cap"] is None:
        raise MarketXError(f"百度估值全部指标失败: {symbol} | " + "; ".join(errors))
    return out


def fetch_valuation(symbol: str) -> Dict[str, Any]:
    """估值指标：PE / PB / 股息率 / 市值。

    财务数据按季更新，一天拉一次完全够 —— 这也是它被放进
    **日频截面采集**（而不是分钟级）的原因。

    取数顺序：乐咕（老 akshare，字段最全）→ 百度估值（现版本唯一可用）。
    乐咕接口在 akshare 1.18 之后被移除，**必须先探测再调**，
    否则每次都是"没有接口"这一个错，69 只票无一幸免。
    """
    code = _code_only(symbol)

    if ak is not None and hasattr(ak, _LEGACY_VALUATION_API):
        try:
            return _valuation_legacy(symbol, code)
        except MarketXError as exc:
            logger.debug(f"乐咕估值失败，改用百度估值: {exc}")

    try:
        return _valuation_baidu(symbol, code)
    except MarketXError as exc:
        raise MarketXError(
            f"估值不可用 {symbol}: {exc}（已尝试 {_LEGACY_VALUATION_API}"
            f"{'（当前 akshare 版本无此接口）' if ak is None or not hasattr(ak, _LEGACY_VALUATION_API) else ''}"
            f" / stock_zh_valuation_baidu）"
        ) from exc


def fetch_financial_indicator(symbol: str) -> Dict[str, Any]:
    """财务指标：ROE / 毛利率 / 净利率 / 营收增长 / 净利增长 / 负债率。

    报告期数据（季报）。``report_date`` 必须一起存 ——
    用"今天的日期"去标记一份三季报，回看时会误以为当时就知道了。
    """
    code = _code_only(symbol)
    df = _call("stock_financial_analysis_indicator", symbol=code,
               start_year="2020", limit_key="finance")
    col_date = _pick(df, ["日期", "报告期"])
    col_roe = _pick(df, ["净资产收益率(%)", "roe"])
    col_gross = _pick(df, ["销售毛利率(%)", "gross_profit_margin"])
    col_net = _pick(df, ["销售净利率(%)", "net_profit_margin"])
    col_rev = _pick(df, ["主营业务收入增长率(%)", "or_yoy"])
    col_profit = _pick(df, ["净利润增长率(%)", "netprofit_yoy"])
    col_debt = _pick(df, ["资产负债率(%)", "debt_asset_ratio"])
    col_eps = _pick(df, ["每股净资产_调整后(元)", "bps"])
    if df.empty:
        raise MarketXError(f"财务数据为空: {symbol}")
    df = df.sort_values(by=col_date) if col_date else df
    last = df.iloc[-1]
    return {
        "symbol": normalize_symbol(symbol),
        "report_date": _to_date(last.get(col_date)) if col_date else None,
        "roe": _num(last.get(col_roe)) if col_roe else None,
        "gross_margin": _num(last.get(col_gross)) if col_gross else None,
        "net_margin": _num(last.get(col_net)) if col_net else None,
        "revenue_yoy": _num(last.get(col_rev)) if col_rev else None,
        "profit_yoy": _num(last.get(col_profit)) if col_profit else None,
        "debt_ratio": _num(last.get(col_debt)) if col_debt else None,
        "bps": _num(last.get(col_eps)) if col_eps else None,
    }


def _code_only(symbol: str) -> str:
    """akshare 多数接口只吃 6 位代码，不吃带后缀的代码。"""
    s = normalize_symbol(symbol)
    return s.split(".")[0]


# ===========================================================================
# 多类别截面数据（写入通用因子值表）
# ===========================================================================
# 这些函数的产出**不是 DataFrame 表**，而是 [symbol, date, name, value] 的行，
# 直接喂给 kline_repo.save_factor_values。
#
# 为什么统一成行：新增一类数据不再需要建表、写仓储、写查询 ——
# 只要产出这些行即可。表结构的演进成本从"改 4 个文件"降到"加 1 个函数"。

#: 数据类别目录（界面/CLI 靠它列出"现在支持采什么"）
CATEGORIES: Dict[str, Dict[str, str]] = {
    "lhb": {"name": "龙虎榜", "freq": "日频", "doc": "机构/游资席位净买卖，资金类"},
    "margin": {"name": "融资融券", "freq": "日频", "doc": "两融余额与融资买入，杠杆资金"},
    "forecast": {"name": "业绩预告", "freq": "季频", "doc": "业绩预增/预减幅度，基本面"},
    "holders": {"name": "股东户数", "freq": "季频", "doc": "户数变化=筹码集中/分散"},
    "rating": {"name": "机构评级", "freq": "不定期", "doc": "研报评级与目标价，基本面+舆情"},
    "news": {"name": "新闻舆情", "freq": "日频", "doc": "新闻/公告，带情感分"},
    "macro": {"name": "宏观指标", "freq": "月/季", "doc": "CPI/PMI/M2/利率/汇率"},
    "breadth": {"name": "市场宽度", "freq": "日频", "doc": "涨跌家数、涨停家数、成交额"},
}


def _rows(symbol: str, day: Any, pairs: Dict[str, Any],
          category: str, extra: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    out = []
    for k, v in pairs.items():
        if v is None:
            continue
        out.append({
            "symbol": normalize_symbol(symbol), "date": day,
            "category": category, "name": k, "value": float(v),
            "extra": extra or None,
        })
    return out


def fetch_dragon_tiger(symbol: str, trade_date: Optional[str] = None
                       ) -> List[Dict[str, Any]]:
    """龙虎榜：当日机构/游资净买入额。

    龙虎榜是**唯一能看到"谁在买"的公开数据** ——
    资金流只知道"大单在流入"，龙虎榜能区分是机构专用席位还是游资营业部。
    两者的持续性完全不同：机构买入通常有基本面支撑，游资往往是隔日游。
    """
    code = _code_only(symbol)
    # 个股龙虎榜接口的签名是 (symbol, date, flag)，date 是**单日**且必填。
    # 没给日期就从"全市场龙虎榜"反查这只票最近一次上榜日 ——
    # 龙虎榜不是每天都有（只在异动时才上），按今天查会大量空返回。
    day_str = str(trade_date or "").replace("-", "")
    if not day_str:
        try:
            all_df = _call("stock_lhb_detail_em", start_date="20240101",
                           end_date=date.today().strftime("%Y%m%d"),
                           limit_key="lhb")
            col_code = _pick(all_df, ["代码", "股票代码"])
            col_day = _pick(all_df, ["交易日", "日期"])
            if col_code:
                hit = all_df[all_df[col_code].astype(str).str.contains(code)]
                if not hit.empty:
                    row = hit.iloc[-1]
                    day_str = str(row.get(col_day) or "").replace("-", "")[:8]
        except Exception as exc:
            logger.debug(f"反查龙虎榜上榜日失败: {exc}")
    if not day_str:
        raise MarketXError(f"{symbol} 近期没有龙虎榜数据（只在异动时才上榜）")

    df = _call("stock_lhb_stock_detail_em", symbol=code, date=day_str,
               flag="买入", limit_key="lhb")
    col_date = _pick(df, ["交易日", "日期"])
    col_buy = _pick(df, ["买入金额", "机构买入净额", "龙虎榜成交额"])
    if col_buy is None:
        raise MarketXError("龙虎榜缺少买入金额列")
    df = df.sort_values(by=col_date) if col_date else df
    last = df.iloc[-1]
    day = _to_date(last.get(col_date)) if col_date else date.today()
    # 上榜金额只反映"有多大动静"，方向要看净买额（有则用，没有就用买入额）
    col_net = _pick(df, ["机构买入净额", "净买额", "净买入额"])
    net = _num(last.get(col_net)) if col_net else None
    return _rows(symbol, day, {
        "lhb_net_buy": net,
        "lhb_buy_amount": _num(last.get(col_buy)),
    }, "lhb", {"date": day_str})


def fetch_margin(symbol: str) -> List[Dict[str, Any]]:
    """融资融券余额。

    两融余额是**杠杆资金的温度计**：余额快速上升说明风险偏好抬升，
    快速下降往往是被动去杠杆（踩踏）。它比资金流更能反映市场整体的
    风险偏好，但更新慢（T+1 盘后）。
    """
    code = _code_only(symbol)
    market = _market_of(symbol)
    if market == "sh":
        df = _call("stock_margin_detail_sse", date="", symbol=code,
                   limit_key="margin")
    else:
        df = _call("stock_margin_detail_szse", date="", limit_key="margin")
    col_date = _pick(df, ["信用交易日期", "日期", "交易日期"])
    col_rz = _pick(df, ["融资余额", "本日融资余额"])
    col_rq = _pick(df, ["融券余额", "本日融券余额"])
    if col_rz is None:
        raise MarketXError("两融数据缺少融资余额列")
    df = df.sort_values(by=col_date) if col_date else df
    last = df.iloc[-1]
    day = _to_date(last.get(col_date)) if col_date else date.today()
    return _rows(symbol, day, {
        "margin_balance": _num(last.get(col_rz)),
        "short_balance": _num(last.get(col_rq)),
    }, "margin")


def fetch_forecast(symbol: str) -> List[Dict[str, Any]]:
    """业绩预告：预增/预减幅度与类型。

    业绩预告是**财报之前**唯一的业绩前瞻，季频但领先财报 1~2 个月。
    "预增"和"大幅上升"在市场上的反应强度完全不同，所以类型和幅度都要存。
    """
    code = _code_only(symbol)
    df = _call("stock_yjyg_em", symbol=code, limit_key="forecast")
    if df.empty:
        raise MarketXError(f"无业绩预告: {symbol}")
    col_type = _pick(df, ["业绩变动类型", "类型"])
    col_range = _pick(df, ["预告净利润变动幅度", "业绩变动幅度"])
    col_date = _pick(df, ["公告日期", "报告期"])
    last = df.iloc[-1]
    day = _to_date(last.get(col_date)) if col_date else date.today()
    text = str(last.get(col_type) or "") if col_type else ""
    # 幅度可能是 "45.00%~60.00%"，取中值
    amp = None
    raw = str(last.get(col_range) or "") if col_range else ""
    nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", raw)]
    if nums:
        amp = sum(nums) / len(nums)
    return _rows(symbol, day, {"forecast_yoy": amp}, "forecast",
                 {"type": text, "raw": raw})


def fetch_holders(symbol: str) -> List[Dict[str, Any]]:
    """股东户数：户数减少 = 筹码集中（往往有人在收集）。

    这是少数**季频但很有效**的筹码指标：
    股价横盘 + 户数持续减少，通常意味着主力在悄悄收集。
    """
    code = _code_only(symbol)
    df = _call("stock_zh_a_gdhs_detail_em", symbol=code, limit_key="holders")
    col_date = _pick(df, ["股东户数统计截止日", "日期", "截止日期"])
    col_n = _pick(df, ["股东户数-本次", "股东户数", "本期户数"])
    col_avg = _pick(df, ["户均持股市值", "户均持股数量"])
    if col_n is None:
        raise MarketXError("股东户数数据缺少户数列")
    df = df.sort_values(by=col_date) if col_date else df
    if len(df) >= 2:
        last, prev = df.iloc[-1], df.iloc[-2]
        n_now = _num(last.get(col_n))
        n_prev = _num(prev.get(col_n))
        chg = ((n_now - n_prev) / n_prev) if (n_now and n_prev) else None
    else:
        last, chg = df.iloc[-1], None
    day = _to_date(last.get(col_date)) if col_date else date.today()
    return _rows(symbol, day, {
        "holder_count": _num(last.get(col_n)),
        "holder_change_pct": (chg * 100.0) if chg is not None else None,
        "avg_hold_value": _num(last.get(col_avg)) if col_avg else None,
    }, "holders")


def fetch_rating(symbol: str) -> List[Dict[str, Any]]:
    """机构评级：研报评级、目标价、机构家数。

    评级本身不该直接当信号（卖方评级长期偏乐观），
    但**评级变化的密度**有价值：一个月内十几家上调，说明基本面确实在改善。
    """
    code = _code_only(symbol)
    df = _call("stock_institute_recommend_detail", symbol=code,
               limit_key="rating")
    col_date = _pick(df, ["日期", "发布日期"])
    col_rate = _pick(df, ["评级", "机构评级"])
    col_target = _pick(df, ["目标价", "预测目标价"])
    if col_rate is None:
        raise MarketXError("机构评级缺少评级列")
    df = df.sort_values(by=col_date) if col_date else df
    last = df.iloc[-1]
    day = _to_date(last.get(col_date)) if col_date else date.today()
    text = str(last.get(col_rate) or "")
    # 评级文本 → 数值：买入/增持 高，中性 中，减持/卖出 低
    score = None
    if text:
        if any(w in text for w in ("买入", "强烈推荐", "推荐")):
            score = 3.0
        elif any(w in text for w in ("增持", "跑赢", "Outperform")):
            score = 2.0
        elif any(w in text for w in ("中性", "持有")):
            score = 1.0
        elif any(w in text for w in ("减持", "卖出", "回避")):
            score = 0.0
    return _rows(symbol, day, {
        "rating_score": score,
        "target_price": _num(last.get(col_target)) if col_target else None,
        "report_count": float(len(df)),
    }, "rating", {"rating": text})


def fetch_news(symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
    """个股新闻（含情感分）。

    情感分由关键词规则给（离线可用）；配了 LLM 会用大模型结果覆盖。
    没有情感分的新闻也存 —— "出现了多少条"本身就是关注度信号。
    """
    code = _code_only(symbol)
    df = _call("stock_news_em", symbol=code, limit_key="news")
    col_title = _pick(df, ["新闻标题", "标题"])
    col_date = _pick(df, ["发布时间", "日期", "新闻时间"])
    col_url = _pick(df, ["新闻链接", "链接"])
    if col_title is None:
        raise MarketXError("新闻缺少标题列")
    df = df.head(int(limit))
    out: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        title = str(r.get(col_title) or "").strip()
        if not title:
            continue
        day = _to_date(r.get(col_date)) if col_date else date.today()
        out.append({
            "symbol": normalize_symbol(symbol),
            "date": day or date.today(),
            "title": title[:500],
            "kind": "news",
            "source": "akshare",
            "sentiment": _sentiment(title),
            "url": str(r.get(col_url) or "")[:500] if col_url else None,
        })
    return out


#: 情感词典（离线、可维护；LLM 配了会用模型结果，这里是兜底）
_POSITIVE = ("超预期", "预增", "增长", "中标", "突破", "涨停", "新高", "回购",
             "增持", "扩产", "获批", "龙头", "领先", "合作", "订单", "盈利",
             "利好", "上调", "复苏", "景气")
_NEGATIVE = ("亏损", "下滑", "减持", "违规", "处罚", "诉讼", "退市", "跌停",
             "暴跌", "下调", "风险", "警示", "问询", "亏损扩大", "商誉",
             "质押", "冻结", "造假", "停产", "召回")


def _sentiment(text: str) -> Optional[float]:
    """关键词情感打分（离线兜底）。

    刻意做得粗糙：情感分析的精度远不如"有没有重大事件"，
    所以只给 -1~1 的粗粒度分，且不指望它单独产生信号。
    """
    if not text:
        return None
    pos = sum(1 for w in _POSITIVE if w in text)
    neg = sum(1 for w in _NEGATIVE if w in text)
    if pos == 0 and neg == 0:
        return 0.0
    return round(max(-1.0, min(1.0, (pos - neg) / max(3, pos + neg))), 3)


def fetch_macro(name: str = "cpi") -> List[Dict[str, Any]]:
    """宏观指标（市场级）。

    宏观不预测明天涨跌，但决定**哪类策略在这个环境下能活**：
    利率上行 → 高估值成长股承压；PMI 拐点 → 周期股领先。
    所以它作为"环境开关"参与打分，不是择时信号。
    """
    table = {
        "cpi": "macro_china_cpi_monthly",
        "ppi": "macro_china_ppi_yearly",
        "pmi": "macro_china_pmi_yearly",
        "m2": "macro_china_money_supply",
        "shibor": "macro_china_shibor_all",
        "lpr": "macro_china_lpr",
        "gdp": "macro_china_gdp_yearly",
        "usdcny": "macro_china_usd_cny",
    }
    if name not in table:
        raise MarketXError(f"不支持的宏观指标 {name!r} | 可用: {', '.join(table)}")
    df = _call(table[name], limit_key="macro")
    col_date = _pick(df, ["日期", "月份", "统计月份"])
    col_val = _pick(df, ["今值", "值", "数值", "中国CPI同比", "全国居民消费价格指数"])
    if col_val is None:
        # 宏观表列名不统一，退而取最后一个数值列
        num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        if not num_cols:
            raise MarketXError(f"{table[name]} 找不到数值列")
        col_val = num_cols[-1]
    df = df.sort_values(by=col_date) if col_date else df
    last = df.iloc[-1]
    day = _to_date(last.get(col_date)) if col_date else date.today()
    return [{
        "date": day or date.today(), "name": str(name),
        "value": _num(last.get(col_val)), "period": "month",
        "source": "akshare",
    }]


def fetch_market_breadth() -> List[Dict[str, Any]]:
    """市场宽度/情绪（市场级）。

    同一只票在普涨日和普跌日里，同样的技术形态胜率完全不同 ——
    这是所有个股策略的**共同背景**，必须作为特征参与判断。
    """
    # 这个接口返回的是 **item/value 两列的长表**，不是宽表 ——
    # 按列名取会全取到 None，然后静默写成一条全 0 的记录。
    # 那种"数据看起来在，其实全是 0"比直接报错难查得多。
    df = _call("stock_market_activity_legu", limit_key="breadth")
    if "item" not in df.columns or "value" not in df.columns:
        raise MarketXError("市场活跃度接口结构变了（不是 item/value 两列）")
    kv = {str(r["item"]): r["value"] for _, r in df.iterrows()}

    def _v(key: str) -> Optional[float]:
        raw = kv.get(key)
        if raw is None:
            return None
        nums = re.findall(r"-?\d+\.?\d*", str(raw))
        return float(nums[0]) if nums else None

    day = None
    for key in ("统计日期", "日期"):
        raw = kv.get(key)
        if raw:
            day = _to_date(raw)
            break
    sentiment = _v("活跃度")
    # 涨跌家数这个接口不直接给，用涨停/跌停家数与活跃度共同刻画市场温度
    return [{
        "date": day or date.today(),
        "up_count": int(_v("上涨") or 0), "down_count": int(_v("下跌") or 0),
        "limit_up_count": int(_v("涨停") or 0),
        "limit_down_count": int(_v("跌停") or 0),
        "sentiment": sentiment,
        "total_amount": None,
    }]


# 日线（K线）数据源注册：引擎本来就 import 了 marketx，
# 放在文件末尾可避开循环导入 —— 详见 docs/DATA_SOURCES.md「新增数据源：三步」。
import app.providers.market.compat.kline_sources  # noqa: F401  触发 daily 源注册
# 本地兜底源放在最后 import：它注册在 hot_rank 的**最后一位**，
# 顺序上必须晚于东财/雪球那几个外网源，否则会"抢跑"本地旧数据。
import app.providers.market.compat.local_source  # noqa: F401  触发 local_hot 注册

# 把上面注册的源**同时**装配进新的 Provider 层（能力声明 / 能力级熔断 / 路由）。
# 取数入口（fetch_hot_rank / fetch_fund_flow / fetch_fund_flow_rank / fetch_daily）
# 已经改走 DataRouter；旧注册表保留是为了兼容既有面板与旧链路调用方。
_bootstrap()


def call_akshare(name: str, *args: Any, limit_key: str = "marketx",
                 retry: Optional[int] = None, **kwargs: Any) -> Any:
    """``_call`` 的公开版本：给"业务需要但不是行情"的取数用。

    标的主表同步要拉交易所的股票列表（``stock_info_sh_name_code`` 等），
    这些接口在结构上和行情接口毫无区别 —— 一样限流、一样会抽风、
    一样需要指数退避。所以复用同一个限速器与重试策略，
    而不是在各业务模块里各自写一遍 ``import akshare; getattr(ak, name)``：
    那样会绕开限速器，结局是"某个没人记得住限流的模块把 IP 封了"。

    :raises MarketXError: 接口不存在或调用失败
    """
    return _call(name, *args, limit_key=limit_key, retry=retry, **kwargs)


__all__ = [
    "CATEGORIES",
    "MarketXError",
    "call_akshare",
    "fetch_board_fund_flow",
    "fetch_boards",
    "fetch_chip",
    "fetch_concept_cons",
    "fetch_dragon_tiger",
    "fetch_financial_indicator",
    "fetch_forecast",
    "fetch_fund_flow",
    "fetch_fund_flow_rank",
    "fetch_holders",
    "fetch_hot_rank",
    "fetch_limitup_pool",
    "fetch_macro",
    "fetch_margin",
    "fetch_market_breadth",
    "fetch_news",
    "fetch_rating",
    "fetch_spot",
    "fetch_trade_dates",
    "fetch_valuation",
]
