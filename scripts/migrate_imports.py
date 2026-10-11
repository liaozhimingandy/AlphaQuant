#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaQuant app/ 目录重构 —— 阶段二：全量 import 路径改写。

策略：用正则扫出所有 `app.xxx.yyy` 形式的路径，精确查表替换。
一次性扫描 + 查表天然避免了「长路径被短前缀先替换」的顺序陷阱。

用法：python migrate_imports.py [--apply]
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

# ------------------------------------------------------------------ 路径映射
# 「旧 import 路径」→「新 import 路径」。键必须写全，不做前缀推断。
MAP: dict[str, str] = {
    # ================= L0 common =================
    "app.common.logging": "app.common.logging",
    "app.common.jsonio": "app.common.jsonio",
    "app.common": "app.common",
    "app.common.config": "app.common.config",

    # ================= L1 domain =================
    "app.domain.types": "app.domain.types",
    "app.domain.calendar": "app.domain.calendar",
    "app.domain.clean": "app.domain.clean",
    "app.domain.series": "app.domain.series",
    "app.domain": "app.domain",
    "app.domain.symbol": "app.domain.symbol",

    # ================= L2 datasource =================
    "app.datasource.model.enums": "app.datasource.model.enums",
    "app.datasource.model.kline": "app.datasource.model.kline",
    "app.datasource.model.rank": "app.datasource.model.rank",
    "app.datasource.model.fundflow": "app.datasource.model.fundflow",
    "app.datasource.model": "app.datasource.model",
    "app.datasource.transport.net": "app.datasource.transport.net",
    "app.datasource.provider.base": "app.datasource.provider.base",
    "app.datasource.provider.health": "app.datasource.provider.health",
    "app.datasource.provider.registry": "app.datasource.provider.registry",
    "app.datasource.provider.capability": "app.datasource.provider.capability",
    "app.datasource.routing.config": "app.datasource.routing.config",
    "app.datasource.routing.router": "app.datasource.routing.router",
    "app.datasource.routing.quality": "app.datasource.routing.quality",
    "app.datasource.adapters.baostock": "app.datasource.adapters.baostock",
    "app.datasource.adapters.akshare": "app.datasource.adapters.akshare",
    "app.datasource.adapters.eastmoney": "app.datasource.adapters.eastmoney",
    "app.datasource.adapters.xueqiu": "app.datasource.adapters.xueqiu",
    "app.datasource.adapters.mootdx": "app.datasource.adapters.mootdx",
    "app.datasource.adapters.astockdata": "app.datasource.adapters.astockdata",
    "app.datasource.adapters.tushare": "app.datasource.adapters.tushare",
    "app.datasource.adapters.local": "app.datasource.adapters.local",
    "app.datasource.adapters": "app.datasource.adapters",
    "app.datasource.provider": "app.datasource.provider",
    "app.datasource.service.data_service": "app.datasource.service.data_service",
    "app.datasource.service": "app.datasource.service",
    "app.datasource.service.facade": "app.datasource.service.facade",
    "app.datasource.raw.raw_store": "app.datasource.raw.raw_store",
    "app.datasource.raw": "app.datasource.raw",
    # 兼容期保留的旧实现对 A -> compat/
    "app.datasource.compat.registry": "app.datasource.compat.registry",
    "app.datasource.compat.kline_base": "app.datasource.compat.kline_base",
    "app.datasource.compat.kline_sources": "app.datasource.compat.kline_sources",
    "app.datasource.compat.hot_sources": "app.datasource.compat.hot_sources",
    "app.datasource.compat.local_source": "app.datasource.compat.local_source",
    "app.datasource.compat.mootdx_source": "app.datasource.compat.mootdx_source",
    "app.datasource.compat.astockdata_source": "app.datasource.compat.astockdata_source",

    # ================= L3 storage =================
    "app.storage.engine": "app.storage.engine",
    "app.storage.schema": "app.storage.schema",
    "app.storage": "app.storage",
    "app.storage.service": "app.storage.service",
    "app.storage.repo.kline_repo": "app.storage.repo.kline_repo",
    "app.storage.repo.minute_repo": "app.storage.repo.minute_repo",
    "app.storage.repo.stock_repo": "app.storage.repo.stock_repo",
    "app.storage.repo.trade_repo": "app.storage.repo.trade_repo",
    "app.storage.repo": "app.storage.repo",

    # ================= L4 feature =================
    "app.feature.indicator.base": "app.feature.indicator.base",
    "app.feature.indicator.builtin": "app.feature.indicator.builtin",
    "app.feature.indicator.registry": "app.feature.indicator.registry",
    "app.feature.indicator": "app.feature.indicator",
    "app.feature.context": "app.feature.context",
    "app.feature.library": "app.feature.library",
    "app.feature.builtin": "app.feature.builtin",
    "app.feature.registry": "app.feature.registry",
    "app.feature.evaluate": "app.feature.evaluate",
    "app.feature.expr": "app.feature.expr",
    "app.feature.flow": "app.feature.flow",
    "app.feature.catalog": "app.feature.catalog",
    "app.feature.mining.mining": "app.feature.mining.mining",
    "app.feature.mining.ml": "app.feature.mining.ml",
    "app.feature.base": "app.feature.base",
    "app.feature": "app.feature",
    "app.feature.features": "app.feature.features",
    "app.feature": "app.feature",

    # ================= L5 alpha =================
    "app.alpha.score": "app.alpha.score",
    "app.alpha": "app.alpha",
    "app.alpha.pool.pool": "app.alpha.pool.pool",
    "app.alpha.pool.sync": "app.alpha.pool.sync",
    "app.alpha.pool.demo": "app.alpha.pool.demo",
    "app.alpha.pool": "app.alpha.pool",

    # ================= L6 strategy =================
    "app.strategy.base": "app.strategy.base",
    "app.strategy.state": "app.strategy.state",
    "app.strategy.spec": "app.strategy.spec",
    "app.strategy.builtin": "app.strategy.builtin",
    "app.strategy.store": "app.strategy.store",
    "app.strategy.registry": "app.strategy.registry",
    "app.strategy.discovery": "app.strategy.discovery",
    "app.strategy.score_strategy": "app.strategy.score_strategy",
    "app.strategy.multi_factor_strategy": "app.strategy.multi_factor_strategy",
    "app.strategy": "app.strategy",
    "app.strategy.rule.base": "app.strategy.rule.base",
    "app.strategy.rule.builtin": "app.strategy.rule.builtin",
    "app.strategy.rule.combinator": "app.strategy.rule.combinator",
    "app.strategy.rule.spec": "app.strategy.rule.spec",
    "app.strategy.rule": "app.strategy.rule",

    # ================= risk / portfolio / execution =================
    "app.risk.base": "app.risk.base",
    "app.risk.builtin": "app.risk.builtin",
    "app.risk.registry": "app.risk.registry",
    "app.risk": "app.risk",
    "app.portfolio.sizer": "app.portfolio.sizer",
    "app.portfolio.manager": "app.portfolio.manager",
    "app.portfolio": "app.portfolio",
    "app.execution.gateway": "app.execution.gateway",
    "app.execution.broker": "app.execution.broker",
    "app.execution.endpoints": "app.execution.endpoints",
    "app.execution.http_gateway": "app.execution.http_gateway",
    "app.execution.live_broker": "app.execution.live_broker",
    "app.execution": "app.execution",

    # ================= L7 engine =================
    "app.engine.base": "app.engine.base",
    "app.engine.control": "app.engine.control",
    "app.engine.builder": "app.engine.builder",
    "app.engine.event": "app.engine.event",
    "app.engine.settings": "app.engine.settings",
    "app.engine.task": "app.engine.task",
    "app.engine.utils": "app.engine.utils",
    "app.engine.daemon": "app.engine.daemon",
    "app.engine.service_runner": "app.engine.service_runner",
    "app.engine.service": "app.engine.service",
    "app.engine.component.base": "app.engine.component.base",
    "app.engine.component.collector": "app.engine.component.collector",
    "app.engine.component.dataguard": "app.engine.component.dataguard",
    "app.engine.component.extdata": "app.engine.component.extdata",
    "app.engine.component.live_gateway": "app.engine.component.live_gateway",
    "app.engine.component.market": "app.engine.component.market",
    "app.engine.component.news": "app.engine.component.news",
    "app.engine.component.strategy": "app.engine.component.strategy",
    "app.engine.component.timer": "app.engine.component.timer",
    "app.engine.component.trading_store": "app.engine.component.trading_store",
    "app.engine.component": "app.engine.component",
    "app.engine": "app.engine",
    "app.engine.tasking.task": "app.engine.tasking.task",
    "app.engine.tasking.runtime": "app.engine.tasking.runtime",
    "app.engine.tasking.spec": "app.engine.tasking.spec",
    "app.engine.tasking": "app.engine.tasking",
    "app.engine.collect.spec": "app.engine.collect.spec",
    "app.engine.collect.schedule": "app.engine.collect.schedule",
    "app.engine.collect": "app.engine.collect",
    "app.engine.bus.components": "app.engine.bus.components",
    "app.engine.bus.filebus": "app.engine.bus.filebus",
    "app.engine.bus": "app.engine.bus",
    "app.engine.hub.hub": "app.engine.hub.hub",
    "app.engine.hub.panel": "app.engine.hub.panel",
    "app.engine.hub.spec": "app.engine.hub.spec",
    "app.engine.hub.supervisor": "app.engine.hub.supervisor",
    "app.engine.hub.worker": "app.engine.hub.worker",
    "app.engine.hub": "app.engine.hub",
    "app.engine.integrity": "app.engine.integrity",

    # ================= news =================
    "app.news.source": "app.news.source",
    "app.news.analyzer": "app.news.analyzer",
    "app.news.center": "app.news.center",
    "app.news": "app.news",

    # ================= research / monitoring / tests =================
    "app.research.analyzers": "app.research.analyzers",
    "app.research.config": "app.research.config",
    "app.research.defaults": "app.research.defaults",
    "app.research.registry": "app.research.registry",
    "app.research.report": "app.research.report",
    "app.research.result": "app.research.result",
    "app.research.runner": "app.research.runner",
    "app.research": "app.research",
    "app.monitoring.snapshot": "app.monitoring.snapshot",
    "app.monitoring.server": "app.monitoring.server",
    "app.monitoring.api.backtest": "app.monitoring.api.backtest",
    "app.monitoring.dashboard_html": "app.monitoring.dashboard_html",
    "app.monitoring": "app.monitoring",
    "app.tests.helpers": "app.tests.helpers",
    "app.tests": "app.tests",

    # ================= core 兜底（放最后，避免误伤上面的精确项） =================
    "app": "app",
}

