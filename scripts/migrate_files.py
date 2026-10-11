#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaQuant app/ 目录重构 —— 阶段一：文件迁移（保留 git 追踪）。

用法：python migrate_files.py [--apply]
不带 --apply 时只打印计划。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 迁移映射
# key: 旧路径(相对 app/), value: 新路径(相对 app/)
MOVES: dict[str, str] = {
    # ================= L0  common：零依赖基础设施 =================
    "utils/logger.py": "common/logging.py",
    "utils/jsonio.py": "common/jsonio.py",
    "core/config.py": "common/config.py",

    # ================= L1  domain：领域模型（零业务依赖） =================
    "core/market/types.py": "domain/types.py",
    "core/market/calendar.py": "domain/calendar.py",
    "core/market/clean.py": "domain/clean.py",
    "core/market/series.py": "domain/series.py",
    "data/datasource.py": "domain/symbol.py",

    # ================= L2  datasource：DataSource 全链路 =================
    # --- Canonical Model ---
    "data/models/enums.py": "datasource/model/enums.py",
    "data/models/kline.py": "datasource/model/kline.py",
    "data/models/rank.py": "datasource/model/rank.py",
    "data/models/fundflow.py": "datasource/model/fundflow.py",
    # --- 传输层 ---
    "data/net.py": "datasource/transport/net.py",
    # --- Provider / Capability ---
    "data/providers/base.py": "datasource/provider/base.py",
    "data/providers/health.py": "datasource/provider/health.py",
    "data/providers/registry.py": "datasource/provider/registry.py",
    # --- Router ---
    "data/providers/config.py": "datasource/routing/config.py",
    "data/providers/router.py": "datasource/routing/router.py",
    "data/providers/quality.py": "datasource/routing/quality.py",
    # --- Adapter ---
    "data/providers/adapters/baostock.py": "datasource/adapters/baostock.py",
    "data/providers/adapters/akshare.py": "datasource/adapters/akshare.py",
    "data/providers/adapters/eastmoney.py": "datasource/adapters/eastmoney.py",
    "data/providers/adapters/xueqiu.py": "datasource/adapters/xueqiu.py",
    "data/providers/adapters/mootdx.py": "datasource/adapters/mootdx.py",
    "data/providers/adapters/astockdata.py": "datasource/adapters/astockdata.py",
    "data/providers/adapters/tushare.py": "datasource/adapters/tushare.py",
    "data/providers/adapters/local.py": "datasource/adapters/local.py",
    # --- DataService ---
    "data/services/data_service.py": "datasource/service/data_service.py",
    "data/marketx.py": "datasource/service/facade.py",
    # --- 旧.Old沾 events registry<｜hy_place▁holder▁no▁813｜>
    "data/sources.py": "datasource/compat/registry.py",
    "data/kline_base.py": "datasource/compat/kline_base.py",
    "data/kline_sources.py": "datasource/compat/kline_sources.py",
    "data/hot_sources.py": "datasource/compat/hot_sources.py",
    "data/local_source.py": "datasource/compat/local_source.py",
    "data/mootdx_source.py": "datasource/compat/mootdx_source.py",
    "data/astockdata_source.py": "datasource/compat/astockdata_source.py",

    # ================= L3  storage =================
    "db/database.py": "storage/engine.py",
    "db/models.py": "storage/schema.py",
    "data/service.py": "storage/service.py",
    "repository/marketx_repository.py": "storage/repo/kline_repo.py",
    "repository/minute_repository.py": "storage/repo/minute_repo.py",
    "repository/stock_repository.py": "storage/repo/stock_repo.py",
    "repository/trading_repository.py": "storage/repo/trade_repo.py",

    # ================= L4  feature：指标 → 因子 → 挖掘 =================
    "core/indicator/base.py": "feature/indicator/base.py",
    "core/indicator/builtin.py": "feature/indicator/builtin.py",
    "core/indicator/registry.py": "feature/indicator/registry.py",
    "core/factor/base.py": "feature/base.py",
    "core/factor/context.py": "feature/context.py",
    "core/factor/library.py": "feature/library.py",
    "core/factor/builtin.py": "feature/builtin.py",
    "core/factor/registry.py": "feature/registry.py",
    "core/factor/evaluate.py": "feature/evaluate.py",
    "core/factor/expr.py": "feature/expr.py",
    "core/factor/flow.py": "feature/flow.py",
    "core/factor/catalog.py": "feature/catalog.py",
    "core/factor/mining.py": "feature/mining/mining.py",
    "core/factor/ml.py": "feature/mining/ml.py",
    "core/data/features.py": "feature/features.py",

    # ================= L5  alpha：打分引擎 + 候选池 =================
    "core/score/engine.py": "alpha/score.py",
    "core/pool/pool.py": "alpha/pool/pool.py",
    "core/pool/sync.py": "alpha/pool/sync.py",
    "core/pool/demo.py": "alpha/pool/demo.py",

    # ================= L6  strategy =================
    "core/strategy/base.py": "strategy/base.py",
    "core/strategy/state.py": "strategy/state.py",
    "core/strategy/spec.py": "strategy/spec.py",
    "core/strategy/builtin.py": "strategy/builtin.py",
    "core/strategy/store.py": "strategy/store.py",
    "core/strategy/registry.py": "strategy/registry.py",
    "core/strategy/discovery.py": "strategy/discovery.py",
    "core/strategy/score_strategy.py": "strategy/score_strategy.py",
    "core/strategy/multi_factor_strategy.py": "strategy/multi_factor_strategy.py",
    "core/rule/base.py": "strategy/rule/base.py",
    "core/rule/builtin.py": "strategy/rule/builtin.py",
    "core/rule/combinator.py": "strategy/rule/combinator.py",
    "core/rule/spec.py": "strategy/rule/spec.py",

    # ================= 决策辅助：risk / portfolio =================
    "core/risk/base.py": "risk/base.py",
    "core/risk/builtin.py": "risk/builtin.py",
    "core/risk/registry.py": "risk/registry.py",
    "core/portfolio/sizer.py": "portfolio/sizer.py",
    "core/portfolio/manager.py": "portfolio/manager.py",

    # ================= 交易执行 =================
    "core/execution/gateway.py": "execution/gateway.py",
    "core/execution/broker.py": "execution/broker.py",
    "core/execution/endpoints.py": "execution/endpoints.py",
    "core/execution/http_gateway.py": "execution/http_gateway.py",
    "core/execution/live_broker.py": "execution/live_broker.py",

    # ================= L7  engine 运行时 =================
    "core/engine/engine.py": "engine/base.py",
    "core/engine/control.py": "engine/control.py",
    "core/engine/builder.py": "engine/builder.py",
    "core/engine/event.py": "engine/event.py",
    "core/engine/settings.py": "engine/settings.py",
    "core/engine/task.py": "engine/task.py",
    "core/engine/utils.py": "engine/utils.py",
    "core/engine/daemon.py": "engine/daemon.py",
    "core/engine/service.py": "engine/service.py",
    "core/engine/service_runner.py": "engine/service_runner.py",
    "core/engine/components/ibase.py": "engine/component/base.py",
    "core/engine/components/collector.py": "engine/component/collector.py",
    "core/engine/components/dataguard.py": "engine/component/dataguard.py",
    "core/engine/components/extdata.py": "engine/component/extdata.py",
    "core/engine/components/live_gateway.py": "engine/component/live_gateway.py",
    "core/engine/components/market.py": "engine/component/market.py",
    "core/engine/components/news.py": "engine/component/news.py",
    "core/engine/components/strategy.py": "engine/component/strategy.py",
    "core/engine/components/timer.py": "engine/component/timer.py",
    "core/engine/components/trading_store.py": "engine/component/trading_store.py",
    "core/task/task.py": "engine/tasking/task.py",
    "core/task/runtime.py": "engine/tasking/runtime.py",
    "core/task/spec.py": "engine/tasking/spec.py",
    "core/collect/spec.py": "engine/collect/spec.py",
    "core/collect/schedule.py": "engine/collect/schedule.py",
    "core/bus/components.py": "engine/bus/components.py",
    "core/bus/filebus.py": "engine/bus/filebus.py",
    "core/hub/hub.py": "engine/hub/hub.py",
    "core/hub/panel.py": "engine/hub/panel.py",
    "core/hub/spec.py": "engine/hub/spec.py",
    "core/hub/supervisor.py": "engine/hub/supervisor.py",
    "core/hub/worker.py": "engine/hub/worker.py",
    "core/data/integrity.py": "engine/integrity.py",

    # ================= 舆情 =================
    "core/news/source.py": "news/source.py",
    "core/news/analyzer.py": "news/analyzer.py",
    "core/news/center.py": "news/center.py",

    # ================= research（回测） =================
    "backtest/analyzers.py": "research/analyzers.py",
    "backtest/config.py": "research/config.py",
    "backtest/defaults.py": "research/defaults.py",
    "backtest/registry.py": "research/registry.py",
    "backtest/report.py": "research/report.py",
    "backtest/result.py": "research/result.py",
    "backtest/runner.py": "research/runner.py",

    # ================= monitoring =================
    "core/monitor/snapshot.py": "monitoring/snapshot.py",
    "core/monitor/web.py": "monitoring/server.py",
    "core/monitor/backtest_api.py": "monitoring/api/backtest.py",

    # ================= tests =================
    "test/conftest.py": "tests/conftest.py",
    "test/helpers.py": "tests/helpers.py",
}

