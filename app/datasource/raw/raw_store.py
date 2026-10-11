#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : raw_store.py
# @Description : Raw / Bronze 层 —— 原始响应存档
#
#                为什么一定要存原始数据：
#
#                    "为什么 2025-08-01 的贵州茅台收盘价和历史数据库不一样？"
#
#                没有 Raw 层，这个问题只能靠重新请求一遍外部接口来回答 ——
#                而外部接口往往已经变了、限流了、或者根本不认这段历史。
#                有了 Raw 层，链路是完整的：
#
#                    因子 → 特征 → 标准 K 线 → raw_id → 原始响应 → 那次请求的参数
#
#                第三方接口会改字段、改口径、突然失效，甚至偶尔返回错数据。
#                只有原始响应在手，才能**事后重新处理**（replay），
#                而不是对着已经入库的"成品"干瞪眼。这也是 Medallion 分层里
#                Bronze 层最核心的价值：可审计 + 可重放。
#
#                落盘策略（刻意做得轻量）：
#                  * 一天一个目录、一条记录一个 JSON 文件，不做索引表、不进数据库；
#                  * 默认只存前 200 行 + 列名 + 总行数 —— 目的是**排查**，
#                    不是做数据湖，别把磁盘写满；
#                  * 写失败一律忽略（只读盘/权限问题不该影响取数）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.common.logging import logger

#: 每条记录默认最多存多少行
DEFAULT_MAX_ROWS: int = 200
#: 默认保留天数
DEFAULT_KEEP_DAYS: int = 7


def _jsonable(value: Any) -> Any:
    """把 date/datetime/NaN 之类转成 JSON 能写的形态。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if value != value else value      # NaN → None
    return str(value)


class RawStore:
    """原始响应存档。"""

    def __init__(self, root: Optional[str] = None, max_rows: int = DEFAULT_MAX_ROWS,
                 keep_days: int = DEFAULT_KEEP_DAYS, enabled: bool = True):
        self.root = Path(root) if root else (self._default_root())
        self.max_rows = max(1, int(max_rows))
        self.keep_days = max(1, int(keep_days))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    @staticmethod
    def _default_root() -> Path:
        try:
            from app.common.config import settings

            return Path(settings.DATA_DIR) / "raw"
        except Exception:
            return Path("data") / "raw"

    # ---------------- 写 ----------------
    def save(self, *, provider: str, capability: str, route: str = "",
             params: Optional[Dict[str, Any]] = None,
             frame: Any = None) -> str:
        """存档一次原始响应，返回 ``raw_id``（写失败返回空串）。"""
        if not self.enabled:
            return ""
        try:
            rows: List[Dict[str, Any]] = []
            columns: List[str] = []
            total = 0
            if frame is not None and hasattr(frame, "empty") and not frame.empty:
                try:
                    columns = [str(c) for c in frame.columns]
                    total = int(len(frame))
                    head = frame.head(self.max_rows)
                    rows = json.loads(
                        head.to_json(orient="records", force_ascii=False,
                                     date_format="iso")
                    )
                except Exception as exc:
                    logger.debug(f"Raw 层序列化失败（只存元信息）: {exc}")
                    rows = []

            raw_id = (f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-"
                      f"{route or provider}-{random.randint(1000, 9999)}")
            payload = {
                "raw_id": raw_id,
                "at": datetime.now().isoformat(timespec="seconds"),
                "provider": provider,
                "capability": capability,
                "route": route or provider,
                "params": _jsonable(params or {}),
                "columns": columns,
                "rows": total,
                "stored_rows": len(rows),
                "truncated": total > len(rows),
                "records": rows,
            }
            day_dir = self.root / date.today().strftime("%Y-%m-%d")
            with self._lock:
                day_dir.mkdir(parents=True, exist_ok=True)
                target = day_dir / f"{raw_id}.json"
                tmp = target.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, target)
            return raw_id
        except Exception as exc:
            logger.debug(f"Raw 层存档失败（忽略）: {type(exc).__name__}: {exc}")
            return ""

    # ---------------- 读 ----------------
    def load(self, raw_id: str) -> Optional[Dict[str, Any]]:
        """按 raw_id 取回原始记录（今天/昨天找不到就返回 None）。"""
        if not raw_id:
            return None
        try:
            day_dir = self.root / date.today().strftime("%Y-%m-%d")
            target = day_dir / f"{raw_id}.json"
            if not target.exists():
                # 跨零点的情况：再翻最近几天
                for back in range(1, self.keep_days):
                    older = (self.root /
                             (date.today() - timedelta(days=back)).strftime("%Y-%m-%d")
                             / f"{raw_id}.json")
                    if older.exists():
                        target = older
                        break
                else:
                    return None
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug(f"Raw 层读取失败 {raw_id}: {exc}")
            return None

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近的存档元信息（**不含** records，避免把内存吃光）。"""
        out: List[Dict[str, Any]] = []
        try:
            if not self.root.exists():
                return out
            files = sorted(self.root.glob("*/*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[:max(1, int(limit))]:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                payload.pop("records", None)
                out.append(payload)
        except Exception as exc:
            logger.debug(f"Raw 层列举失败: {exc}")
        return out

    # ---------------- 维护 ----------------
    def purge(self, keep_days: Optional[int] = None) -> int:
        """清掉过期的原始存档，返回删除的文件数。"""
        days = self.keep_days if keep_days is None else max(1, int(keep_days))
        cutoff = date.today() - timedelta(days=days)
        removed = 0
        try:
            if not self.root.exists():
                return 0
            for day_dir in self.root.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    when = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if when >= cutoff:
                    continue
                for f in day_dir.glob("*.json"):
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass
                try:
                    day_dir.rmdir()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug(f"Raw 层清理失败: {exc}")
        return removed

    def stats(self) -> Dict[str, Any]:
        try:
            files = list(self.root.glob("*/*.json")) if self.root.exists() else []
            size = sum(f.stat().st_size for f in files)
            return {"root": str(self.root), "files": len(files),
                    "bytes": size, "enabled": self.enabled}
        except Exception as exc:
            return {"root": str(self.root), "files": 0, "bytes": 0,
                    "enabled": self.enabled, "error": str(exc)}


__all__ = ["DEFAULT_KEEP_DAYS", "DEFAULT_MAX_ROWS", "RawStore"]
