#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : store.py
# @Description : 事件流水账（Outbox）：事件落盘、链路查询与重放
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么事件也要落盘
==================

任务已经能活过重启了（``tasking/journal.py``），但**事件一直是内存广播**：
进程一死，"market.kline.updated 曾经发生过"这件事就消失了。
于是链路在"事件 → 任务"这一环是断的 —— 事件丢了，任务压根不会被创建，
任务落盘救不了它，日志里也只剩一句来路不明的话。

这一层就是补上那一环：

    事件发布 → 立即记进流水账 → 规则命中 → 建任务 → 任务也记进流水账
        └────── 两本账靠 trace_id 串起来，事后能一路追回去 ──────┘

为什么不开 WAL 之外的花样
--------------------------

参考过 Rhapsody 的预分配块 + head/tail（见 EVENT_TASK.md §十四）：
它适合每秒上千条、跨系统、合规要求零丢失的场景。我们是单机量化引擎，
每天几千条事件，SQLite 足够，而块文件带来的位点管理/块回收复杂度远超收益。

心跳为什么要采样
----------------

``timer_3min`` / ``system.maintenance.tick`` 这类节拍事件每秒都在发，
全量落盘会在几小时内把表撑到几十万行，而它们的价值只是"节拍还在跑"。
所以对它们**采样落盘**（默认 1/30，落盘时记下序号），
既不淹没真正的业务事件，又保留了"能在面板上看见它在跑、能估算频率"。

与任务流水账的关系
------------------

两张表分在两个文件（``tasks.db`` / ``events.db``）：写入频率差一个量级，
清理策略也不同（任务表要保留未终结任务、事件表只需要一个保留窗口）。
跨库关联不做 SQL JOIN —— 事件与任务都带 ``trace_id``，
读出来在 Python 里对齐即可，零依赖项目不值得为此引入额外复杂度。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.common.logging import logger

#: 节拍类事件：采样落盘，不全量记
HEARTBEAT_EVENT_TYPES = (
    "timer_3min",
    "system.maintenance.tick",
    "engine.heartbeat",
)

#: 心跳采样率（每隔多少条记一条）
DEFAULT_HEARTBEAT_SAMPLE = 30

