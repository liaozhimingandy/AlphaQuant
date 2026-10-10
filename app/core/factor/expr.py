#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : expr.py
# @Description : 因子表达式引擎：写一句公式 = 一个因子
#
#                为什么要有表达式因子：
#                  因子是策略的核心，而"想到一个因子"和"把它写出来"之间
#                  隔着一个 Python 类 + 一次重启。研究阶段一天要试十几个想法，
#                  这个摩擦会直接劝退 —— 最后就只剩下几个别人写好的经典指标。
#                  表达式因子让"加一个因子"变成写一行文本，界面上就能做。
#
#                为什么不用 eval / 不接 pandas.eval：
#                  表达式会来自**界面输入框**。eval 等于给了输入框一个
#                  任意代码执行的口子，这在跑着真钱的机器上不可接受。
#                  这里是自己写的 tokenizer + 递归下降 parser + 白名单函数表，
#                  能做的事只有"对数组做四则与白名单运算"。
#
#                为什么所有算子都必须是**因果**的：
#                  RANK(close) 如果用"全样本排名"，第 100 天的值会用到
#                  第 500 天的数据 —— 那是未来函数，回测出来漂亮，实盘一分钱赚不到。
#                  所以这里的 RANK 是**截至当前**的扩展窗口排名，只能看到过去。
#                  这是新手写因子最容易犯、也最难自己发现的错。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# ===========================================================================
# 词法
# ===========================================================================
_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<number>\d+\.?\d*(?:[eE][+-]?\d+)?)
      | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
      | (?P<op><=|>=|==|!=|[<>+\-*/%^(),])
    )
    """,
    re.VERBOSE,
)

_OPS = {"<=", ">=", "==", "!=", "<", ">", "+", "-", "*", "/", "%", "^", "(", ")", ","}


class ExprError(ValueError):
    """表达式写错了。要给出**能照着改**的提示，而不是一句"语法错误"。"""


def tokenize(text: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            rest = text[pos:pos + 20]
            raise ExprError(f"无法识别的内容: {rest!r}（位置 {pos}）")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        out.append((kind, val))
    if not out:
        raise ExprError("表达式为空")
    return out


# ===========================================================================
# 语法（递归下降）
# ===========================================================================
# AST 节点统一用 tuple：
#   ("num", v) | ("var", name) | ("call", name, [args]) | ("bin", op, l, r) | ("neg", x)
class _Parser:
    def __init__(self, tokens: List[Tuple[str, str]], text: str) -> None:
        self.tokens = tokens
        self.text = text
        self.i = 0

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> Tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ExprError("表达式不完整（末尾缺少操作数或右括号）")
        self.i += 1
        return tok

    def expect_op(self, op: str) -> None:
        kind, val = self.next()
        if kind != "op" or val != op:
            raise ExprError(f"期望 {op!r}，读到 {val!r}")

    # 比较运算放在**最低**优先级：先算完两边的表达式再比。
    # 结果是 0/1 的数组，所以能直接参与四则运算
    # （IF(close > MA(close,10), 1, 0) 之外，也能写 (close > MA(close,20)) * 1）
    _CMP = (">", "<", ">=", "<=", "==", "!=")

    def parse(self):
        node = self.parse_cmp()
        if self.peek() is not None:
            _, v = self.peek()
            raise ExprError(f"表达式末尾有多余内容: {v!r}")
        return node

    def parse_cmp(self):
        node = self.parse_add()
        while True:
            tok = self.peek()
            if tok and tok[0] == "op" and tok[1] in self._CMP:
                self.next()
                node = ("bin", tok[1], node, self.parse_add())
            else:
                return node

    def parse_add(self):
        node = self.parse_mul()
        while True:
            tok = self.peek()
            if tok and tok[0] == "op" and tok[1] in ("+", "-"):
                self.next()
                node = ("bin", tok[1], node, self.parse_mul())
            else:
                return node

    def parse_mul(self):
        node = self.parse_unary()
        while True:
            tok = self.peek()
            if tok and tok[0] == "op" and tok[1] in ("*", "/", "%"):
                self.next()
                node = ("bin", tok[1], node, self.parse_unary())
            else:
                return node

    def parse_unary(self):
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("+", "-"):
            self.next()
            node = self.parse_unary()
            return node if tok[1] == "+" else ("neg", node)
        return self.parse_power()

    def parse_power(self):
        node = self.parse_primary()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] == "^":
            self.next()
            return ("call", "POW", [node, self.parse_unary()])
        return node

    def parse_primary(self):
        kind, val = self.next()
        if kind == "number":
            return ("num", float(val))
        if kind == "op" and val == "(":
            node = self.parse_cmp()
            self.expect_op(")")
            return node
        if kind == "name":
            nxt = self.peek()
            if nxt and nxt[0] == "op" and nxt[1] == "(":
                self.next()
                args = []
                if not (self.peek() and self.peek()[0] == "op" and self.peek()[1] == ")"):
                    while True:
                        args.append(self.parse_cmp())
                        tok = self.peek()
                        if tok and tok[0] == "op" and tok[1] == ",":
                            self.next()
                            continue
                        break
                self.expect_op(")")
                return ("call", val.upper(), args)
            return ("var", val.lower())
        raise ExprError(f"意外的记号: {val!r}")


# ===========================================================================
# 算子实现（全部向量化：数组进 → 数组出）
# ===========================================================================
def _a(x: Any) -> np.ndarray:
    """把入参统一成一维 float 数组。标量会广播成长度 1 再靠 numpy 自动扩展。"""
    if isinstance(x, np.ndarray):
        return x.astype(float, copy=False)
    if isinstance(x, (int, float)):
        return np.array([float(x)])
    return np.asarray(pd.Series(x).to_numpy(), dtype=float)


def _n(x: Any, default: int = 1) -> int:
    """窗口长度。表达式里写 MA(close, 5)，5 是标量。"""
    arr = _a(x)
    v = int(round(float(arr[0]))) if arr.size else default
    return max(1, v)


def _roll(x: np.ndarray, n: int) -> pd.core.rolling.Rolling:
    return pd.Series(x).rolling(window=int(n), min_periods=int(n))


# ---- 数学 ----
def f_abs(x): return np.abs(_a(x))
def f_log(x): return np.log(np.where(_a(x) > 0, _a(x), np.nan))
def f_sign(x): return np.sign(_a(x))
def f_sqrt(x): return np.sqrt(np.where(_a(x) >= 0, _a(x), np.nan))
def f_exp(x): return np.exp(np.clip(_a(x), -50, 50))
def f_pow(x, y): return np.power(_a(x), _a(y))


# ---- 时序 ----
def f_ma(x, n):
    return _roll(_a(x), _n(n)).mean().to_numpy()


def f_ema(x, n):
    return pd.Series(_a(x)).ewm(span=max(1, _n(n)), adjust=False).mean().to_numpy()


def f_std(x, n):
    return _roll(_a(x), _n(n)).std().to_numpy()


def f_var(x, n):
    return _roll(_a(x), _n(n)).var().to_numpy()


def f_sum(x, n):
    return _roll(_a(x), _n(n)).sum().to_numpy()


def f_ref(x, n):
    """N 天前的值。DELAY 的同义写法。"""
    return pd.Series(_a(x)).shift(_n(n)).to_numpy()


def f_delta(x, n):
    return pd.Series(_a(x)).diff(_n(n)).to_numpy()


def f_pct_change(x, n):
    return pd.Series(_a(x)).pct_change(_n(n)).to_numpy()


def f_ts_max(x, n):
    return _roll(_a(x), _n(n)).max().to_numpy()


def f_ts_min(x, n):
    return _roll(_a(x), _n(n)).min().to_numpy()


def f_ts_mean(x, n):
    return f_ma(x, n)


def f_zscore(x, n):
    """滚动标准化。用来把不同量纲的因子拉到可比区间。"""
    s = pd.Series(_a(x))
    r = s.rolling(window=_n(n), min_periods=_n(n))
    m = r.mean()
    sd = r.std()
    return ((s - m) / sd.replace(0.0, np.nan)).to_numpy()


def f_ts_rank(x, n):
    """最近 N 天里，今天排第几（0~1）。**因果**：只看窗口内。"""
    arr = _a(x)
    n = _n(n)
    out = np.full(arr.shape, np.nan)
    if arr.size >= n:
        win = pd.Series(arr).rolling(window=n, min_periods=n)
        last = arr[n - 1:]
        # 窗口内有多少个值 <= 当前值 → 百分位
        cnt = win.apply(lambda w: float((w <= w[-1]).sum()), raw=True).to_numpy()
        cnt = cnt[~np.isnan(cnt)]
        k = min(len(cnt), len(last))
        if k > 0:
            out[n - 1:n - 1 + k] = cnt[:k] / float(n)
    return out


def f_corr(x, y, n):
    a, b = _a(x), _a(y)
    n = _n(n)
    if a.size != b.size:
        raise ExprError("CORR 的两个输入长度必须一致")
    return pd.Series(a).rolling(window=n, min_periods=n).corr(pd.Series(b)).to_numpy()


def f_covar(x, y, n):
    a, b = _a(x), _a(y)
    n = _n(n)
    return pd.Series(a).rolling(window=n, min_periods=n).cov(pd.Series(b)).to_numpy()


# ---- 截面/全序列 ----
def f_rank(x):
    """**截至当前**的扩展窗口百分位排名（0~1）。

    刻意不做"全样本排名" —— 那会用未来数据给历史打分。
    代价是前几天的排名不稳定（样本少），所以检验时会跳过预热期。
    """
    arr = _a(x)
    out = np.full(arr.shape, np.nan)
    for i in range(arr.size):
        if math.isnan(arr[i]):
            continue
        hist = arr[:i + 1]
        hist = hist[~np.isnan(hist)]
        if hist.size == 0:
            continue
        out[i] = float((hist <= arr[i]).sum()) / float(hist.size)
    return out


def f_scale(x, a=1.0):
    """缩放到和为 a（alpha101 的 scale）。用于把因子变成权重。"""
    arr = _a(x)
    s = float(np.nansum(np.abs(arr)))
    if s == 0 or math.isnan(s):
        return arr
    return arr * (float(_a(a)[0]) / s)


# ---- 元素级 ----
def f_max(x, y): return np.maximum(_a(x), _a(y))
def f_min(x, y): return np.minimum(_a(x), _a(y))
def f_if(cond, a, b):
    c = _a(cond)
    return np.where(c > 0, _a(a), _a(b))
def f_clip(x, lo, hi): return np.clip(_a(x), _a(lo)[0], _a(hi)[0])
def f_mid(x, y): return (_a(x) + _a(y)) / 2.0


#: 白名单函数表：名字 → (可调用对象, 最少参数, 最多参数, 说明)
#: 只有出现在这里的函数才能被表达式调用 —— 这是不用 eval 的关键。
FUNCS: Dict[str, Tuple[Callable, int, int, str]] = {
    "ABS": (f_abs, 1, 1, "绝对值"),
    "LOG": (f_log, 1, 1, "自然对数（非正数得空）"),
    "SIGN": (f_sign, 1, 1, "符号：正1/负-1/零0"),
    "SQRT": (f_sqrt, 1, 1, "平方根"),
    "EXP": (f_exp, 1, 1, "指数"),
    "POW": (f_pow, 2, 2, "幂：POW(x, 2)"),
    "MA": (f_ma, 2, 2, "简单移动平均：MA(close, 5)"),
    "EMA": (f_ema, 2, 2, "指数移动平均：EMA(close, 12)"),
    "STD": (f_std, 2, 2, "滚动标准差：STD(close, 20)"),
    "VAR": (f_var, 2, 2, "滚动方差"),
    "SUM": (f_sum, 2, 2, "滚动求和：SUM(volume, 5)"),
    "REF": (f_ref, 2, 2, "N 天前的值：REF(close, 1)"),
    "DELAY": (f_ref, 2, 2, "REF 的同义写法"),
    "DELTA": (f_delta, 2, 2, "今日 - N 日前"),
    "PCT_CHANGE": (f_pct_change, 2, 2, "N 日涨跌幅"),
    "TS_MAX": (f_ts_max, 2, 2, "滚动最大值"),
    "TS_MIN": (f_ts_min, 2, 2, "滚动最小值"),
    "TS_MEAN": (f_ts_mean, 2, 2, "滚动均值（同 MA）"),
    "ZSCORE": (f_zscore, 2, 2, "滚动标准化：ZSCORE(close, 60)"),
    "TS_RANK": (f_ts_rank, 2, 2, "窗口内排名百分位（因果）"),
    "CORR": (f_corr, 3, 3, "滚动相关系数：CORR(close, volume, 10)"),
    "COVAR": (f_covar, 3, 3, "滚动协方差"),
    "RANK": (f_rank, 1, 1, "截至当前的百分位排名（因果，不用未来数据）"),
    "SCALE": (f_scale, 1, 2, "缩放到绝对值和为 a：SCALE(x, 1)"),
    "MAX": (f_max, 2, 2, "逐元素取大"),
    "MIN": (f_min, 2, 2, "逐元素取小"),
    "IF": (f_if, 3, 3, "条件取值：IF(cond, a, b)，cond>0 取 a"),
    "CLIP": (f_clip, 3, 3, "截断到区间：CLIP(x, -1, 1)"),
    "MID": (f_mid, 2, 2, "两者均值"),
}

#: 表达式里可直接引用的行情字段
VARIABLES: Dict[str, str] = {
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "amount": "成交额",
}


def func_catalog() -> List[Dict[str, Any]]:
    """给界面/CLI 的算子清单。"""
    return [
        {"name": k, "min_args": v[1], "max_args": v[2], "doc": v[3]}
        for k, v in sorted(FUNCS.items())
    ]


def variable_catalog() -> List[Dict[str, str]]:
    return [{"name": k, "doc": v} for k, v in VARIABLES.items()]


# ===========================================================================
# 求值
# ===========================================================================
def _eval_node(node: Any, env: Dict[str, np.ndarray]) -> np.ndarray:
    kind = node[0]
    if kind == "num":
        return np.array([node[1]])
    if kind == "var":
        name = node[1]
        if name in env:
            return env[name]
        if name in FUNCS:
            raise ExprError(f"{name!r} 是函数，要写成 {name.upper()}(...)")
        raise ExprError(
            f"未知变量 {name!r} | 可用: {', '.join(sorted(VARIABLES))}"
        )
    if kind == "neg":
        return -_eval_node(node[1], env)
    if kind == "bin":
        op = node[1]
        l = _eval_node(node[2], env)
        r = _eval_node(node[3], env)
        with np.errstate(divide="ignore", invalid="ignore"):
            if op == "+":
                return l + r
            if op == "-":
                return l - r
            if op == "*":
                return l * r
            if op == "/":
                return l / np.where(r == 0, np.nan, r)
            if op == "%":
                return l % np.where(r == 0, np.nan, r)
            # 比较：返回 0/1（NaN 参与比较仍得 NaN，保持"算不出来"的语义）
            if op == ">":
                return (l > r).astype(float)
            if op == "<":
                return (l < r).astype(float)
            if op == ">=":
                return (l >= r).astype(float)
            if op == "<=":
                return (l <= r).astype(float)
            if op == "==":
                return (l == r).astype(float)
            if op == "!=":
                return (l != r).astype(float)
        raise ExprError(f"未知运算符 {op!r}")
    if kind == "call":
        name = node[1]
        args = node[2]
        spec = FUNCS.get(name)
        if spec is None:
            raise ExprError(
                f"未知函数 {name!r} | 可用: {', '.join(sorted(FUNCS))}"
            )
        fn, lo, hi, _ = spec
        if not (lo <= len(args) <= hi):
            raise ExprError(
                f"{name} 需要 {lo}{'' if lo == hi else f'~{hi}'} 个参数，"
                f"收到 {len(args)} 个"
            )
        vals = [_eval_node(a, env) for a in args]
        try:
            return np.asarray(fn(*vals), dtype=float)
        except ExprError:
            raise
        except Exception as exc:
            raise ExprError(f"{name} 计算失败: {exc}") from exc
    raise ExprError(f"无法识别的表达式节点: {node!r}")


def parse(text: str):
    """解析表达式。语法错会抛 :class:`ExprError`（带位置与建议）。"""
    if not text or not str(text).strip():
        raise ExprError("表达式为空")
    return _Parser(tokenize(str(text)), str(text)).parse()


def evaluate(text: str, data: Dict[str, Sequence[float]]) -> np.ndarray:
    """在给定行情上求值，返回与输入等长的 numpy 数组。

    :param data: 字段 → 序列（open/high/low/close/volume/amount）
    """
    env = {k: np.asarray(v, dtype=float) for k, v in data.items()}
    if not env:
        raise ExprError("没有行情数据")
    out = _eval_node(parse(text), env)
    # 标量广播回全长度（比如 close / 100 里的 100）
    if out.size == 1:
        n = len(next(iter(env.values())))
        out = np.full(n, float(out[0]))
    return out


def validate(text: str) -> Dict[str, Any]:
    """静态检查：能不能解析、用了哪些变量/函数、最长窗口大概是多少。

    界面保存前先跑一次这个 —— 让用户立刻看到"closee 是什么"，
    而不是等跑回测时才炸。
    """
    try:
        ast = parse(text)
    except ExprError as exc:
        return {"ok": False, "error": str(exc)}

    used_vars: List[str] = []
    used_funcs: List[str] = []
    max_window = 0

    def walk(node: Any) -> None:
        nonlocal max_window
        if node[0] == "var":
            if node[1] not in used_vars:
                used_vars.append(node[1])
        elif node[0] == "call":
            if node[1] not in used_funcs:
                used_funcs.append(node[1])
            for a in node[2]:
                walk(a)
            # 最后一个参数常是窗口长度（MA/STD/REF/...）
            last = node[2][-1]
            if last[0] == "num" and node[1] not in ("POW", "SCALE", "CLIP",
                                                    "MAX", "MIN", "IF"):
                max_window = max(max_window, int(last[1]))
        elif node[0] in ("bin",):
            walk(node[2])
            walk(node[3])
        elif node[0] == "neg":
            walk(node[1])

    walk(ast)

    # 未知变量必须在**保存时**就拦下来，而不是等半夜回测才炸：
    # 手滑写成 closee 是最常见的一类错，报错时配上"你是不是想写 close"。
    unknown = [v for v in used_vars if v not in VARIABLES]
    if unknown:
        hints = []
        for v in unknown:
            near = _closest(v, VARIABLES)
            hints.append(f"未知变量 {v!r}" + (f"（是不是想写 {near!r}？）" if near else ""))
        return {"ok": False, "error": "；".join(hints)}

    return {
        "ok": True,
        "variables": used_vars,
        "functions": used_funcs,
        "max_window": max_window,
    }


def _closest(name: str, candidates) -> Optional[str]:
    """给拼错的变量名一个"你是不是想写…"的建议（编辑距离 ≤ 2）。"""
    best, best_d = None, 99
    for c in candidates:
        d = _levenshtein(name.lower(), c.lower())
        if d < best_d:
            best, best_d = c, d
    return best if best_d <= 2 else None


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


__all__ = [
    "ExprError",
    "FUNCS",
    "VARIABLES",
    "evaluate",
    "func_catalog",
    "parse",
    "tokenize",
    "validate",
    "variable_catalog",
]
