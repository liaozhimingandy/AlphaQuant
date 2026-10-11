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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.common.config import settings
from app.feature.features import FeatureProvider, get_provider
from app.alpha.score import ScoreConfig, ScoreEngine, ScoreResult
from app.storage.repo import kline_repo as R
from app.common.logging import logger

try:
    from app.datasource.service import facade as MX
except Exception:  # pragma: no cover
    MX = None  # type: ignore[assignment]


#: 候选池来源
SOURCES = ("limitup", "hot", "theme", "flow", "watch", "all")

import re as _re

#: ST 股票代码缓存（进程内一次性拉取，盘中没有意义反复请求）
_ST_CACHE: Dict[str, Any] = {"ts": 0.0, "codes": set()}


def classify_board(sym: str) -> str:
    """按 A 股代码前缀判断板块。

    sh=上海主板(60) · sz=深圳主板(00/001/002/003) · cyb=创业板(30)
    kcb=科创板(68) · bj=北交所(8/4/920) · other=其它(含 B 股/异常码)
    """
    s = str(sym or "").strip().lower()
    s = _re.sub(r"^(sh|sz|bj)", "", s)
    if not _re.fullmatch(r"\d{6}", s):
        return "other"
    if s.startswith("60"):
        return "sh"
    if s.startswith("68"):
        return "kcb"
    if s.startswith("30"):
        return "cyb"
    if s.startswith(("00", "001", "002", "003")):
        return "sz"
    if s.startswith(("8", "4")) or s.startswith("920"):
        return "bj"
    return "other"


def _st_codes() -> set:
    """当前被标记 ST/*ST 的股票代码集合（6 位字符串）。

    拿不到（无网络/接口变更）时返回空集 —— 这时**跳过 ST 剔除**而不是
    误删一池子票，并在日志里提示。ST 名单盘中不变化，进程内缓存一次即可。
    """
    now = __import__("time").time()
    if _ST_CACHE["codes"] and (now - _ST_CACHE["ts"]) < 3600:
        return _ST_CACHE["codes"]
    try:
        import akshare as ak  # type: ignore

        df = ak.stock_zh_a_st_em()
        codes = set()
        col = "代码" if "代码" in getattr(df, "columns", []) else None
        if col and len(df):
            for v in df[col].tolist():
                codes.add(str(v).strip())
        _ST_CACHE["codes"] = codes
        _ST_CACHE["ts"] = now
        return codes
    except Exception as exc:
        logger.warning(f"ST 名单获取失败（本次不剔除 ST）: {exc}")
        return set()


def apply_exclusions(syms: Sequence[str],
                     exclude_st: bool = True, exclude_cyb: bool = False,
                     exclude_kcb: bool = False, exclude_bj: bool = False
                     ) -> Tuple[List[str], Dict[str, int]]:
    """按剔除条件过滤候选标的，返回 (保留列表, 各条件剔除数)。"""
    st_set = _st_codes() if exclude_st else set()
    kept: List[str] = []
    cnt = {"st": 0, "cyb": 0, "kcb": 0, "bj": 0}
    for sym in syms:
        board = classify_board(sym)
        if exclude_cyb and board == "cyb":
            cnt["cyb"] += 1
            continue
        if exclude_kcb and board == "kcb":
            cnt["kcb"] += 1
            continue
        if exclude_bj and board == "bj":
            cnt["bj"] += 1
            continue
        if exclude_st and sym in st_set:
            cnt["st"] += 1
            continue
        kept.append(sym)
    return kept, cnt



