#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票池视图：配置/取票/日期列表/刷新/个股评分。"""
from __future__ import annotations


from datetime import date, datetime
from typing import Any, Deque, Dict, List, Optional

from app.common.logging import logger


class PoolViewsMixin:
    # ============================================================
    # 股票池 / 综合评分
    # ============================================================
    def pool_config_view(self) -> Dict[str, Any]:
        try:
            from app.modules.universe.pool import PoolConfig, SOURCES
            from app.modules.universe.score import ScoreConfig

            return {
                **PoolConfig.load().to_dict(),
                "sources": list(SOURCES),
                "weights": ScoreConfig.load().weights,
            }
        except Exception as exc:
            logger.debug(f"股票池配置读取失败: {exc}")
            return {}

    def pool_top(self, limit: int = 30, source: str = "", day: Any = None
                 ) -> List[Dict[str, Any]]:
        try:
            from app.modules.universe.pool import PoolConfig, StockPool
            from app.data.repo import kline_repo as R

            # 不指定日期时，读"库里最新的一个评分日"——
            # 否则未开盘时分数都按上一交易日存着、列表却读今天，会显示一片空。
            if day in (None, ""):
                ds = R.score_dates(1)
                day = ds[0] if ds else None
            # 来源为空（前端"（默认）"选项）时，回退到配置里的默认候选池来源，
            # 让"读取"与"刷新"口径一致：否则读取会把所有来源的分数都捞出来，
            # 出现"刷新说 11 只、表格却 15 只"的错位。
            if not source:
                source = PoolConfig.load().source or ""
            return StockPool.top(limit=int(limit), source=source, day=day)
        except Exception as exc:
            logger.debug(f"读取股票池失败: {exc}")
            return []

    def pool_dates(self) -> List[str]:
        try:
            from app.modules.universe.pool import StockPool

            return StockPool.dates()
        except Exception:
            return []

    def refresh_pool(self, source: str = "", limit: Any = None,
                     sync: Any = None, symbols: Any = None,
                     exclusions: Any = None, force: Any = None) -> Dict[str, Any]:
        """刷新股票池（工作线程执行）。

        面板点一下就可能触发几十只票的联网同步 + 打分，
        同步阻塞会把行情、策略、撮合一起停摆。

        :param force: True = 忽略"库里已有"判断强制重采。默认 False：
            同一交易日的数据重复拉没有意义，还容易被限流。
        """
        try:
            from app.modules.universe.pool import StockPool

            pool = StockPool()
            excl = None
            if isinstance(exclusions, dict):
                excl = {k: bool(v) for k, v in exclusions.items()}
            report = pool.refresh(
                source=source or None,
                limit=int(limit) if limit else None,
                do_sync=None if sync is None else bool(sync),
                symbols=list(symbols) if symbols else None,
                exclusions=excl,
                force=bool(force),
            )
            if report.get("error"):
                return {"ok": False, "error": report["error"]}
            self.audit.append({
                "at": datetime.now().isoformat(), "action": "pool_refresh",
                "detail": {"source": report.get("source"),
                           "count": report.get("count")},
            })
            return {"ok": True, **report}
        except Exception as exc:
            logger.error(f"股票池刷新失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def stock_score(self, symbol: str) -> Dict[str, Any]:
        """单只票的多维度评分明细。"""
        try:
            from app.modules.factor.features import get_provider
            from app.modules.universe.score import ScoreEngine
            from app.domain.symbol import normalize_symbol
            from app.data.repo import kline_repo as R

            sym = normalize_symbol(symbol)
            r = ScoreEngine(provider=get_provider(ttl=0)).score(sym)
            out = {"ok": True, "symbol": sym, **r.to_dict()}
            try:
                names = R.resolve_names([sym])
                out["name"] = names.get(sym, "") or ""
            except Exception:
                out["name"] = ""
            return out
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
