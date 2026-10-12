#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目录重构后的导入路径批量迁移。

用法::

    python scripts/refactor_paths.py            # dry-run，只统计
    python scripts/refactor_paths.py --apply    # 真正写入

规则按**最长前缀优先**匹配，避免 app.engine.task 误伤 app.engine.tasking。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 迁移映射表
# 格式: (旧前缀, 新前缀)。务必保持**长在前**。
MIGRATIONS: list[tuple[str, str]] = [
    # ---- engine 内部：业务服务 -> modules ----
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
    # ---- engine 内部：调度 runtime ----
    ("app.engine.tasking", "app.runtime.tasking"),
    ("app.engine.bus", "app.runtime.bus"),
    ("app.engine.event", "app.runtime.event_bus"),
    ("app.engine.utils", "app.runtime.utils"),
    ("app.engine.task", "app.runtime.task"),
    # ---- engine 内部：采集 -> modules/collection ----
    ("app.engine.collect.spec", "app.modules.collection.spec"),
    ("app.engine.collect.schedule", "app.modules.collection.schedule"),
    ("app.engine.collect", "app.modules.collection"),
    ("app.engine.integrity", "app.modules.collection.integrity"),
    # ---- engine 自身收窄 ----
    ("app.engine.control", "app.control.control_plane"),
    ("app.engine.settings", "app.engine.context"),
    ("app.engine.base", "app.engine.engine"),
    # app.engine.{hub,daemon,builder,service,service_runner} 原地保留
    # ---- 持久化 / provider ----
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
    # ---- 业务包 -> modules ----
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

# 按长度降序，保证最长前缀先命中
MIGRATIONS.sort(key=lambda kv: len(kv[0]), reverse=True)

# 匹配形如 app.xxx.yyy 的模块路径（含后续的属性访问，先整段抓出来再切）
_PATH_RE = re.compile(r"\bapp(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _replace(text: str) -> tuple[str, int]:
    """替换文本里的模块路径，返回 (新文本, 替换次数)。"""
    hits = 0

    def _sub(m: re.Match) -> str:
        nonlocal hits
        path = m.group(0)
        for old, new in MIGRATIONS:
            if path == old:
                hits += 1
                return new
            if path.startswith(old + "."):
                hits += 1
                return new + path[len(old):]
        return path

    return _PATH_RE.sub(_sub, text), hits


def _iter_files() -> list[Path]:
    """遍历需要改写的 python 文件。"""
    files: list[Path] = []
    for pat in ("app/**/*.py", "scripts/**/*.py", "main.py", "main.pyw"):
        for p in ROOT.glob(pat):
            if "__pycache__" in str(p) or ".pytest_tmp" in str(p):
                continue
            # 本脚本自身跳过，避免把映射表改坏
            if p.name == "refactor_paths.py":
                continue
            files.append(p)
    # 历史迁移脚本含大量旧路径示例，改了反而失真，排除
    files = [f for f in files if f.name not in (
        "migrate_imports.py", "finalize_imports.py", "merge_services.py")]
    return sorted(set(files))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    args = ap.parse_args()

    total_hits = 0
    changed: list[tuple[Path, int]] = []
    for f in _iter_files():
        try:
            src = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new, n = _replace(src)
        if n:
            total_hits += n
            changed.append((f, n))
            if args.apply:
                f.write_text(new, encoding="utf-8")

    mode = "已写入" if args.apply else "dry-run"
    print(f"[{mode}] 命中文件 {len(changed)} 个，替换 {total_hits} 处")
    for f, n in sorted(changed, key=lambda kv: -kv[1])[:25]:
        print(f"  {n:4d}  {f.relative_to(ROOT)}")
    if len(changed) > 25:
        print(f"  ... 其余 {len(changed) - 25} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
