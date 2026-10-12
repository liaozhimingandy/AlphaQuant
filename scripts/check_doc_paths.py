#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验文档里提到的 app 路径是否真实存在。

用法::

    python scripts/check_doc_paths.py            # 人类可读报告
    python scripts/check_doc_paths.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 匹配文档里的 app/**/*.py 或 app.*.* 形式路径
_SLASH_RE = re.compile(r"app/[A-Za-z_][\w/]*(?:\.py)?")
_DOT_RE = re.compile(r"\bapp(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}")

_PY_CACHE = {"__pycache__", ".pytest_tmp"}


def _exists(ref: str) -> bool:
    if ref.startswith("app/"):
        return (ROOT / ref).exists() or (ROOT / (ref + ".py")).exists()
    # 点形式：转成路径试一次目录，再试一次模块文件
    p = ROOT / ref.replace(".", "/")
    return p.with_suffix(".py").exists() or p.is_dir()


def scan(files: list[Path]) -> list[dict]:
    bad: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        seen: set[str] = set()
        for ref in set(_SLASH_RE.findall(text) + _DOT_RE.findall(text)):
            ref = ref.rstrip(".,;:)")
            if ref in seen or not ref:
                continue
            # 跳过明显是叙述性的包层概念（没有对应文件很正常）
            if ref.startswith("app/") and ref.count("/") < 2 and not ref.endswith(".py"):
                continue
            seen.add(ref)
            if any(c in ref for c in _PY_CACHE):
                continue
            if not _exists(ref):
                bad.append({"file": str(f.relative_to(ROOT)),
                            "ref": ref,
                            "line": text[:text.find(ref)].count("\n") + 1})
    return sorted(bad, key=lambda d: (d["file"], d["line"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files = sorted(set(list(ROOT.glob("docs/*.md")) + [ROOT / "README.md"]))
    bad = scan(files)
    if args.json:
        print(json.dumps(bad, ensure_ascii=False, indent=2))
    else:
        print(f"文档共 {len(files)} 份，发现 {len(bad)} 处引用不到的路径：\n")
        cur = None
        for item in bad:
            if item["file"] != cur:
                cur = item["file"]
                print(f"-- {cur}")
            print(f"   L{item['line']:<5} {item['ref']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
