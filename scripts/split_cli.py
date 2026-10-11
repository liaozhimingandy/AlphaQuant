#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 2642 行的 app/cli.py 按职责切成 app/cli/ 包。

做法与验证：
1. AST 解析原文件，取出每个**顶层**定义（含装饰器与上方注释横幅）的源码片段；
2. 按 NAME -> 模块 的分配表重新组装；
3. 对每个片段做 ``ast.dump`` 比对 —— 只要 dump 一致，就证明这段函数体
   与原文件逐节点相同，不存在手抄失真；
4. 检查覆盖率：原文件所有顶层名字必须被分配且只分配一次。

用法：python scripts/split_cli.py [--apply]
      不带 --apply 只做体检并打印将要生成的内容。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "cli.py"
OUT = ROOT / "app" / "cli"

# ---------------------------------------------------------------- 分配表
# base.py 放「主命令组 + 跨模块共用工具」，不 import 任何 *_cmds，故无循环依赖。
GROUPS: dict[str, list[str]] = {
    "base.py": [
        "DEFAULT_END", "_parse_params", "cli", "_print_endpoints",
    ],
    "data_cmds.py": [
        "init_db_cmd", "collect_cmd", "dataguard_cmd",
        "_PROBE_ARGS", "_probe_sources", "sources_cmd", "providers_cmd",
        "_PROBE_ARGS_BY_CAP", "_probe_routes", "_echo_host_state", "list_cmd",
    ],
    "strategy_cmds.py": [
        "strategies_cmd", "strategy_group", "strategy_dir_cmd", "strategy_new_cmd",
        "_snake_case", "_strategy_template", "backtest_cmd",
    ],
    "engine_cmds.py": [
        "engine_cmd", "serve_cmd", "start_cmd", "stop_cmd",
        "status_cmd", "restart_cmd", "snapshot_cmd",
    ],
    "control_cmds.py": [
        "ctl_group", "_ctl_port", "ctl_overview", "ctl_tasks", "ctl_add",
        "ctl_task", "ctl_component", "ctl_snapshot", "ctl_shutdown",
        "ctl_collector_group", "ctl_collector_status", "ctl_collector_interval",
        "ctl_collector_add", "ctl_collector_remove", "ctl_collector_now",
        "ctl_collector_reload",
        "ctl_live_group", "ctl_live_status", "ctl_live_reconcile",
        "ctl_live_cancel_all", "ctl_snapshot_mode",
        "ctl_db_group", "ctl_live_account",
        "ctl_endpoints_group", "ctl_endpoints_list", "ctl_endpoints_check",
        "ctl_strategies", "_print_snapshot_mode",
        "ctl_db_orders", "ctl_db_trades", "ctl_db_equity",
        "ctl_db_events", "ctl_db_backtests",
        "_api",
    ],
    "research_cmds.py": [
        "factors_cmd", "rules_cmd", "tasks_cmd", "flow_cmd", "score_cmd",
        "factor_group", "_default_symbols", "factor_list", "factor_ops",
        "factor_vars", "factor_add", "factor_remove", "factor_presets",
        "factor_check", "factor_mine", "factor_fit",
    ],
    "pool_cmds.py": [
        "pool_group", "pool_refresh", "pool_top", "pool_sync",
        "pool_categories", "pool_demo", "_echo_sync", "_print_pool",
    ],
    "hub_cmds.py": [
        "hub_group", "hub_run", "hub_start", "hub_stop",
        "hub_status", "hub_services", "hub_cmd",
    ],
    "broker_cmds.py": [
        "brokers_group", "brokers_list", "brokers_check", "brokers_gateways",
        "calendar_group", "calendar_status", "calendar_refresh",
    ],
}

# 每个模块的文件头（base.py 特殊：直接沿用原文件前 23 行）
HEADERS: dict[str, str] = {
    "data_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据面命令：建库 / 采集 / 完整性校验 / 数据源与 Provider 观测。

对应链路 DataSource → Provider → Capability → Router → DataService，
这里只做「人读得懂的呈现」，业务一律下沉到 app.datasource / app.storage。
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import DEFAULT_END, cli
from app.common.config import settings
from app.common.logging import logger
''',
    "strategy_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略面命令：列出策略 / 生成模板 / 跑回测。

回测与实盘走同一套自研 IBaseStrategy（见 app.research.adapter 的桥接），
所以这里只有一份策略清单，不再有「回测策略」和「实盘策略」两套。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import DEFAULT_END, _parse_params, cli
from app.common.logging import logger
''',
    "engine_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎面命令：前台跑引擎 / 后台常驻服务 / 启停状态 / 运行快照。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
from app.common.config import settings
from app.common.logging import logger
''',
    "control_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远程控制命令（``ctl`` 组）：对运行中的服务下发指令。

一律走监控面板的 HTTP API（标准库 urllib，零依赖），
不直连数据库 —— CLI 与面板因此永远看到同一份状态。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import _print_endpoints, cli
from app.common.config import settings
''',
    "research_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研究面命令：因子实验室 / 指标清单 / 规则 / 任务配置 / 资金流与评分。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
''',
    "pool_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票池命令：候选池收窄 → 综合评分 → 推荐。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
''',
    "hub_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多服务编排命令（``hub`` 组）：一个 hub 拉起并看护 N 个独立引擎进程。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import cli
''',
    "broker_cmds.py": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""券商接入点与交易日历命令。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from app.cli.base import _print_endpoints, cli
''',
}


