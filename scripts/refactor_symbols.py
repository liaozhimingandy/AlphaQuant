#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把服务/组件类从旧的聚合门面 ``app.engine.component`` 改写到各自的新模块。

用法::

    python scripts/refactor_symbols.py            # dry-run
    python scripts/refactor_symbols.py --apply
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 组件契约留在本模块，其余按"谁实现归谁"重新指向
OLD_MODULE = "app.engine.component"
SYMBOL_TO_MODULE = {
    # ---- 留在本模块的契约（命中时不改）----
    "IBaseComponent": None,
    "Progress": None,
    "ComponentState": None,
    # ---- 业务服务 -> modules ----
    "CollectionService": "app.modules.collection.service",
    "parse_interval": "app.modules.collection.service",
    "MarketService": "app.modules.market.service",
    "df_to_bars": "app.modules.market.service",
    "TradingService": "app.modules.trading.service",
    "LiveBroker": "app.modules.trading.live_broker",
    "RiskService": "app.modules.risk.service",
    "PortfolioService": "app.modules.portfolio.service",
    "Reservation": "app.modules.portfolio.service",
    "NewsService": "app.modules.news.service",
    "StrategyComponent": "app.modules.strategy.component",
    # ---- 调度/执行 -> runtime ----
    "SchedulerComponent": "app.runtime.scheduler",
    "WorkerComponent": "app.runtime.executor",
}

_MULTI_RE = re.compile(
    r"^(\s*)from\s+" + re.escape(OLD_MODULE) + r"\s+import\s+\(([^)]*)\)",
    re.MULTILINE | re.DOTALL,
)
_SINGLE_RE = re.compile(
    r"^(\s*)from\s+" + re.escape(OLD_MODULE) + r"\s+import\s+([^\n(]+)$",
    re.MULTILINE,
)


def _split_symbols(body: str) -> list[str]:
    return [s.strip() for s in body.replace("\\\n", " ").split(",") if s.strip()]


def _rewrite_stmts(symbols: list[str]) -> tuple[list[str], list[str]]:
    """返回 (保留在本模块的符号, 需要重写的 import 语句列表)。"""
    keep: list[str] = []
    grouped: dict[str, list[str]] = {}
    for sym in symbols:
        # 去掉 "X as Y" 形式里的别名，只按原名归类
        base = sym.split(" as ")[0].strip()
        mod = SYMBOL_TO_MODULE.get(base)
        if mod is None:
            keep.append(sym)
        else:
            grouped.setdefault(mod, []).append(sym)
    lines = [f"from {mod} import " + ", ".join(syms)
             for mod, syms in grouped.items()]
    return keep, lines


def _process(text: str) -> tuple[str, int]:
    hits = 0

    def _multi(m: re.Match) -> str:
        nonlocal hits
        indent, body = m.group(1), m.group(2)
        symbols = _split_symbols(body)
        if not any(s.split(" as ")[0].strip() in SYMBOL_TO_MODULE
                   and SYMBOL_TO_MODULE[s.split(" as ")[0].strip()]
                   for s in symbols):
            return m.group(0)
        keep, lines = _rewrite_stmts(symbols)
        hits += 1
        out = []
        if keep:
            joined = ", ".join(keep)
            if len(joined) <= 60:
                out.append(f"{indent}from {OLD_MODULE} import {joined}")
            else:
                out.append(f"{indent}from {OLD_MODULE} import (")
                out.extend(f"{indent}    {s}," for s in keep)
                out.append(f"{indent})")
        for line in lines:
            if indent and len(line) + len(indent) > 88:
                mod, rest = line.split(" import ", 1)
                out.append(f"{indent}from {mod} import (")
                out.extend(f"{indent}    {s}," for s in rest.split(", "))
                out.append(f"{indent})")
            else:
                out.append(f"{indent}{line}")
        return "\n".join(out)

    def _single(m: re.Match) -> str:
        nonlocal hits
        indent, body = m.group(1), m.group(2)
        symbols = _split_symbols(body.rstrip())
        moved = [s for s in symbols
                 if SYMBOL_TO_MODULE.get(s.split(" as ")[0].strip())]
        if not moved:
            return m.group(0)
        keep, lines = _rewrite_stmts(symbols)
        hits += 1
        out = []
        if keep:
            out.append(f"{indent}from {OLD_MODULE} import {', '.join(keep)}")
        out.extend(f"{indent}{ln}" for ln in lines)
        return "\n".join(out)

    text = _MULTI_RE.sub(_multi, text)
    text = _SINGLE_RE.sub(_single, text)
    return text, hits


def _iter_files() -> list[Path]:
    out = []
    for p in ROOT.glob("app/**/*.py"):
        s = str(p)
        if "__pycache__" in s or ".pytest_tmp" in s:
            continue
        out.append(p)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    total, changed = 0, []
    for f in _iter_files():
        src = f.read_text(encoding="utf-8")
        new, n = _process(src)
        if n:
            total += n
            changed.append((f, n))
            if args.apply:
                f.write_text(new, encoding="utf-8")
    mode = "已写入" if args.apply else "dry-run"
    print(f"[{mode}] 命中文件 {len(changed)} 个，改写语句 {total} 条")
    for f, n in changed:
        print(f"  {n:3d}  {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
