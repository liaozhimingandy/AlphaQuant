#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : jsonio.py
# @Description : JSON 落盘工具：把任意领域对象安全序列化，并保证写文件是原子的
#               面板、快照、文件总线都依赖这里，避免各写各的 to_dict
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional


def json_safe(obj: Any, _depth: int = 0) -> Any:
    """把领域对象递归转换成可 JSON 序列化的结构。

    处理：dataclass / Enum / datetime / Path / set / tuple / numpy 标量与数组。
    无法识别的对象降级为 ``repr``，绝不抛异常——监控链路不能因为一个字段而整条挂掉。
    """
    if _depth > 12:  # 防御自引用结构
        return str(obj)
    if obj is None:
        return obj
    # 注意：Enum 必须在基础类型之前判断！
    # 本项目大量枚举是 ``class Side(str, Enum)`` 这种 str 子类，
    # 若先走 isinstance(obj, str) 分支，枚举会被原样返回，JSON 里就成了
    # "<Side.BUY: 'BUY'>" 而不是 "BUY"。
    if isinstance(obj, Enum):
        return json_safe(obj.value, _depth + 1)
    if isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: json_safe(v, _depth + 1) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): json_safe(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_safe(v, _depth + 1) for v in obj]
    # numpy 标量 / 数组
    if hasattr(obj, "tolist"):
        try:
            return json_safe(obj.tolist(), _depth + 1)
        except Exception:
            pass
    if hasattr(obj, "item"):
        try:
            return json_safe(obj.item(), _depth + 1)
        except Exception:
            pass
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            return json_safe(obj.to_dict(), _depth + 1)
        except Exception:
            pass
    return repr(obj)


def dumps(obj: Any, indent: Optional[int] = None) -> str:
    """序列化为 JSON 字符串（中文不转义，保证面板上直接可读）。"""
    return json.dumps(json_safe(obj), ensure_ascii=False, indent=indent, default=str)


def write_json_atomic(
    path: str | Path,
    obj: Any,
    indent: Optional[int] = 2,
    fsync: bool = True,
) -> Path:
    """原子写 JSON：先写临时文件再 ``os.replace``，避免读到半截文件。

    面板每 1~2 秒读一次快照，非原子写会出现"读到半截 JSON"的偶发解析错误。

    :param fsync: 是否强制刷盘。高频、可容忍丢失的小文件（如总线进度、心跳）
        建议传 False —— Windows 上每次 fsync 都是 FlushFileBuffers，
        在带实时防护的机器上单次可达数百毫秒，会明显拖慢 poll 循环。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(obj, indent=indent)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def read_json(path: str | Path, default: Any = None) -> Any:
    """读 JSON。文件不存在或损坏时返回 ``default``，不抛异常。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def append_jsonl(path: str | Path, obj: Any) -> None:
    """追加一行 JSON（订单流等 append-only 场景）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(dumps(obj))
        f.write("\n")


def read_jsonl(path: str | Path, limit: Optional[int] = None, newest_first: bool = False) -> list:
    """读 JSONL。``newest_first`` 时先倒序再截断，适合"最近 N 条"场景。"""
    p = Path(path)
    if not p.exists():
        return []
    rows: list = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        return []
    if newest_first:
        rows.reverse()
    if limit is not None and limit >= 0:
        rows = rows[:limit]
    return rows


def tail_lines(path: str | Path, n: int = 200) -> list:
    """读取文本文件最后 n 行（日志查看用）。文件缺失返回空列表。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-n:]


__all__ = [
    "json_safe",
    "dumps",
    "write_json_atomic",
    "read_json",
    "append_jsonl",
    "read_jsonl",
    "tail_lines",
]
