#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板前端资源的加载入口。

历史上整个 dashboard 是一个 2501 行的 ``.py``，里面只有一条赋值语句
``DASHBOARD_HTML = r\"\"\"<!DOCTYPE html>...\"\"\"`` —— 也就是说它压根不是 Python，
只是装在 Python 文件里的 HTML。编辑器无法折叠/高亮、diff 里全是噪点、
一行 CSS 改动会让整个模块看起来被重写。

现在 HTML 回到它该在的位置（``monitoring/static/dashboard.html``），
本模块只负责把它读进来，并做好缓存与编码约定。
"""
from __future__ import annotations

import threading
from pathlib import Path

# 静态资源目录：monitoring/static/
STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_FILE = STATIC_DIR / "dashboard.html"

#: (mtime, html) 缓存。按文件修改时间失效 —— 以前用 lru_cache，
#: 结果是改一行 HTML 必须重启服务才能看到，排障时改个提示文案
#: 都要走一遍完整重启。按 mtime 失效后，刷新页面就是最新的。
_cache_lock = threading.Lock()
_cache: tuple[float, str] | None = None


def load_dashboard_html() -> str:
    """读取面板 HTML（按文件 mtime 缓存）。

    面板刻意做成单文件自包含 —— 不引 CDN，图表用 canvas 手绘。
    因此在离线/内网环境也能直接打开，代价是不做代码分割。
    """
    global _cache
    mtime = DASHBOARD_FILE.stat().st_mtime
    with _cache_lock:
        if _cache is not None and _cache[0] == mtime:
            return _cache[1]
    html = DASHBOARD_FILE.read_text(encoding="utf-8")
    with _cache_lock:
        _cache = (mtime, html)
    return html


def dashboard_html() -> str:
    """返回面板 HTML 字符串。出错时降级为一个提示页而不是 500。"""
    try:
        return load_dashboard_html()
    except OSError as exc:  # pragma: no cover
        return (
            "<!DOCTYPE html><meta charset='utf-8'>"
            f"<h1>面板资源缺失</h1><p>{DASHBOARD_FILE}</p><pre>{exc}</pre>"
        )


__all__ = ["STATIC_DIR", "DASHBOARD_FILE", "dashboard_html", "load_dashboard_html"]
