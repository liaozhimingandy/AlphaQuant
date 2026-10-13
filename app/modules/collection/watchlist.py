#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : watchlist.py
# @Description : 关注标的（自选股）的配置与执行编排
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
关注标的是怎么被自动采起来的
============================

::

    config/collector.json  ──►  WatchlistConfig         (默认频率/扫描周期/护栏)
    库里的 watchlist 表     ──►  due_watchlist()         (谁到点了)
                                      │
                                      ▼
                            collect_due_watchlist()     (本文件)
                                      │
                                      ├─► run_full_collection()  真正去取数
                                      └─► mark_watch_result()    记账：成功才记时间戳

为什么把编排放在这里，而不是塞进服务或者塞进仓储
------------------------------------------------
放进服务（``CollectionService``）：配置解析、分组、成败判定这些纯逻辑会和
``LoopingCall`` / ``deferToThread`` 的线程语义缠在一起，于是想测"到期的判定"
就必须先把 twisted 的 reactor 拉起来。

放进仓储（``watchlist_repo``）：那会让数据层反向依赖业务层
（``run_full_collection`` 属于采集业务）。

所以两边都不收，单独开一层：**仓储只管存取、编排只管"谁该采、采得怎么样"、
服务只管什么时候驱动它**。三者都能单独测。

配置优先级
----------
``config/collector.json`` 的 ``watchlist`` 段 > 环境变量（``Settings.WATCHLIST_*``）
> 内置默认值。

为什么允许环境变量插一脚：容器里改环境变量比重挂配置文件方便，
而"默认采集频率"恰恰是最常被按环境调的那个值
（本地开发想要 30m 看效果，生产只想要 1h）。

注意
----
``categories: []`` 是**有意义的声明**（"不额外采任何类别"），不能用 ``or``
兜底成服务默认 —— 项目里这条纪律踩过坑（采集 `categories: []` 反向变成全采）。
一律 ``is not None`` 判断。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.common.config import settings
from app.common.logging import logger

#: 关注标的的配置放在采集配置文件里（和采集的其它齿轮同一个文件），
#: 理由与 :data:`app.modules.collection.schedule.SCHEDULE_KEY` 相同 ——
#: "采什么、多久采、什么时候采"本来就是一件事，拆两个文件只会漏改一处。
WATCHLIST_KEY = "watchlist"

#: 默认额外类别。K 线 / 资金流 / 筹码 / 基本面**本来就在** run_full_collection
#: 的采集范围里，这里补的是那些"个股级但非截面"的类别。
DEFAULT_CATEGORIES: Tuple[str, ...] = (
    "lhb", "margin", "forecast", "holders", "rating", "news",
)


def _default_seconds() -> float:
    try:
        from app.data.repo.watchlist_repo import DEFAULT_FREQ_SECONDS

        return float(DEFAULT_FREQ_SECONDS)
    except Exception:
        return 3600.0


def _freq_seconds(value: Any) -> float:
    """把任意写法的频率解析成秒。解析不了退回默认（不会因为一个错字停摆）。"""
    raw = str(value or "").strip()
    if not raw:
        return _default_seconds()
    try:
        from app.modules.collection.spec import parse_frequency

        got = float(parse_frequency(raw, _default_seconds()))
    except Exception:
        return _default_seconds()
    return got if got > 0 else _default_seconds()


