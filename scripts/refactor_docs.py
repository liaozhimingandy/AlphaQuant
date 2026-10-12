#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把文档（docs/*.md、README.md）里的旧模块路径改成重构后的路径。

同时处理两种写法：``app.datasource.service`` 与 ``app/datasource/service.py``。

用法::

    python scripts/refactor_docs.py            # dry-run
    python scripts/refactor_docs.py --apply
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (旧, 新)，用点号写；脚本内部会同时生成 slash 变体的匹配
MIGRATIONS: list[tuple[str, str]] = [
    ("app.engine.component.service.market", "app.modules.market.service"),
    ("app.engine.component.service.collection", "app.modules.collection.service"),
    ("app.engine.component.service.trading", "app.modules.trading.service"),
    ("app.engine.component.service.risk", "app.modules.risk.service"),
    ("app.engine.component.service.portfolio", "app.modules.portfolio.service"),
    ("app.engine.component.service.news", "app.modules.news.service"),
    ("app.engine.component.strategy", "app.modules.strategy.component"),
    ("app.engine.component.scheduler", "app.runtime.scheduler"),
    ("app.engine.component.worker", "app.runtime.executor"),
    ("app.engine.component.base", "app.engine.component"),
    ("app.engine.component.kinds", "app.engine.capability"),
    ("app.engine.component.registry", "app.engine.registry"),
    ("app.engine.tasking", "app.runtime.tasking"),
    ("app.engine.bus", "app.runtime.bus"),
    ("app.engine.event", "app.runtime.event_bus"),
    ("app.engine.utils", "app.runtime.utils"),
    ("app.engine.task", "app.runtime.task"),
    ("app.engine.collect.spec", "app.modules.collection.spec"),
    ("app.engine.collect.schedule", "app.modules.collection.schedule"),
    ("app.engine.collect", "app.modules.collection"),
    ("app.engine.integrity", "app.modules.collection.integrity"),
    ("app.engine.control", "app.control.control_plane"),
    ("app.engine.settings", "app.engine.context"),
    ("app.engine.base", "app.engine.engine"),
    ("app.storage.repo", "app.data.repo"),
    ("app.storage.service", "app.data.service"),
    ("app.storage.engine", "app.data.engine"),
    ("app.storage.schema", "app.data.schema"),
    ("app.storage", "app.data"),
    ("app.datasource", "app.providers.market"),
    ("app.execution.live_broker", "app.modules.trading.live_broker"),
    ("app.execution.gateway", "app.providers.broker.gateway"),
    ("app.execution.http_gateway", "app.providers.broker.http_gateway"),
    ("app.execution.endpoints", "app.providers.broker.endpoints"),
    ("app.execution.broker", "app.providers.broker.simulated"),
    ("app.execution", "app.providers.broker"),
    ("app.portfolio.manager", "app.account.portfolio"),
    ("app.portfolio.sizer", "app.account.sizer"),
    ("app.portfolio", "app.account"),
    ("app.alpha.pool", "app.modules.universe.pool"),
    ("app.alpha.score", "app.modules.universe.score"),
    ("app.alpha", "app.modules.universe"),
    ("app.feature", "app.modules.factor"),
    ("app.strategy", "app.modules.strategy"),
    ("app.research", "app.modules.backtest"),
    ("app.risk", "app.modules.risk"),
    ("app.news", "app.modules.news"),
]

MIGRATIONS.sort(key=lambda kv: len(kv[0]), reverse=True)


def build_pattern(old: str) -> re.Pattern:
    """把 "a.b.c" 变成能匹配 a.b.c / a/b/c / a/b/c.py 的正则。"""
    parts = old.split(".")
    # 只允许分隔符在模块段之间变化；段名本身是字面量
    body = r"[\./]".join(re.escape(p) for p in parts)
    return re.compile(r"(?<![\w/])" + body + r"(?![\w-])")


def build_new(old: str, new: str, matched: str) -> str:
    """按原匹配用的分隔符风格，还原新路径。"""
    sep = "/" if "/" in matched else "."
    if "." in old and "." in matched.replace("/", "."):
        return new.replace(".", sep)
    return new.replace(".", sep)


def process(text: str) -> tuple[str, int]:
    hits = 0
    for old, new in MIGRATIONS:
        pat = build_pattern(old)

        def _sub(m: re.Match) -> str:
            nonlocal hits
            hits += 1
            return build_new(old, new, m.group(0))

        text = pat.sub(_sub, text)
    return text, hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    files = sorted(set(list(ROOT.glob("docs/*.md")) + [ROOT / "README.md"]))
    total, changed = 0, []
    for f in files:
        src = f.read_text(encoding="utf-8")
        new, n = process(src)
        if n:
            total += n
            changed.append((f, n))
            if args.apply:
                f.write_text(new, encoding="utf-8")
    mode = "已写入" if args.apply else "dry-run"
    print(f"[{mode}] 命中文件 {len(changed)} 个，替换 {total} 处")
    for f, n in sorted(changed, key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
