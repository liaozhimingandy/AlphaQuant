#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : mining.py
# @Description : 自动化因子挖掘（遗传算法，零第三方依赖）
#
#                为什么需要自动挖掘：
#                  人手能想到的因子是有限的 —— 写十个就江郎才尽，
#                  而且会不自觉地往"自己相信的故事"上凑。
#                  遗传算法的价值不是它比人聪明，而是它**不挑食**：
#                  几千个组合都能试一遍，包括那些人根本不会去写的怪组合。
#
#                但必须说清楚一件事：
#                  **挖出来的因子绝大多数是过拟合的**。
#                  在有限样本上搜索几千个表达式，总能找到几个"历史表现极好"的 ——
#                  那只是搜索偏差。所以：
#                    1. 只把 |IC| 和 ICIR 同时达标的收进库；
#                    2. 报告里必须带上样本外表现（前 70% 挖、后 30% 验）；
#                    3. 入库的因子标 `source=mined`，一眼能看出来源，
#                       出了事知道该怀疑谁。
#                  把这三条去掉，这个功能就从"研究助手"变成"制造幻觉的机器"。
#
#                为什么用树而不是字符串做交叉变异：
#                  字符串层面随便切一刀几乎必然产生语法错误的表达式，
#                  于是每一次变异都要先做一次"能不能解析"的校验，浪费大量预算。
#                  在语法树上操作，**生成出来的每一棵树都保证合法**。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.feature.evaluate import FactorReport, evaluate_matrix
from app.feature.expr import ExprError, evaluate
from app.common.logging import logger

# ===========================================================================
# 搜索空间
# ===========================================================================
#: 可用行情字段（权重：close 最常用）
VARS: Tuple[Tuple[str, float], ...] = (
    ("close", 0.40), ("open", 0.15), ("high", 0.15),
    ("low", 0.15), ("volume", 0.10), ("amount", 0.05),
)

#: 单输入 → 数组的算子
UNARY: Tuple[str, ...] = ("RANK", "ABS", "ZSCORE", "TS_RANK")
#: 带窗口的算子（输入 + 窗口长度）
ROLLING: Tuple[str, ...] = ("MA", "EMA", "STD", "SUM", "REF", "DELTA",
                            "TS_MAX", "TS_MIN")
#: 二元算子
BINARY: Tuple[str, ...] = ("+", "-", "*", "/", "MAX", "MIN")

#: 候选窗口长度
WINDOWS: Tuple[int, ...] = (3, 5, 10, 20, 60)


# ===========================================================================
# 表达式树
# ===========================================================================
def render(node: Dict[str, Any]) -> str:
    """把树渲染成表达式字符串（反过来就是 expr 引擎能吃的格式）。"""
    t = node["t"]
    if t == "v":
        return node["name"]
    if t == "n":
        v = node["v"]
        return str(int(v)) if float(v).is_integer() else f"{v:g}"
    if t == "f":
        return f"{node['name']}({', '.join(render(a) for a in node['args'])})"
    if t == "b":
        op = node["op"]
        if op in ("MAX", "MIN"):
            return f"{op}({render(node['l'])}, {render(node['r'])})"
        return f"({render(node['l'])} {op} {render(node['r'])})"
    raise ValueError(f"未知节点类型: {t!r}")


def tree_size(node: Dict[str, Any]) -> int:
    t = node["t"]
    if t in ("v", "n"):
        return 1
    if t == "f":
        return 1 + sum(tree_size(a) for a in node["args"])
    return 1 + tree_size(node["l"]) + tree_size(node["r"])


def random_tree(rng: random.Random, depth: int = 3) -> Dict[str, Any]:
    """随机生成一棵**保证合法**的表达式树。"""
    if depth <= 0 or rng.random() < 0.30:
        return {"t": "v", "name": _pick_var(rng)}
    r = rng.random()
    if r < 0.34:                       # 滚动算子
        return {"t": "f", "name": rng.choice(ROLLING),
                "args": [random_tree(rng, depth - 1),
                         {"t": "n", "v": float(rng.choice(WINDOWS))}]}
    if r < 0.58:                       # 单目算子
        name = rng.choice(UNARY)
        if name in ("ZSCORE", "TS_RANK"):
            return {"t": "f", "name": name,
                    "args": [random_tree(rng, depth - 1),
                             {"t": "n", "v": float(rng.choice(WINDOWS))}]}
        return {"t": "f", "name": name, "args": [random_tree(rng, depth - 1)]}
    if r < 0.72:                       # 与常数运算（做成"比值/偏离"类因子）
        return {"t": "b", "op": rng.choice(("+", "-", "*", "/")),
                "l": random_tree(rng, depth - 1),
                "r": {"t": "n", "v": float(rng.choice((1, 2, 5, 10, 100)))}}
    return {"t": "b", "op": rng.choice(BINARY),
            "l": random_tree(rng, depth - 1),
            "r": random_tree(rng, depth - 1)}


