#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : snapshot.py
# @Description : 运行快照：把引擎的瞬时状态固化到磁盘，方便事后查看细节
#               - latest.json   当前状态（面板读它，原子写）
#               - history/*.json 滚动历史（保留最近 N 份，可用于复盘趋势）
#               - orders.jsonl   订单流，append-only，永不丢
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import hashlib
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from app.core.config import settings
from app.utils.jsonio import (
    append_jsonl,
    dumps,
    json_safe,
    read_json,
    read_jsonl,
    write_json_atomic,
)
from app.utils.logger import logger

#: 权益曲线最多保留多少个采样点，避免快照体积随运行时长无限膨胀
MAX_CURVE_POINTS = 400

#: 算"内容指纹"时要剔除的字段。
#: 这些字段每份快照都不一样（时间戳、序号、心跳、内存占用），
#: 但它们的差异**不代表状态有任何实质变化**。不剔除的话，
#: "变了才存"永远成立 —— 每份都会被认为"变了"，等于没做。
_VOLATILE_KEYS = frozenset({
    "captured_at", "snapshot_seq", "seq", "ts", "timestamp", "updated_at",
    "uptime_sec", "uptime", "last_snapshot_at", "age_sec", "elapsed",
    "memory_mb", "rss_mb", "threads", "api_calls", "requests",
    "heartbeat", "heartbeat_at", "checked_at", "last_seen",
})


def _strip_volatile(obj: Any) -> Any:
    """递归去掉易变字段，只留下"状态本体"。"""
    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v)
            for k, v in obj.items()
            if k not in _VOLATILE_KEYS
        }
    if isinstance(obj, (list, tuple)):
        return [_strip_volatile(v) for v in obj]
    return obj


def content_fingerprint(snap: Dict[str, Any]) -> str:
    """对快照的**实质内容**取指纹。

    用途：判断"这次到底有没有变化"，决定要不要落盘。
    只看状态本体（任务权益/持仓/订单/组件状态），忽略时间戳之类
    每份都会变的字段——否则比较永远是不相等，等于在按时间无脑写盘。
    """
    body = _strip_volatile(snap)
    return hashlib.blake2b(dumps(body).encode("utf-8"), digest_size=16).hexdigest()


def _sample_curve(curve: List[Any], max_points: int = MAX_CURVE_POINTS) -> List[List[Any]]:
    """把 ``[(dt, equity), ...]`` 等距降采样成 ``[[iso_dt, equity], ...]``。"""
    pts = [(dt, float(eq)) for dt, eq in curve if eq is not None]
    if not pts:
        return []
    n = len(pts)
    if n > max_points:
        step = n / max_points
        idx = sorted({int(i * step) for i in range(max_points)} | {n - 1})
        pts = [pts[i] for i in idx]
    out: List[List[Any]] = []
    for dt, eq in pts:
        stamp = dt.isoformat() if isinstance(dt, datetime) else str(dt)
        out.append([stamp, round(eq, 4)])
    return out


