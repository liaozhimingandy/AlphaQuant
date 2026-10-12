#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : evaluate.py
# @Description : 因子检验：这个因子到底有没有预测力
#
#                为什么必须先有检验才能谈"机器学习找因子"：
#                  挖掘算法一天能吐出几千个表达式，里面绝大多数是**噪音**。
#                  没有统一的判据，就只能靠人眼看曲线挑 —— 那不是自动挖掘，
#                  那是自动制造焦虑。所以检验标准是整套流程的地基：
#                  它决定了"什么算好因子"，也就决定了挖掘往哪个方向走。
#
#                用什么判据：
#                  IC      因子值与未来收益的相关系数（**截面**：同一天横着比）
#                  RankIC  秩相关，抗极值，实践中比 IC 更稳
#                  ICIR    IC 的均值/标准差 —— 衡量的是**稳定性**，不是强度
#                          （一个偶尔很准、平时乱指的因子，ICIR 会很差）
#                  分层收益 按因子值分 5 组，看各组未来收益是否**单调**
#                          单调比"第一组最高"更可信：只有一组高很可能是噪音
#                  t 值    IC 均值是否显著不为 0（样本少时尤其重要）
#
#                为什么用**截面**而不是时间序列相关：
#                  时间序列上"因子 vs 未来收益"的相关会被趋势主导 ——
#                  牛市里几乎所有因子都"有效"，那是假的。
#                  同一天在所有标的之间横着比，才是在问"这个因子能不能区分好坏"。
#                  这也是为什么检验至少要 3 个以上标的才有意义。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from app.modules.factor.expr import ExprError, evaluate
from app.common.logging import logger


@dataclass
class FactorReport:
    """一份因子检验报告。"""

    name: str
    expr: str
    symbols: int = 0
    rows: int = 0
    dates: int = 0
    horizon: int = 5

    ic_mean: Optional[float] = None
    ic_std: Optional[float] = None
    icir: Optional[float] = None
    ic_positive_rate: Optional[float] = None
    ic_t: Optional[float] = None
    rank_ic_mean: Optional[float] = None
    rank_ic_ir: Optional[float] = None

    #: 分层（1 组最差 → 5 组最好）的日均未来收益 %
    quantile_returns: Dict[str, float] = field(default_factory=dict)
    #: 多空（最好组 - 最差组）日均收益 %
    long_short: Optional[float] = None
    #: 分层是否单调（含容差）
    monotonic: bool = False
    #: 有效样本占比
    coverage: float = 0.0
    #: 一句话结论
    verdict: str = ""
    score: Optional[float] = None      # 0~100 的综合可用性分
    #: 检验口径：cross_section（推荐，横着比）/ timeseries（标的太少时的兜底）
    mode: str = "cross_section"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "expr": self.expr, "symbols": self.symbols,
            "rows": self.rows, "dates": self.dates, "horizon": self.horizon,
            "ic_mean": self.ic_mean, "ic_std": self.ic_std, "icir": self.icir,
            "ic_positive_rate": self.ic_positive_rate, "ic_t": self.ic_t,
            "rank_ic_mean": self.rank_ic_mean, "rank_ic_ir": self.rank_ic_ir,
            "quantile_returns": dict(self.quantile_returns),
            "long_short": self.long_short, "monotonic": self.monotonic,
            "coverage": self.coverage, "verdict": self.verdict,
            "score": self.score, "mode": self.mode,
        }


# ===========================================================================
# 数据准备
# ===========================================================================
def load_panel(symbols: Sequence[str], start: Any = None,
               end: Any = None) -> Dict[str, pd.DataFrame]:
    """从本地库读多只票的日线。读不到就返回空（不联网）。"""
    from app.data.engine import SessionLocal
    from app.data.repo.stock_repo import StockRepository

    out: Dict[str, pd.DataFrame] = {}
    if not symbols:
        return out
    with SessionLocal() as db:
        for sym in symbols:
            try:
                df = StockRepository.get_stock_df(db, sym, start, end)
            except Exception as exc:
                logger.debug(f"读取 {sym} 失败: {exc}")
                continue
            if df is not None and not df.empty:
                out[str(sym)] = df
    return out


def compute_factor(expr: str, df: pd.DataFrame) -> Optional[np.ndarray]:
    """在单只票上算因子值。"""
    try:
        return evaluate(expr, {
            "open": df["open"].to_numpy(dtype=float),
            "high": df["high"].to_numpy(dtype=float),
            "low": df["low"].to_numpy(dtype=float),
            "close": df["close"].to_numpy(dtype=float),
            "volume": df["volume"].to_numpy(dtype=float),
            "amount": df["amount"].to_numpy(dtype=float)
            if "amount" in df else np.zeros(len(df)),
        })
    except ExprError as exc:
        raise
    except Exception as exc:
        logger.debug(f"因子计算失败: {exc}")
        return None


