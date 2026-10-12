#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : journal.py
# @Description : TaskJournal —— 任务流水账，让"没干完的活"活过一次重启
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么任务必须落盘
==================

内存态队列有一个致命的性质：**进程死了，队列就跟着死了**，而且死得
特别安静 —— 没有报错、没有告警，那些任务只是"从来没被执行过"。

对采集任务来说这不算大事（下次定时会再来一遍）。但对"下单"这类
任务就不是了：一笔该下的单因为进程重启而消失，系统不会认为它有错，
它只是"没发生过"。要说清楚这笔钱为什么没投出去，只能去翻日志猜。

所以本文件做的事只有一件：把**每一个未终结的任务**写进 SQLite，
并在启动时把它们读回来。

为什么用独立的 SQLite 文件，而不是业务库
----------------------------------------

1. **生命周期不同**：业务库（K 线、订单）是资产，要长期保留、要备份；
   任务流水是工作台，重启恢复完就该归档清理。混在一起，"清理任务表"
   会变成一个不敢执行的操作。
2. **写入形态不同**：任务表是"高频小写入 + 随时被 truncate"；
   业务库是"批量追加 + 长期查询"。同一个库里两种形态互相拖累
   （WAL 文件被任一方长期占用）。
3. **故障隔离**：业务库出问题时，至少任务流水还能写，
   于是"数据库挂了"这件事本身能被记下来。

为什么用标准库 sqlite3 而不是项目里的 SQLAlchemy 封装
----------------------------------------------------

任务表是引擎的**内部工作台**，不是业务资产：它不需要 ORM、不需要迁移、
不需要被业务代码查询。用标准库能让 runtime 层不依赖数据访问层，
也就不会出现"改一次 ORM 模型，调度器跟着动"这种耦合。
WAL 与 busy_timeout 两个 PRAGMA 在这里手工设置，与生产库保持一致。

写盘策略：脏集合 + 后台合并，关键状态立即落
------------------------------------------

每次状态变更都单独开事务会把调度热路径拖慢（任务一秒能建几十个），
所以普通变更只标脏，后台线程每 ``flush_interval`` 秒合并成一个事务写。

但**三件事必须立即写**，不能等后台：

* 任务创建（QUEUED）—— 建出来就没了是最坏的情况；
* 进死信 —— 那是等人工处理的，丢了就没人对账；
* 终态（SUCCESS/FAILED/CANCELLED）—— 用来把这条流水账划掉。

代价是最多一次额外的毫秒级写，换来的是"任何时刻 kill -9，
未完成的活都还在盘上"。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.common.config import settings
from app.common.jsonio import json_safe
from app.common.logging import logger

