#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段二驱动力：从 git 的 rename 记录推导 import 路径映射。

为什么不手写映射表：上一版 migrate_imports.py 自己也是 .py，被自己的替换规则扫到，
导致 MAP 的键从「旧路径」变成了「新路径」从而失效。git 已经忠实记录了每一个
``git mv``，从 rename 记录反推才是唯一可靠的事实来源。

产出 / 直接改写：
  app/<a>/<b>/<c>.py   ->  app/<x>/<y>/<z>.py
  意味着  app.a.b.c     ->  app.x.y.z

用法：python gen_map_from_git.py [--apply] [--self]
  --apply 真正改写源码；不加则只打印映射
  --self  连同 scripts/ 与 docs 一起改写（默认只动 app/ 与入口）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

_PATTERN = re.compile(r"\bapp(?:\.[A-Za-z_][A-Za-z_0-9]*)+")
_FROM_IMPORT = re.compile(
    r"^(\s*)(from\s+)(app(?:\.[A-Za-z_][A-Za-z_0-9]*)+)(\s+import\s+)(.+)$")
_PLAIN_IMPORT = re.compile(
    r"^(\s*)(import\s+)(app(?:\.[A-Za-z_][A-Za-z_0-9]*)+)(\s+as\s+\w+)?\s*$")
SKIP_DIRS = {"__pycache__"}


def _git_out(*args: str) -> str:
    r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)
    return r.stdout


def _to_module(py_path: str) -> str:
    """app/utils/logger.py -> app.utils.logger ; __init__.py -> 包路径"""
    rel = py_path.replace("\\", "/")
    if not rel.startswith("app/"):
        return ""
    mod = rel[:-3] if rel.endswith(".py") else rel
    if mod.endswith("/__init__"):
        mod = mod[: -len("/__init__")]
    elif mod.endswith("__init__"):
        mod = mod[: -len("__init__")]
    return mod.replace("/", ".")


def build_map() -> dict[str, str]:
    status = _git_out("diff", "--cached", "-M", "--name-status", "HEAD")
    status += _git_out("diff", "-M", "--name-status")
    mp: dict[str, str] = {}
    for line in status.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        tag = parts[0][0]
        if tag not in ("R", "C") or len(parts) < 3:
            continue
        old, new = parts[1], parts[2]
        if not (old.startswith("app/") and new.startswith("app/")):
            continue
        if not (old.endswith(".py") and new.endswith(".py")):
            continue
        om, nm = _to_module(old), _to_module(new)
        if om and nm and om != nm:
            mp[om] = nm
    return mp


def resolve(mp: dict[str, str], old: str) -> str | None:
    """精确命中优先，否则逐级缩短前缀（处理 ``模块.属性`` 的长串）。"""
    if old in mp:
        return mp[old]
    parts = old.split(".")
    best: tuple[int, str] | None = None
    for i in range(len(parts) - 1, 1, -1):
        prefix = ".".join(parts[:i])
        if prefix in mp:
            cand = mp[prefix] + "." + ".".join(parts[i:])
            # 取最长前缀命中（最精确）
            if best is None or i > best[0]:
                best = (i, cand)
            break
    return best[1] if best else None


def rewrite_line(mp: dict[str, str], line: str, hits: dict[str, int]) -> str:
    m = _FROM_IMPORT.match(line.rstrip("\r\n"))
    if m:
        indent, kw, module, imp, names = m.groups()
        target = None
        if module in mp:
            target = mp[module]
        else:
            first = names.split(",")[0].strip().split()[0]
            if f"{module}.{first}" in mp:
                target = mp[f"{module}.{first}"]
            else:
                target = resolve(mp, module)
        if target and target != module:
            hits[module] = hits.get(module, 0) + 1
            # from app.data import net  ->  from app.providers.market.transport import net
            if target.count(".") >= 1 and not names.startswith("*"):
                parent, _, child = target.rpartition(".")
                if parent:
                    return f"{indent}{kw}{parent}{imp}{child}\n"
            return f"{indent}{kw}{target}{imp}{names}\n"
        return line

    m = _PLAIN_IMPORT.match(line.rstrip("\r\n"))
    if m:
        indent, kw, module, alias = m.groups()
        new = mp.get(module) or resolve(mp, module)
        if new and new != module:
            hits[module] = hits.get(module, 0) + 1
            return f"{indent}{kw}{new}{alias or ''}\n"
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
    mp = build_map()
    print(f"从 git rename 推导出 {len(mp)} 条映射\n")

    if "--self" in sys.argv:
        roots = [APP, os.path.join(ROOT, "scripts"), os.path.join(ROOT, "docs")]
    else:
        roots = [APP, os.path.join(ROOT, "scripts")]
    targets: list[str] = []
    for base in roots:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith((".py", ".md")):
                    targets.append(os.path.join(dirpath, fn))
    for extra in ("main.py", "main.pyw", "README.md"):
        p = os.path.join(ROOT, extra)
        if os.path.exists(p):
            targets.append(p)

    # 本脚本自身的映射表不能被改写
    self_path = os.path.abspath(__file__)

    files, total = 0, 0
    agg: dict[str, int] = {}
    for path in sorted(targets):
        if os.path.abspath(path) == self_path:
            continue
        if os.path.basename(path) == "gen_map_from_git.py":
            continue
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

    print(f"改写 {files} 个文件，{total} 处。明细 TOP40：")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:40]:
        print(f"  {v:>3}  {k}  ->  {mp.get(k) or resolve(mp, k)}")
    if not apply:
        print("\n（dry-run，加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
