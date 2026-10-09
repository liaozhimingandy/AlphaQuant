#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : spec.py
# @Description : 多服务编排配置：config/services.json → ServiceSpec 列表
#               一个服务 = 一个独立进程 + 一份任务 + 一份快照目录 + 一个面板端口
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.core.config import settings
from app.utils.jsonio import read_json
from app.utils.logger import logger


@dataclass
class ServiceSpec:
    """单个后台服务的声明。"""

    id: str
    role: str = ""
    enabled: bool = True

    # 进程与面板
    port: Optional[int] = None
    host: str = ""
    namespace: str = ""

    # 引擎参数
    mode: str = "SIMULATE"
    market_mode: str = "poll"
    interval: Optional[float] = None
    start: str = ""
    end: str = ""
    data_source: str = "db"
    symbols: List[str] = field(default_factory=list)
    news: bool = False
    monitor: bool = True
    snapshot_interval: Optional[float] = None

    # 任务：inline 列表 或 引用外部配置文件
    tasks: List[Mapping[str, Any]] = field(default_factory=list)
    task_config: str = ""

    # 服务协作：发布/订阅的总线主题
    publish: List[str] = field(default_factory=list)
    subscribe: List[str] = field(default_factory=list)
    bus_interval: float = 1.0
    bus_seek_end: bool = False

    # 运维
    autorestart: bool = True
    max_restarts: int = 20
    env: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "enabled": self.enabled,
            "port": self.port,
            "host": self.host,
            "namespace": self.namespace or self.id,
            "mode": self.mode,
            "market_mode": self.market_mode,
            "interval": self.interval,
            "start": self.start,
            "end": self.end,
            "data_source": self.data_source,
            "symbols": list(self.symbols),
            "news": self.news,
            "monitor": self.monitor,
            "snapshot_interval": self.snapshot_interval,
            "task_count": len(self.tasks),
            "publish": list(self.publish),
            "subscribe": list(self.subscribe),
            "autorestart": self.autorestart,
        }


@dataclass
class HubSpec:
    services: List[ServiceSpec] = field(default_factory=list)
    host: str = ""
    port: Optional[int] = None
    state_dir: str = ""
    bus_dir: str = ""
    heartbeat_timeout: float = 0.0
    source: str = ""

    def enabled(self) -> List[ServiceSpec]:
        return [s for s in self.services if s.enabled]

    def get(self, service_id: str) -> Optional[ServiceSpec]:
        for s in self.services:
            if s.id == service_id:
                return s
        return None

    @property
    def state_root(self) -> Path:
        return Path(self.state_dir or settings.SERVICES_DIR)

    @property
    def bus_root(self) -> Path:
        return Path(self.bus_dir or settings.BUS_DIR)

    def service_dir(self, service_id: str) -> Path:
        return self.state_root / service_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host or settings.HUB_HOST,
            "port": self.port if self.port is not None else settings.HUB_PORT,
            "state_dir": str(self.state_root),
            "bus_dir": str(self.bus_root),
            "heartbeat_timeout": self.heartbeat_timeout or settings.SERVICE_HEARTBEAT_TIMEOUT,
            "services": [s.to_dict() for s in self.services],
            "source": self.source,
        }


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return []


def parse_service(raw: Mapping[str, Any]) -> ServiceSpec:
    sid = str(raw.get("id") or "").strip()
    if not sid:
        raise ValueError("服务配置缺少 id")

    tasks = raw.get("tasks") or []
    if isinstance(tasks, Mapping):
        tasks = tasks.get("tasks") or []
    if not isinstance(tasks, list):
        raise ValueError(f"服务 {sid} 的 tasks 必须是数组")

    mode = str(raw.get("mode") or "SIMULATE")
    # 回测模式的服务"跑完就该退出"，默认不自动重启；
    # 否则 supervisor 会把一个正常结束的回放服务无限拉起来。
    default_autorestart = mode.upper() != "BACKTEST"

    return ServiceSpec(
        id=sid,
        role=str(raw.get("role") or ""),
        enabled=bool(raw.get("enabled", True)),
        port=int(raw["port"]) if raw.get("port") is not None else None,
        host=str(raw.get("host") or ""),
        namespace=str(raw.get("namespace") or ""),
        mode=mode,
        market_mode=str(raw.get("market_mode") or "poll"),
        interval=float(raw["interval"]) if raw.get("interval") is not None else None,
        start=str(raw.get("start") or ""),
        end=str(raw.get("end") or ""),
        data_source=str(raw.get("data_source") or "db"),
        symbols=_as_list(raw.get("symbols")),
        news=bool(raw.get("news", False)),
        monitor=bool(raw.get("monitor", True)),
        snapshot_interval=(
            float(raw["snapshot_interval"]) if raw.get("snapshot_interval") is not None else None
        ),
        tasks=[dict(t) for t in tasks],
        task_config=str(raw.get("task_config") or ""),
        publish=_as_list(raw.get("publish")),
        subscribe=_as_list(raw.get("subscribe")),
        bus_interval=float(raw.get("bus_interval") or 1.0),
        bus_seek_end=bool(raw.get("bus_seek_end", False)),
        autorestart=bool(raw.get("autorestart", default_autorestart)),
        max_restarts=int(raw.get("max_restarts") or 20),
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
    )


def load_hub_spec(path: Optional[str | Path] = None) -> HubSpec:
    """读取 config/services.json。

    容错策略：单个服务配置有问题只跳过该服务并告警，不整体失败——
    编排文件写错一个服务不该让整台机器起不来。
    """
    file = Path(path or settings.SERVICES_CONFIG)
    if not file.exists():
        logger.warning(f"服务编排文件不存在: {file}")
        return HubSpec(source=str(file))

    raw = read_json(file, default=None)
    if not isinstance(raw, dict):
        raise ValueError(f"服务编排文件必须是 JSON object: {file}")

    hub = raw.get("hub") or {}
    spec = HubSpec(
        host=str(hub.get("host") or ""),
        port=int(hub["port"]) if hub.get("port") is not None else None,
        state_dir=str(raw.get("state_dir") or ""),
        bus_dir=str(raw.get("bus_dir") or ""),
        heartbeat_timeout=float(
            raw.get("heartbeat_timeout") or settings.SERVICE_HEARTBEAT_TIMEOUT
        ),
        source=str(file),
    )

    for item in raw.get("services") or []:
        try:
            spec.services.append(parse_service(item))
        except Exception as exc:
            logger.error(f"服务配置解析失败，已跳过: {exc}")
    return spec


__all__ = ["ServiceSpec", "HubSpec", "load_hub_spec", "parse_service"]