#: 自适应降采样的触发阈值：同一事件类型在 :data:`FLOOD_WINDOW` 秒内
#: 超过这么多条，之后就按 :data:`FLOOD_SAMPLE` 采样。
#:
#: 为什么要自适应：硬编码"哪些类型是心跳"永远跟不上现实 ——
#: ``market.bar.received`` 这种每秒几十条、且**没有任何订阅者**的事件
#: （规则没配 = 链路在这一跳就断了）几小时就能把表撑到几十万行，
#: 真正有价值的业务事件反而被淹没。按实测频率自动降采样，
#: 并把"哪些类型被降采样了"报给面板 —— 这件事本身就是一条线索：
#: 一个被降采样的业务事件，多半同时是"发了没人接"的那类。
FLOOD_THRESHOLD = 120
FLOOD_WINDOW = 60.0
#: 降采样比例（每隔多少条记一条）
FLOOD_SAMPLE = 20


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class EventStore:
    """事件流水账。

    :param path: 落盘文件；不传则用 ``output/runtime/events.db``
    :param persist: 是否落盘（测试环境关掉，避免污染真实输出目录）
    :param heartbeat_sample: 心跳事件的采样间隔（每 N 条记 1 条）
    :param flush_interval: 后台合并写的间隔（秒）
    :param retention_hours: 保留窗口，清理时只动这个窗口之外的
    """

    def __init__(
        self,
        path: Optional[str] = None,
        persist: Optional[bool] = None,
        heartbeat_sample: int = DEFAULT_HEARTBEAT_SAMPLE,
        flush_interval: float = 1.0,
        retention_hours: float = 72.0,
        flood_threshold: int = FLOOD_THRESHOLD,
        flood_sample: int = FLOOD_SAMPLE,
    ) -> None:
        self.path = Path(path) if path else self.default_path()
        self.persist = _env_bool("AQ_EVENT_PERSIST", True) if persist is None else bool(persist)
        self.heartbeat_sample = max(1, int(heartbeat_sample))
        self.flush_interval = max(0.2, float(flush_interval))
        self.retention_hours = max(0.5, float(retention_hours))
        self.flood_threshold = max(1, int(flood_threshold))
        self.flood_sample = max(1, int(flood_sample))
        #: 事件类型 → [窗口起点, 窗口内计数]。用来识别"谁在暴涨"
        self._rate: Dict[str, List[Any]] = {}
        #: 已被降采样的事件类型（面板要能看见：被降采样的多半是"发了没人接"）
        self._flooded: set = set()

        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._dirty: List[tuple] = []
        self._flusher: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._heartbeat_count = 0
        self._seq = 0
        self.enabled = False
        self.stats: Dict[str, Any] = {
            "recorded": 0,
            "skipped_heartbeat": 0,
            "skipped_flood": 0,
            "flushed": 0,
            "replays": 0,
            "last_error": "",
        }

    # ---------------- 生命周期 ----------------
    @staticmethod
    def default_path() -> Path:
        try:
            from app.common.config import settings

            return Path(settings.OUTPUT_DIR) / "runtime" / "events.db"
        except Exception:
            return Path("output") / "runtime" / "events.db"

    def open(self) -> bool:
        """建库。**落盘失败就降级为纯内存** —— 磁盘坏了不能让事件总线起不来。"""
        if not self.persist:
            return False
        if self.enabled and self._conn is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS engine_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT,
                    trace_id TEXT,
                    causation_id TEXT,
                    occurred_at TEXT,
                    published_at TEXT,
                    subscribers INTEGER DEFAULT 0,
                    data TEXT,
                    replay_of TEXT,
                    run_id TEXT
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_trace "
                "ON engine_events(trace_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_type "
                "ON engine_events(event_type)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS event_replay_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    origin_id TEXT,
                    event_type TEXT,
                    replayed_at TEXT,
                    by TEXT,
                    note TEXT
                )"""
            )
            conn.commit()
            self._conn = conn
            self.enabled = True
            self._start_flusher()
            return True
        except Exception as exc:
            self.enabled = False
            self._conn = None
            self.stats["last_error"] = str(exc)
            logger.error(f"事件流水账初始化失败，降级为不落盘: {exc}")
            return False

    def _start_flusher(self) -> None:
        if self._flusher is not None and self._flusher.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(self.flush_interval):
                try:
                    self.flush()
                except Exception as exc:  # pragma: no cover
                    self.stats["last_error"] = str(exc)
            try:
                self.flush()
            except Exception:  # pragma: no cover
                pass

        self._flusher = threading.Thread(target=loop, name="event-store-flusher",
                                         daemon=True)
        self._flusher.start()

    # ---------------- 写 ----------------
    def record(
        self,
        event: Any,
        subscribers: int = 0,
        replay_of: str = "",
        run_id: str = "",
    ) -> bool:
        """记一条事件。**不抛异常** —— 记流水账失败不该影响事件本身。

        :return: 是否真的落盘（心跳被采样跳过时返回 False）
        """
        if not self.persist:
            return False
        if not self.open():
            return False

        etype = str(getattr(event, "event_type", "") or "")

        # 1) 已知节拍类：固定采样
        if etype in HEARTBEAT_EVENT_TYPES:
            self._heartbeat_count += 1
            if self._heartbeat_count % self.heartbeat_sample != 0:
                self.stats["skipped_heartbeat"] += 1
                return False
            return self._write(event, etype, subscribers, replay_of, run_id)

        # 2) 自适应：任何类型只要在 FLOOD_WINDOW 秒内超过阈值就降采样。
        #    硬编码"哪些是心跳"永远跟不上现实（market.bar.received 就是例子）。
        with self._lock:
            now = time.time()
            slot = self._rate.get(etype)
            if slot is None or now - float(slot[0]) > FLOOD_WINDOW:
                self._rate[etype] = [now, 1]
                flooded = False
            else:
                slot[1] = int(slot[1]) + 1
                flooded = slot[1] > self.flood_threshold
            if flooded:
                self._flooded.add(etype)
                if int(slot[1]) % self.flood_sample != 0:
                    self.stats["skipped_flood"] += 1
                    return False
        return self._write(event, etype, subscribers, replay_of, run_id)

    def _write(self, event: Any, etype: str, subscribers: int = 0,
               replay_of: str = "", run_id: str = "") -> bool:
        """真正组装一行并放进待写批次（采样判定已经在 record 里做完）。"""
        try:
            payload = json.dumps(
                getattr(event, "data", None) or {}, ensure_ascii=False, default=str
            )
        except Exception:
            payload = "{}"
        occurred = getattr(event, "occurred_at", None)
        occurred_iso = (
            occurred.isoformat(timespec="seconds")
            if isinstance(occurred, datetime) else _now_iso()
        )
        row = (
            str(getattr(event, "event_id", "") or ""),
            etype,
            str(getattr(event, "source", "") or ""),
            str(getattr(event, "trace_id", "") or ""),
            str(getattr(event, "causation_id", "") or ""),
            occurred_iso,
            _now_iso(),
            int(subscribers or 0),
            payload,
            str(replay_of or ""),
            str(run_id or ""),
        )
        with self._lock:
            self._seq += 1
            self._dirty.append(row)
            self.stats["recorded"] += 1
        return True

    def flooded_types(self) -> List[str]:
        """当前被降采样的事件类型。

        这个清单本身就是一条排障线索：一个业务事件被降采样，
        说明它每秒几十条地在发 —— 而这么密集却往往**没有订阅者**
        （规则没配），也就是链路在第一跳就断了却没人知道。
        """
        with self._lock:
            return sorted(self._flooded)

    def flush(self) -> int:
        """把待写批次落盘。失败时**把批次放回去**，下次再试。"""
        if not self.enabled or self._conn is None:
            return 0
        with self._lock:
            if not self._dirty:
                return 0
            batch, self._dirty = self._dirty, []
        try:
            with self._lock:
                self._conn.executemany(
                    "INSERT INTO engine_events (event_id, event_type, source, "
                    "trace_id, causation_id, occurred_at, published_at, "
                    "subscribers, data, replay_of, run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
                self._conn.commit()
            self.stats["flushed"] += len(batch)
            return len(batch)
        except Exception as exc:
            with self._lock:
                self._dirty = batch + self._dirty
            self.stats["last_error"] = str(exc)
            logger.error(f"事件流水账写盘失败: {exc}")
            return 0

    def log_replay(
        self, event_id: str, origin_id: str, event_type: str,
        by: str = "manual", note: str = "",
    ) -> None:
        """记一次重放。重放是会改变系统行为的操作，必须留痕。"""
        if not self.open():
            return
        try:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return
                conn.execute(
                    "INSERT INTO event_replay_log "
                    "(event_id, origin_id, event_type, replayed_at, by, note) "
                    "VALUES (?,?,?,?,?,?)",
                    (event_id, origin_id, event_type, _now_iso(), by, note),
                )
                conn.commit()
                self.stats["replays"] += 1
        except Exception as exc:
            self.stats["last_error"] = str(exc)
            logger.error(f"记录事件重放失败: {exc}")

    def close(self) -> None:
        self._stop.set()
        try:
            self.flush()
        except Exception:  # pragma: no cover
            pass
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception as exc:  # pragma: no cover
            self.stats["last_error"] = str(exc)
        self._conn = None
        self.enabled = False

    # ---------------- 读 ----------------
    def _read(self, sql: str, args: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        """读走**独立连接**：写连接可能正被 flush 线程占着。"""
        if not self.enabled:
            return []
        try:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
        except Exception as exc:
            self.stats["last_error"] = str(exc)
            return []
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self.stats["last_error"] = str(exc)
            logger.error(f"读取事件流水账失败: {exc}")
            rows = []
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return rows

    @staticmethod
    def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
        raw = row.get("data") or "{}"
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        return {
            "id": row.get("id"),
            "event_id": row.get("event_id"),
            "event_type": row.get("event_type"),
            "source": row.get("source"),
            "trace_id": row.get("trace_id"),
            "causation_id": row.get("causation_id"),
            "occurred_at": row.get("occurred_at"),
            "published_at": row.get("published_at"),
            "subscribers": row.get("subscribers") or 0,
            "replay_of": row.get("replay_of") or "",
            "data": data,
        }

    def recent(
        self, limit: int = 50, event_type: str = "", keyword: str = ""
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM engine_events"
        where, args = [], []
        if event_type:
            where.append("event_type = ?")
            args.append(event_type)
        if keyword:
            where.append(
                "(event_id LIKE ? OR trace_id LIKE ? OR source LIKE ? OR data LIKE ?)"
            )
            args.extend([f"%{keyword}%"] * 4)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [self._decode(r) for r in self._read(sql, tuple(args))]

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read("SELECT * FROM engine_events WHERE event_id = ? "
                          "ORDER BY id DESC LIMIT 1", (str(event_id),))
        return self._decode(rows[0]) if rows else None

    def trace(self, trace_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """一条链路上的全部事件（按发生顺序）。

        只按 ``trace_id`` 查，不递归追 ``causation_id``：
        子事件继承父的 trace_id（见 EngineEvent.child），
        所以一条链本来就是"同一个 trace_id 的全部事件"。
        """
        rows = self._read(
            "SELECT * FROM engine_events WHERE trace_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (str(trace_id), max(1, int(limit))),
        )
        return [self._decode(r) for r in rows]

    def traces(self, limit: int = 30, min_events: int = 1) -> List[Dict[str, Any]]:
        """最近活跃的链路：按链路聚合，给出首末事件与环节数。

        min_events 默认 1（单事件也算一条链），排查"这条数据从哪来"时
        往往就是要看那些只有一跳的孤儿事件 —— 它们多半是链路断掉的地方。
        """
        rows = self._read(
            "SELECT trace_id, COUNT(*) AS n, MIN(published_at) AS started, "
            "MAX(published_at) AS ended, "
            "GROUP_CONCAT(DISTINCT event_type) AS types "
            "FROM engine_events WHERE trace_id <> '' "
            "GROUP BY trace_id HAVING n >= ? ORDER BY MAX(id) DESC LIMIT ?",
            (max(1, int(min_events)), max(1, int(limit))),
        )
        out = []
        for r in rows:
            types = [t for t in (r.get("types") or "").split(",") if t]
            out.append({
                "trace_id": r.get("trace_id"),
                "events": r.get("n") or 0,
                "started": r.get("started") or "",
                "ended": r.get("ended") or "",
                "types": types,
                "head": types[0] if types else "",
                "tail": types[-1] if types else "",
            })
        return out

    def replay_log(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self._read(
            "SELECT * FROM event_replay_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )

    def prune(self, older_than_hours: float = 0.0, keep: int = 2000) -> int:
        """清掉保留窗口之外的老事件，但**永远保留最近 keep 条**。

        保留下限是必须的：保留窗口配错（比如 0.5 小时）不至于把
        整个流水账清空 —— 排障时"什么都没有"比"有点旧"糟得多。
        """
        if not self.enabled or self._conn is None:
            return 0
        hours = float(older_than_hours or self.retention_hours)
        cutoff = (datetime.now() - timedelta(hours=max(0.0, hours))).isoformat(
            timespec="seconds"
        )
        with self._lock:
            try:
                cur = self._conn.execute(
                    "DELETE FROM engine_events WHERE published_at < ? "
                    "AND id NOT IN ("
                    "  SELECT id FROM engine_events ORDER BY id DESC LIMIT ?)",
                    (cutoff, int(keep)),
                )
                self._conn.commit()
                return int(cur.rowcount or 0)
            except Exception as exc:
                self.stats["last_error"] = str(exc)
                logger.error(f"清理事件流水账失败: {exc}")
                return 0

    def counts(self) -> Dict[str, int]:
        if not self.enabled:
            return {"events": 0, "traces": 0, "replays": 0}
        try:
            rows = self._read(
                "SELECT (SELECT COUNT(*) FROM engine_events) AS events, "
                "(SELECT COUNT(DISTINCT trace_id) FROM engine_events) AS traces, "
                "(SELECT COUNT(*) FROM event_replay_log) AS replays"
            )
            return {
                "events": int(rows[0].get("events") or 0),
                "traces": int(rows[0].get("traces") or 0),
                "replays": int(rows[0].get("replays") or 0),
            }
        except Exception:
            return {"events": 0, "traces": 0, "replays": 0}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "persist": bool(self.persist),
            "path": str(self.path),
            "stats": dict(self.stats),
            "counts": self.counts(),
            "heartbeat_sample": self.heartbeat_sample,
            "retention_hours": self.retention_hours,
            "heartbeat_types": list(HEARTBEAT_EVENT_TYPES),
            # 被降采样的类型本身就是线索：密集到要采样，
            # 往往同时意味着"发了没人接"（规则没配，链路在第一跳断了）
            "flooded_types": self.flooded_types(),
            "flood_threshold": self.flood_threshold,
            "flood_sample": self.flood_sample,
        }


__all__ = ["EventStore", "HEARTBEAT_EVENT_TYPES", "DEFAULT_HEARTBEAT_SAMPLE"]
