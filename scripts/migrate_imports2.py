#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段二补充：处理第一轮没能替换的``from app.X import Y``与注释/字符串里的模块路径。

第一轮用的是「精确查表」。剩下这几类它管不了：
  1. ``from app.data import net`` —— MAP 里存的是 ``app.data.net``，中间隔着 import 关键字；
  2. 注释 / docstring / mock.patch("app.core.pool.sync_fund_flow") 里的「模块.属性」长串。

本轮用「逐级缩短前缀」算法：从完整串开始逐段回退，首个命中的前缀即映射点。

用法：python migrate_imports2.py [--apply]
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from migrate_imports import MAP  # noqa: E402

_PATTERN = re.compile(r"\bapp(?:\.[A-Za-z_][A-Za-z_0-9]*)+")
# from app.x.y import a, b as c
_FROM_IMPORT = re.compile(
    r"^(\s*)(from\s+)(app(?:\.[A-Za-z_][A-Za-z_0-9]*)+)(\s+import\s+)(.+)$"
)
_PLAIN_IMPORT = re.compile(r"^(\s*)(import\s+)(app(?:\.[A-Za-z_][A-Za-z_0-9]*)+)(\s+as\s+\w+)?\s*$")

SKIP_DIRS = {"__pycache__"}


def resolve(old: str) -> str | None:
    """逐级缩短前缀查表。返回 None 表示这条路径不在迁移范围内。"""
    if old in MAP:
        return MAP[old]
    parts = old.split(".")
    for i in range(len(parts) - 1, 1, -1):
        prefix = ".".join(parts[:i])
        if prefix in MAP:
            return MAP[prefix] + "." + ".".join(parts[i:])
    return None


def rewrite_line(line: str, hits: dict[str, int]) -> str:
    m = _FROM_IMPORT.match(line.rstrip("\n"))
    if m:
        indent, kw, module, imp, names = m.groups()
        target: str | None = None
        if module in MAP:
            target = MAP[module]
        else:
            # from app.data import net  ->  MAP 里有 app.data.net
            first = names.split(",")[0].strip().split()[0]
            probe = f"{module}.{first}"
            if probe in MAP:
                target = MAP[probe]
            elif module in MAP:
                target = MAP[module]
        if target is None:
            target = resolve(module)
        if target and target != module:
            hits[module] = hits.get(module, 0) + 1
            if target.count(".") >= 1 and "." in module:
                # 拆成 from 父包 import 子模块 的形式
                parent, _, child = target.rpartition(".")
                if "." in target:
                    return f"{indent}{kw}{parent}{imp}{child}\n"
            return f"{indent}{kw}{target}{imp}{names}\n"
        return line

    m = _PLAIN_IMPORT.match(line.rstrip("\n"))
    if m:
        indent, kw, module, alias = m.groups()
        new = MAP.get(module) or resolve(module)
        if new and new != module:
            hits[module] = hits.get(module, 0) + 1
            return f"{indent}{kw}{new}{alias or ''}\n"
        return line

    # 普通行：注释 / docstring / mock.patch 字符串
    out: list[str] = []
    last = 0
    for m in _PATTERN.finditer(line):
        old = m.group(0)
        new = MAP.get(old) or resolve(old)
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
    targets: list[str] = []
    for base in (APP, os.path.join(ROOT, "scripts")):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith((".py", ".md")):
                    targets.append(os.path.join(dirpath, fn))
    for extra in ("main.py", "main.pyw", "README.md"):
        p = os.path.join(ROOT, extra)
        if os.path.exists(p):
            targets.append(p)

    files = 0
    agg: dict[str, int] = {}
    total = 0
    for path in sorted(targets):
        try:
            lines = open(path, encoding="utf-8").readlines()
        except Exception:
            continue
        hits: dict[str, int] = {}
        new_lines = [rewrite_line(ln, hits) for ln in lines]
        if not hits:
            continue
        files += 1
        for k, v in hits.items():
            agg[k] = agg.get(k, 0) + v
            total += v
        if apply:
            open(path, "w", encoding="utf-8", newline="\n").writelines(new_lines)

    print(f"改写 {files} 个文件，{total} 处\n明细：")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:45]:
        new = MAP.get(k) or resolve(k)
        print(f"  {v:>3}  {k}  ->  {new}")
    if not apply:
        print("\n（dry-run，加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
