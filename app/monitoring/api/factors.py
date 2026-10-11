#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子实验室视图：列表/校验/增删/检验/挖掘。"""
from __future__ import annotations


from typing import Any, Deque, Dict, List, Optional

from app.common.logging import logger


class FactorViewsMixin:
    # ============================================================
    # 因子实验室
    # ============================================================
    def factors_view(self) -> Dict[str, Any]:
        """因子总目录（按类别分组）+ 算子/变量速查。

        **必须是全量因子**，不只是因子库里的表达式因子 ——
        只显示 8 条会给用户一个错觉：以为系统只有这几个因子可用，
        于是题材/基本面/资金/舆情/宏观那几十个从来没被用过。
        """
        try:
            from app.feature.catalog import CATEGORY_DOCS, factor_catalog
            from app.feature.expr import func_catalog, variable_catalog
            from app.feature.library import get_library

            lib = get_library()
            cat = factor_catalog()
            return {
                "factors": [s.to_dict() for s in lib.list()],
                "groups": cat["categories"],
                "group_docs": CATEGORY_DOCS,
                "total": cat["total"],
                "categories": lib.categories(),
                "ops": func_catalog(),
                "vars": variable_catalog(),
                "path": str(lib.path),
            }
        except Exception as exc:
            logger.debug(f"因子库读取失败: {exc}")
            return {"factors": [], "groups": [], "ops": [], "vars": []}

    def factor_validate(self, expr: str) -> Dict[str, Any]:
        try:
            from app.feature.expr import validate

            return validate(expr)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def factor_add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.feature.library import get_library

            ok, msg, spec = get_library().add(
                str(body.get("name") or ""),
                str(body.get("expr") or ""),
                description=str(body.get("description") or ""),
                category=str(body.get("category") or "自定义"),
                overwrite=bool(body.get("overwrite")),
            )
            out: Dict[str, Any] = {"ok": ok, "message": msg}
            if spec is not None:
                out["factor"] = spec.to_dict()
            return out
        except Exception as exc:
            logger.error(f"新增因子失败: {exc}", exc_info=True)
            return {"ok": False, "message": str(exc)}

    def factor_remove(self, name: str) -> Dict[str, Any]:
        try:
            from app.feature.library import get_library

            ok, msg = get_library().remove(name)
            return {"ok": ok, "message": msg}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def factor_check(self, expr: str, symbols: Any = None,
                     horizon: int = 5) -> Dict[str, Any]:
        """检验一个因子（工作线程执行）。"""
        try:
            from app.feature.evaluate import evaluate_factor

            rep = evaluate_factor(expr, self._factor_symbols(symbols),
                                  horizon=int(horizon))
            return {"ok": True, "report": rep.to_dict()}
        except Exception as exc:
            logger.error(f"因子检验失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def factor_mine(self, symbols: Any = None, horizon: int = 5,
                    generations: int = 10, population: int = 50,
                    seconds: float = 60, min_score: float = 45,
                    save: bool = False) -> Dict[str, Any]:
        """遗传算法挖掘（工作线程执行）。"""
        try:
            from app.feature.evaluate import load_panel
            from app.feature.library import SOURCE_MINED, get_library
            from app.feature.mining.mining import FactorMiner

            syms = self._factor_symbols(symbols)
            panel = load_panel(syms)
            if len(panel) < 3:
                return {"ok": False, "error":
                        f"可用标的只有 {len(panel)} 个，截面检验至少需要 3 个"}
            miner = FactorMiner(panel, horizon=int(horizon), seed=None)
            found = miner.run(generations=int(generations),
                              population=int(population), top=10,
                              max_seconds=float(seconds),
                              min_score=float(min_score))
            saved = 0
            if save and found:
                lib = get_library()
                for f in found:
                    ok, _, _ = lib.add(
                        f"mined_{abs(hash(f.expr)) % 100000:05d}", f.expr,
                        description=f"自动挖掘（{f.score}分）",
                        category="挖掘", source=SOURCE_MINED,
                        metrics={"ic": f.ic, "icir": f.icir, "score": f.score},
                        overwrite=True)
                    saved += bool(ok)
            return {
                "ok": True, "evaluated": miner.evaluated, "saved": saved,
                "factors": [f.to_dict() for f in found],
            }
        except Exception as exc:
            logger.error(f"因子挖掘失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def _factor_symbols(self, symbols: Any = None) -> List[str]:
        if symbols:
            return [str(s) for s in symbols]
        from app.storage.engine import SessionLocal
        from app.storage.repo.stock_repo import StockRepository

        try:
            with SessionLocal() as db:
                return [r["symbol"] for r in StockRepository.list_symbols(db)]
        except Exception:
            return []
