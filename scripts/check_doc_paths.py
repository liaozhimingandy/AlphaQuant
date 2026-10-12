#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验文档里提到的 app 路径是否真实存在。

用法::

    python scripts/check_doc_paths.py            # 人类可读报告
    python scripts/check_doc_paths.py --json     # 机器可读

有意保留的失效引用（历史对照表、示例占位名）用下面两种方式豁免，
不要用「全局静默」糊过去——报告一噪就没人看了，一静默就没人信了：

* 示例占位名（``mysource.py`` 这类"你自己建一个"的文件名）由正则自动豁免
* 历史对照表在文档里打 ``<!-- check_doc_paths:allow-stale-paths -->`` 标记，
  标记之后到下一个二级标题之间的引用全部豁免

已知代价：对照表里"新路径"那一列如果写错，也会被一起豁免。
换来的好处是报告能干净到当门禁用——**一条噪音都没有时，剩下每条都值得看**。
真要收紧可以再要求"每行最后一个路径必须存在"，但那要理解表格语义，暂不做。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 匹配文档里的 app/**/*.<ext> 或 app.*.* 形式路径。
# 扩展名必须显式列出来：只写 (?:\.py)? 会把 ``dashboard.html`` 砍成
# ``dashboard`` 再报"路径不存在"——是校验器自己造出来的假故障。
_EXT = r"py|html|htm|js|css|json|ya?ml|toml|ini|cfg|md|txt|sql|csv|pid|log"
_SLASH_RE = re.compile(rf"app/[A-Za-z_][\w/]*(?:\.(?:{_EXT}))?")
_DOT_RE = re.compile(r"\bapp(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}")

_PY_CACHE = {"__pycache__", ".pytest_tmp"}

# 文档里的示例占位名：教读者"自己建一个叫 X 的文件"，X 本就不该存在。
# 命名约定统一为 my*（mysource.py / my_sources.py / my_strategy.py）。
_PLACEHOLDER_RE = re.compile(r"(?:^|/)my[^/]*\.(?:py|json)$", re.I)

# 文档里打这个标记，表示其后到下一个二级标题之间是**有意保留的旧路径**
# （例如"重构前后对照表"的旧路径那一列）。标记本身不参与校验。
_STALE_MARK = "<!-- check_doc_paths:allow-stale-paths -->"
_HEADING_RE = re.compile(r"^#{1,2}\s")


def _exists(ref: str) -> bool:
    if ref.startswith("app/"):
        return (ROOT / ref).exists() or (ROOT / (ref + ".py")).exists()
    # 点形式：转成路径试一次目录，再试一次模块文件
    p = ROOT / ref.replace(".", "/")
    if p.with_suffix(".py").exists() or p.is_dir():
        return True
    # 末段可能是模块内的**符号**（常量 / 类名）而不是模块本身，
    # 例如 ``app.providers.market.compat.kline_base.DAILY_COLUMNS``。
    # 不去掉这一段就会把正确引用误报成失效，报告一噪就没人看了。
    parent = p.parent
    return parent.with_suffix(".py").exists() or parent.is_dir()


def _is_placeholder(ref: str) -> bool:
    """示例占位名（``mysource.py``）——文档教读者自己建，本就不存在。"""
    return bool(_PLACEHOLDER_RE.search(ref))


def _stale_line_numbers(text: str) -> set[int]:
    """被打上 ``allow-stale-paths`` 标记的行号集合。

    两种打法，按精度选：

    * 标记**独占一行** —— 从该行到下一个一/二级标题之间的引用全部豁免
      （用于"重构前后对照表"这类整段都是旧路径的地方）
    * 标记**贴在行尾** —— 只豁免这一行（用于正文里偶尔夹一句旧路径）
    """
    stale: set[int] = set()
    armed = False
    for idx, line in enumerate(text.split("\n"), start=1):
        if _HEADING_RE.match(line):
            armed = False
        if _STALE_MARK not in line:
            if armed:
                stale.add(idx)
            continue
        stale.add(idx)
        armed = line.strip() == _STALE_MARK
    return stale


def scan(files: list[Path]) -> list[dict]:
    bad: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(f.relative_to(ROOT))
        stale_lines = _stale_line_numbers(text)
        seen: set[str] = set()
        for lineno, line in enumerate(text.split("\n"), start=1):
            for ref in _SLASH_RE.findall(line) + _DOT_RE.findall(line):
                ref = ref.rstrip(".,;:)")
                if ref in seen or not ref:
                    continue
                # 跳过明显是叙述性的包层概念（没有对应文件很正常）
                if ref.startswith("app/") and ref.count("/") < 2 and ".py" not in ref:
                    continue
                seen.add(ref)
                if any(c in ref for c in _PY_CACHE):
                    continue
                if lineno in stale_lines or _is_placeholder(ref):
                    continue
                if not _exists(ref):
                    bad.append({"file": rel, "ref": ref, "line": lineno})
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
