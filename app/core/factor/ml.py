#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : ml.py
# @Description : 因子层面的机器学习（零第三方依赖，numpy 实现）
#
#                为什么不用 sklearn：
#                  这个项目坚持零新依赖，而"给因子挑权重"这件事
#                  本质上就是线性模型 —— 岭回归有闭式解，Lasso 用坐标下降，
#                  两个都是十几行 numpy 的事。为了它们引入一整条
#                  scipy/sklearn 依赖链不划算，也让部署变重。
#
#                为什么选 Lasso 做**因子筛选**：
#                  Lasso 的 L1 惩罚会把不重要因子的权重直接压成 0 ——
#                  这正好是"从 50 个候选里挑出 8 个"要做的事。
#                  岭回归只会把所有权重缩小，一个都删不掉，
#                  于是你永远看不出哪些因子是真正有用的。
#
#                但 Lasso 有个坑必须说清：
#                  高度相关的因子之间，Lasso 会**随机**挑一个留下。
#                  所以先做相关性去冗余，再喂给 Lasso。
#                  否则你以为是模型选出来的因子，其实只是它先撞上了哪个。
#
#                为什么用**时间序列切分**而不是随机切分：
#                  随机切分会让训练集里出现"未来"，测试集里出现"过去"，
#                  样本之间还高度重叠 —— 得到的样本外表现是假的。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


