#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 1871 行的 app/monitoring/server.py 拆开：HTTP 传输层 + 组件核心 + 业务视图 mixin。

为什么用 mixin 而不是「函数 + 传参」：
    视图方法是 ``self._xxx`` 重度用户（快照、事件总线、行情中心…）。
    改成自由函数就得把整个组件对象传进去，等于把 40 多个方法全部改写一遍，
    风险与工作量都不可控。用 mixin 让方法留在类里，``self`` 仍是同一个对象
    —— **纯代码搬移，零行为变更**，可以用 AST 逐方法比对来证明。

产物：
    monitoring/resource.py    MonitorResource（HTTP 传输层，516 行）
    monitoring/api/*.py       按业务域划分的视图 mixin
    monitoring/server.py      组件核心（生命周期/快照/事件）+ mixin 组装 + 再导出

对外接口不变：``from app.monitoring.server import MonitorWebComponent`` 仍然可用。

用法：python scripts/split_monitoring.py [--apply]
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "monitoring" / "server.py"
RES = ROOT / "app" / "monitoring" / "resource.py"
API = ROOT / "app" / "monitoring" / "api"

BUILTINS = set(dir(builtins))

# ---------------------------------------------------------------- 分配表
# 组件核心：生命周期 + 快照落盘 + 事件订阅。留在 server.py。
CORE = [
    "__init__", "on_initialize", "on_start", "on_stop", "is_busy",
    "_make_trigger", "mark_dirty", "_schedule_flush", "_do_event_flush",
    "_snapshot_ready", "_write_snapshot", "_tick_snapshot", "_after_write",
    "flush", "set_snapshot_mode", "_extra",
    "_on_order", "_on_rejected", "_on_control",
    "run_id", "control", "snapshot",
]

# 业务视图 -> (目标文件, mixin 类名, 方法列表)
VIEWS: list[tuple[str, str, list[str]]] = [
    ("overview.py", "OverviewViewsMixin", [
        "build_overview", "order_feed", "build_live_overview",
        "build_capabilities", "build_config_view", "stats_dict", "log_tail",
    ]),
    ("live.py", "LiveViewsMixin", [
        "reconcile_now", "refresh_live_account", "cancel_all_orders",
    ]),
    ("brokers.py", "BrokerViewsMixin", [
        "endpoints_view", "check_endpoint",
    ]),
    ("factors.py", "FactorViewsMixin", [
        "factors_view", "factor_validate", "factor_add", "factor_remove",
        "factor_check", "factor_mine", "_factor_symbols",
    ]),
    ("pool.py", "PoolViewsMixin", [
        "pool_config_view", "pool_top", "pool_dates",
        "refresh_pool", "stock_score",
    ]),
    ("collect.py", "CollectViewsMixin", [
        "collect_now", "schedule_view", "schedule_save", "calendar_view",
    ]),
    ("strategies.py", "StrategyViewsMixin", [
        "strategies_view", "strategy_save", "strategy_remove",
        "strategy_deploy", "reload_strategies",
    ]),
    ("backtest.py", "BacktestViewsMixin", [
        "submit_backtest", "_run_backtest_in_thread",
    ]),
]


#: 从 git 暂存区恢复出来的 server.py 是**旧快照**——"dashboard.py 抽成静态 HTML"
#: 那次改动当时没有 git add，索引里还是 import 一个早已不存在的模块。
#: 每次重新拆分都会把它带回来，所以在这里显式纠偏，避免修一次、退一次。
SOURCE_FIXES = [
    ("from app.monitoring.dashboard_html import DASHBOARD_HTML",
     "from app.monitoring.dashboard import dashboard_html"),
    ("self._text(request, DASHBOARD_HTML,",
     "self._text(request, dashboard_html(),"),
    # 仓库模块改名时漏掉的两处（cli.py 里也有一处，同样是这个原因）。
    # 它在 try/except 里，失败会静默返回空列表 —— 面板股票池一直空白却不报错。
    ("from app.data.repo import marketx_repository as R",
     "from app.data.repo import kline_repo as R"),
]


def load():
    src = SRC.read_text(encoding="utf-8")
    for old, new in SOURCE_FIXES:
        if old in src:
            src = src.replace(old, new)
            print(f"   ⚙ 纠正索引版遗留引用: {old.strip()[:60]}")
    lines = src.splitlines()
    tree = ast.parse(src, filename=str(SRC))
    return src, lines, tree


def method_segments(lines, cls: ast.ClassDef) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    """返回 ({方法名: 源码片段}, {方法名: (起始行, 结束行)})，含装饰器与上方注释。"""
    out: dict[str, str] = {}
    rng: dict[str, tuple[int, int]] = {}
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = min([d.lineno for d in m.decorator_list] + [m.lineno])
        end = m.end_lineno or m.lineno
        # 向上吃掉紧邻的注释行
        cursor = start - 1
        banner: list[str] = []
        while cursor >= 1:
            t = lines[cursor - 1].strip()
            if t.startswith("#") or not t:
                banner.insert(0, lines[cursor - 1])
                cursor -= 1
            else:
                break
        while banner and not banner[0].strip():
            banner.pop(0)
        body = "\n".join(lines[start - 1:end])
        out[m.name] = ("\n".join(banner) + "\n" + body).strip("\n")
        rng[m.name] = (start, end)
    return out, rng


def class_attrs(cls: ast.ClassDef, lines: list[str]) -> list[str]:
    """类体里的**非方法语句**（常量、开关、事件表…），连同上方注释一起取出。

    只切方法会静默丢掉这些类级属性：方法搬走了，``SNAPSHOT_MODES`` /
    ``TRIGGER_EVENTS`` / ``name`` 却留在原地没人认领，
    于是运行时报 ``AttributeError: type object has no attribute``。
    """
    out: list[str] = []
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            continue          # docstring 由新文件的文档字符串承担
        start = n.lineno
        end = n.end_lineno or n.lineno
        cursor = start - 1
        banner: list[str] = []
        while cursor >= 1:
            t = lines[cursor - 1].strip()
            if t.startswith("#") or not t:
                banner.insert(0, lines[cursor - 1])
                cursor -= 1
            else:
                break
        while banner and not banner[0].strip():
            banner.pop(0)
        seg = ("\n".join(banner) + "\n" + "\n".join(lines[start - 1:end])).strip("\n")
        out.append(seg)
    return out


def parse_as_class(segs: list[str]) -> ast.AST:
    """方法片段带缩进，单独 parse 会 IndentationError —— 套一个临时类再解析。"""
    return ast.parse("class _W:\n" + indent_block(segs))


def bound_names(tree: ast.AST) -> set[str]:
    """片段内部绑定的名字（参数/局部变量/导入）"""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.ClassDef):
            # 片段里可能嵌着局部类（如 ApiRow），它没有 args
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            for a in (*node.args.posonlyargs, *node.args.args,
                      *node.args.kwonlyargs, node.args.vararg, node.args.kwarg):
                if a is not None:
                    bound.add(a.arg)
        elif isinstance(node, ast.Lambda):
            for a in (*node.args.posonlyargs, *node.args.args,
                      *node.args.kwonlyargs, node.args.vararg, node.args.kwarg):
                if a is not None:
                    bound.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for g in node.generators:
                for n in ast.walk(g.target):
                    if isinstance(n, ast.Name):
                        bound.add(n.id)
    return bound


def used_names(tree: ast.AST) -> set[str]:
    """片段里**裸名**用到的名字（排除 self.xxx 的属性基座）"""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    return used


def import_table(tree: ast.AST, src_lines: list[str]) -> dict[str, str]:
    """server.py 顶层 import 提供的 {本地名: 整行}

    src_lines 必须是**纠偏之后**的文本。早先这里直接读磁盘上的 SRC，
    于是 SOURCE_FIXES 改掉了源码、import 表却还是旧的，生成的模块
    一边 import 老模块、一边调用新函数。
    """
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                table[a.asname or a.name.split(".")[0]] = \
                    src_lines[node.lineno - 1].strip()
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                table[a.asname or a.name] = src_lines[node.lineno - 1].strip()
    return table


def indent_block(segs: list[str]) -> str:
    """把若干方法片段拼成一个类体（片段之间空一行）。

    注意：**不要再额外缩进**。从 server.py 里切出来的片段本身就是类成员，
    已经带 4 格缩进；再压 4 格会变成 8 格，而且多行字符串（docstring）的
    内容会被真的改写 —— 那样 AST 比对必然不一致，产物也是错的。
    """
    return "\n\n".join(segs) + "\n"


#: 原文件里**用到了却没有顶层导入**的名字。自动推导无法解析，在此显式补齐。
#: json_safe 就是这么翻出来的：build_live_overview 里调用了它，而 server.py
#: 的 import 表只有 dumps / tail_lines —— 也就是说这个 NameError 早就潜伏着，
#: 只差一条会走到那行分支的调用链。
EXTRA_IMPORTS: dict[str, list[str]] = {
    "overview.py": ["from app.common.jsonio import json_safe"],
}


def make_header(needed: set[str], table: dict[str, str], doc: str,
                fname: str = "") -> str:
    unresolved = sorted(n for n in needed
                        if n not in table
                        and n not in {s.split()[-1] for s in EXTRA_IMPORTS.get(fname, [])})
    needed = needed - {s.split()[-1] for s in EXTRA_IMPORTS.get(fname, [])}
    if unresolved:
        # 这些名字在原 server.py 里也**没有顶层导入**。多数是方法内的局部 import，
        # 但也有真 bug（如 json_safe 被用却从未导入）。宁可报出来人工确认。
        print(f"   ⚠ 无法从 import 表解析，需人工确认: {unresolved}")
    stmts = sorted({table[n] for n in needed if n in table})
    stdlib, third, first = [], [], []
    for s in stmts:
        if s.startswith("from app."):
            first.append(s)
        elif s.startswith(("import twisted", "from twisted")):
            third.append(s)
        else:
            stdlib.append(s)
    parts = [f'#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"""{doc}"""\nfrom __future__ import annotations\n']
    for block in (stdlib, third, first):
        if block:
            parts.append("\n".join(block))
    extra = EXTRA_IMPORTS.get(fname) or []
    if extra:
        parts.append("# 原文件缺失、按实际引用补齐：\n" + "\n".join(extra))
    return "\n\n".join(p for p in parts if p) + "\n\n"


def main() -> int:
    apply = "--apply" in sys.argv
    src, lines, tree = load()

    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    if set(classes) != {"MonitorResource", "MonitorWebComponent"}:
        print(f"❌ 预期两个类，实际: {sorted(classes)}")
        return 1
    table = import_table(tree, lines)

    res_cls = classes["MonitorResource"]
    comp_cls = classes["MonitorWebComponent"]
    comp_methods, comp_rng = method_segments(lines, comp_cls)

    # 原文件里每个方法的 AST，按 (类名, 方法名) 索引
    orig_nodes: dict[tuple[str, str], str] = {}
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            for m in cls.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    orig_nodes[(cls.name, m.name)] = ast.dump(m)

    # ---- 覆盖率体检 ----
    assigned = CORE + [m for _, _, ms in VIEWS for m in ms]
    missing = sorted(set(comp_methods) - set(assigned))
    unknown = sorted(set(assigned) - set(comp_methods))
    dup = sorted({m for m in assigned if assigned.count(m) > 1})
    if missing or unknown or dup:
        print("❌ 分配表与源文件不一致")
        print(f"   未分配: {missing}\n   多余  : {unknown}\n   重复  : {dup}")
        return 1
    print(f"✅ MonitorWebComponent 的 {len(comp_methods)} 个方法全部已分配且无重复")

    # ---- 生成各 mixin ----
    docs = {
        "overview.py": "总览视图：引擎/任务/能力清单/配置/统计/日志尾部。",
        "live.py": "实盘视图：对账、账户刷新、全撤。",
        "brokers.py": "券商接入点视图：列出与连通性自检。",
        "factors.py": "因子实验室视图：列表/校验/增删/检验/挖掘。",
        "pool.py": "股票池视图：配置/取票/日期列表/刷新/个股评分。",
        "collect.py": "采集编排视图：立即补采/计划查看与保存/交易日历。",
        "strategies.py": "策略视图：列表/保存/删除/部署/重新扫描。",
        "backtest.py": "回测视图：提交作业与后台执行（追加到既有 backtest.py）。",
    }

    pieces: dict[str, str] = {}
    bodies: dict[str, str] = {}   # 只有类体，用于往已有文件里追加
    for fname, mixin, methods in VIEWS:
        segs = [comp_methods[m] for m in methods]
        joined = "\n\n".join(segs)
        tree_v = parse_as_class(segs)
        needed = used_names(tree_v) - bound_names(tree_v) - BUILTINS - {"self"}
        # 视图方法会互相调用，也在别的 mixin 里 —— 运行时由 self 解析，无需 import
        needed -= set(assigned)
        needed -= {"MonitorWebComponent", "MonitorResource"}
        body = f"class {mixin}:\n" + indent_block(segs)
        head = make_header(needed, table, docs[fname], fname)
        pieces[fname] = head + "\n" + body
        bodies[fname] = body

    # ---- 生成 resource.py ----
    res_lines = lines[res_cls.lineno - 1:res_cls.end_lineno]
    res_src = "\n".join(res_lines)
    tree_r = ast.parse(res_src)          # 整个顶层类，直接 parse
    needed_r = used_names(tree_r) - bound_names(tree_r) - BUILTINS - {"self"}
    needed_r -= {"MonitorWebComponent"}
    pieces["__resource__"] = make_header(
        needed_r, table,
        "监控面板的 HTTP 传输层。\n\n"
        "只负责「请求进来 → 找到处理函数 → 结果序列化」：\n"
        "URL 分段、读 body、JSON/HTML 响应、把 Deferred 接到 Twisted 的 request 上。\n"
        "**不含任何业务判断** —— 业务全在 MonitorWebComponent 及其视图 mixin 里。\n\n"
        "依赖方向：本模块 -> app.monitoring.api.* / app.monitoring.server（仅类型注解）"
    ) + "\n" + res_src + "\n"

    # ---- 生成新的 server.py ----
    comp_attrs = class_attrs(comp_cls, lines)
    core_segs = comp_attrs + [comp_methods[m] for m in CORE]
    core_joined = "\n\n".join(core_segs)
    tree_c = parse_as_class(core_segs)
    needed_c = used_names(tree_c) - bound_names(tree_c) - BUILTINS - {"self"}
    # 类基线（MonitorWebComponent(..., IBaseComponent)）是脚本生成的，
    # 不在任何方法体里，自动推导抓不到 —— 显式补上。
    needed_c.add("IBaseComponent")
    needed_c -= set(assigned)
    needed_c -= {"MonitorResource"}   # 改为从 resource 再导出

    mixin_imports = "\n".join(
        f"from app.monitoring.api.{f[:-3]} import {mx}" for f, mx, _ in VIEWS
    )
    bases = ", ".join([mx for _, mx, _ in VIEWS] + ["IBaseComponent"])
    core_body = f"class MonitorWebComponent({bases}):\n" + indent_block(core_segs)

    server_doc = (
        "内嵌网页监控组件：在引擎自己的 reactor 上挂一个 Twisted Web Site。\n\n"
        "直接读引擎内存状态（零拷贝延迟），控制指令即时生效，零额外进程；\n"
        "通过 /api/* 暴露 JSON，既给面板用，也给 CLI / hub 用。\n\n"
        "本文件原本 1871 行——一个组件类里塞了生命周期、快照落盘、事件订阅\n"
        "和 40 多个业务视图方法，改一条「因子列表」要翻一千行。现在：\n\n"
        "    server.py       组件核心（生命周期 / 快照 / 事件）—— 本文件\n"
        "    resource.py     MonitorResource：HTTP 传输层\n"
        "    api/*.py        按业务域划分的视图 mixin（overview/live/brokers/\n"
        "                    factors/pool/collect/strategies/backtest）\n\n"
        "视图用** mixin **而非自由函数：方法体重度依赖 self._xxx，改成传参\n"
        "等于把 40 多个方法全改一遍。mixin 保持 self 语义，是纯代码搬移。\n\n"
        "对外接口未变：from app.monitoring.server import MonitorWebComponent / MonitorResource"
    )
    head_s = make_header(needed_c, table, server_doc)
    pieces["__server__"] = (
        head_s
        + mixin_imports + "\n"
        + "from app.monitoring.resource import MonitorResource  # noqa: F401  对外再导出\n\n"
        + "# MonitorWebComponent 里方法被调用的顺序（MRO）：先各视图 mixin，最后 IBaseComponent。\n"
        + core_body
    )

    # ---- 逐方法 AST 比对：证明搬移过程没改写一个字符 ----
    # 生成物是完整模块（方法带缩进），可以直接 parse；
    # 拿它里面的类/方法与原文件对应节点比 dump。
    # (产物键, 生成物里的类名, 方法列表, **原文件里的类名**)
    # mixin 类名是新造的，比对时要用它在原文件里的来源类去查。
    expect = [(fname, mixin, ms, "MonitorWebComponent")
              for fname, mixin, ms in VIEWS]
    expect.append(("__resource__", "MonitorResource",
                   [m.name for m in res_cls.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))],
                   "MonitorResource"))
    expect.append(("__server__", "MonitorWebComponent", CORE, "MonitorWebComponent"))

    checked = 0
    for key, cls_name, methods, orig_cls in expect:
        try:
            gen = ast.parse(pieces[key])
        except SyntaxError as exc:
            print(f"❌ {key}: 生成物无法解析 -> {exc}")
            return 1
        gen_cls = next((c for c in gen.body
                        if isinstance(c, ast.ClassDef) and c.name == cls_name), None)
        if gen_cls is None:
            print(f"❌ {key}: 生成物里找不到类 {cls_name}")
            return 1
        gen_methods = {m.name: m for m in gen_cls.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
        gen_methods["__init__"] = gen_methods.get("__init__")  # 保持键存在
        for m in methods:
            if m not in gen_methods or gen_methods[m] is None:
                print(f"❌ {key}.{cls_name}: 缺方法 {m}")
                return 1
            got = ast.dump(gen_methods[m])
            want = orig_nodes.get((orig_cls, m))
            if want is None:
                print(f"❌ 原文件里没有 {orig_cls}.{m}")
                return 1
            if got != want:
                print(f"❌ {orig_cls}.{m}: AST 与原文件不一致")
                return 1
            checked += 1
    print(f"✅ {checked} 个方法的 AST 与原文件逐节点一致")

    if not apply:
        for k, v in pieces.items():
            print(f"───── {k}  ({len(v.splitlines())} 行)")
        print(f"\n共 {len(pieces)} 个产物；加 --apply 落地")
        return 0

    # ---- 落地 ----
    API.mkdir(parents=True, exist_ok=True)
    RES.write_text(pieces["__resource__"], encoding="utf-8")
    print(f"  写入 app/monitoring/resource.py ({len(pieces['__resource__'].splitlines())} 行)")
    for fname, _, _ in VIEWS:
        target = API / fname
        if fname == "backtest.py" and target.exists():
            # 已有文件里追加：只加类体，绝不能再塞一份 __future__ 头。
            # 且必须**幂等** —— 反复 apply 不能把同一个 mixin 叠好几份（真踩过：
            # 连跑 4 次，文件里出现 4 个 class BacktestViewsMixin）。
            old = target.read_text(encoding="utf-8")
            begin = "# --- BEGIN 自动追加（scripts/split_monitoring.py）---"
            end = "# --- END 自动追加 ---"
            if begin in old:
                old = old[:old.index(begin)].rstrip()
            block = f"\n\n\n{begin}\n{bodies[fname]}{end}\n"
            target.write_text(old.rstrip() + block, encoding="utf-8")
            print(f"  追加 app/monitoring/api/{fname}（仅类体，幂等）")
        else:
            target.write_text(pieces[fname], encoding="utf-8")
            print(f"  写入 app/monitoring/api/{fname} ({len(pieces[fname].splitlines())} 行)")
    SRC.write_text(pieces["__server__"], encoding="utf-8")
    print(f"  重写 app/monitoring/server.py ({len(pieces['__server__'].splitlines())} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
