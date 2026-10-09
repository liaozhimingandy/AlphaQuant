#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : filebus.py
# @Description : 文件消息总线：让多个独立进程的量化服务真正"互相配合"
#               发布者 append JSONL，订阅者按 byte offset 增量 tail —— 零依赖、可审计
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.utils.jsonio import json_safe, read_json, write_json_atomic
from app.utils.logger import logger

#: 单次 poll 最多读取多少字节。防止一个服务落后太久时一次性把内存打满
MAX_READ_BYTES = 8 * 1024 * 1024
#: 单个主题文件超过该大小就归档轮转，避免无限增长
MAX_TOPIC_BYTES = 64 * 1024 * 1024
#: 读取文件头部多少字节做"这个文件还是不是原来那个文件"的指纹
ANCHOR_BYTES = 128


def _safe_topic(topic: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(topic))
    return keep or "default"


def _fingerprint(head: bytes) -> str:
    if not head:
        return ""
    return hashlib.blake2b(head, digest_size=8).hexdigest()


class FileBus:
    """基于文件的消息总线。

    为什么不用真正的 MQ：
      - 量化服务天然是"低吞吐、强顺序、要能事后审计"的场景，文件足够
      - 零依赖、跨平台、进程崩了消息也不会丢（已 append 的就在磁盘上）
      - 出问题时直接 cat 文件就能看清楚发生了什么，排查成本最低

    语义：**至少一次**。订阅者用 byte offset 记录进度，只在成功解析后推进，
    进程重启后从上次位置继续；重复消费由下游幂等（新闻有去重、行情有 bar_count）兜住。
    """

    def __init__(
        self,
        base_dir: Optional[str | Path] = None,
        node_id: str = "anonymous",
        max_read_bytes: int = MAX_READ_BYTES,
    ) -> None:
        self.base_dir = Path(base_dir or settings.BUS_DIR)
        self.node_id = str(node_id or "anonymous")
        self.offset_dir = self.base_dir / ".offsets"
        self.max_read_bytes = int(max_read_bytes)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.offset_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = int(time.time() * 1000)
        self.stats: Dict[str, int] = {"published": 0, "consumed": 0, "skipped": 0, "errors": 0}

    # ============================================================
    # 路径
    # ============================================================
    def topic_path(self, topic: str) -> Path:
        return self.base_dir / f"{_safe_topic(topic)}.jsonl"

    def offset_path(self, topic: str) -> Path:
        return self.offset_dir / f"{self.node_id}__{_safe_topic(topic)}.json"

    def topics(self) -> List[str]:
        return sorted(
            p.stem for p in self.base_dir.glob("*.jsonl")
        )

    # ============================================================
    # 发布
    # ============================================================
    def publish(self, topic: str, payload: Any, kind: str = "") -> Dict[str, Any]:
        """追加一条消息。返回写入的记录（含 seq / ts）。"""
        path = self.topic_path(topic)
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": datetime.now().isoformat(),
                "node": self.node_id,
                "topic": _safe_topic(topic),
                "kind": kind or topic,
                "payload": json_safe(payload),
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
            self.stats["published"] += 1
        return record

    def publish_many(self, topic: str, payloads: List[Any], kind: str = "") -> int:
        n = 0
        for p in payloads:
            self.publish(topic, p, kind=kind)
            n += 1
        return n

    # ============================================================
    # 订阅（增量）
    # ============================================================
    def _read_offset_state(self, topic: str) -> Dict[str, Any]:
        """读进度文件全文（offset + 头部指纹）。"""
        data = read_json(self.offset_path(topic))
        return data if isinstance(data, dict) else {}

    def _read_offset(self, topic: str) -> int:
        try:
            return max(0, int(self._read_offset_state(topic).get("offset") or 0))
        except (TypeError, ValueError):
            return 0

    def _file_anchor(self, path: Path) -> str:
        """主题文件头部的指纹。

        为什么纯 byte offset 不够：offset 分不清"文件被追加了"和"文件被清空后
        重写成了差不多长"——后者在文件重新长到 ≥ 原 offset 之后看起来完全合法，
        订阅者会静默地读到**错位的数据**（半截 JSON、丢了消息）而不报错。
        存一个头部指纹，就能明确识别出"这已经是另一个文件了"，然后从头重放。
        """
        try:
            with path.open("rb") as f:
                head = f.read(ANCHOR_BYTES)
        except OSError:
            return ""
        return _fingerprint(head)

    def _write_offset(self, topic: str, offset: int, extra: Optional[Dict[str, Any]] = None) -> None:
        # 进度文件不 fsync：它是"至少一次"语义下的性能取舍——
        # 极端情况下丢了进度只会重放少量消息，而消息本身已经在磁盘上了。
        write_json_atomic(
            self.offset_path(topic),
            {
                "topic": _safe_topic(topic),
                "node": self.node_id,
                "offset": int(offset),
                "updated_at": datetime.now().isoformat(),
                **(extra or {}),
            },
            indent=None,
            fsync=False,
        )

    def seek_end(self, topic: str) -> int:
        """把进度直接推到文件末尾（只关心新消息时用，避免启动即回放整个积压）。"""
        path = self.topic_path(topic)
        size = path.stat().st_size if path.exists() else 0
        self._write_offset(topic, size, {"reason": "seek_end", "anchor": self._file_anchor(path)})
        return size

    def poll(self, topic: str, limit: int = 200, include_own: bool = False) -> List[Dict[str, Any]]:
        """从上次进度开始读取新消息。

        :param limit: 单次最多返回多少条（余下的下次 poll 继续，不会丢）
        :param include_own: 是否包含自己发布的消息（默认跳过，防止自激循环）
        """
        path = self.topic_path(topic)
        if not path.exists():
            return []

        with self._lock:
            state = self._read_offset_state(topic)
            try:
                offset = max(0, int(state.get("offset") or 0))
            except (TypeError, ValueError):
                offset = 0
            size = path.stat().st_size

            with path.open("rb") as f:
                anchor = _fingerprint(f.read(ANCHOR_BYTES))
                saved_anchor = state.get("anchor")
                if saved_anchor and anchor and saved_anchor != anchor:
                    # 文件被换掉/清空后重写过，偏移量已无意义
                    logger.warning(f"总线主题 {topic} 头部指纹变化（文件被重写），进度重置")
                    offset = 0
                if offset > size:
                    # 文件被轮转/截断，进度失效 → 从头开始
                    logger.warning(f"总线主题 {topic} 进度({offset}) 超过文件大小({size})，重置进度")
                    offset = 0
                if size - offset > self.max_read_bytes:
                    logger.warning(
                        f"总线主题 {topic} 积压 {size - offset} 字节，"
                        f"跳过错过的部分（只处理最新数据）"
                    )
                    offset = max(0, size - self.max_read_bytes)
                if offset >= size:
                    return []
                f.seek(offset)
                blob = f.read(self.max_read_bytes)

        # 只消费完整的行，半行留给下次（可能有并发写入）
        last_nl = blob.rfind(b"\n")
        if last_nl < 0:
            return []
        consumed = blob[: last_nl + 1]

        records: List[Dict[str, Any]] = []
        for raw in consumed.split(b"\n"):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except Exception:
                self.stats["errors"] += 1
                continue
            if not include_own and rec.get("node") == self.node_id:
                self.stats["skipped"] += 1
                continue
            records.append(rec)

        new_offset = offset + len(consumed)
        kept = records
        if limit and len(records) > limit:
            # 只消费 limit 条：把进度停在对应位置，剩下的下次再读
            kept = records[:limit]
            lines = consumed.split(b"\n")
            consumed_bytes = 0
            n = 0
            for raw in lines:
                if not raw.strip():
                    continue
                consumed_bytes += len(raw) + 1
                n += 1
                if n >= limit:
                    break
            new_offset = offset + consumed_bytes

        self._write_offset(topic, new_offset, {"anchor": anchor})
        self.stats["consumed"] += len(kept)
        return kept

    def reset_offset(self, topic: str) -> None:
        """把进度归零，下次 poll 会从头重放。"""
        self._write_offset(topic, 0, {"reason": "reset"})

    def tail(self, topic: str, n: int = 50) -> List[Dict[str, Any]]:
        """看最近 n 条（不影响进度），面板展示用。"""
        path = self.topic_path(topic)
        if not path.exists():
            return []
        from app.utils.jsonio import read_jsonl

        return read_jsonl(path, limit=n, newest_first=True)

    def offsets(self) -> Dict[str, int]:
        return {t: self._read_offset(t) for t in self.topics()}

    def sizes(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.topics():
            try:
                out[t] = self.topic_path(t).stat().st_size
            except OSError:
                out[t] = 0
        return out

    def snapshot(self) -> Dict[str, Any]:
        return {
            "node": self.node_id,
            "base_dir": str(self.base_dir),
            "topics": self.topics(),
            "offsets": self.offsets(),
            "sizes": self.sizes(),
            "stats": dict(self.stats),
        }

    def rotate_if_needed(self, topic: str, max_bytes: int = MAX_TOPIC_BYTES) -> bool:
        """主题文件过大时归档为 .1，并重置进度。"""
        path = self.topic_path(topic)
        if not path.exists() or path.stat().st_size < max_bytes:
            return False
        rotated = path.with_suffix(path.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            os.replace(path, rotated)
            self._write_offset(topic, 0, {"reason": "rotate"})
            logger.info(f"总线主题 {topic} 已归档到 {rotated.name}")
            return True
        except OSError as exc:
            logger.warning(f"总线主题 {topic} 归档失败: {exc}")
            return False


__all__ = ["FileBus", "MAX_TOPIC_BYTES", "MAX_READ_BYTES", "ANCHOR_BYTES"]
