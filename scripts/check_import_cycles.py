#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐个**独立**导入每个模块，揪出"能跑但脆弱"的隐性循环依赖。

原理：正常跑测试时模块按某个顺序加载，环会被掩盖；单独导入时
Python 会明确报 `partially initialized module`。把每个模块放进
独立子进程导入，就能一次性找全。

用法::

    python scripts/check_import_cycles.py
    python scripts/check_import_cycles.py --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _modules() -> list[str]:
    out = []
    for p in sorted(ROOT.glob("app/**/*.py")):
        s = str(p)
        if "__pycache__" in s or ".pytest_tmp" in s:
            continue
        rel = p.relative_to(ROOT).with_suffix("")
        mod = ".".join(rel.parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        # main 块会在 import 期起 reactor，跳过入口脚本
        if any(k in mod for k in ("service_runner", "hub.hub", "hub.worker",
                                  "monitoring.server")):
            continue
        out.append(mod)
    return out


def _try_import(mod: str) -> tuple[str, bool, str]:
    code = f"import {mod}"
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=120)
    if r.returncode == 0:
        return mod, True, ""
    tail = (r.stderr or "").strip().splitlines()
    msg = ""
    for line in reversed(tail):
        line = line.strip()
        if line.startswith(("ImportError", "ModuleNotFoundError",
                            "AttributeError", "RuntimeError", "SystemError")):
            msg = line
            break
    if not msg and tail:
        msg = tail[-1]
    return mod, False, msg[:160]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    mods = _modules()
    bad: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for mod, ok, msg in ex.map(_try_import, mods):
            if not ok:
                bad.append({"module": mod, "error": msg})

    if args.json:
        print(json.dumps(bad, ensure_ascii=False, indent=2))
    else:
        print(f"独立导入 {len(mods)} 个模块，失败 {len(bad)} 个：\n")
        for item in bad:
            print(f"  {item['module']}")
            print(f"      {item['error']}")
        if not bad:
            print("  （无）所有模块都能被独立导入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
