#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : endpoints.py
# @Description : 券商接入点配置：把"接哪家券商、用哪个资金账号、连哪个前置"
#               变成配置，而不是改代码
#
#                为什么需要这一层：
#                  「BROKER_GATEWAY=simulated」只回答了"用哪种网关实现"，
#                  没回答"连谁"。真实场景里会有：
#                    模拟托盘 / 实盘主账户 / 实盘小号 / 另一家券商的备份通道
#                  —— 它们是**同一个网关类型、不同接入点**。
#                  把这些参数写死在代码或环境变量里，切换成本高且容易连错账户
#                  （连错账户是实盘里代价最高的错误之一）。
#
#                凭据刻意**不落配置文件**：配置里只写"去哪个环境变量取"，
#                真正的密码放在环境变量或密钥管理里。这样 brokers.json 可以入库。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.core.config import settings
from app.utils.logger import logger

#: 配置里可以写、但**不该出现在日志/面板**的字段名（用于兜底脱敏）
_SECRET_HINTS = ("password", "pwd", "secret", "token", "key", "passwd")


def mask_secret(value: str) -> str:
    """把凭据打码。空值返回空串，便于面板显示"未配置"。"""
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}{'*' * max(2, len(s) - 4)}{s[-2:]}"


@dataclass
class BrokerEndpoint:
    """一个券商接入点。

    :param id: 接入点标识（配置里的 key），CLI/面板用它指定"用哪个"
    :param gateway: 网关实现名（见 :func:`app.core.execution.gateway.list_gateways`）
    :param account: 资金账号。**同一个网关类型可以对应多个资金账号**，
                    所以账号属于接入点而不是网关类型
    :param readonly: 只读档位。首次接入建议先挂只读跑一段 ——
                     能拉账户/持仓/回报，但不下单，用来验证账本对不对
    :param credential_env: 字段名 → 环境变量名。例如 ``{"password": "HT_PASSWORD"}``
    :param params: 透传给网关构造函数的其它参数（host/port/前端地址等）
    """

    id: str
    gateway: str = "simulated"
    name: str = ""
    account: str = ""
    enabled: bool = True
    readonly: bool = False
    note: str = ""
    credential_env: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    #: 解析出来的凭据，**只在内存里**，不落盘、不进快照
    credentials: Dict[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    @property
    def label(self) -> str:
        return self.name or self.id

    def resolve_credentials(self, env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """按 ``credential_env`` 从环境变量取值。缺失时告警但不抛错 ——

        有些网关（比如本地模拟）本来就不需要凭据，硬性报错会挡住"先用模拟跑通"。
        真正需要凭据的网关会在 ``connect()`` 里自己失败。
        """
        source = env if env is not None else os.environ
        out: Dict[str, str] = {}
        missing: List[str] = []
        for field_name, env_name in self.credential_env.items():
            val = str(source.get(env_name, "") or "")
            if val:
                out[field_name] = val
            else:
                missing.append(env_name)
        if missing:
            logger.warning(
                f"接入点 {self.id} 缺少凭据环境变量: {', '.join(missing)}"
                f"（若该网关不需要凭据可忽略）"
            )
        self.credentials = out
        return out

    def gateway_kwargs(self, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        """构造网关时传入的关键字参数。"""
        creds = self.credentials or self.resolve_credentials(env)
        kw: Dict[str, Any] = dict(self.params or {})
        kw.setdefault("account", self.account)
        kw.setdefault("readonly", bool(self.readonly))
        kw.setdefault("endpoint_id", self.id)
        kw.update(creds)
        return kw

    def to_dict(self, mask: bool = True) -> Dict[str, Any]:
        """给面板/CLI 的结构。默认脱敏 —— 打码漏一次就是事故。"""
        creds: Dict[str, Any] = {}
        for k, v in (self.credentials or {}).items():
            creds[k] = mask_secret(v)
        for k, env_name in self.credential_env.items():
            creds.setdefault(k, {
                "env": env_name,
                "set": bool(os.environ.get(env_name)),
            })
        params = {
            k: (mask_secret(v) if any(h in k.lower() for h in _SECRET_HINTS) else v)
            for k, v in (self.params or {}).items()
        }
        return {
            "id": self.id,
            "name": self.label,
            "gateway": self.gateway,
            "account": self.account,
            "enabled": self.enabled,
            "readonly": self.readonly,
            "note": self.note,
            "params": params,
            "credentials": creds if mask else dict(self.credentials or {}),
        }

    @classmethod
    def from_dict(cls, endpoint_id: str, raw: Mapping[str, Any]) -> "BrokerEndpoint":
        # 显式写了空串 ≠ 没写。前者说明配置还没填完，要保持空
        # 让上层给出"这个接入点没配网关"的清楚报错，而不是悄悄用模拟网关顶上。
        raw_gateway = raw.get("gateway")
        gateway = "simulated" if raw_gateway is None else str(raw_gateway).strip().lower()
        return cls(
            id=str(endpoint_id).strip(),
            gateway=gateway,
            name=str(raw.get("name") or "").strip(),
            account=str(raw.get("account") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            readonly=bool(raw.get("readonly", False)),
            note=str(raw.get("note") or ""),
            credential_env={
                str(k): str(v)
                for k, v in dict(raw.get("credential_env") or {}).items()
            },
            params=dict(raw.get("params") or {}),
        )


@dataclass
class EndpointsConfig:
    """brokers.json 的全部内容。"""

    endpoints: Dict[str, BrokerEndpoint] = field(default_factory=dict)
    default_endpoint: str = ""
    path: str = ""

    def get(self, endpoint_id: str = "") -> Optional[BrokerEndpoint]:
        key = str(endpoint_id or "").strip() or self.default_endpoint
        if not key:
            return None
        return self.endpoints.get(key)

    def resolve(self, endpoint_id: str = "") -> Optional[BrokerEndpoint]:
        """取接入点；**显式指定的找不到时报错**，默认的找不到时返回 None。

        区别对待的原因：显式指定而找不到，说明用户以为自己连的是 A，
        实际会连到别的地方（或连不上）—— 静默降级是实盘最危险的行为。
        """
        wanted = str(endpoint_id or "").strip()
        if wanted:
            ep = self.endpoints.get(wanted)
            if ep is None:
                raise KeyError(
                    f"未找到券商接入点 {wanted!r} | 可用: "
                    f"{', '.join(sorted(self.endpoints)) or '（无）'}"
                )
            return ep
        return self.get("")

    def list(self, only_enabled: bool = False) -> List[BrokerEndpoint]:
        items = [e for e in self.endpoints.values() if (e.enabled or not only_enabled)]
        return sorted(items, key=lambda e: e.id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "default_endpoint": self.default_endpoint,
            "endpoints": [e.to_dict() for e in self.list()],
        }


def load_endpoints(path: Optional[str] = None) -> EndpointsConfig:
    """读取券商接入点配置。文件不存在返回空配置（不是错误）。

    ``config/brokers.json`` 不存在时，回退到"用环境变量 BROKER_GATEWAY
    构造一个临时接入点"，这样老用法（只配网关名）仍然可用。
    """
    target = Path(path or settings.BROKER_ENDPOINTS_CONFIG)
    cfg = EndpointsConfig(path=str(target))
    if not target.exists():
        fallback = str(settings.BROKER_GATEWAY or "").strip()
        if fallback:
            cfg.endpoints["env"] = BrokerEndpoint(
                id="env", gateway=fallback, name="来自环境变量 BROKER_GATEWAY",
                note="未找到 brokers.json，按环境变量构造",
            )
            cfg.default_endpoint = "env"
        return cfg

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"券商接入点配置解析失败（将按未配置处理）: {exc}")
        return cfg

    if not isinstance(raw, Mapping):
        logger.error(f"券商接入点配置必须是 JSON 对象，收到 {type(raw).__name__}")
        return cfg

    for key, item in dict(raw.get("endpoints") or {}).items():
        if not isinstance(item, Mapping):
            logger.warning(f"接入点 {key} 配置不是对象，已跳过")
            continue
        ep = BrokerEndpoint.from_dict(str(key), item)
        cfg.endpoints[ep.id] = ep

    cfg.default_endpoint = str(
        raw.get("default_endpoint") or settings.BROKER_ENDPOINT or ""
    ).strip()
    if not cfg.default_endpoint and cfg.endpoints:
        # 没显式指定就取第一个启用的 —— 只有一个接入点时这是最自然的语义
        enabled = [e.id for e in cfg.list(only_enabled=True)]
        cfg.default_endpoint = enabled[0] if enabled else ""
    return cfg


def describe_endpoints(path: Optional[str] = None) -> Dict[str, Any]:
    """给面板用的接入点概览（全部脱敏）。"""
    cfg = load_endpoints(path)
    active = cfg.default_endpoint
    data = cfg.to_dict()
    data["active"] = active
    data["active_exists"] = bool(cfg.get(active))
    return data


__all__ = [
    "BrokerEndpoint",
    "EndpointsConfig",
    "describe_endpoints",
    "load_endpoints",
    "mask_secret",
]