@dataclass
class WatchlistConfig:
    """关注标的的**全局默认**参数。

    这里的 ``interval`` 只是"新加进来的标的用多久"，
    每只标的真正生效的频率存在库里（可被面板单独覆盖）。
    """

    #: 这个单元是否启用
    enabled: bool = True
    #: 新增关注标的的默认采集频率（"30s" / "5m" / "1h" / "1d"）
    interval: str = "1h"
    #: 扫描周期（秒）：多久看一眼有没有标的到点。**不是**采集频率
    sweep_interval: float = 60.0
    #: 单轮最多采几只（限流护栏）
    max_per_round: int = 20
    #: 额外采集的类别。``[]`` = 不额外采（不是"跟随默认"——跟随默认在下游由
    #: ``None``/缺省表达，这里保持与配置文件里 ``[]`` 的字面含义一致）
    categories: List[str] = field(default_factory=list)
    #: 初始关注清单（**只用于首次灌库**，不覆盖库里已有的记录）
    symbols: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.enabled = bool(self.enabled)
        self.interval = str(self.interval or "1h").strip()
        try:
            self.sweep_interval = max(5.0, float(self.sweep_interval))
        except (TypeError, ValueError):
            self.sweep_interval = 60.0
        try:
            self.max_per_round = max(0, int(self.max_per_round))
        except (TypeError, ValueError):
            self.max_per_round = 20
        self.categories = [str(c).strip() for c in (self.categories or [])
                           if str(c).strip()]
        self.symbols = [str(s).strip() for s in (self.symbols or [])
                        if str(s).strip()]

    # ------------------------------------------------------------------
    @property
    def interval_seconds(self) -> float:
        """默认频率折算成秒（面板展示与到期计算用）。"""
        return _freq_seconds(self.interval)

    @property
    def sweep_seconds(self) -> float:
        """扫描周期（秒）。

        取它与默认频率里更小的那个：配了 30m 的频率却用 60m 的扫描周期，
        会导致实际采到的间隔在 30~90 分钟之间飘 —— 而用户会以为是 bug。
        """
        base = float(self.sweep_interval)
        freq = self.interval_seconds
        return max(5.0, min(base, freq))

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["interval_seconds"] = self.interval_seconds
        d["sweep_seconds"] = self.sweep_seconds
        return d

    @classmethod
    def from_dict(cls, raw: Any) -> "WatchlistConfig":
        if not isinstance(raw, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        # categories / symbols 用 ``is not None``：写 [] 是"一个都不要"的显式声明，
        # 不能退化成默认值（默认里有初始清单时会变成"全都关注"）。
        payload = {k: v for k, v in raw.items() if k in known}
        for key in ("categories", "symbols"):
            if raw.get(key) is not None:
                payload[key] = [str(x).strip() for x in raw[key]
                                if str(x).strip()]
        return cls(**payload)


def _from_settings() -> WatchlistConfig:
    """按环境变量造一份配置（作为文件缺失时的底）."""
    return WatchlistConfig(
        enabled=settings.WATCHLIST_ENABLED,
        interval=settings.WATCHLIST_INTERVAL,
        sweep_interval=settings.WATCHLIST_SWEEP_INTERVAL,
        max_per_round=settings.WATCHLIST_MAX_PER_ROUND,
        categories=settings.watchlist_category_list(),
    )


def load_watchlist_config(path: Optional[str] = None) -> WatchlistConfig:
    """读 collector.json 的 watchlist 段；没有/写错就退回环境变量与内置默认。

    配置坏了不该让采集服务起不来 —— 这里的代价顶多是"退回默认频率继续采"，
    比起不来（那才是真的没数据）便宜得多。
    """
    base = _from_settings()
    file = Path(path or settings.COLLECTOR_CONFIG)
    if not file.exists():
        return base
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"采集配置解析失败（关注标的用默认参数）: {exc}")
        return base
    section = (raw or {}).get(WATCHLIST_KEY) if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return base
    merged = {**base.to_dict(), **{k: v for k, v in section.items()
                                   if not k.startswith("_")}}
    # `_comment` 之外，只认 dataclass 字段（防止手滑多写键导致 from_dict 静默丢弃）
    known = {f for f in WatchlistConfig.__dataclass_fields__}
    try:
        return WatchlistConfig.from_dict(
            {k: v for k, v in merged.items() if k in known}
        )
    except Exception as exc:
        logger.warning(f"关注标配置异常（{exc}），用默认参数")
        return base