#: 未终结状态：这些才是"下次启动要接着干"的活
OPEN_STATES = ("pending", "queued", "running", "retrying")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_tasks (
    task_id          TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL,
    queue            TEXT,
    priority         INTEGER,
    status           TEXT NOT NULL,
    params           TEXT,
    dedup_key        TEXT,
    source           TEXT,
    source_event_id  TEXT,
    correlation_id   TEXT,
    causation_id     TEXT,
    worker           TEXT,
    attempts         INTEGER DEFAULT 0,
    max_retries      INTEGER DEFAULT 0,
    manual_retries   INTEGER DEFAULT 0,
    retry_interval   REAL,
    timeout_seconds  REAL,
    error            TEXT,
    interrupted      INTEGER DEFAULT 0,
    created_at       TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    updated_at       TEXT,
    run_id           TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON engine_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON engine_tasks(updated_at);

CREATE TABLE IF NOT EXISTS dead_letter_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT,
    task_type       TEXT,
    queue           TEXT,
    error           TEXT,
    attempts        INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 0,
    manual_retries  INTEGER DEFAULT 0,
    dead_at         TEXT,
    resolved_at     TEXT,
    resolution      TEXT,
    run_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_dead_task ON dead_letter_log(task_id);
"""

_UPSERT = """
INSERT INTO engine_tasks (
    task_id, task_type, queue, priority, status, params, dedup_key, source,
    source_event_id, correlation_id, causation_id, worker, attempts,
    max_retries, manual_retries, retry_interval, timeout_seconds, error,
    interrupted, created_at, started_at, finished_at, updated_at, run_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(task_id) DO UPDATE SET
    task_type=excluded.task_type, queue=excluded.queue,
    priority=excluded.priority, status=excluded.status,
    params=excluded.params, dedup_key=excluded.dedup_key,
    source=excluded.source, source_event_id=excluded.source_event_id,
    correlation_id=excluded.correlation_id, causation_id=excluded.causation_id,
    worker=excluded.worker, attempts=excluded.attempts,
    max_retries=excluded.max_retries, manual_retries=excluded.manual_retries,
    retry_interval=excluded.retry_interval,
    timeout_seconds=excluded.timeout_seconds, error=excluded.error,
    interrupted=excluded.interrupted, created_at=excluded.created_at,
    started_at=excluded.started_at, finished_at=excluded.finished_at,
    updated_at=excluded.updated_at, run_id=excluded.run_id
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(dt: Any) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.isoformat(timespec="seconds")
    return str(dt)


def _parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


class TaskJournal:
    """任务流水账。

    线程安全：所有写操作在 ``_lock`` 内，SQLite 连接只在写线程与
    flush 线程里使用（读操作另开连接，避免长事务占着写锁）。
    """

    #: 默认落盘文件。放在 ``output/runtime/`` 下，与业务库彻底分开。
    DEFAULT_PATH = "tasks.db"

    def __init__(
        self,
        path: str = "",
        enabled: bool = True,
        flush_interval: float = 1.0,
        run_id: str = "",
    ) -> None:
        self.path = path or os.path.join(
            str(getattr(settings, "RUNTIME_DIR", "output/runtime")),
            self.DEFAULT_PATH,
        )
        self.enabled = bool(enabled)
        self.flush_interval = max(0.1, float(flush_interval))
        self.run_id = run_id

        self._lock = threading.RLock()
        self._dirty: "Dict[str, Dict[str, Any]]" = {}
        self._conn: Optional[sqlite3.Connection] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._opened = False
        self.stats: Dict[str, Any] = {
            "writes": 0, "flushes": 0, "immediate": 0,
            "last_flush_at": None, "last_error": None, "opened": False,
        }

    # ============================================================
    # 生命周期
    # ============================================================
    def open(self) -> bool:
        """建库建表。**惰性**：第一次真正要写时才调，不拖慢启动。"""
        if not self.enabled or self._opened:
            return self._opened
        with self._lock:
            if self._opened:
                return True
            try:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                conn = sqlite3.connect(
                    self.path, timeout=30.0, check_same_thread=False
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.executescript(_SCHEMA)
                conn.commit()
                self._conn = conn
                self._opened = True
                self.stats["opened"] = True
                self._start_flusher()
                logger.debug(f"任务流水账已打开 | {self.path}")
                return True
            except Exception as exc:
                # 落盘失败**不能**让调度器起不来：那会把"记不住任务"
                # 升级成"整个引擎跑不了"。降级成纯内存并记下来。
                self.enabled = False
                self.stats["last_error"] = str(exc)
                logger.error(f"任务流水账打开失败，降级为纯内存: {exc}")
                return False

    def _start_flusher(self) -> None:
        if self._thread is not None:
            return

        def _loop() -> None:
            while not self._stop.wait(self.flush_interval):
                try:
                    self.flush()
                except Exception as exc:  # pragma: no cover - 兜底
                    self.stats["last_error"] = str(exc)
            # 退出前把最后一批写掉：进程正常退出时靠这一下收尾
            try:
                self.flush()
            except Exception:
                pass

        self._thread = threading.Thread(
            target=_loop, name="task-journal", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """关闭：先刷干净，再停线程、断连接。"""
        self._stop.set()
        try:
            self.flush()
        except Exception:
            pass
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self._opened = False

    # ============================================================
    # 写
    # ============================================================
    def row_of(self, task: Any) -> Dict[str, Any]:
        """把 :class:`EngineTask` 摊成一行。"""
        params_raw = json_safe(task.params or {})
        try:
            params = json.dumps(params_raw, ensure_ascii=False)
        except Exception:
            params = "{}"
        return (
            task.task_id, task.task_type, task.queue, int(task.priority),
            task.status.value if hasattr(task.status, "value") else str(task.status),
            params, task.dedup_key, task.source, task.source_event_id,
            task.correlation_id, task.causation_id, task.worker,
            int(task.attempts), int(task.max_retries),
            int(getattr(task, "manual_retries", 0) or 0),
            float(task.retry_interval), float(task.timeout_seconds),
            str(task.error or ""),
            1 if getattr(task, "interrupted", False) else 0,
            _iso(task.created_at), _iso(task.started_at), _iso(task.finished_at),
            _now_iso(), self.run_id,
        )

    def write(self, task: Any, immediate: bool = False) -> None:
        """登记一次状态变更。

        :param immediate: True 时立刻落盘（用于创建 / 死信 / 终态）。
        """
        if not self.enabled:
            return
        if getattr(task, "ephemeral", False):
            return  # 一次性函数型任务不留痕（参数含不可序列化的 callable）
        try:
            row = self.row_of(task)
        except Exception as exc:
            self.stats["last_error"] = str(exc)
            return
        with self._lock:
            self._dirty[task.task_id] = row
            self.stats["writes"] += 1
        if immediate:
            self.stats["immediate"] += 1
            self.flush()

    def flush(self) -> int:
        """把积压的变更合并成一个事务写掉。

        :return: 本次写入的行数
        """
        if not self.enabled or not self._opened:
            if self.enabled and not self._opened:
                self.open()
            if not self._opened:
                return 0
        with self._lock:
            if not self._dirty:
                return 0
            batch = list(self._dirty.values())
            self._dirty.clear()
        try:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return 0
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                try:
                    cur.executemany(_UPSERT, batch)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self.stats["flushes"] += 1
            self.stats["last_flush_at"] = _now_iso()
            return len(batch)
        except Exception as exc:
            # 写失败不能静默：把批次放回脏集合，下次再试，
            # 并在 stats 里留痕，让面板看得到"落盘坏了"。
            with self._lock:
                for row in batch:
                    self._dirty[row[0]] = row
            self.stats["last_error"] = str(exc)
            logger.error(f"任务流水账写入失败（{len(batch)} 行已保留待重试）: {exc}")
            return 0

    def log_dead(self, task: Any) -> None:
        """死信留痕：即使运维之后清空了内存里的死信，这里仍查得到。"""
        if not self.enabled or not self.open():
            return
        row = (
            task.task_id, task.task_type, task.queue, str(task.error or ""),
            int(task.attempts), int(task.max_retries),
            int(getattr(task, "manual_retries", 0) or 0),
            _now_iso(), None, None, self.run_id,
        )
        try:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return
                conn.execute(
                    "INSERT INTO dead_letter_log (task_id, task_type, queue, "
                    "error, attempts, max_retries, manual_retries, dead_at, "
                    "resolved_at, resolution, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                conn.commit()
        except Exception as exc:
            self.stats["last_error"] = str(exc)
            logger.error(f"死信留痕失败: {exc}")

    def resolve_dead(self, task_id: str, resolution: str) -> None:
        """记录人工处置结果（``retry`` / ``discard``）。"""
        if not self.enabled or not self.open():
            return
        try:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return
                conn.execute(
                    "UPDATE dead_letter_log SET resolved_at=?, resolution=? "
                    "WHERE task_id=? AND resolved_at IS NULL",
                    (_now_iso(), resolution, task_id),
                )
                conn.commit()
        except Exception as exc:
            self.stats["last_error"] = str(exc)

    # ============================================================
    # 读
    # ============================================================
    def _read(self, sql: str, args: Tuple[Any, ...] = ()) -> "List[Dict[str, Any]]":
        if not self.enabled or not self.open():
            return []
        # 读走独立连接：写连接可能正被 flush 线程占着，
        # 而且读操作不该被卷进写事务。
        try:
            conn = sqlite3.connect(self.path, timeout=30.0)
        except Exception as exc:
            self.stats["last_error"] = str(exc)
            return []
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            cur = conn.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as exc:
            self.stats["last_error"] = str(exc)
            logger.error(f"任务流水账读取失败: {exc}")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def load_open(self, limit: int = 500) -> "List[Dict[str, Any]]":
        """读出所有**未终结**的任务（重启恢复的输入）。"""
        marks = ",".join("?" for _ in OPEN_STATES)
        rows = self._read(
            f"SELECT * FROM engine_tasks WHERE status IN ({marks}) "
            f"ORDER BY priority ASC, created_at ASC LIMIT ?",
            (*OPEN_STATES, int(limit)),
        )
        for r in rows:
            r["params"] = self._decode_params(r.get("params"))
        return rows

    def load_dead(self, limit: int = 200) -> "List[Dict[str, Any]]":
        rows = self._read(
            "SELECT * FROM engine_tasks WHERE status='dead_letter' "
            "ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        )
        for r in rows:
            r["params"] = self._decode_params(r.get("params"))
        return rows

    @staticmethod
    def _decode_params(raw: Any) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def dead_history(self, limit: int = 100) -> "List[Dict[str, Any]]":
        """死信处置历史（内存里清空后仍能追溯）。"""
        return self._read(
            "SELECT * FROM dead_letter_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )

    def counts(self) -> Dict[str, Any]:
        """库里现在有多少条、各状态多少。面板的"持久化"卡读它。"""
        rows = self._read("SELECT status, COUNT(*) c FROM engine_tasks GROUP BY status")
        by_status = {r["status"]: r["c"] for r in rows}
        total = sum(by_status.values())
        oldest = self._read(
            "SELECT MIN(created_at) m FROM engine_tasks WHERE status IN "
            "('pending','queued','running','retrying')"
        )
        return {
            "enabled": self.enabled,
            "opened": self._opened,
            "path": self.path,
            "total": total,
            "by_status": by_status,
            "open": sum(by_status.get(s, 0) for s in OPEN_STATES),
            "dead": by_status.get("dead_letter", 0),
            "oldest_open_at": (oldest[0]["m"] if oldest else None),
            "pending_writes": len(self._dirty),
            "writes": self.stats["writes"],
            "flushes": self.stats["flushes"],
            "last_flush_at": self.stats["last_flush_at"],
            "last_error": self.stats["last_error"],
        }

    # ============================================================
    # 清理
    # ============================================================
    def delete(self, task_id: str) -> None:
        if not self.enabled or not self.open():
            return
        with self._lock:
            self._dirty.pop(task_id, None)
            conn = self._conn
            if conn is None:
                return
            try:
                conn.execute("DELETE FROM engine_tasks WHERE task_id=?", (task_id,))
                conn.commit()
            except Exception as exc:
                self.stats["last_error"] = str(exc)

    def prune_terminal(self, older_than_hours: float = 72.0, keep: int = 500) -> int:
        """清掉老的终态记录，**未终结与死信一条都不动**。

        终态任务留着只是占地方；未终结的是"还没干完的活"，死信是
        "等人处理的待办"——这两类绝不能被清理掉。
        """
        if not self.enabled or not self.open():
            return 0
        cutoff = time.time() - max(0.0, older_than_hours) * 3600.0
        cut_iso = datetime.fromtimestamp(cutoff).isoformat(timespec="seconds")
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            try:
                cur = conn.execute(
                    "DELETE FROM engine_tasks WHERE status IN "
                    "('success','failed','cancelled') AND updated_at < ? "
                    "AND task_id NOT IN ("
                    "  SELECT task_id FROM engine_tasks WHERE status IN "
                    "  ('success','failed','cancelled') "
                    "  ORDER BY updated_at DESC LIMIT ?)",
                    (cut_iso, int(keep)),
                )
                conn.commit()
                return int(cur.rowcount or 0)
            except Exception as exc:
                self.stats["last_error"] = str(exc)
                logger.error(f"清理终态任务记录失败: {exc}")
                return 0


def task_from_row(row: Dict[str, Any], task_cls: Any) -> Any:
    """把一行还原成 :class:`EngineTask`。

    放在这里而不是 models 里，是为了让 models 不必知道"还有个数据库"。
    """
    from app.runtime.tasking.models import TaskState

    raw_status = str(row.get("status") or "queued")
    try:
        status = TaskState(raw_status)
    except ValueError:
        status = TaskState.QUEUED
    task = task_cls(
        task_type=str(row.get("task_type") or ""),
        params=row.get("params") or {},
        queue=str(row.get("queue") or ""),
        priority=int(row.get("priority") or 5),
        dedup_key=str(row.get("dedup_key") or ""),
        source=str(row.get("source") or "recovered"),
        source_event_id=str(row.get("source_event_id") or ""),
        correlation_id=str(row.get("correlation_id") or ""),
        causation_id=str(row.get("causation_id") or ""),
        task_id=str(row.get("task_id") or ""),
        max_retries=int(row.get("max_retries") or 0),
        retry_interval=float(row.get("retry_interval") or 5.0),
        timeout_seconds=float(row.get("timeout_seconds") or 60.0),
    )
    task.status = status
    task.attempts = int(row.get("attempts") or 0)
    task.error = str(row.get("error") or "")
    task.worker = str(row.get("worker") or "")
    task.created_at = _parse_dt(row.get("created_at")) or datetime.now()
    task.started_at = _parse_dt(row.get("started_at"))
    task.finished_at = _parse_dt(row.get("finished_at"))
    task.manual_retries = int(row.get("manual_retries") or 0)
    task.interrupted = bool(row.get("interrupted"))
    return task


__all__ = ["TaskJournal", "task_from_row", "OPEN_STATES"]
