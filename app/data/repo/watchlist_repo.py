#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : watchlist_repo.py
# @Description : 关注标的（自选股）仓储 —— 谁在被盯着、多久盯一次、上次盯成功没
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
关注标的的持久化访问。

这一层刻意**不联网、不打业务逻辑**：它只负责把"用户静态里勾了哪些票、
每只多久采一次、上次采成功没有"这件事稳定地存下来和读出来。
"什么时候该采"由采集服务判（见 collection.service 的 watchlist 单元），
"怎么采"由 run_full_collection 负责 —— 三者互不越界，改任何一边都不会
把另外两边拖下水。

两条硬约束
----------
1. **所有写操作幂等**。重复 add 同一只票不能报错、不能产生两行；
   接口可能被面板重发、被 `:meth:`CollectionService` 重启时重放。
2. **频率写得进去就一定是合法的**。写 ``"1x"`` 这种非法频率会在
   **添加那一刻**由 :func:`_parse_freq` 兜底成默认值，
   绝不允许把 "解析不了" 存进 `freq_seconds` —— 那会变成
   "这只票永远不到期也永远不报错" 的静默失效项。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.common.logging import logger
from app.data.engine import SessionLocal
from app.data.schema import Watchlist

#: 频率兜底值（秒）。同时也是默认频率 —— 一小时对"关注一下"是个体面的默认值：
#: 日内再勤多半是在浪费请求额度，再懒就跟没关注一样。
DEFAULT_FREQ_SECONDS: float = 3600.0
DEFAULT_FREQ: str = "1h"

#: 失败后的重试退避上限（秒）。
#: 一只票抽风失败，让用户干等整整一个频率周期（可能是一小时）太久了；
#: 但"失败就立刻重来"又会让它每 60 秒打一次必然失败的请求 ——
#: 所以它取 ``min(标的空间自己的频率, 300s)``：比正常节奏快，又不至于失控。
RETRY_BACKOFF_CAP: float = 300.0


def _parse_freq(value: Any) -> tuple[str, float]:
    """把任意写法的频率归一成 ``(人读字符串, 秒数)``。

    写错/undefined/负数一律兜底到默认值，不抛给调用方 ——
    采集频率是"体验"参数，不是"能不能跑"参数：因为它把整个服务打断，
    代价远大于用一小时默认值继续跑。
    """
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_FREQ, DEFAULT_FREQ_SECONDS
    try:
        # 延迟导入：spec 属于 modules 层，顶层引用会让"数据层反向依赖业务层"，
        # 也会在只测仓储时把整条采集编排链拖起来。
        from app.modules.collection.spec import parse_frequency

        # 哨兵默认值：任何 <=0 的结果都表示"没解析出合法频率"。
        # 用 -1.0 而不是 ``DEFAULT_FREQ_SECONDS``，是为了能**区分**
        # "用户写的恰好等于默认频率" 与 "用户写的根本没法解析"。
        # 后者要是按原样把字符串存进去（UI 显示 'bad'、实际按 1h 跑），
        # 就成了"面板说的和引擎做的不一致"——排障时最费时间的一种 bug。
        seconds = float(parse_frequency(raw, -1.0))
    except Exception:
        seconds = -1.0
    # 0/负数是最危险的输入：它会让这批票每 60 秒被抓一轮，
    # 没几天就把所有数据源一起送进限流名单。必须按非法处理。
    if seconds <= 0:
        logger.warning(f"关注标的频率 {raw!r} 非法，回退默认 {DEFAULT_FREQ}")
        return DEFAULT_FREQ, DEFAULT_FREQ_SECONDS
    return raw, seconds


def _cats_to_text(categories: Any) -> str:
    """类别列表 → 存储用的 JSON 字符串。空/非法一律存空串（=跟随服务默认）。"""
    if not categories:
        return ""
    if isinstance(categories, str):
        try:
            parsed = json.loads(categories)
        except Exception:
            return categories.strip()
        categories = parsed
    try:
        items = [str(c).strip() for c in categories if str(c).strip()]
    except TypeError:
        return ""
    return json.dumps(items, ensure_ascii=False) if items else ""


def _text_to_cats(text: Any) -> List[str]:
    """JSON 字符串 → 类别列表。存的是空串就返回空列表（=跟随服务默认）。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(c).strip() for c in parsed if str(c).strip()]


def _row_to_dict(row: Watchlist) -> Dict[str, Any]:
    """ORM 行 → 面板/服务要用的字典。"""
    return {
        "symbol": str(row.symbol or ""),
        "name": str(row.name or ""),
        "enabled": bool(row.enabled),
        "freq": str(row.freq or DEFAULT_FREQ),
        "freq_seconds": float(row.freq_seconds or DEFAULT_FREQ_SECONDS),
        "categories": _text_to_cats(row.categories),
        "added_at": row.added_at.isoformat() if row.added_at else None,
        "last_collected_at": (
            row.last_collected_at.isoformat() if row.last_collected_at else None
        ),
        "next_retry_at": (
            row.next_retry_at.isoformat() if row.next_retry_at else None
        ),
        "last_error": str(row.last_error or "") or None,
        "runs": int(row.runs or 0),
        "errors": int(row.errors or 0),
        "note": str(row.note or ""),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@contextmanager
def _session(session: Optional[Session] = None) -> Iterator[Session]:
    """会话上下文：自动提交/回滚/关闭。传入外部 session 时只借用、不关闭。"""
    own = session is None
    db = session or SessionLocal()
    try:
        yield db
        if own:
            db.commit()
    except Exception:
        if own:
            db.rollback()
        raise
    finally:
        if own:
            db.close()


def _norm(symbol: Any) -> str:
    from app.domain.symbol import normalize_symbol

    return normalize_symbol(str(symbol or "").strip())


# =============================================================================
# 读
# =============================================================================
def list_watchlist(enabled_only: bool = False,
                   session: Optional[Session] = None) -> List[Dict[str, Any]]:
    """列出关注标的。默认连停用的也列（面板要能把它再开回来）。"""
    with _session(session) as db:
        q = db.query(Watchlist)
        if enabled_only:
            q = q.filter(Watchlist.enabled.is_(True))
        rows = q.order_by(Watchlist.added_at.asc()).all()
        return [_row_to_dict(r) for r in rows]


def get_watch(symbol: Any, session: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    """取一只标的的关注记录；没关注过返回 ``None``。"""
    try:
        sym = _norm(symbol)
    except Exception:
        return None
    with _session(session) as db:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        return _row_to_dict(row) if row else None


def count_watchlist(enabled_only: bool = True,
                    session: Optional[Session] = None) -> int:
    with _session(session) as db:
        q = db.query(Watchlist)
        if enabled_only:
            q = q.filter(Watchlist.enabled.is_(True))
        return int(q.count())


def due_watchlist(now: Optional[datetime] = None,
                  limit: int = 0,
                  session: Optional[Session] = None) -> List[Dict[str, Any]]:
    """到点了该采的关注标的。

    到期判定 ``现在 - 上次成功采集 >= 频率``。
    **没采过（``last_collected_at`` 为空）的排在最前面**：用户刚把一只票
    加进关注，他期待的是"马上有数据"，而不是耐心等第一个周期过去。

    :param limit: 单轮最多返回多少只（``0`` = 不限）。这是**防雷名单**——
        一次拉 200 只去重打全类别，足够把数据源打进限流。
    """
    moment = now or datetime.now()
    out: List[Dict[str, Any]] = []
    with _session(session) as db:
        rows = db.query(Watchlist).filter(Watchlist.enabled.is_(True)).all()
        for r in rows:
            item = _row_to_dict(r)
            # 库里的 seconds 可能因为历史数据损坏而是 0/None，
            # 这里再兜一次底（写路径已校验过，这是读路径的双保险）
            seconds = item["freq_seconds"] if item["freq_seconds"] > 0 else DEFAULT_FREQ_SECONDS
            last = r.last_collected_at
            if last is None:
                out.append(item)          # 从未采成功过 → 立刻该采
                continue
            elapsed = (moment - last).total_seconds()
            if elapsed >= seconds:
                out.append(item)
                continue
            # 上一轮失败安排的重试到了。它比"正常节奏"更早，
            # 但仍然是一次按计划的尝试，不是盲目的失败立即重来。
            retry = r.next_retry_at
            if retry is not None and moment >= retry:
                out.append(item)
    # 从没采过的优先，其余按"逾期越久越优先"
    out.sort(key=lambda d: (0 if not d.get("last_collected_at")
                            else 1, d.get("last_collected_at") or ""))
    if limit and limit > 0:
        return out[:int(limit)]
    return out


# =============================================================================
# 写（全部幂等）
# =============================================================================
def add_watch(symbol: Any,
              freq: Any = "",
              categories: Any = None,
              name: str = "",
              note: str = "",
              session: Optional[Session] = None) -> Dict[str, Any]:
    """加入关注。**重复添加同一只票等价于更新**（不报错、不产生第二行）。"""
    sym = _norm(symbol)
    if not sym:
        raise ValueError("关注标的缺少 symbol")
    freq_text, seconds = _parse_freq(freq)
    now = datetime.now()
    with _session(session) as db:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row is None:
            row = Watchlist(
                symbol=sym, name=str(name or ""), added_at=now,
                enabled=True, freq=freq_text, freq_seconds=seconds,
                categories=_cats_to_text(categories),
                runs=0, errors=0,
                note=str(note or ""), updated_at=now,
            )
            db.add(row)
        else:
            # 再次添加 = 更新配置项。**不重置 last_collected_at**——
            # 那会把"刚采过"抹掉，导致下一次扫描立刻重采一轮。
            row.freq = freq_text
            row.freq_seconds = seconds
            row.categories = _cats_to_text(categories)
            if name:
                row.name = str(name)
            if note:
                row.note = str(note)
            row.updated_at = now
        db.flush()
        return _row_to_dict(row)


def remove_watch(symbol: Any, session: Optional[Session] = None) -> bool:
    """取消关注。本来就没关注返回 ``False``（幂等，不报错）。"""
    sym = _norm(symbol)
    with _session(session) as db:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True


def set_watch_freq(symbol: Any, freq: Any,
                   session: Optional[Session] = None) -> Dict[str, Any]:
    """改单只标的的采集频率（"1h" / "30m" / "600"）。"""
    sym = _norm(symbol)
    freq_text, seconds = _parse_freq(freq)
    with _session(session) as db:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row is None:
            raise KeyError(f"标的 {sym} 不在关注列表里")
        row.freq = freq_text
        row.freq_seconds = seconds
        row.updated_at = datetime.now()
        db.flush()
        return _row_to_dict(row)


def set_watch_categories(symbol: Any, categories: Any,
                         session: Optional[Session] = None) -> Dict[str, Any]:
    """改单只标的额外采集的类别。传空 = 跟随服务默认类别。"""
    sym = _norm(symbol)
    with _session(session) as db:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row is None:
            raise KeyError(f"标的 {sym} 不在关注列表里")
        row.categories = _cats_to_text(categories)
        row.updated_at = datetime.now()
        db.flush()
        return _row_to_dict(row)


def set_watch_enabled(symbol: Any, enabled: bool,
                      session: Optional[Session] = None) -> Dict[str, Any]:
    """停/启单只标的的自动采集（保留记录与已配频率）。"""
    sym = _norm(symbol)
    with _session(session) as db:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row is None:
            raise KeyError(f"标的 {sym} 不在关注列表里")
        row.enabled = bool(enabled)
        row.updated_at = datetime.now()
        db.flush()
        return _row_to_dict(row)


def mark_watch_result(symbol: Any, ok: bool = True, error: str = "",
                      session: Optional[Session] = None) -> None:
    """记一次采集的结果。**只有成功才更新 ``last_collected_at``**。

    成功后这笔数据到底有多旧是可计算的，于是它按自己的频率继续走上正常节奏。

    失败则**另起一条更快的重试线**：``next_retry_at = now + min(频率, 300s)``。
    为什么不干脆让它下一轮（60s 后）就重来：那样一只永远失败的票
    （退市、代码写错）会每隔一分钟打一次注定失败的请求，
    而用户往往要过好一阵子才会在面板上发现它。
    为什么又不让它等满整个频率周期：那样一次偶发抖动就让用户干等一小时。

    反过来若在这里**乐观更新**成功时间戳，一次偶发失败会把这只票
    整整屏蔽一个频率周期 —— 而那恰恰是最该再试一次的时候。
    """
    sym = _norm(symbol)
    now = datetime.now()
    with _session(session) as db:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row is None:
            return
        row.updated_at = now
        if ok:
            row.last_collected_at = now
            row.next_retry_at = None      # 成功就把待重试的计划作废
            row.runs = int(row.runs or 0) + 1
            row.last_error = None
        else:
            row.errors = int(row.errors or 0) + 1
            row.last_error = str(error or "")[:300] or None
            # 比正常频率更快地再试一次，但不追着失败打 ——
            # 上限 300s 保证再糟也只是一条每五分钟一次的请求。
            seconds = float(row.freq_seconds or 0) or DEFAULT_FREQ_SECONDS
            row.next_retry_at = now + timedelta(
                seconds=min(seconds, RETRY_BACKOFF_CAP))


def init_watchlist_from(symbols: Sequence[Any],
                        freq: Any = "",
                        categories: Any = None,
                        session: Optional[Session] = None) -> int:
    """把配置里的初始清单灌进库。**只补缺、不改存量**。

    为什么"不改存量"：用户在面板上改过某只票的频率，
    重启一次就被配置覆盖回去，他会觉得面板在骗人。
    配置项是**新标的的默认值**，不是"每次启动都要恢复出厂设置"。
    """
    added = 0
    for raw in symbols or []:
        sym = str(raw or "").strip()
        if not sym:
            continue
        if get_watch(sym) is None:
            add_watch(sym, freq=freq, categories=categories,
                      note="config", session=session)
            added += 1
    return added


__all__ = [
    "DEFAULT_FREQ",
    "DEFAULT_FREQ_SECONDS",
    "RETRY_BACKOFF_CAP",
    "add_watch",
    "count_watchlist",
    "due_watchlist",
    "get_watch",
    "init_watchlist_from",
    "list_watchlist",
    "mark_watch_result",
    "remove_watch",
    "set_watch_categories",
    "set_watch_enabled",
    "set_watch_freq",
]