def save_watchlist_config(cfg: WatchlistConfig,
                          path: Optional[str] = None) -> str:
    """把 watchlist 段写回采集配置文件，**保留其它字段**。

    为什么要落盘：用户在面板上改了"默认采集频率"，重启之后还应该是那个值。
    只改内存的话，重启等于没改 —— 而面板又不会提示，于是用户会以为自己改成功了。
    """
    file = Path(path or settings.COLLECTOR_CONFIG)
    raw: Dict[str, Any] = {}
    if file.exists():
        try:
            got = json.loads(file.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                raw = got
        except Exception as exc:
            logger.error(f"采集配置解析失败，将重写: {exc}")

    known = {f for f in WatchlistConfig.__dataclass_fields__}
    payload = {k: v for k, v in asdict(cfg).items() if k in known}
    payload["_comment"] = [
        "关注标的（自选股）：进了这张清单的标的会被定时自动采集相关数据。",
        "interval        新增关注标的的默认采集频率；单只标的可以在面板里单独改，",
        "                那份改动存在库里，不被这个文件覆盖。",
        "sweep_interval  扫描周期（秒）：多久看一眼有没有标的到点了。",
        "                这是「检查」间隔而不是采集频率。",
        "max_per_round   单轮最多采几只（限流护栏）。",
        "symbols         初始关注清单，只用于首次灌库。",
    ]
    raw[WATCHLIST_KEY] = payload
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(file)


_CONFIG: Optional[WatchlistConfig] = None


def get_watchlist_config(reload: bool = False) -> WatchlistConfig:
    global _CONFIG
    if _CONFIG is None or reload:
        _CONFIG = load_watchlist_config()
    return _CONFIG


def set_watchlist_config(cfg: WatchlistConfig,
                         persist: bool = True) -> WatchlistConfig:
    global _CONFIG
    if persist:
        save_watchlist_config(cfg)
    _CONFIG = cfg
    return cfg


# =============================================================================
# 执行编排
# =============================================================================
def seed_from_config(cfg: Optional[WatchlistConfig] = None) -> int:
    """把配置里的初始清单灌进库（**只补缺、不改存量**）。

    为什么坚决不改存量：用户在面板上把某只票从 1h 改成 5m，
    重启一次就被配置覆盖回 1h，他会认为面板在骗人。
    配置项是"新标的的默认值"，不是"每次启动都要恢复出厂设置"。
    """
    cfg = cfg or get_watchlist_config()
    if not cfg.symbols:
        return 0
    try:
        from app.data.repo.watchlist_repo import init_watchlist_from

        added = init_watchlist_from(
            cfg.symbols, freq=cfg.interval, categories=cfg.categories)
    except Exception as exc:
        logger.warning(f"关注标的初始清单灌库失败: {exc}")
        return 0
    if added:
        logger.info(f"关注标的：从配置灌入 {added} 只初始标的")
    return added


def _norm(symbol: Any) -> str:
    try:
        from app.domain.symbol import normalize_symbol

        return normalize_symbol(str(symbol or "").strip())
    except Exception:
        return str(symbol or "").strip()


def collect_due_watchlist(now: Optional[datetime] = None,
                          config: Optional[WatchlistConfig] = None,
                          limit: int = 0,
                          symbols: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """把当前**到点了**的关注标的采一轮。

    这是面板之外的唯一驱动路径，服务单元的 tick 也调它 ——
    于是"手动点立即采集"和"定时器到了自动采"走的是同一份代码，
    不会出现两条实现各自漂移。

    返回摘要 ``{"ok", "due", "symbols", "ok_symbols", "failed_symbols",
    "groups", "error"}``。

    约定（与 :func:`run_full_collection` 一致）：
      **数据层面**的失败（某只票退市、接口抽风）记进 ``failed_symbols``，
      不中断整轮；**存储后端故障**（:class:`StorageError`）向上抛出，
      由调用方判失败、帮你留痕。
    """
    from app.data.repo.watchlist_repo import (
        due_watchlist, mark_watch_result,
    )

    cfg = config or get_watchlist_config()
    items = due_watchlist(now=now, limit=int(limit or cfg.max_per_round or 0))
    if symbols:
        # 只采调用点指定的那几只（面板"采这一个"按钮用）。
        # 在这里过滤而不是让调用方自己算到期：到期判定属于编排的职责，
        # 放出去就会出现两份各算一半的实现，然后各漂移一半。
        want = {_norm(s) for s in symbols if str(s or "").strip()}
        items = [i for i in items if _norm(i["symbol"]) in want]
    summary: Dict[str, Any] = {
        "ok": True,
        "due": len(items),
        "symbols": [i["symbol"] for i in items],
        "ok_symbols": [],
        "failed_symbols": [],
        "groups": [],
        "error": "",
    }
    if not items:
        return summary

    # 按"要采哪些类别"分组：大多数标的 categories 为空 = 跟随默认，
    # 一次打成一个组，只发一轮请求，而不是每只票各跑一遍全类别。
    groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for item in items:
        cats = tuple(item.get("categories") or cfg.categories or [])
        groups.setdefault(cats, []).append(item)

    for cats, batch in groups.items():
        syms = [b["symbol"] for b in batch]
        group_note = {"categories": list(cats), "symbols": syms}
        try:
            from app.modules.collection.service import run_full_collection

            report = run_full_collection(syms, list(cats) or None)
        except Exception as exc:
            # StorageError 也要上浮：存储挂了重试也是白试，
            # 必须让上层知道（记进 unit 的 errors 计数）。
            from app.common.errors import StorageError

            if isinstance(exc, StorageError):
                raise
            reason = f"{type(exc).__name__}: {exc}"
            for s in syms:
                try:
                    mark_watch_result(s, ok=False, error=reason)
                except Exception:
                    pass
            summary["failed_symbols"].extend(syms)
            group_note.update({"ok": False, "error": reason})
            summary["groups"].append(group_note)
            logger.warning(f"关注标的采集失败 | {','.join(syms)} | {reason}")
            continue

        # 成败要落到**每一只**标的上：一轮里 5 只成功、1 只失败，
        # 不该让那只失败的一起被记成"采过了"（它的 last_collected_at 不更新，
        # 下一轮自然重试 —— 这正是想要的）。
        failed: set = set()
        hard_error = ""
        for _label, detail in (report.get("detail") or {}).items():
            if not isinstance(detail, dict):
                continue
            for s in (detail.get("failed") or []):
                failed.add(_norm(s))
            if detail.get("error"):
                hard_error = f"{_label}: {detail['error']}"

        for b in batch:
            sym = b["symbol"]
            bad = _norm(sym) in failed or bool(hard_error)
            try:
                mark_watch_result(sym, ok=not bad, error=hard_error or
                                  ("该标的部分类别采集失败" if bad else ""))
            except Exception as exc:
                logger.debug(f"记录关注标的采集结果失败: {exc}")
            (summary["failed_symbols"] if bad else summary["ok_symbols"]).append(sym)

        group_note.update({"ok": not failed and not hard_error,
                           "error": hard_error or ""})
        summary["groups"].append(group_note)

    summary["ok"] = not summary["failed_symbols"]
    if summary["failed_symbols"]:
        summary["error"] = (
            f"{len(summary['failed_symbols'])} 只标的未完成"
            + (f" | {summary['groups'][0].get('error') or ''}"
               if summary["groups"] else "")
        )
    return summary


def watchlist_view(enabled_only: bool = False) -> Dict[str, Any]:
    """面板/API 用的全景视图：配置 + 清单 + 各自的到期状态。

    把"还有多久该采"算出来给前端，是为了让面板能回答那个最常被问的问题：
    「我明明加了自选，为什么它还没数据」。
    """
    from app.data.repo.watchlist_repo import due_watchlist, list_watchlist

    cfg = get_watchlist_config()
    items = list_watchlist(enabled_only=enabled_only)
    due_map: Dict[str, bool] = {}
    try:
        for d in due_watchlist():
            due_map[d["symbol"]] = True
    except Exception as exc:
        logger.debug(f"计算关注标的到期状态失败: {exc}")

    now = datetime.now()
    view_items: List[Dict[str, Any]] = []
    for it in items:
        seconds = it.get("freq_seconds") or 0.0
        last = it.get("last_collected_at")
        due_in = None
        if not it["enabled"]:
            due_in = None
        elif last is None:
            due_in = 0.0
        else:
            try:
                last_dt = datetime.fromisoformat(last)
                due_in = max(0.0, seconds - (now - last_dt).total_seconds())
            except Exception:
                due_in = 0.0
        enriched = dict(it)
        enriched["due"] = bool(due_map.get(it["symbol"]))
        enriched["due_in_seconds"] = due_in
        view_items.append(enriched)

    return {
        "config": cfg.to_dict(),
        "default_categories": list(DEFAULT_CATEGORIES),
        "total": len(items),
        "enabled_count": sum(1 for i in items if i["enabled"]),
        "due_count": len(due_map),
        "items": view_items,
    }


__all__ = [
    "DEFAULT_CATEGORIES",
    "WATCHLIST_KEY",
    "WatchlistConfig",
    "collect_due_watchlist",
    "get_watchlist_config",
    "load_watchlist_config",
    "save_watchlist_config",
    "seed_from_config",
    "set_watchlist_config",
    "watchlist_view",
]
