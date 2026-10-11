#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段二收尾：用权威 MOVES 表 + git rename 推导并集，改掉所有遗留的旧路径。

映射来源优先级：
  1. scripts/migrate_files.py 里的 MOVES —— 手写的事实清单，键形如 ``utils/logger.py``，
     不以 ``app.`` 开头，因此没被前几轮的正则替换污染，是最可靠来源；
  2. git 的 rename 记录 —— 覆盖 MOVES 没显式列出的 test_*.py 等。

两者取并集后统一改写 app/、scripts/、docs/、入口文件里的所有 ``app.xxx`` 路径，
同时处理 ``from app.X import Y``（MAP 里存的是 ``app.X.Y``）与注释/字符串中的长串。

用法：python finalize_imports.py [--apply] [--self]
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_map_from_git import build_map, resolve  # noqa: E402
from migrate_files import MOVES  # noqa: E402

_PATTERN = re.compile(r"\bapp(?:\.[A-Za-z_][A-Za-z_0-9]*)+")
_FROM_IMPORT = re.compile(
    r"^(\s*)(from\s+)(app(?:\.[A-Za-z_][A-Za-z_0-9]*)+)(\s+import\s+)(.+)$")
_PLAIN_IMPORT = re.compile(
    r"^(\s*)(import\s+)(app(?:\.[A-Za-z_][A-Za-z_0-9]*)+)(\s+as\s+\w+)?\s*$")
SKIP_DIRS = {"__pycache__"}


def _mod(rel: str) -> str:
    """app/data/net.py -> app.data.net"""
    rel = rel.replace("\\", "/").rstrip("/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    return "app." + rel.replace("/", ".")


def build_final_map() -> dict[str, str]:
    mp: dict[str, str] = {}
    for old, new in MOVES.items():
        om, nm = _mod(old), _mod(new)
        if om != nm:
            mp[om] = nm
    # git rename 补充（test_*.py 等 MOVES 未显式列出的）
    for k, v in build_map().items():
        mp.setdefault(k, v)
    # MOVES 只列了文件，包级（往往是 ``from app.core.pool import sync`` 的形态）
    # 需要显式补齐。每一项都可从 MOVES 的文件归属严格推出，不做猜测。
    EXTRA = {
        # app.data.** → app.datasource.**
        "app.data": "app.datasource",
        "app.data.models": "app.datasource.model",
        "app.data.providers": "app.datasource.provider",
        "app.data.providers.adapters": "app.datasource.adapters",
        "app.data.services": "app.datasource.service",
        "app.data.storage": "app.datasource.raw",
        # app.core.** → 各目标层
        "app.core.market": "app.domain",
        "app.core.factor": "app.feature",
        "app.core.indicator": "app.feature.indicator",
        "app.core.strategy": "app.strategy",
        "app.core.rule": "app.strategy.rule",
        "app.core.score": "app.alpha",
        "app.core.pool": "app.alpha.pool",
        "app.core.engine": "app.engine",
        "app.core.engine.components": "app.engine.component",
        "app.core.execution": "app.execution",
        "app.core.risk": "app.risk",
        "app.core.portfolio": "app.portfolio",
        "app.core.news": "app.news",
        "app.core.bus": "app.engine.bus",
        "app.core.hub": "app.engine.hub",
        "app.core.task": "app.engine.tasking",
        "app.core.collect": "app.engine.collect",
        "app.core.monitor": "app.monitoring",
        # 顶层包
        "app.backtest": "app.research",
        "app.test": "app.tests",
        "app.db": "app.storage",
        "app.repository": "app.storage.repo",
        "app.utils": "app.common",
        # 注意：刻意不给 ``app.core`` 加兜底。core 下的模块已经全部穷举，
        # 裸 ``app.core`` 一旦映射到 ``app`` 会把漏网路径静默改成错路径，
        # 宁可让它留在原地由人工复核。
    }
    for k, v in EXTRA.items():
        mp.setdefault(k, v)
    # 长路径优先放在前面，避免被短前缀抢先命中
    return dict(sorted(mp.items(), key=lambda kv: -len(kv[0])))


def rewrite_line(mp: dict[str, str], line: str, hits: dict[str, int]) -> str:
    body = line.rstrip("\r\n")
    tail = line[len(body):]

    m = _FROM_IMPORT.match(body)
    if m:
        indent, kw, module, imp, names = m.groups()
        target = None
        # 关键：先试 ``module.name``。MAP 里存的是完整模块路径，
        # 直接拿 ``module`` 查表会把 ``from app.data import net``
        # 错改成 ``from app.datasource import net``。
        first = names.split(",")[0].strip().split()[0]
        if f"{module}.{first}" in mp:
            target = mp[f"{module}.{first}"]
        elif module in mp:
            target = mp[module]
        else:
            target = resolve(mp, module)
        if target and target != module:
            hits[module] = hits.get(module, 0) + 1
            if "." in target and not names.startswith("*") and target.count(".") >= 1:
                parent, _, child = target.rpartition(".")
                if parent and parent != module:
                    return f"{indent}{kw}{parent}{imp}{child}{tail}"
            return f"{indent}{kw}{target}{imp}{names}{tail}"
        return line

    m = _PLAIN_IMPORT.match(body)
    if m:
        indent, kw, module, alias = m.groups()
        new = mp.get(module) or resolve(mp, module)
        if new and new != module:
            hits[module] = hits.get(module, 0) + 1
            return f"{indent}{kw}{new}{alias or ''}{tail}"
        return line

    out: list[str] = []
    last = 0
    for m in _PATTERN.finditer(line):
        old = m.group(0)
        new = mp.get(old) or resolve(mp, old)
        if not new or new == old:
            continue
        out.append(line[last:m.start()])
        out.append(new)
        last = m.end()
        hits[old] = hits.get(old, 0) + 1
    if not out:
        return line
    out.append(line[last:])
    return "".join(out)


def main() -> int:
    apply = "--apply" in sys.argv
    mp = build_final_map()
    print(f"最终映射 {len(mp)} 条\n")

    for base in (APP, os.path.join(ROOT, "scripts"), os.path.join(ROOT, "docs")):
        pass
    roots = [APP]
    if "--self" in sys.argv:
        roots += [os.path.join(ROOT, "scripts"), os.path.join(ROOT, "docs")]
    targets: list[str] = []
    for base in roots:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith((".py", ".md")):
                    targets.append(os.path.join(dirpath, fn))
    for extra in ("main.py", "main.pyw"):
        p = os.path.join(ROOT, extra)
        if os.path.exists(p):
            targets.append(p)

    files, total = 0, 0
    agg: dict[str, int] = {}
    for path in sorted(targets):
        try:
            lines = open(path, encoding="utf-8").readlines()
        except Exception:
            continue
        hits: dict[str, int] = {}
        new_lines = [rewrite_line(mp, ln, hits) for ln in lines]
        if not hits:
            continue
        files += 1
        for k, v in hits.items():
            agg[k] = agg.get(k, 0) + v
            total += v
        if apply:
            open(path, "w", encoding="utf-8", newline="\n").writelines(new_lines)

    print(f"改写 {files} 个文件，{total} 处。明细：")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:50]:
        print(f"  {v:>3}  {k}  ->  {mp.get(k) or resolve(mp, k)}")
    if not apply:
        print("\n（dry-run，加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
