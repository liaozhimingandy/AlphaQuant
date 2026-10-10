#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : pool.py
# @Description : 股票池：候选池 → 综合打分 → 排序推荐
#
#                为什么需要"池"这一步：
#                  全市场 5000 多只票，每只都拉资金流+筹码+题材，
#                  既慢（几十分钟）又必然被限流，而且**没有意义** ——
#                  绝大多数票根本不在你的关注范围内。
#
#                  先用一个小候选池把范围收窄（几十~几百只），
#                  再在这个池子里做精细打分。这也是实盘上的真实做法。
#
#                候选池来源：
#                  limitup  涨停池（当日涨停，市场最强的一批）
#                  hot      人气榜（散户关注度，情绪指标）
#                  theme    热门题材成分股（跟着资金主线走）
#                  flow     资金流排名（主力净流入前列）
#                  watch    自选股（config/pool.json 里配）
#                  all      全市场（慎用：慢，且容易被限流）
#
#                刷新流程刻意分成两段：
#                  **先同步数据，再打分**。混在一起做的话，
#                  一只票数据没拉到就会被"用着旧数据打一次分"，
#                  出来的排名是新旧混合的，比不做还糟。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.core.data.features import FeatureProvider, get_provider
from app.core.score.engine import ScoreConfig, ScoreEngine, ScoreResult
from app.repository import marketx_repository as R
from app.utils.logger import logger

try:
    from app.data import marketx as MX
except Exception:  # pragma: no cover
    MX = None  # type: ignore[assignment]


#: 候选池来源
SOURCES = ("limitup", "hot", "theme", "flow", "watch", "all")


class PoolConfig:
    """股票池配置（config/pool.json）。"""

    def __init__(self, source: str = "limitup", limit: int = 30,
                 watch: Optional[List[str]] = None,
                 theme_top: int = 10,
                 sync: bool = True,
                 sync_fundamental: bool = True,
                 min_score: Optional[float] = None,
                 source_path: str = "") -> None:
        self.source = str(source or "limitup")
        self.limit = int(limit)
        self.watch = list(watch or [])
        self.theme_top = int(theme_top)
        self.sync = bool(sync)
        #: 基本面是季频数据，每只票要 2 次请求；候选池很大时关掉它更快
        self.sync_fundamental = bool(sync_fundamental)
        self.min_score = min_score
        self.path = source_path

    @classmethod
    def load(cls, path: Optional[str] = None) -> "PoolConfig":
        target = Path(path or settings.BASE_DIR / "config" / "pool.json")
        cfg = cls(source_path=str(target))
        if not target.exists():
            return cfg
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"股票池配置解析失败（用默认值）: {exc}")
            return cfg
        if not isinstance(raw, dict):
            return cfg
        cfg.source = str(raw.get("source") or cfg.source)
        cfg.limit = int(raw.get("limit") or cfg.limit)
        cfg.theme_top = int(raw.get("theme_top") or cfg.theme_top)
        cfg.sync = bool(raw.get("sync", cfg.sync))
        cfg.sync_fundamental = bool(raw.get("sync_fundamental", cfg.sync_fundamental))
        w = raw.get("watch")
        if isinstance(w, list):
            cfg.watch = [str(x).strip() for x in w if str(x).strip()]
        ms = raw.get("min_score")
        cfg.min_score = float(ms) if ms not in (None, "") else None
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source, "limit": self.limit, "watch": self.watch,
            "theme_top": self.theme_top, "sync": self.sync,
            "sync_fundamental": self.sync_fundamental,
            "min_score": self.min_score, "path": self.path,
        }