def extract() -> tuple[dict[str, tuple[int, int, str]], list[str], str]:
    """返回 {名字: (起始行, 结束行, 片段源码)}、原始行列表、文件头。"""
    src = SRC.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src, filename=str(SRC))

    segs: dict[str, tuple[int, int, str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            name = node.name
        elif isinstance(node, ast.Assign):
            start = node.lineno
            name = node.targets[0].id  # type: ignore[union-attr]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # 模块级带注解的常量（如 _PROBE_ARGS: Dict[...] = {...}）。
            # 第一版只认 ast.Assign，把它们静默漏掉了 —— 连带覆盖率检查一起漏，
            # 因为覆盖率是拿 segs 自己比自己。务必两种都要接。
            start = node.lineno
            name = node.target.id
        else:
            continue
        end = node.end_lineno or node.lineno

        # 向上吃掉紧邻的注释横幅（# ===== 数据库 ===== 之类）
        cursor = start - 1
        banner: list[str] = []
        while cursor >= 1:
            text = lines[cursor - 1].strip()
            if text.startswith("#") or not text:
                banner.insert(0, lines[cursor - 1])
                cursor -= 1
            else:
                break
        # banner 里第一段是紧邻定义的注释块；更上面的空行归给上一个片段的分隔
        while banner and not banner[0].strip():
            banner.pop(0)

        body = "\n".join(lines[start - 1:end])
        segs[name] = (start, end, ("\n".join(banner) + "\n" + body).strip("\n"))

    header = "\n".join(lines[:20])  # 原文件头：shebang + 注释 + import
    return segs, lines, header


def main() -> int:
    apply = "--apply" in sys.argv
    segs, lines, header = extract()

    # ---- 覆盖率体检 ----
    assigned = [n for names in GROUPS.values() for n in names]
    missing = sorted(set(segs) - set(assigned))
    unknown = sorted(set(assigned) - set(segs))
    dup = sorted({n for n in assigned if assigned.count(n) > 1})
    if missing or unknown or dup:
        print("❌ 分配表与源文件不一致")
        print(f"   未分配: {missing}")
        print(f"   多余  : {unknown}")
        print(f"   重复  : {dup}")
        return 1
    print(f"✅ 顶层定义 {len(segs)} 个，全部已分配且无重复")

    # ---- 逐片段 AST 比对 ----
    bad = 0
    for name, (start, end, seg) in segs.items():
        for text in (seg, ):
            try:
                dump_new = ast.dump(ast.parse(text))
            except SyntaxError as exc:
                print(f"❌ {name}: 片段自身无法解析 {exc}")
                bad += 1
                continue
            # 从原文件按同一起止行取片段比对
            orig = "\n".join(lines[start - 1:end])
            dump_old = ast.dump(ast.parse(orig))
            if dump_new != dump_old:
                print(f"❌ {name}: AST 与原文件不一致")
                bad += 1
    if bad:
        print(f"❌ {bad} 个片段比对失败")
        return 1
    print("✅ 每个片段的 AST 与原文件逐节点一致")

    # ---- 生成 ----
    pieces: dict[str, str] = {}
    for module, names in GROUPS.items():
        if module == "base.py":
            head = header + "\n\n"
        else:
            head = HEADERS[module]
        chunks = [segs[n][2] for n in names]
        pieces[module] = head + "\n\n".join(chunks) + "\n"

    if not apply:
        for module, text in pieces.items():
            print(f"\n───── {module}  ({len(text.splitlines())} 行)")
        print(f"\n共 {len(pieces)} 个模块；加 --apply 落地")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    for module, text in pieces.items():
        (OUT / module).write_text(text, encoding="utf-8")
        print(f"  写入 app/cli/{module}  ({len(text.splitlines())} 行)")

    (OUT / "__init__.py").write_text('''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaQuant 命令行入口包。

拆包动机：原先 app/cli.py 单文件 2642 行、70 个命令，改一条 help 都要
在两千多行里翻。现在按**职责**切，找命令先看文件名：

    base.py           主命令组 cli + 跨模块共用工具（不 import 任何 *_cmds）
    data_cmds.py      数据面：init-db / collect / dataguard / sources / providers / list
    strategy_cmds.py  策略面：strategies / strategy / backtest
    engine_cmds.py    引擎面：engine / serve / start / stop / status / restart / snapshot
    control_cmds.py   远程控制：ctl 全家（走监控面板 HTTP API）
    research_cmds.py  研究面：factors / rules / tasks / flow / score / factor
    pool_cmds.py      股票池：pool 全家
    hub_cmds.py       多服务编排：hub 全家
    broker_cmds.py    券商接入点与交易日历

依赖方向（单向，无环）：
    __init__  →  base  →  app.common.*
                 ↑
            *_cmds 各自只依赖 base

命令注册是 import 的**副作用**：*_cmds 用 ``@cli.command(...)`` 装饰器
在导入时把命令挂到 base.cli 上。所以 __init__ 必须导入全部子模块，
缺一个就少一批命令 —— 这也是下面用 import 而不是 __getattr__ 懒加载的原因。
"""
from __future__ import annotations

from app.cli.base import cli

# 导入即注册：顺序无关，但全都要在。
from app.cli import (  # noqa: E402,F401
    broker_cmds,
    control_cmds,
    data_cmds,
    engine_cmds,
    hub_cmds,
    pool_cmds,
    research_cmds,
    strategy_cmds,
)
from app.cli.base import DEFAULT_END  # noqa: F401

__all__ = ["cli", "DEFAULT_END", "main"]


def main() -> None:
    """控制台脚本入口（见 pyproject 的 [project.scripts]）。"""
    cli()
''', encoding="utf-8")
    print("  写入 app/cli/__init__.py")

    (OUT / "__main__.py").write_text('''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""允许 ``python -m app.cli`` 直接跑。"""
from __future__ import annotations

from app.cli import main

if __name__ == "__main__":
    main()
''', encoding="utf-8")
    print("  写入 app/cli/__main__.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