def build_matrix(expr: str, panel: Dict[str, pd.DataFrame],
                 horizon: int = 5) -> pd.DataFrame:
    """拼成 [date × symbol] 的因子矩阵与未来收益矩阵。

    :return: 长表 DataFrame: date, symbol, factor, fwd
    """
    # 向量化拼装：挖掘时要跑几千个候选，逐行 append 会让整轮挖掘慢一个数量级
    frames: List[pd.DataFrame] = []
    for sym, df in panel.items():
        if df is None or len(df) < 5:
            continue
        try:
            fv = compute_factor(expr, df)
        except ExprError:
            raise
        if fv is None:
            continue
        close = df["close"].to_numpy(dtype=float)
        # 未来收益：t 日收盘 → t+horizon 日收盘。这是**标签**，
        # 不是因子输入，所以用 shift(-n) 是正确且必要的。
        fwd = np.full(len(close), np.nan)
        if len(close) > horizon:
            prev = np.where(close[:-horizon] == 0, np.nan, close[:-horizon])
            fwd[:len(close) - horizon] = (close[horizon:] / prev - 1.0) * 100.0
        n = min(len(fv), len(close))
        frames.append(pd.DataFrame({
            "date": df.index[:n], "symbol": str(sym),
            "factor": fv[:n], "fwd": fwd[:n],
        }))
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", "factor", "fwd"])
    m = pd.concat(frames, ignore_index=True)
    return m.dropna(subset=["factor", "fwd"]).reset_index(drop=True)