# ===========================================================================
# 结果
# ===========================================================================
@dataclass
class FitResult:
    """一次拟合的结果。"""

    method: str
    #: 因子名 → 权重（已按绝对值排序，0 表示被筛掉）
    weights: Dict[str, float] = field(default_factory=dict)
    intercept: float = 0.0
    alpha: float = 0.0
    n_train: int = 0
    n_test: int = 0
    #: 训练集 / 测试集的 R²（测试集才说明有没有用）
    r2_train: Optional[float] = None
    r2_test: Optional[float] = None
    #: 测试集上的 IC（模型打分 vs 真实未来收益）
    ic_test: Optional[float] = None
    #: 被保留下来的因子（权重显著）
    selected: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    #: 因与其它因子高度相关而被剔除的（name, 与谁相关, 相关系数）
    collinear: List[List[Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method, "weights": dict(self.weights),
            "intercept": self.intercept, "alpha": self.alpha,
            "n_train": self.n_train, "n_test": self.n_test,
            "r2_train": self.r2_train, "r2_test": self.r2_test,
            "ic_test": self.ic_test, "selected": list(self.selected),
            "dropped": list(self.dropped), "collinear": [list(c) for c in self.collinear],
        }


# ===========================================================================
# 数据工具
# ===========================================================================
def _zscore(x: np.ndarray) -> Tuple[np.ndarray, float, float]:
    mu = float(np.nanmean(x))
    sd = float(np.nanstd(x))
    if sd == 0 or sd != sd:
        return np.zeros_like(x), mu, 1.0
    return (x - mu) / sd, mu, sd


def prepare(features: Dict[str, np.ndarray], target: np.ndarray
            ) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """把 {因子名: 序列} 整理成设计矩阵。

    **按行剔除**任何一列有 NaN 的行：
      用均值填充会凭空造出"这个因子当时是中性"的假象，
      而真实情况是"那天算不出来"。两者在模型眼里完全不同。
    """
    names = [n for n in features if features[n] is not None]
    if not names:
        return np.zeros((0, 0)), np.zeros(0), []
    n = min(len(features[k]) for k in names)
    cols = []
    for k in names:
        v = np.asarray(features[k], dtype=float)[:n]
        cols.append(v)
    X = np.column_stack(cols)
    y = np.asarray(target, dtype=float)[:n]
    ok = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    return X[ok], y[ok], names


def time_split(X: np.ndarray, y: np.ndarray, train_ratio: float = 0.7
               ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """按**时间**切分：前段训练，后段测试。不做随机打乱。"""
    n = len(y)
    cut = int(n * float(train_ratio))
    cut = max(1, min(n - 1, cut))
    return X[:cut], y[:cut], X[cut:], y[cut:]


def _r2(y: np.ndarray, pred: np.ndarray) -> Optional[float]:
    if len(y) < 3:
        return None
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    if ss_tot == 0:
        return None
    return 1.0 - ss_res / ss_tot


def _ic(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    if len(a) < 3:
        return None
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    v = float(np.corrcoef(a, b)[0, 1])
    return None if v != v else v


# ===========================================================================
# 模型
# ===========================================================================
def ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = 1.0
              ) -> Tuple[np.ndarray, float]:
    """岭回归闭式解：w = (XᵀX + αI)⁻¹ Xᵀy。"""
    n, p = X.shape
    if n == 0 or p == 0:
        return np.zeros(p), 0.0
    A = X.T @ X + float(alpha) * np.eye(p)
    b = X.T @ y
    try:
        w = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        w = np.linalg.lstsq(A, b, rcond=None)[0]
    intercept = float(np.mean(y) - np.mean(X @ w))
    return w, intercept


def lasso_fit(X: np.ndarray, y: np.ndarray, alpha: float = 0.01,
              max_iter: int = 500, tol: float = 1e-6
              ) -> Tuple[np.ndarray, float]:
    """Lasso（坐标下降）。

    L1 惩罚不可导，所以用坐标下降：每次只动一个权重，
    其余固定，这一步有闭式解（软阈值算子）。
    """
    n, p = X.shape
    if n == 0 or p == 0:
        return np.zeros(p), 0.0
    w = np.zeros(p)
    # 预计算每列的平方和，避免在循环里反复算
    col_sq = np.sum(X ** 2, axis=0)
    col_sq[col_sq == 0] = 1.0
    y_centered = y - float(np.mean(y))

    for _ in range(int(max_iter)):
        max_delta = 0.0
        for j in range(p):
            # 去掉第 j 列的影响后的残差
            r = y_centered - (X @ w) + X[:, j] * w[j]
            rho = float(X[:, j] @ r)
            new_wj = _soft_threshold(rho, alpha) / col_sq[j]
            max_delta = max(max_delta, abs(new_wj - w[j]))
            w[j] = new_wj
        if max_delta < tol:
            break
    intercept = float(np.mean(y) - np.mean(X @ w))
    return w, intercept


def _soft_threshold(z: float, alpha: float) -> float:
    if z > alpha:
        return z - alpha
    if z < -alpha:
        return z + alpha
    return 0.0


# ===========================================================================
# 去冗余
# ===========================================================================
def decorrelate(features: Dict[str, np.ndarray], threshold: float = 0.8
                ) -> Tuple[List[str], List[Tuple[str, str, float]]]:
    """贪心剔除高度相关的因子。

    保留**先出现**的那个（调用方按因子分数排过序，所以留下的通常是更好的），
    返回 (保留列表, [(被剔除, 因与谁相关, 相关系数), ...])。
    """
    names = list(features.keys())
    keep: List[str] = []
    dropped: List[Tuple[str, str, float]] = []
    for name in names:
        dup_of, corr = None, 0.0
        for k in keep:
            c = _ic(np.asarray(features[name], dtype=float),
                    np.asarray(features[k], dtype=float))
            if c is not None and abs(c) >= threshold:
                dup_of, corr = k, abs(c)
                break
        if dup_of is None:
            keep.append(name)
        else:
            dropped.append((name, dup_of, round(corr, 3)))
    return keep, dropped


# ===========================================================================
# 主入口
# ===========================================================================
#: 自动选惩罚强度时的候选网格
ALPHA_GRID: Tuple[float, ...] = (0.001, 0.005, 0.01, 0.05, 0.1, 0.3, 1.0, 3.0)
#: 权重低于"最大权重 × 这个比例"就当作噪音（Lasso 也很少压到严格的 0）
MIN_WEIGHT_RATIO = 0.05


def fit_factors(features: Dict[str, np.ndarray], target: np.ndarray,
                method: str = "lasso", alpha: float = 0.0,
                train_ratio: float = 0.7,
                decorrelate_threshold: float = 0.8) -> FitResult:
    """给一组因子拟合权重，并报告**样本外**表现。

    :param method: ``lasso``（会筛因子）/ ``ridge``（只缩不放零）
    :param alpha:  惩罚强度。0 表示**自动选**（见下）
    """
    method = str(method or "lasso").lower()
    X, y, names = prepare(features, target)
    res = FitResult(method=method)
    if X.size == 0 or len(y) < 20:
        return res

    # 去冗余必须在拟合**之前**：否则 Lasso 会在共线的因子里随机留一个，
    # 你以为是模型选出来的，其实只是它先撞上了哪个。
    if decorrelate_threshold and len(names) > 1:
        keep, dropped = decorrelate(
            {n: np.asarray(features[n], dtype=float) for n in names},
            threshold=float(decorrelate_threshold),
        )
        res.dropped = [d[0] for d in dropped]
        res.collinear = [list(d) for d in dropped]
        idx = [names.index(k) for k in keep]
        X = X[:, idx]
        names = keep

    # 标准化：让"权重大小"真的能代表重要性
    Xs = np.column_stack([_zscore(X[:, j])[0] for j in range(X.shape[1])])
    Xtr, ytr, Xte, yte = time_split(Xs, y, train_ratio)
    res.n_train, res.n_test = len(ytr), len(yte)

    if alpha <= 0:
        alpha = _choose_alpha(Xtr, ytr, method)
    res.alpha = float(alpha)

    if method == "lasso":
        w, b = lasso_fit(Xtr, ytr, alpha=alpha)
    else:
        w, b = ridge_fit(Xtr, ytr, alpha=alpha)

    res.intercept = float(b)
    res.weights = {n: round(float(v), 6) for n, v in
                   sorted(zip(names, w), key=lambda kv: -abs(kv[1]))}
    res.selected = significant_factors(res.weights)

    res.r2_train = _r2(ytr, Xtr @ w + b)
    if len(yte) >= 3:
        res.r2_test = _r2(yte, Xte @ w + b)
        res.ic_test = _ic(Xte @ w, yte)
    return res


def _choose_alpha(Xtr: np.ndarray, ytr: np.ndarray, method: str) -> float:
    """用**训练集里再切出的验证集**挑惩罚强度。

    直接拿测试集调参是最常见的自欺：调完的"样本外表现"其实已经
    被测试集污染了，上线必崩。所以这里只用训练段里最后 20% 做验证。
    """
    grid = ALPHA_GRID if method == "lasso" else (0.1, 1.0, 10.0, 100.0)
    if len(ytr) < 30:
        return float(grid[len(grid) // 2])
    Xa, ya, Xv, yv = time_split(Xtr, ytr, 0.8)
    best, best_score = float(grid[0]), -np.inf
    for a in grid:
        w, b = (lasso_fit(Xa, ya, alpha=a) if method == "lasso"
                else ridge_fit(Xa, ya, alpha=a))
        score = _ic(Xv @ w, yv)
        if score is None:
            score = -np.inf
        if score > best_score:
            best, best_score = float(a), score
    return best


def significant_factors(weights: Dict[str, float],
                        ratio: float = MIN_WEIGHT_RATIO) -> List[str]:
    """权重显著（相对最大权重）的因子。

    Lasso 理论上会把无用因子压到 0，但数值求解后往往是 0.02 这种小残值。
    用绝对阈值没意义（量纲随数据变），所以按相对量级判。
    """
    if not weights:
        return []
    top = max(abs(v) for v in weights.values())
    if top <= 0:
        return []
    cut = top * float(ratio)
    return [n for n, v in weights.items() if abs(v) >= cut]


__all__ = [
    "ALPHA_GRID",
    "FitResult",
    "decorrelate",
    "fit_factors",
    "lasso_fit",
    "prepare",
    "ridge_fit",
    "significant_factors",
    "time_split",
]