class StockPool:
    """股票池：拉候选 → 同步数据 → 打分 → 排名 → 落库。"""

    def __init__(self, config: Optional[PoolConfig] = None,
                 engine: Optional[ScoreEngine] = None,
                 provider: Optional[FeatureProvider] = None) -> None:
        self.config = config or PoolConfig.load()
        self.provider = provider or get_provider()
        self.engine = engine or ScoreEngine(config=ScoreConfig.load(),
                                            provider=self.provider)

    # ===================================================================
    # 候选池
    # ===================================================================
    def candidates(self, source: Optional[str] = None,
                   day: Any = None) -> Tuple[List[str], Dict[str, Any]]:
        """取候选标的。返回 (symbols, info)。

        取不到时**返回空列表而不是全市场** ——
        "候选池拉失败"和"候选池很大"是两回事，
        静默退化成全市场会把一次几十秒的操作变成几十分钟。
        """
        src = str(source or self.config.source)
        info: Dict[str, Any] = {"source": src}
        syms: List[str] = []

        if src == "watch":
            syms = list(self.config.watch)
            info["note"] = "来自 config/pool.json 的 watch 列表"
        elif src == "limitup":
            rows = R.limitup_pool(day, limit=500)
            syms = [r["symbol"] for r in rows]
            info["note"] = f"涨停池 {len(syms)} 只"
        elif src == "theme":
            boards = R.themes(day, limit=self.config.theme_top)
            # 反查"个股↔题材"映射表，而不是对每个板块拉一遍成分股 ——
            # 后者是 N 次网络请求，必然限流。
            syms = self._symbols_of_boards([b["board"] for b in boards])
            info["note"] = f"热门题材 {len(boards)} 个，成分股 {len(syms)} 只"
        elif src in ("hot", "flow", "all"):
            syms = self._live_symbols(src)
            info["note"] = f"实时来源 {src}"
        else:
            info["error"] = f"未知候选池来源: {src}（可用: {', '.join(SOURCES)}）"

        info["count"] = len(syms)
        return list(dict.fromkeys(syms)), info

    def _symbols_of_boards(self, boards: Sequence[str]) -> List[str]:
        out: List[str] = []
        from app.db.database import SessionLocal
        from app.db.models import StockTheme

        with SessionLocal() as s:
            rows = s.query(StockTheme).filter(StockTheme.board.in_(list(boards))).all()
        out = [r.symbol for r in rows]
        return out

    def _live_symbols(self, src: str) -> List[str]:
        if MX is None:
            return []
        try:
            if src == "hot":
                df = MX.fetch_hot_rank()
            elif src == "flow":
                df = MX.fetch_fund_flow_rank()
            else:
                df = MX.fetch_spot()
        except Exception as exc:
            logger.warning(f"候选池来源 {src} 获取失败: {exc}")
            return []
        return [str(x) for x in df.get("symbol", []) if x]

    # ===================================================================
    # 刷新
    # ===================================================================
    def refresh(self, source: Optional[str] = None, day: Any = None,
                limit: Optional[int] = None, do_sync: Optional[bool] = None,
                save: bool = True, symbols: Optional[Sequence[str]] = None
                ) -> Dict[str, Any]:
        """完整刷新一次：拉候选 → 同步数据 → 打分 → 落库。

        :param symbols: 直接指定标的时用它（跳过候选池）。
                        适合"就这几只票，给我排个序"的场景 ——
                        演示数据、自选股、批量回看都属于这一类。
        """
        from app.core.pool import sync as S

        src = "manual" if symbols else str(source or self.config.source)
        want_sync = self.config.sync if do_sync is None else bool(do_sync)
        if symbols:
            syms = [str(s).strip() for s in symbols if str(s).strip()]
            info = {"source": src, "note": f"指定 {len(syms)} 只", "count": len(syms)}
        else:
            syms, info = self.candidates(src, day)
        report: Dict[str, Any] = {"source": src, "candidates": info,
                                  "sync": {}, "count": 0}

        if not syms:
            report["error"] = info.get("error") or "候选池为空（先同步数据？）"
            return report

        if want_sync:
            report["sync"]["fund_flow"] = S.sync_fund_flow(syms)
            report["sync"]["chip"] = S.sync_chip(syms)
            if self.config.sync_fundamental:
                report["sync"]["fundamental"] = S.sync_fundamentals(syms)
            # 涨停池/题材是"市场级别"的数据，一次就够，不按标的重复拉
            if src == "limitup":
                report["sync"]["limitup"] = S.sync_limitup(_as_str(day))
            report["sync"]["boards"] = S.sync_boards("concept")
            report["sync"]["industry"] = S.sync_boards("industry")
            self.provider.invalidate()
        else:
            report["sync"]["skipped"] = True

        results = self.engine.score_many(syms, day, source=src)
        if self.config.min_score is not None:
            results = [r for r in results
                       if r.total is None or r.total >= float(self.config.min_score)]
        top_n = int(limit if limit is not None else self.config.limit)
        kept = results[:top_n] if top_n > 0 else results

        if save and kept:
            R.save_scores(kept, score_date=day, source=src)

        report["count"] = len(kept)
        report["results"] = [r.to_dict() for r in kept]
        return report

    # ===================================================================
    # 读取
    # ===================================================================
    @staticmethod
    def top(day: Any = None, limit: int = 30,
            min_score: Optional[float] = None) -> List[Dict[str, Any]]:
        return R.scores(day=day, limit=limit, min_score=min_score)

    @staticmethod
    def dates(limit: int = 30) -> List[str]:
        return R.score_dates(limit)


def _as_str(day: Any) -> Optional[str]:
    if day is None:
        return None
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    text = str(day)
    return text.replace("-", "")


__all__ = ["SOURCES", "PoolConfig", "StockPool"]