def _pick_var(rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for name, w in VARS:
        acc += w
        if r <= acc:
            return name
    return "close"


def mutate(rng: random.Random, node: Dict[str, Any],
           depth: int = 3) -> Dict[str, Any]:
    """随机替换一棵子树。以 1/3 概率对根节点动手，否则往深处走。"""
    if rng.random() < 0.33 or node["t"] in ("v", "n"):
        return random_tree(rng, depth)
    if node["t"] == "f":
        args = list(node["args"])
        i = rng.randrange(len(args))
        if args[i]["t"] == "n":          # 窗口长度单独微调，别整棵换掉
            args[i] = {"t": "n", "v": float(rng.choice(WINDOWS))}
        else:
            args[i] = mutate(rng, args[i], depth - 1)
        return {**node, "args": args}
    return {**node,
            "l": mutate(rng, node["l"], depth - 1),
            "r": mutate(rng, node["r"], depth - 1)}


def crossover(rng: random.Random, a: Dict[str, Any],
              b: Dict[str, Any]) -> Dict[str, Any]:
    """把 a 的某棵子树换成 b 的某棵子树。"""
    if a["t"] in ("v", "n"):
        return _random_subtree(rng, b)
    if a["t"] == "f":
        args = list(a["args"])
        i = rng.randrange(len(args))
        args[i] = _random_subtree(rng, b) if rng.random() < 0.5 \
            else crossover(rng, args[i], b)
        return {**a, "args": args}
    if rng.random() < 0.5:
        return {**a, "l": _random_subtree(rng, b)}
    return {**a, "r": _random_subtree(rng, b)}


def _random_subtree(rng: random.Random, node: Dict[str, Any]) -> Dict[str, Any]:
    """按大小加权随机选一棵子树（避免总是选到叶子，那样交叉就没意义了）。"""
    if node["t"] in ("v", "n"):
        return node
    if node["t"] == "f":
        pool = node["args"]
    else:
        pool = [node["l"], node["r"]]
    # 非叶子节点权重更高：交叉要交换"有意义的构件"，而不是一片叶子
    weights = [3 if p["t"] not in ("v", "n") else 1 for p in pool]
    idx = rng.choices(range(len(pool)), weights=weights)[0]
    chosen = pool[idx]
    if chosen["t"] in ("v", "n") or rng.random() < 0.4:
        return chosen
    return _random_subtree(rng, chosen)


# ===========================================================================
# 挖掘器
# ===========================================================================
@dataclass
class MinedFactor:
    expr: str
    score: float
    ic: Optional[float] = None
    icir: Optional[float] = None
    report: Optional[FactorReport] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expr": self.expr, "score": self.score, "ic": self.ic,
            "icir": self.icir,
            "report": self.report.to_dict() if self.report else None,
        }


