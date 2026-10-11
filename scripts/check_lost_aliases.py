#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量改写 import 会静默丢掉 ``as`` 别名 —— 这是本次重构最阴的一类 bug。

症状：``from app.data import datasource as ds`` 被改写成
``from app.domain import symbol``，函数体里的 ``ds.xxx`` 全部变成 NameError，
而模块本身照样能 import 成功，只有跑到那一行才炸。

本脚本拿**暂存区**（第 12 轮 git add 时、改写前的快照）与**工作区**逐文件比对，
列出「暂存区有别名、工作区丢了」的 import，一次扫干净。

用法：python scripts/check_lost_aliases.py
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def _import_bindings(src: str, path: str) -> dict[str, str]:
    """{本地名字: import 原文}"""
    out: dict[str, str] = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                local = a.asname or a.name.split(".")[0]
                out[local] = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for a in node.names:
                local = a.asname or a.name
                out[local] = (f"from {mod} import {a.name}"
                              + (f" as {a.asname}" if a.asname else ""))
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    # 有暂存版本的文件：改写前快照存在
    listed = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
    ).stdout.splitlines()

    bad = 0
    for rel in listed:
        if not rel.endswith(".py"):
            continue
        old_src = subprocess.run(
            ["git", "show", f":{rel}"], cwd=root,
            capture_output=True, text=True, encoding="utf-8",
        ).stdout
        new_file = root / rel
        if not old_src.strip() or not new_file.exists():
            continue
        new_src = new_file.read_text(encoding="utf-8")

        old_b = _import_bindings(old_src, rel)
        new_b = _import_bindings(new_src, rel)

        # 旧有别名（asname 存在）但新的要么没了要么不叫这个名字
        for local, stmt in old_b.items():
            if " as " not in stmt:
                continue
            if local not in new_b:
                bad += 1
                print(f"\n── {rel}")
                print(f"   丢了别名 {local!r}")
                print(f"   改写前: {stmt}")
                # 找新文件里对应的那行
                for line in new_src.splitlines():
                    if line.strip().startswith(("from ", "import ")) and (
                        stmt.split(" import ")[0].replace("from ", "") in line
                    ):
                        print(f"   改写后: {line.strip()}")
                        break
    print(f"\n合计 {bad} 处丢失的 import 别名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