# ===========================================================================
# 检验
# ===========================================================================
def _safe_corr(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 3:
        return None
    x, y = a[ok], b[ok]
    if np.std(x) == 0 or np.std(y) == 0:
        return None
    v = float(np.corrcoef(x, y)[0, 1])
    return None if v != v else v


def _rowwise_corr(fmat: pd.DataFrame, rmat: pd.DataFrame) -> List[float]:
    """按行（每个交易日）算相关系数，返回有效值列表。

    用协方差的展开式一次算完整列，避免逐行 groupby。
    少于 3 个有效标的的行会被丢掉 —— 2 个点的相关系数恒为 ±1，没有意义。
    """
    if fmat is None or rmat is None or fmat.empty:
        return []
    rmat = rmat.reindex_like(fmat)
    valid = fmat.notna() & rmat.notna()
    n = valid.sum(axis=1).astype(float)
    X = fmat.where(valid)
    Y = rmat.where(valid)
    sx = X.sum(axis=1)
    sy = Y.sum(axis=1)
    sxy = (X * Y).sum(axis=1)
    sxx = (X * X).sum(axis=1)
    syy = (Y * Y).sum(axis=1)
    cov = n * sxy - sx * sy
    varx = n * sxx - sx ** 2
    vary = n * syy - sy ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        ic = cov / np.sqrt(varx * vary)
    ok = (n >= 3) & (varx > 0) & (vary > 0) & np.isfinite(ic)
    return [float(v) for v in ic[ok].tolist()]


def _spearman(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 3:
        return None
    ra = pd.Series(a[ok]).rank().to_numpy()
    rb = pd.Series(b[ok]).rank().to_numpy()
    return _safe_corr(ra, rb)


def evaluate_factor(expr: str, symbols: Sequence[str], start: Any = None,
                    end: Any = None, horizon: int = 5,
                    groups: int = 5, name: str = "") -> FactorReport:
    """检验一个表达式因子的有效性。

    :param horizon: 未来多少天的收益作为标签（默认 5 日）
    :param groups:  分层组数

    标的少于 3 个时退到**时间序列口径**，并在结论里明确标注 ——
    截面口径才是"这个因子能不能区分好坏"，时间序列上会被趋势主导
    （牛市里几乎所有因子都显得有效）。所以宁可少给点能力，也不能让人
    拿着一个口径更弱的分数当真。
    """
    rep = FactorReport(name=name or expr[:40], expr=expr, horizon=int(horizon))
    panel = load_panel(symbols, start, end)
    rep.symbols = len(panel)
    if not panel:
        rep.verdict = "没有可用的行情数据（先采集数据，或换一组标的）"
        return rep

    try:
        m = build_matrix(expr, panel, horizon=int(horizon))
    except ExprError as exc:
        rep.verdict = f"表达式有错：{exc}"
        return rep

    if len(panel) >= 3:
        return evaluate_matrix(m, rep, groups=int(groups),
                               total_rows=sum(len(d) for d in panel.values()))
    return evaluate_timeseries(expr, panel, rep, horizon=int(horizon),
                               total_rows=sum(len(d) for d in panel.values()))


def evaluate_matrix(m: pd.DataFrame, rep: FactorReport, groups: int = 5,
                    total_rows: int = 0) -> FactorReport:
    """在已经拼好的 [date × symbol] 长表上做检验。

    与 :func:`evaluate_factor` 分开，是为了让**检验逻辑可以脱离数据库测试** ——
    挖掘时要跑几千个候选，每个都读一遍库是不现实的。
    """
    if m is None or len(m) == 0:
        rep.verdict = "没有有效样本"
        return rep
    rep.rows = len(m)
    if len(m) < 30:
        rep.verdict = f"有效样本太少（{len(m)} 行），至少需要 30 行才能下结论"
        return rep
    rep.dates = int(m["date"].nunique())

    # ---- 截面 IC：每个交易日横着算一次相关 ----
    #     矩阵化实现（而不是 for 循环 groupby）：挖掘时要跑几千个候选，
    #     逐日 groupby 会让一轮挖掘慢一个数量级。
    fmat = m.pivot(index="date", columns="symbol", values="factor")
    rmat = m.pivot(index="date", columns="symbol", values="fwd")
    ic_by_day = _rowwise_corr(fmat, rmat)
    ric_by_day = _rowwise_corr(fmat.rank(axis=1), rmat.rank(axis=1))

    if len(ic_by_day) >= 5:
        arr = np.array(ic_by_day)
        rep.ic_mean = float(np.mean(arr))
        rep.ic_std = float(np.std(arr, ddof=1))
        rep.icir = (rep.ic_mean / rep.ic_std) if rep.ic_std and rep.ic_std > 0 else None
        rep.ic_positive_rate = float((arr > 0).mean())
        rep.ic_t = (rep.ic_mean / (rep.ic_std / np.sqrt(len(arr)))
                    ) if rep.ic_std and rep.ic_std > 0 else None
    if len(ric_by_day) >= 5:
        rarr = np.array(ric_by_day)
        rep.rank_ic_mean = float(np.mean(rarr))
        sd = float(np.std(rarr, ddof=1))
        rep.rank_ic_ir = (rep.rank_ic_mean / sd) if sd > 0 else None

    # ---- 分层：每天按因子值分组，看各组未来收益 ----
    #     用"当日截面"分组，天然因果（不碰未来数据）
    #     groups=0 表示跳过 —— 挖掘时要跑几千个候选，分层是最贵的一步，
    #     先用 IC 快速筛掉绝大多数，只对幸存者做分层。
    qs: Dict[int, List[float]] = {}
    if groups and groups > 1:
        try:
            # 当天截面内的分位分组（因果：只用到当天横截面信息）
            pct = fmat.rank(axis=1, pct=True)
            q = np.floor(pct * groups).clip(0, groups - 1)
            valid = (fmat.notna() & rmat.notna()).to_numpy()
            qv = q.to_numpy()
            rv = rmat.to_numpy()
            for g in range(int(groups)):
                mask = valid & (qv == g)
                vals = rv[mask]
                vals = vals[~np.isnan(vals)]
                if len(vals) >= 5:
                    qs[g + 1] = vals.tolist()
        except Exception as exc:
            logger.debug(f"分层失败: {exc}")

    if qs:
        rep.quantile_returns = {
            str(k): round(float(np.mean(v)), 4) for k, v in sorted(qs.items())
        }
        vals = [rep.quantile_returns[str(k)] for k in sorted(qs)]
        rep.long_short = round(vals[-1] - vals[0], 4)
        # 单调：允许小回撤（噪声），整体趋势必须向上
        diffs = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        tol = max(1e-9, (max(vals) - min(vals)) * 0.15)
        rep.monotonic = all(d >= -tol for d in diffs) and (vals[-1] > vals[0])

    rep.coverage = round(float(len(m) / max(1, total_rows or len(m))), 4)
    rep.score = _score(rep)
    rep.verdict = _verdict(rep)
    return rep


def evaluate_timeseries(expr: str, panel: Dict[str, pd.DataFrame],
                        rep: FactorReport, horizon: int = 5,
                        window: int = 60,
                        total_rows: int = 0) -> FactorReport:
    """标的不足时的兜底口径：滚动窗口的时间序列 IC。

    只在 < 3 个标的时用。结论里会写明口径更弱，避免被当成截面结果用。
    """
    rep.mode = "timeseries"
    all_ic: List[float] = []
    n_rows = 0
    for df in panel.values():
        try:
            fv = compute_factor(expr, df)
        except ExprError as exc:
            rep.verdict = f"表达式有错：{exc}"
            return rep
        if fv is None:
            continue
        close = df["close"].to_numpy(dtype=float)
        fwd = np.full(len(close), np.nan)
        if len(close) > horizon:
            prev = np.where(close[:-horizon] == 0, np.nan, close[:-horizon])
            fwd[:len(close) - horizon] = (close[horizon:] / prev - 1.0) * 100.0
        s = pd.DataFrame({"factor": fv, "fwd": fwd}).dropna()
        n_rows += len(s)
        if len(s) < window + 10:
            continue
        roll = s["factor"].rolling(window).corr(s["fwd"])
        all_ic.extend([float(v) for v in roll.dropna().tolist()])

    rep.rows = n_rows
    rep.dates = int(sum(len(d) for d in panel.values()))
    if len(all_ic) < 10:
        rep.verdict = (f"样本不足（{n_rows} 行）：截面检验至少需要 3 个标的，"
                       "时间序列兜底也没攒够窗口。先多采集几个标的。")
        return rep

    arr = np.array(all_ic)
    rep.ic_mean = float(np.mean(arr))
    rep.ic_std = float(np.std(arr, ddof=1))
    rep.icir = (rep.ic_mean / rep.ic_std) if rep.ic_std > 0 else None
    rep.ic_positive_rate = float((arr > 0).mean())
    rep.ic_t = (rep.ic_mean / (rep.ic_std / np.sqrt(len(arr)))
                ) if rep.ic_std > 0 else None
    rep.coverage = round(float(n_rows / max(1, total_rows or n_rows)), 4)
    rep.score = _score(rep)
    rep.verdict = (
        f"[时间序列口径 · 只有 {len(panel)} 个标的] {_verdict(rep)}"
        f" —— 建议采集 ≥3 个标的后用截面口径复验"
    )
    return rep


def _score(rep: FactorReport) -> Optional[float]:
    """0~100 的可用性分。

    刻意把**稳定性**放在强度前面：IC 均值高但 IR 低的因子，
    实盘上表现为"偶尔大赚、长期磨损"，比没有更危险。
    """
    if rep.ic_mean is None or rep.ic_std is None:
        return None
    parts = []
    # |IC| 强度：0.03 已经不错，0.08 以上很罕见
    parts.append((min(1.0, abs(rep.ic_mean) / 0.08) * 100, 0.35))
    # ICIR 稳定性：0.5 是常用门槛
    ir = abs(rep.icir or 0.0)
    parts.append((min(1.0, ir / 0.8) * 100, 0.30))
    # 正 IC 占比（胜率）
    parts.append(((rep.ic_positive_rate or 0.0) * 100, 0.15))
    # 分层单调性（没做分层时不参与评分，避免凭空扣 20 分）
    if rep.quantile_returns:
        parts.append((100.0 if rep.monotonic else 30.0, 0.20))
    return round(sum(v * w for v, w in parts) / sum(w for _, w in parts), 1)


def _verdict(rep: FactorReport) -> str:
    if rep.score is None:
        return "样本不足，无法下结论"
    if rep.score >= 70:
        return f"可用（{rep.score}分）：IC {rep.ic_mean:+.3f}，ICIR {rep.icir:+.2f}，分层单调"
    if rep.score >= 45:
        return (f"偏弱（{rep.score}分）：IC {rep.ic_mean:+.3f}，"
                f"ICIR {rep.icir if rep.icir else 0:+.2f} —— 建议与其它因子组合使用")
    return (f"无效（{rep.score}分）：IC {rep.ic_mean:+.3f}，"
            f"IC 不稳定或分层不单调 —— 不建议单独使用")


# ===========================================================================
# 批量
# ===========================================================================
def evaluate_many(candidates: Sequence[Dict[str, str]], symbols: Sequence[str],
                  start: Any = None, end: Any = None, horizon: int = 5,
                  top: int = 20) -> List[FactorReport]:
    """一次检验多个候选（挖掘出来的表达式通常成百上千）。

    失败的候选**静默跳过**：挖掘里绝大多数候选本来就该被淘汰，
    为每个失败打一条日志等于用噪音淹没真正的问题。
    """
    out: List[FactorReport] = []
    for c in candidates:
        expr = c.get("expr") if isinstance(c, dict) else str(c)
        name = (c.get("name") if isinstance(c, dict) else "") or ""
        try:
            rep = evaluate_factor(expr, symbols, start, end, horizon, name=name)
        except Exception:
            continue
        if rep.score is None:
            continue
        out.append(rep)
    out.sort(key=lambda r: (r.score or 0), reverse=True)
    return out[: int(top)] if top else out


__all__ = [
    "FactorReport",
    "build_matrix",
    "compute_factor",
    "evaluate_factor",
    "evaluate_many",
    "evaluate_matrix",
    "load_panel",
]