class SnapshotStore:
    """一个 run 的快照仓库。

    目录结构::

        output/snapshots/<run_id>/
          meta.json          运行元信息（模式、启动时间、配置摘要）
          latest.json        最新全量快照
          history/<ts>.json  滚动历史快照
          orders.jsonl       订单流（含被风控否决的信号，便于追溯）
    """

    def __init__(
        self,
        run_id: str,
        base_dir: Optional[str | Path] = None,
        keep: Optional[int] = None,
    ) -> None:
        self.run_id = run_id
        self.root = Path(base_dir or settings.SNAPSHOT_DIR) / run_id
        self.history_dir = self.root / "history"
        self.keep = int(keep if keep is not None else settings.MONITOR_SNAPSHOT_KEEP)

        self.latest_path = self.root / "latest.json"
        self.meta_path = self.root / "meta.json"
        self.orders_path = self.root / "orders.jsonl"

        self._lock = threading.Lock()
        self._recent_orders: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._write_count = 0
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def bump_seq(self) -> int:
        """递增快照序号并返回。面板用它给每份快照编号。"""
        self._write_count += 1
        return self._write_count

    # ============================================================
    # 订单流
    # ============================================================
    def note_order(self, order: Any) -> None:
        """记录一笔订单（成交或被否决都记，方便复盘"为什么没买"）。"""
        payload = order.to_dict() if hasattr(order, "to_dict") else json_safe(order)
        payload["_recorded_at"] = datetime.now().isoformat()
        with self._lock:
            self._recent_orders.append(payload)
        try:
            append_jsonl(self.orders_path, payload)
        except OSError as exc:
            logger.debug(f"订单流写入失败: {exc}")

    def recent_orders(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = list(self._recent_orders)
        return rows[-limit:] if limit > 0 else rows

    def order_history(self, limit: int = 200) -> List[Dict[str, Any]]:
        return read_jsonl(self.orders_path, limit=limit, newest_first=True)

    # ============================================================
    # 采集与落盘
    # ============================================================
    def capture(self, engine: Any, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """把引擎状态采集为一个纯 JSON 结构。"""
        base: Dict[str, Any] = {}
        try:
            base = json_safe(engine.snapshot())
        except Exception as exc:
            logger.error(f"引擎快照采集失败: {exc}", exc_info=True)
            base = {"engine": {}, "components": {}}

        tasks = self._collect_tasks(engine)
        engine_info = dict(base.get("engine") or {})
        engine_info["snapshot_seq"] = self._write_count

        snap: Dict[str, Any] = {
            "run_id": self.run_id,
            "captured_at": datetime.now().isoformat(),
            "engine": engine_info,
            "components": base.get("components") or {},
            "task_count": len(tasks),
            "tasks": tasks,
            "orders_recent": self.recent_orders(50),
            "extra": json_safe(extra or {}),
        }
        return snap

    def _collect_tasks(self, engine: Any) -> List[Dict[str, Any]]:
        """优先从策略中枢的 TaskRuntime 取任务（可拿到权益曲线）。"""
        tasks: List[Dict[str, Any]] = []
        sm = None
        try:
            sm = engine.get_component("strategy_manager")
        except Exception:
            sm = None

        runtime = getattr(sm, "runtime", None) if sm is not None else None
        if runtime is not None:
            for task in runtime:
                try:
                    item = json_safe(task.snapshot())
                    item["equity_curve"] = _sample_curve(
                        list(getattr(task.portfolio, "equity_curve", []))
                    )
                    item["recent_orders"] = [
                        o.to_dict() for o in list(task.orders)[-10:]
                    ]
                    tasks.append(item)
                except Exception as exc:
                    logger.debug(f"任务 {getattr(task, 'task_id', '?')} 快照失败: {exc}")
            return tasks

        # 退化路径：只有组件级快照
        if sm is not None and hasattr(sm, "snapshot"):
            try:
                data = json_safe(sm.snapshot())
                return list(data.get("tasks") or [])
            except Exception:
                return []
        return tasks

    def write(self, engine: Any, extra: Optional[Dict[str, Any]] = None) -> Path:
        """采集 + 落盘（同步）。小规模调用可直接用。"""
        snap = self.capture(engine, extra=extra)
        self._write_count += 1
        snap["engine"]["snapshot_seq"] = self._write_count
        self.persist(snap)
        return self.latest_path

    def persist(self, snap: Dict[str, Any]) -> Path:
        """只落盘（不含采集）。

        采集必须在 reactor 线程上完成——那里才能安全遍历任务字典；
        落盘是纯 IO，可以在工作线程执行。拆开是为了避免
        "读快照时正好有任务在增删"导致 dict 迭代报错、整份快照丢失。
        """
        with self._lock:
            write_json_atomic(self.latest_path, snap, indent=None)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            write_json_atomic(self.history_dir / f"{stamp}.json", snap, indent=None)
            self._prune_history()

        write_json_atomic(
            self.meta_path,
            {
                "run_id": self.run_id,
                "last_write": snap.get("captured_at"),
                "writes": self._write_count,
                "engine": snap.get("engine", {}),
            },
        )
        return self.latest_path

    def _prune_history(self) -> None:
        files = sorted(self.history_dir.glob("*.json"))
        extra = len(files) - self.keep
        for path in files[: max(0, extra)]:
            try:
                path.unlink()
            except OSError:
                pass

    # ============================================================
    # 读取（面板 / 复盘 / 恢复）
    # ============================================================
    def load_latest(self) -> Optional[Dict[str, Any]]:
        return read_json(self.latest_path)

    @staticmethod
    def runs(base_dir: Optional[str | Path] = None) -> List[Dict[str, Any]]:
        """列出所有 run，按更新时间倒序。"""
        root = Path(base_dir or settings.SNAPSHOT_DIR)
        if not root.exists():
            return []
        out: List[Dict[str, Any]] = []
        for d in root.iterdir():
            if not d.is_dir():
                continue
            meta = read_json(d / "meta.json") or {}
            latest = read_json(d / "latest.json") or {}
            latest_path = d / "latest.json"
            out.append(
                {
                    "run_id": d.name,
                    "writes": meta.get("writes", 0),
                    "last_write": meta.get("last_write"),
                    "mtime": latest_path.stat().st_mtime if latest_path.exists() else 0,
                    "status": (latest.get("engine") or {}).get("status"),
                    "mode": (latest.get("engine") or {}).get("mode"),
                    "task_count": latest.get("task_count", 0),
                }
            )
        out.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
        return out

    @staticmethod
    def load(base_dir: Optional[str | Path] = None, run_id: str = "") -> Optional[Dict[str, Any]]:
        """读取指定 run 的最新快照；``run_id`` 为空则取最近一次 run。"""
        root = Path(base_dir or settings.SNAPSHOT_DIR)
        if not run_id:
            runs = SnapshotStore.runs(root)
            if not runs:
                return None
            run_id = runs[0]["run_id"]
        return read_json(root / run_id / "latest.json")


__all__ = ["SnapshotStore", "MAX_CURVE_POINTS"]