# 按长度降序：re.sub 的一次回调里逐次查表，这里只是保证诊断输出清晰
_KEYS = sorted(MAP, key=len, reverse=True)
_PATTERN = re.compile(r"\bapp(?:\.[A-Za-z_][A-Za-z_0-9]*)+")

# 命中但目标模块不存在的项（迁移遗留，需要人工处理）
SKIP_DIRS = {"__pycache__"}


def rewrite(text: str) -> tuple[str, dict[str, int]]:
    hits: dict[str, int] = {}

    def _sub(m: re.Match) -> str:
        old = m.group(0)
        new = MAP.get(old)
        if new is None:
            return old
        hits[old] = hits.get(old, 0) + 1
        return new

    return _PATTERN.sub(_sub, text), hits


def main() -> int:
    apply = "--apply" in sys.argv
    total_files = 0
    total_hits = 0
    agg: dict[str, int] = {}

    targets: list[str] = []
    for base in (APP, os.path.join(ROOT, "scripts")):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if os.path.basename(dirpath) in SKIP_DIRS:
                continue
            for fn in filenames:
                if fn.endswith(".py"):
                    targets.append(os.path.join(dirpath, fn))
    # 入口文件
    for extra in ("main.py", "main.pyw"):
        p = os.path.join(ROOT, extra)
        if os.path.exists(p):
            targets.append(p)

    for path in sorted(targets):
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            continue
        new_text, hits = rewrite(text)
        if not hits or new_text == text:
            continue
        total_files += 1
        for k, v in hits.items():
            agg[k] = agg.get(k, 0) + v
            total_hits += v
        if apply:
            open(path, "w", encoding="utf-8", newline="\n").write(new_text)

    print(f"改写 {total_files} 个文件，替换 {total_hits} 处路径\n")
    print("命中明细 TOP40：")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:40]:
        print(f"  {v:>4}  {k}  ->  {MAP[k]}")
    if not apply:
        print("\n（dry-run，加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
