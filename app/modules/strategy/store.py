#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : store.py
# @Description : 用户自定义策略库（config/strategies.json）
#
#                为什么需要它（已有 strategies/ 目录放 Python 文件了）：
#                  写 Python 文件是"开发者路径"，而绝大多数策略调整其实是
#                  **改几个参数或者换一个因子** —— 为了把阈值从 30 改成 35
#                  去改一个 .py 文件再热加载，摩擦太大，最后的结果就是
#                  大家都不去调，策略一旦写完就再也不动。
#
#                所以这里存的是**声明式**策略：一段配置，不是一段代码。
#                  - 界面上能直接编辑（下拉选因子、填阈值）
#                  - 保存即生效，不用碰文件、不用重启
#                  - 能被回测（declarative 链路）也能被实盘直接用
#                  - 仍然可以"一键上线为任务"，接到实盘链路
#
#                需要写复杂逻辑时，仍然走 strategies/ 写 Python ——
#                两条路径并存，各自解决各自的问题。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.common.config import settings
from app.common.logging import logger

STRATEGIES_FILE = "config/strategies.json"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")

#: 支持的策略类型（界面按这个渲染不同的参数表单）
KIND_RULE = "declarative"      # 规则组合：因子 + 比较 + 阈值
KIND_SCORE = "score"           # 综合评分阈值
KIND_MULTI = "multi_factor"    # 多因子加权

KINDS: Dict[str, Dict[str, str]] = {
    KIND_RULE: {
        "name": "规则策略",
        "doc": "选一个因子 + 比较运算 + 阈值，例如 综合评分 > 70 买入",
    },
    KIND_SCORE: {
        "name": "综合评分",
        "doc": "按六/八维综合分决策：≥ entry 买入，≤ exit 卖出",
    },
    KIND_MULTI: {
        "name": "多因子加权",
        "doc": "把因子库里的因子标准化后加权求和，按阈值开平仓",
    },
}


@dataclass
class UserStrategy:
    """一条用户自定义策略。"""

    name: str
    kind: str = KIND_RULE
    #: 策略参数（结构随 kind 不同，见 to_task_strategy）
    spec: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    #: 默认风控链（上线为任务时带上）
    risk: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["UserStrategy"]:
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            kind=str(raw.get("kind") or KIND_RULE),
            spec=dict(raw.get("spec") or {}),
            description=str(raw.get("description") or ""),
            risk=[dict(r) for r in (raw.get("risk") or []) if isinstance(r, dict)],
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )

    # ------------------------------------------------------------------
    def to_task_strategy(self) -> Dict[str, Any]:
        """转成 tasks.json / 控制面能吃的 strategy 结构。

        三种 kind 生成的形状不同，这是**唯一**做这个转换的地方 ——
        界面、CLI、回测都调用它，避免三处各写一份然后漂移。
        """
        if self.kind == KIND_SCORE:
            return {
                "type": "score",
                "params": {
                    "entry_score": float(self.spec.get("entry_score", 70.0)),
                    "exit_score": float(self.spec.get("exit_score", 55.0)),
                },
            }
        if self.kind == KIND_MULTI:
            return {
                "type": "multi_factor",
                "params": {
                    "factors": list(self.spec.get("factors") or []),
                    "entry": float(self.spec.get("entry", 0.55)),
                    "exit": float(self.spec.get("exit", -0.30)),
                },
            }
        # declarative：把"因子 比较 阈值"拼成规则
        return {
            "type": "declarative",
            "params": {
                "entry": self._rule("entry"),
                "exit": self._rule("exit"),
            },
        }

    def _rule(self, side: str) -> Any:
        """把一侧（entry/exit）的配置转成规则节点。

        支持三种写法，**同一个入口**处理：

        1. 单条：``{"factor": "mom_20", "op": "gt", "value": 3}``
        2. 组合：``{"combine": "all", "rules": [ {...}, {...} ]}``
        3. 嵌套：``{"combine": "any", "rules": [ {...}, {"combine":"all", ...} ]}``

        第 3 种让人能表达"资金流好 **或者**（题材热 **且** 涨停）"
        这类真实的条件，而不是只能写一条平铺的规则。
        """
        r = self.spec.get(side)
        return _node(r)


def _node(r: Any) -> Any:
    """递归把配置节点转成规则结构。"""
    if not isinstance(r, dict):
        return "never"
    rules = r.get("rules")
    if isinstance(rules, list) and rules:
        combine = str(r.get("combine") or "all").lower()
        if combine not in ("all", "any"):
            combine = "all"
        children = [_node(x) for x in rules]
        children = [c for c in children if c != "never"]
        if not children:
            return "never"
        if len(children) == 1:
            return children[0]
        return {combine: children}

    if not r.get("factor"):
        return "never"
    node: Dict[str, Any] = {
        "factor": str(r["factor"]),
        "op": str(r.get("op") or "gt"),
        "value": float(r.get("value") or 0.0),
    }
    params = r.get("params") or {}
    if params:
        node["params"] = dict(params)
    return node


