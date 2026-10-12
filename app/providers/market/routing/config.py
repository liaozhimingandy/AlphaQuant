#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : config.py
# @Description : Provider 配置（config/providers.json）
#
#                为什么路由顺序必须能配置：
#                  "哪个源先试" 这件事，一半是**常识**（baostock 稳就先试它），
#                  一半是**现场**（本机代理把东财拦了，那就该先试通达信）。
#                写死在代码里，每次网络环境变了都要改代码重启；
#                写在配置里，改个 JSON 重启即可，且事后能追"当时是怎么配的"。
#
#                配置只管三件事：谁启用、优先级多少、每类能力按什么顺序试。
#                **运行时实测出来的健康度比配置优先级权重更高**（见 registry.ranking）。
#
#                注：本项目其余配置都是 JSON，这里也保持一致 ——
#                不为了一份配置引入 PyYAML 依赖。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.common.logging import logger

#: 默认配置。**文件缺失时用这份**，所以删掉配置文件也不会开不了机。
DEFAULT_CONFIG: Dict[str, Any] = {
    "_comment": (
        "providers = 数据源身份的开关与优先级；"
        "routes = 每种能力按什么顺序尝试（未列出的路由排最后）；"
        "router = 路由器的重试/熔断/质量校验参数。"
        "想全部恢复默认，删掉本文件即可。"
    ),
    "providers": {
        "baostock": {"enabled": True, "priority": 90},
        "akshare": {"enabled": True, "priority": 80},
        "astockdata": {"enabled": True, "priority": 60},
        "mootdx": {"enabled": True, "priority": 55},
        "eastmoney": {"enabled": True, "priority": 100},
        "xueqiu": {"enabled": True, "priority": 70},
        "tushare": {"enabled": True, "priority": 50},
        "local": {"enabled": True, "priority": 10},
    },
    "routes": {
        # 日线：baostock 最稳（独立服务器、无需 token）→ akshare（东财，数据全）
        #      → 腾讯 → 通达信（离线/线上）。东财被拦时后面两个能顶上。
        "daily_kline": ["baostock_daily", "akshare_daily", "astockdata", "mootdx"],
        # 人气榜：emappdata 实测最稳 → 雪球（不同风控面）→ 东财 push2 → 全市场快照
        "hot_rank": ["emapp_hot", "xueqiu_hot", "akshare_em", "akshare_spot"],
        "flow_rank": ["akshare_flow"],
        "fund_flow": ["akshare", "tushare"],
    },
    "router": {
        "retries": 3,              # 每个源先原地重试几次（仅可重试的失败类型）
        "backoff_base": 0.5,       # 退避基数：0.5s → 1s → 2s
        "circuit_threshold": 3,    # 连续几次源级失败后熔断
        "cooldown": 300.0,         # 熔断冷却（秒）
        "quality_check": True,     # 取到数据后做质量校验
        "raw_enabled": True,       # 保存原始响应（Raw/Bronze 层）
        "raw_max_rows": 200,       # 每条原始响应最多存多少行
    },
}


def config_path() -> Path:
    try:
        from app.common.config import settings

        raw = os.getenv("PROVIDERS_CONFIG") or getattr(
            settings, "PROVIDERS_CONFIG", "")
        if raw:
            return Path(raw)
        return Path(settings.CONFIG_DIR) / "providers.json"
    except Exception:
        return Path("config") / "providers.json"


@dataclass
class ProviderConfig:
    """Provider 配置的强类型视图。"""

    providers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    routes: Dict[str, List[str]] = field(default_factory=dict)
    router: Dict[str, Any] = field(default_factory=dict)
    source: str = ""

    # ---------------- 读 ----------------
    def provider_settings(self, name: str) -> Dict[str, Any]:
        return dict(self.providers.get(str(name)) or {})

    def enabled(self, name: str) -> bool:
        """没写进配置 = 启用。禁用必须**显式**写出来，
        否则加一个新源还得记得同步配置，太容易漏。"""
        value = self.provider_settings(name).get("enabled")
        return True if value is None else bool(value)

    def priority(self, name: str, default: int) -> int:
        raw = self.provider_settings(name).get("priority")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(default)

    def route_order(self, capability: str) -> List[str]:
        return list(self.routes.get(str(capability)) or [])

    def router_value(self, key: str, default: Any) -> Any:
        value = self.router.get(key)
        return default if value is None else value

    # ---------------- 加载 ----------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "ProviderConfig":
        """读配置；读不到/写错就退回默认（**配置问题不能让取数链起不来**）。"""
        target = Path(path) if path else config_path()
        raw: Dict[str, Any] = {}
        try:
            if target.exists():
                parsed = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw = parsed
                else:
                    logger.warning(f"{target} 不是 JSON 对象，使用默认 Provider 配置")
        except Exception as exc:
            logger.warning(f"读取 {target} 失败（{type(exc).__name__}: {exc}），"
                           f"使用默认 Provider 配置")
            raw = {}

        merged = {
            "providers": {**DEFAULT_CONFIG["providers"], **(raw.get("providers") or {})},
            "routes": {**DEFAULT_CONFIG["routes"], **(raw.get("routes") or {})},
            "router": {**DEFAULT_CONFIG["router"], **(raw.get("router") or {})},
        }
        return cls(providers=merged["providers"], routes=merged["routes"],
                   router=merged["router"], source=str(target))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "providers": self.providers,
            "routes": self.routes,
            "router": self.router,
            "source": self.source,
        }


def load_config(path: Optional[str] = None) -> ProviderConfig:
    return ProviderConfig.load(path)


__all__ = ["DEFAULT_CONFIG", "ProviderConfig", "config_path", "load_config"]