# test_*.py 统一进 app/tests/
TEST_PREFIX = ("test/test_", "tests/")

# 直接删除的死代码（零引用）
DELETES = [
    "analyzer/__init__.py",
    "core/signal/__init__.py",
    "core/signal/base.py",
    "core/data/interfaces.py",
    "core/portfolio/base.py",
    "core/execution/base.py",
    "data/collector/__init__.py",
    "data/collector/stock_collector.py",
    "db/init_db.py",
    # engine/component.py 里的 ComponentState 与 components/ibase.py 重复，保留后者
    "core/engine/component.py",
]

# 脚手架 __init__.py（会被真正的 __init__.py 覆盖，无需移动）
SCAFFOLD_INITS = [
    "core/data/__init__.py",
    "core/market/__init__.py",
    "core/signal/__init__.py",
    "data/__init__.py",
    "strategy/__init__.py",
]


def _git(*args: str) -> None:
    subprocess.run(["git", "-C", ROOT, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def main() -> int:
    apply = "--apply" in sys.argv
    moved: list[tuple[str, str]] = []

    for old, new in MOVES.items():
        src = os.path.join(ROOT, "app", old)
        dst = os.path.join(ROOT, "app", new)
        if not os.path.exists(src):
            print(f"  [skip] 源不存在: {old}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        moved.append((old, new))
        if apply:
            if os.path.exists(dst):
                os.remove(dst)
            _git("mv", "-f", os.path.join("app", old), os.path.join("app", new))

    # 测试文件
    tdir = os.path.join(ROOT, "app", "test")
    if os.path.exists(tdir):
        for name in sorted(os.listdir(tdir)):
            if not name.endswith(".py"):
                continue
            if name in ("__init__.py", "conftest.py", "helpers.py"):
                continue  # 已在 MOVES 里
            if not name.startswith("test_"):
                continue
            moved.append((f"test/{name}", f"tests/{name}"))
            if apply:
                _git("mv", "-f", f"app/test/{name}", f"app/tests/{name}")

    # 删除死代码
    for rel in DELETES:
        path = os.path.join(ROOT, "app", rel)
        if os.path.exists(path):
            print(f"  [del ] {rel}")
            if apply:
                _git("rm", "-q", "-f", os.path.join("app", rel))

    print(f"\n移动 {len(moved)} 个文件，删除 {len(DELETES)} 个死代码")
    if not apply:
        print("（dry-run，加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