class PoolConfig:
    """股票池配置（config/pool.json）。"""

    def __init__(self, source: str = "limitup", limit: int = 30,
                 watch: Optional[List[str]] = None,
                 theme_top: int = 10,
                 sync: bool = True,
                 sync_fundamental: bool = True,
                 min_score: Optional[float] = None,
                 source_path: str = "",
                 exclude_st: bool = True,
                 exclude_cyb: bool = False,
                 exclude_kcb: bool = False,
                 exclude_bj: bool = False) -> None:
        self.source = str(source or "limitup")
        self.limit = int(limit)
        self.watch = list(watch or [])
        self.theme_top = int(theme_top)
        self.sync = bool(sync)
        #: 基本面是季频数据，每只票要 2 次请求；候选池很大时关掉它更快
        self.sync_fundamental = bool(sync_fundamental)
        self.min_score = min_score
        self.path = source_path
        #: 剔除条件：ST / 创业板(300) / 科创板(688) / 北交所(8或4开头)
        #: 「没有某板块交易权限」就勾上对应项即可，候选池不会混入这些票。
        self.exclude_st = bool(exclude_st)
        self.exclude_cyb = bool(exclude_cyb)
        self.exclude_kcb = bool(exclude_kcb)
        self.exclude_bj = bool(exclude_bj)

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
        cfg.exclude_st = bool(raw.get("exclude_st", cfg.exclude_st))
        cfg.exclude_cyb = bool(raw.get("exclude_cyb", cfg.exclude_cyb))
        cfg.exclude_kcb = bool(raw.get("exclude_kcb", cfg.exclude_kcb))
        cfg.exclude_bj = bool(raw.get("exclude_bj", cfg.exclude_bj))
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source, "limit": self.limit, "watch": self.watch,
            "theme_top": self.theme_top, "sync": self.sync,
            "sync_fundamental": self.sync_fundamental,
            "min_score": self.min_score, "path": self.path,
            "exclude_st": self.exclude_st, "exclude_cyb": self.exclude_cyb,
            "exclude_kcb": self.exclude_kcb, "exclude_bj": self.exclude_bj,
        }

    def save(self, path: Optional[str] = None) -> None:
        """把当前配置写回 config/pool.json（面板勾选的剔除条件要持久化）。"""
        target = Path(path or self.path or settings.BASE_DIR / "config" / "pool.json")
        try:
            raw: Dict[str, Any] = {}
            if target.exists():
                try:
                    raw = json.loads(target.read_text(encoding="utf-8")) or {}
                except Exception:
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            raw.update({
                "source": self.source, "limit": self.limit, "watch": self.watch,
                "theme_top": self.theme_top, "sync": self.sync,
                "sync_fundamental": self.sync_fundamental,
                "min_score": self.min_score,
                "exclude_st": self.exclude_st, "exclude_cyb": self.exclude_cyb,
                "exclude_kcb": self.exclude_kcb, "exclude_bj": self.exclude_bj,
            })
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except Exception as exc:
            logger.warning(f"股票池配置保存失败（沿用内存值）: {exc}")


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
            if not rows and day is not None:
                # 指定/回退交易日涨停池未入库（多半是非开盘日或首次刷新）
                # → 联网补拉一次，让刷新在非开盘日也能拿到上一个交易日的数据
                try:
                    from app.alpha.pool import sync as S

                    S.sync_limitup(_as_str(day))
                    rows = R.limitup_pool(day, limit=500)
                except Exception as exc:
                    logger.debug(f"涨停池联网补拉失败（{day}）: {exc}")
            syms = [r["symbol"] for r in rows]
            info["note"] = f"涨停池 {len(syms)} 只"
        elif src == "theme":
            boards = R.themes(day, limit=self.config.theme_top)
            if not boards and day is not None:
                try:
                    from app.alpha.pool import sync as S

                    S.sync_boards("concept")
                    S.sync_boards("industry")
                    boards = R.themes(day, limit=self.config.theme_top)
                except Exception as exc:
                    logger.debug(f"题材联网补拉失败（{day}）: {exc}")
            # 反查"个股↔题材"映射表，而不是对每个板块拉一遍成分股 ——
            # 后者是 N 次网络请求，必然限流。
            syms = self._symbols_of_boards([b["board"] for b in boards])
            info["note"] = f"热门题材 {len(boards)} 个，成分股 {len(syms)} 只"
        elif src == "hot":
            # 人气榜是实时榜单，但**落库兜底**：先读库（上一期还能用），
            # 库空且指定了交易日才联网补拉，再不行用实时榜单做最后兜底。
            # 否则代理一抽风，整页 0 条、且拉到的数据下次还得再拉。
            rows = R.hot_pool(day)
            reuse_day = ""
            if not rows:
                # 该日没采过 → 先复用库里"最近一天"已采到的榜单（别白联网）
                rows = R.latest_pool("hot", day, limit=500)
                if rows:
                    reuse_day = str(rows[0].get("date") or "")
            if not rows and day is not None:
                try:
                    from app.alpha.pool import sync as S

                    S.sync_hot_rank(_as_str(day))
                    rows = R.hot_pool(day)
                except Exception as exc:
                    logger.debug(f"人气榜联网补拉失败（{day}）: {exc}")
                    info["warning"] = f"人气榜补拉失败（{day}）：{exc}"
            if not rows:
                rows = [{"symbol": s} for s in self._live_symbols("hot", info)]
            syms = [r["symbol"] for r in rows]
            info["note"] = (f"人气榜 {len(syms)} 只"
                            + (f"（复用 {reuse_day}）" if reuse_day else ""))
        elif src == "flow":
            # 资金榜与人气榜同逻辑：先读库、库空补拉、再兜底实时。
            rows = R.flow_pool(day)
            reuse_day = ""
            if not rows:
                rows = R.latest_pool("flow", day, limit=500)
                if rows:
                    reuse_day = str(rows[0].get("date") or "")
            if not rows and day is not None:
                try:
                    from app.alpha.pool import sync as S

                    S.sync_flow_rank(_as_str(day))
                    rows = R.flow_pool(day)
                except Exception as exc:
                    logger.debug(f"资金榜联网补拉失败（{day}）: {exc}")
                    info["warning"] = f"资金榜补拉失败（{day}）：{exc}"
            if not rows:
                rows = [{"symbol": s} for s in self._live_symbols("flow", info)]
            syms = [r["symbol"] for r in rows]
            info["note"] = (f"资金榜 {len(syms)} 只"
                            + (f"（复用 {reuse_day}）" if reuse_day else ""))
        elif src == "all":
            syms = self._live_symbols(src)
            info["note"] = f"实时来源 {src}"
        else:
            info["error"] = f"未知候选池来源: {src}（可用: {', '.join(SOURCES)}）"

        info["count"] = len(syms)
        return list(dict.fromkeys(syms)), info

    def _symbols_of_boards(self, boards: Sequence[str]) -> List[str]:
        out: List[str] = []
        from app.storage.engine import SessionLocal
        from app.storage.schema import StockTheme

        with SessionLocal() as s:
            rows = s.query(StockTheme).filter(StockTheme.board.in_(list(boards))).all()
        out = [r.symbol for r in rows]
        return out

    def _live_symbols(self, src: str, info: Optional[Dict[str, Any]] = None
                      ) -> List[str]:
        """实时榜单兜底：数据源注册表会依次尝试所有源（重试 N 次 → 换源）。

        失败原因（含"用了哪些源、各试几次"）写进 ``info["warning"]``，
        由 refresh 汇总到报告里 —— 否则界面上只剩一句"候选池为空"，
        根本看不出是网络问题还是真的没数据。
        """
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
            if info is not None:
                info["warning"] = f"候选池来源 {src} 采集失败：{exc}"
            return []
        return [str(x) for x in df.get("symbol", []) if x]

    # ===================================================================
    # 刷新
    # ===================================================================
    def refresh(self, source: Optional[str] = None, day: Any = None,
                limit: Optional[int] = None, do_sync: Optional[bool] = None,
                save: bool = True, symbols: Optional[Sequence[str]] = None,
                exclusions: Optional[Dict[str, bool]] = None,
                force: bool = False
                ) -> Dict[str, Any]:
        """完整刷新一次：拉候选 → 同步数据 → 打分 → 落库。

        :param symbols: 直接指定标的时用它（跳过候选池）。
                        适合"就这几只票，给我排个序"的场景 ——
                        演示数据、自选股、批量回看都属于这一类。
        :param exclusions: 面板勾选的剔除条件
                         {exclude_st, exclude_cyb, exclude_kcb, exclude_bj}；
                         不传则用 config 里的默认值。传了会写回 config/pool.json。
        :param force: True = 忽略"库里已有"判断，强制重新联网采集。
                         默认 False —— 同一交易日的数据拉第二次拿到的还是同一份，
                         重复请求只会白白消耗额度并招来限流。
        """
        from app.alpha.pool import sync as S

        if exclusions:
            self.config.exclude_st = bool(exclusions.get("exclude_st", self.config.exclude_st))
            self.config.exclude_cyb = bool(exclusions.get("exclude_cyb", self.config.exclude_cyb))
            self.config.exclude_kcb = bool(exclusions.get("exclude_kcb", self.config.exclude_kcb))
            self.config.exclude_bj = bool(exclusions.get("exclude_bj", self.config.exclude_bj))
            self.config.save()
        excl = {
            "exclude_st": self.config.exclude_st,
            "exclude_cyb": self.config.exclude_cyb,
            "exclude_kcb": self.config.exclude_kcb,
            "exclude_bj": self.config.exclude_bj,
        }

        # 未开盘时回退到"上一个交易日"——否则取当天会拿到空数据。
        # 显式传 day 的场景（CLI/回测）尊重调用方，不做覆盖。
        is_recent_day = False
        if day is None:
            from app.domain.calendar import get_calendar

            day, is_recent_day = _resolve_refresh_day(get_calendar(), datetime.now())
        report: Dict[str, Any] = {
            "source": "",
            "candidates": {},
            "sync": {},
            "count": 0,
            "data_day": _as_str(day),
            "is_recent_day": bool(is_recent_day),
        }

        src = "manual" if symbols else str(source or self.config.source)
        want_sync = self.config.sync if do_sync is None else bool(do_sync)
        if symbols:
            syms = [str(s).strip() for s in symbols if str(s).strip()]
            info = {"source": src, "note": f"指定 {len(syms)} 只", "count": len(syms)}
        else:
            syms, info = self.candidates(src, day)
        report["source"] = src
        report["candidates"] = info

        if not syms:
            report["error"] = info.get("error") or "候选池为空（先同步数据？）"
            # 候选池为空时必须把"为什么"带上：是源全挂了，还是真的没票？
            if info.get("warning"):
                report["warnings"] = [info["warning"]]
                logger.warning(info["warning"])
            return report

        # ---- 剔除 ST / 创业板 / 科创板 / 北交所 ----
        kept, excl_cnt = apply_exclusions(
            syms, excl["exclude_st"], excl["exclude_cyb"],
            excl["exclude_kcb"], excl["exclude_bj"])
        report["excluded"] = excl_cnt
        removed = len(syms) - len(kept)
        if removed:
            info["note"] = (info.get("note") or "") + f" ｜ 剔除 {removed} 只"
        syms = kept

        if not syms:
            report["error"] = "剔除后候选池为空（放宽剔除条件再试）"
            return report

        if want_sync:
            # 各 sync 内部会先看库里有没有当日数据，有就跳过（force=True 除外）
            report["sync"]["fund_flow"] = S.sync_fund_flow(syms, force=force, day=day)
            report["sync"]["chip"] = S.sync_chip(syms, force=force, day=day)
            if self.config.sync_fundamental:
                report["sync"]["fundamental"] = S.sync_fundamentals(syms)
            # 涨停池/题材是"市场级别"的数据，一次就够，不按标的重复拉
            if src == "limitup":
                report["sync"]["limitup"] = S.sync_limitup(_as_str(day), force=force)
            elif src == "hot":
                # 候选池已先读库+补拉；这里再确保一次（当天想拿最新也行），
                # 顺手把名称灌进对照表，评分列表才能显示标的名称。
                report["sync"]["hot"] = S.sync_hot_rank(_as_str(day), force=force)
            elif src == "flow":
                report["sync"]["flow"] = S.sync_flow_rank(_as_str(day), force=force)
            report["sync"]["boards"] = S.sync_boards("concept", force=force, day=day)
            report["sync"]["industry"] = S.sync_boards("industry", force=force, day=day)
            # 名称对照表为空时一次性补齐（让"600042 是哪只票"这类问题有答案）
            try:
                if R.stock_info_count() == 0:
                    report["sync"]["stock_info"] = S.sync_stock_info()
            except Exception as exc:
                logger.debug(f"名称对照表补齐失败（忽略）: {exc}")
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
        self._attach_diagnostics(report, info)
        return report

    def _attach_diagnostics(self, report: Dict[str, Any],
                            info: Dict[str, Any]) -> None:
        """把"这次刷新到底发生了什么"汇总进报告，供界面直接显示。

        两块内容：
          * ``warnings``     —— 需要用户知道的告警（候选池源全挂、同步失败…）；
          * ``data_sources`` —— 每种数据实际用的是哪个源（回答"数据从哪来的"）。
        """
        warnings: List[str] = []
        if info.get("warning"):
            warnings.append(str(info["warning"]))

        skipped: List[str] = []
        for key, res in (report.get("sync") or {}).items():
            if not isinstance(res, dict):
                continue
            if res.get("skipped"):
                skipped.append(f"{key} 已存在({res.get('skipped')}条)")
            if res.get("failed"):
                n = len(res["failed"])
                sample = "；".join(f"{s}→{str(r)[:60]}" for s, r in res["failed"][:2])
                warnings.append(f"{key} 同步失败 {n} 项（样例: {sample}）")

        try:
            from app.datasource.compat.registry import REGISTRY
            report["data_sources"] = REGISTRY.used_sources()
            report["source_events"] = REGISTRY.recent_events(10)
        except Exception as exc:  # 诊断信息缺失不能影响主流程
            logger.debug(f"数据源诊断信息读取失败（忽略）: {exc}")

        if skipped:
            report["skipped"] = skipped
            logger.info("本次刷新跳过重复采集: " + "、".join(skipped))
        if warnings:
            report["warnings"] = warnings
            for w in warnings:
                logger.warning(f"股票池刷新告警: {w}")

    # ===================================================================
    # 读取
    # ===================================================================
    @staticmethod
    def top(day: Any = None, limit: int = 30,
            min_score: Optional[float] = None,
            source: str = "") -> List[Dict[str, Any]]:
        return R.scores(day=day, limit=limit, min_score=min_score, source=source)

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


def _resolve_refresh_day(cal: Any, now: Optional[datetime] = None
                         ) -> Tuple[date, bool]:
    """决定"这次刷新用哪一天的数据"。

    返回 (数据日期, 是否为回退的历史交易日)。

    - 此刻处于可采窗口（交易中 / 午休 / 收盘结算缓冲）时，用今天 ——
      今天的数据已经定稿（或盘中实时），没必要回退。
    - 未开盘（非交易日 / 盘前 / 已收市且超出结算缓冲）时，**回退到最近的
      已收盘交易日**，这样周末、节假日、盘前点刷新都能拿到"上一个开盘日"
      的数据，而不是因为取了当天而拿到空。

    用户诉求："如果未开盘，数据获取上一个开盘日数据"。
    """
    now = now or datetime.now()
    if cal.is_open(now):
        return now.date(), False
    d = now.date()
    for _ in range(15):                       # 假期最长 ~10 天，15 足够
        d -= timedelta(days=1)
        if cal.is_trading_day(d):
            return d, True
    return d, True



__all__ = ["SOURCES", "PoolConfig", "StockPool"]
