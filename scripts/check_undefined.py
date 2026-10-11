#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迁移体检：找出被 import 改写脚本弄丢的名字。

背景：批量改写 import 时，若同一个包被拆成多行 import
（``from pkg.comp import A`` / ``from pkg.comps import B``），
两行会落到同一个目标路径，容易丢掉其中一个名字；
``from pkg import mod as MX`` 在被拆成 ``from parent import child`` 时也可能丢 asname。

本脚本做一次简化版的作用域分析，报告每个文件里「用到了但没有任何定义来源」的名字。

用法：python check_undefined.py [目录]
"""
from __future__ import annotations

import ast
import builtins
import os
import sys

BUILTINS = set(dir(builtins))

# 收集绑定的名字
def _bind_pattern(node: ast.AST, out: set[str]) -> None:
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            _bind_pattern(e, out)
    elif isinstance(node, ast.Starred):
        _bind_pattern(node.value, out)
    elif isinstance(node, ast.MatchAs) and node.name:
        out.add(node.name)
    elif isinstance(node, ast.MatchStar) and node.name:
        out.add(node.name)
    elif isinstance(node, ast.MatchMapping) and node.rest:
        out.add(node.rest)


def collect_bindings(body: list[ast.stmt]) -> set[str]:
    """收集一个代码块（含嵌套）里所有被绑定的名字，粗略但够用。"""
    bound: set[str] = set()
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.Lambda):
            # lambda 的参数也是绑定。漏了它就会把 lambda x: ... / lambda g, tot: ...
            # 里的参数误报成「未定义」，逼人每次肉眼甄别。
            args = node.args
            for a in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                      args.vararg, args.kwarg):
                if a is not None:
                    bound.add(a.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                          args.vararg, args.kwarg):
                    if a is not None:
                        bound.add(a.arg)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _bind_pattern(t, bound)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            _bind_pattern(node.target, bound)
        elif isinstance(node, ast.For):
            _bind_pattern(node.target, bound)
        elif isinstance(node, ast.AsyncFor):
            _bind_pattern(node.target, bound)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _bind_pattern(item.optional_vars, bound)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp,
                               ast.DictComp)):
            for g in node.generators:
                _bind_pattern(g.target, bound)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                _bind_pattern(t, bound)
    return bound


def check_file(path: str) -> list[tuple[str, int, str, str]]:
    """返回 [(文件, 行号, 名字, 使用场景)]"""
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        return [(path, exc.lineno or 0, "<SyntaxError>", str(exc))]

    module = collect_bindings(tree.body)
    lines = src.splitlines()

    problems: list[tuple[str, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            if name in module or name in BUILTINS:
                continue
            if name.startswith("__") and name.endswith("__"):
                continue
            line = lines[node.lineno - 1].strip()[:90] if node.lineno <= len(lines) else ""
            problems.append((path, node.lineno, name, line))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            # MX.xxx 这种：value 是裸名
            name = node.value.id
            if name in module or name in BUILTINS:
                continue
            line = lines[node.lineno - 1].strip()[:90] if node.lineno <= len(lines) else ""
            problems.append((path, node.lineno, name, line))
    # 去重：同名只报第一次 + 计数
    seen: dict[str, tuple[str, int, str, str, int]] = {}
    for p, ln, n, ctx in problems:
        if n in seen:
            seen[n] = (*seen[n][:4], seen[n][4] + 1)
        else:
            seen[n] = (p, ln, n, ctx, 1)
    return [(p, ln, f"{n}  x{cnt}" if cnt > 1 else n, ctx)
            for p, ln, n, ctx, cnt in seen.values()]


def main() -> int:
    roots = sys.argv[1:] or ["app"]
    total = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                probs = check_file(path)
                if not probs:
                    continue
                total += len(probs)
                print(f"\n── {path}")
                for p, ln, name, ctx in probs[:15]:
                    print(f"   L{ln:<5} {name:<28} {ctx}")
    print(f"\n合计 {total} 处可疑的未定义引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