class StrategyStore:
    """用户策略库。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or settings.BASE_DIR / STRATEGIES_FILE)
        self.items: Dict[str, UserStrategy] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    def load(self, force: bool = False) -> "StrategyStore":
        if self._loaded and not force:
            return self
        self.items = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error(f"策略库解析失败（按空库启动）: {exc}")
                raw = {}
            got = raw.get("strategies") if isinstance(raw, dict) else raw
            for item in got or []:
                s = UserStrategy.from_dict(item)
                if s is not None:
                    self.items[s.name] = s
        self._loaded = True
        return self

    def save(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": [
                "用户自定义策略库。界面「策略」页编辑的内容存在这里。",
                "",
                "kind: declarative=规则 / score=综合评分 / multi_factor=多因子加权",
                "risk: 上线为任务时默认带的风控链，可留空。",
                "",
                "需要写复杂逻辑时，仍然去 strategies/ 写 Python 文件 —— 两条路并存。",
            ],
            "strategies": [s.to_dict() for s in
                           sorted(self.items.values(), key=lambda x: x.name)],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(self.path)

    # ------------------------------------------------------------------
    def add(self, name: str, kind: str, spec: Dict[str, Any],
            description: str = "", risk: Optional[List[Dict[str, Any]]] = None,
            overwrite: bool = True) -> Tuple[bool, str, Optional[UserStrategy]]:
        name = str(name or "").strip()
        if not name:
            return False, "策略名不能为空", None
        if not _NAME_RE.match(name):
            return False, (f"策略名 {name!r} 不合法：只能用字母/数字/下划线，"
                           "且不能以数字开头"), None
        kind = str(kind or KIND_RULE)
        if kind not in KINDS:
            return False, (f"未知策略类型 {kind!r} | "
                           f"可用: {', '.join(KINDS)}"), None
        err = _validate_spec(kind, spec or {})
        if err:
            return False, err, None

        self.load()
        now = datetime.now().isoformat(timespec="seconds")
        s = UserStrategy(
            name=name, kind=kind, spec=dict(spec or {}),
            description=str(description or ""),
            risk=[dict(r) for r in (risk or []) if isinstance(r, dict)],
            created_at=(self.items[name].created_at if name in self.items else now),
            updated_at=now,
        )
        self.items[name] = s
        self.save()
        return True, ("已更新" if name in self.items else "已保存"), s

    def remove(self, name: str) -> Tuple[bool, str]:
        self.load()
        if self.items.pop(str(name), None) is None:
            return False, f"策略 {name!r} 不存在"
        self.save()
        return True, f"已删除策略 {name!r}"

    def get(self, name: str) -> Optional[UserStrategy]:
        self.load()
        return self.items.get(str(name))

    def list(self) -> List[UserStrategy]:
        self.load()
        return sorted(self.items.values(), key=lambda s: s.name)


def _validate_spec(kind: str, spec: Dict[str, Any]) -> str:
    """保存前的结构校验。

    界面上填错的地方要**立刻**指出来，而不是等上线运行时才发现
    （那时可能已经在真金白银地下单了）。
    """
    if kind == KIND_SCORE:
        entry = float(spec.get("entry_score", 70.0) or 0)
        exit_ = float(spec.get("exit_score", 55.0) or 0)
        if exit_ >= entry:
            return f"exit_score({exit_}) 必须小于 entry_score({entry})，否则刚买就卖"
        return ""
    if kind == KIND_MULTI:
        if float(spec.get("exit", -0.3)) >= float(spec.get("entry", 0.55)):
            return "exit 必须小于 entry"
        return ""
    # declarative：至少要有一条**能展开成规则**的入场或出场条件。
    # 注意要走 _node() 展开 —— 组合写法下顶层没有 "factor"，
    # 只看 factor 会把合法的组合配置误判成空。
    from app.modules.strategy.store import _node

    for side in ("entry", "exit"):
        if _node(spec.get(side)) != "never":
            return ""
    return "规则策略至少要配一条有效的入场或出场规则（因子不能为空）"


_STORE: Optional[StrategyStore] = None


def get_store(reload: bool = False) -> StrategyStore:
    global _STORE
    if _STORE is None or reload:
        s = StrategyStore()
        s.load(force=True)
        _STORE = s
    return _STORE


__all__ = [
    "KINDS",
    "KIND_MULTI",
    "KIND_RULE",
    "KIND_SCORE",
    "STRATEGIES_FILE",
    "StrategyStore",
    "UserStrategy",
    "get_store",
]