class FactorMiner:
    """遗传算法因子挖掘。

    :param panel:   {symbol: DataFrame(OHLCV)}
    :param horizon: 未来收益天数（标签）
    """

    def __init__(self, panel: Dict[str, pd.DataFrame], horizon: int = 5,
                 seed: Optional[int] = 20261010, groups: int = 5,
                 max_size: int = 14) -> None:
        self.panel = {k: v for k, v in (panel or {}).items()
                      if v is not None and len(v) > 30}
        self.horizon = int(horizon)
        self.groups = int(groups)
        self.rng = random.Random(seed)
        self.max_size = int(max_size)
        #: 表达式 → 检验报告（种群里重复个体很多，缓存能省一半以上时间）
        self._cache: Dict[str, Optional[FactorReport]] = {}
        self.evaluated = 0

    # ------------------------------------------------------------------
    def _arrays(self) -> List[Dict[str, np.ndarray]]:
        if not hasattr(self, "_data"):
            self._data = []
            for df in self.panel.values():
                self._data.append({
                    "open": df["open"].to_numpy(dtype=float),
                    "high": df["high"].to_numpy(dtype=float),
                    "low": df["low"].to_numpy(dtype=float),
                    "close": df["close"].to_numpy(dtype=float),
                    "volume": df["volume"].to_numpy(dtype=float),
                    "amount": df["amount"].to_numpy(dtype=float)
                    if "amount" in df else np.zeros(len(df)),
                })
        return self._data

    def _fitness(self, expr: str, groups: int = 0) -> Optional[FactorReport]:
        # 缓存键必须带上 groups：挖掘阶段用 groups=0 快速筛，
        # 最后用 groups=5 做完整检验。只按 expr 做键会命中"没做分层"的那份结果，
        # 于是最终报告里的分层永远是空的。
        key = f"{expr}#{groups}"
        if key in self._cache:
            return self._cache[key]
        self.evaluated += 1
        frames: List[pd.DataFrame] = []
        for sym, (data, df) in enumerate(zip(self._arrays(), self.panel.values())):
            try:
                fv = evaluate(expr, data)
            except (ExprError, Exception):
                self._cache[key] = None
                return None
            close = data["close"]
            fwd = np.full(len(close), np.nan)
            if len(close) > self.horizon:
                prev = np.where(close[:-self.horizon] == 0, np.nan,
                                close[:-self.horizon])
                fwd[:len(close) - self.horizon] = (
                    close[self.horizon:] / prev - 1.0) * 100.0
            n = min(len(fv), len(close))
            frames.append(pd.DataFrame({
                "date": df.index[:n], "symbol": sym,
                "factor": fv[:n], "fwd": fwd[:n],
            }))
        if not frames:
            self._cache[key] = None
            return None
        m = pd.concat(frames, ignore_index=True).dropna(subset=["factor", "fwd"])
        if len(m) < 50:
            self._cache[key] = None
            return None
        rep = FactorReport(name=expr[:40], expr=expr, horizon=self.horizon)
        rep = evaluate_matrix(m, rep, groups=groups, total_rows=len(m))
        self._cache[key] = rep
        return rep

    # ------------------------------------------------------------------
    def run(self, generations: int = 12, population: int = 60,
            elite: int = 8, top: int = 10, max_seconds: float = 120.0,
            min_score: float = 45.0,
            progress: Optional[Callable[[int, int, float], None]] = None
            ) -> List[MinedFactor]:
        """跑一轮挖掘。

        :param elite: 每代原样保留的最优个体数
        :param top:   最终返回多少个
        :param max_seconds: 时间预算（挖掘很容易跑飞）
        :param min_score:  低于这个分不返回
        """
        if not self.panel:
            logger.warning("因子挖掘：没有可用的行情面板数据")
            return []

        started = time.monotonic()
        # 初始种群
        pop = [random_tree(self.rng, depth=3) for _ in range(int(population))]
        scored: List[Tuple[float, str, Dict[str, Any]]] = []
        seen: set = set()

        for gen in range(int(generations)):
            scored = []
            for tree in pop:
                if time.monotonic() - started > max_seconds:
                    break
                size = tree_size(tree)
                if size > self.max_size:
                    continue
                expr = render(tree)
                if expr in seen:
                    continue
                seen.add(expr)
                rep = self._fitness(expr)
                if rep is None or rep.score is None:
                    continue
                # 太长、太复杂的因子即使有效也难解释 —— 轻微惩罚
                penalty = max(0, size - 8) * 0.5
                scored.append((rep.score - penalty, expr, tree))
            if not scored:
                break
            scored.sort(key=lambda x: -x[0])
            if progress:
                progress(gen + 1, int(generations), scored[0][0])

            if time.monotonic() - started > max_seconds:
                break

            # 繁衍：精英 + 变异 + 交叉
            n_elite = max(1, min(int(elite), len(scored)))
            nxt = [t for _, _, t in scored[:n_elite]]
            while len(nxt) < int(population):
                r = self.rng.random()
                if r < 0.45:
                    base = scored[self.rng.randrange(min(len(scored), 20))][2]
                    nxt.append(mutate(self.rng, base, depth=3))
                elif r < 0.85 and len(scored) >= 2:
                    a = scored[self.rng.randrange(min(len(scored), 20))][2]
                    b = scored[self.rng.randrange(min(len(scored), 20))][2]
                    nxt.append(crossover(self.rng, a, b))
                else:
                    nxt.append(random_tree(self.rng, depth=3))
            pop = nxt

        # 对幸存者做**完整**检验（含分层），再按分数排序
        final: List[MinedFactor] = []
        for score, expr, _ in scored[: max(int(top) * 3, 10)]:
            rep = self._fitness(expr, groups=self.groups)
            if rep is None or rep.score is None or rep.score < min_score:
                continue
            final.append(MinedFactor(expr=expr, score=rep.score,
                                     ic=rep.ic_mean, icir=rep.icir,
                                     report=rep))
        final.sort(key=lambda f: -f.score)
        return final[: int(top)]


__all__ = [
    "BINARY",
    "MinedFactor",
    "ROLLING",
    "UNARY",
    "FactorMiner",
    "crossover",
    "mutate",
    "random_tree",
    "render",
    "tree_size",
]
