#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : library.py
# @Description : 因子库：把"写好的因子"存下来，下次启动还在，且能直接被策略引用
#
#                为什么需要一个库（而不是散在各处的类）：
#                  Qbot 那句"策略是量化的核心，因子是策略的核心"落到实处就是
#                  **因子要能被积累**。每次研究出来的因子都沉淀进库里，
#                  下一次做策略时是从几十个已验证的候选里挑，
#                  而不是从零开始重新想。没有库，研究就不会有复利。
#
#                库里每条因子有四个来源（``source``）：
#                  builtin  框架自带（代码里的类，不写进这个文件）
#                  user     用户在界面/CLI 上写的
#                  mined    机器学习挖出来的（带检验指标，可追溯）
#                  preset   预置的经典因子（随包发布，可删）
#
#                入库前**必须**先通过表达式校验：一个写错的因子混进库里，
#                会在半夜回测时炸掉，而那时没人看得懂报错。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.common.config import settings
from app.modules.factor.expr import ExprError, validate
from app.common.logging import logger

FACTORS_FILE = "config/factors.json"

#: 因子来源
SOURCE_BUILTIN = "builtin"
SOURCE_USER = "user"
SOURCE_MINED = "mined"
SOURCE_PRESET = "preset"


@dataclass
class FactorSpec:
    """一条因子定义。"""

    name: str
    expr: str
    description: str = ""
    category: str = "自定义"
    source: str = SOURCE_USER
    enabled: bool = True
    #: 挖掘/检验留下的指标（ic / icir / 分层单调性 …），用于排序与追溯
    metrics: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        self.expr = str(self.expr or "").strip()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["FactorSpec"]:
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        expr = str(raw.get("expr") or "").strip()
        if not name or not expr:
            return None
        return cls(
            name=name, expr=expr,
            description=str(raw.get("description") or ""),
            category=str(raw.get("category") or "自定义"),
            source=str(raw.get("source") or SOURCE_USER),
            enabled=bool(raw.get("enabled", True)),
            metrics=dict(raw.get("metrics") or {}),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


class FactorLibrary:
    """因子库（config/factors.json）。

    加载即注册：库里每条启用的因子都会变成注册表里可用的因子，
    于是规则、策略、面板下拉框、回测全都能直接引用它，不需要改代码。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or settings.BASE_DIR / FACTORS_FILE)
        self.specs: Dict[str, FactorSpec] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ===================================================================
    # 加载 / 保存
    # ===================================================================
    def load(self, force: bool = False) -> "FactorLibrary":
        if self._loaded and not force:
            return self
        self.specs = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error(f"因子库解析失败（按空库启动）: {exc}")
                raw = {}
            items = raw.get("factors") if isinstance(raw, dict) else raw
            for item in items or []:
                spec = FactorSpec.from_dict(item)
                if spec is not None:
                    self.specs[spec.name] = spec
        self._loaded = True
        return self

    def save(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": [
                "因子库。界面/CLI 上写的因子存在这里，启动即注册，策略可直接引用。",
                "",
                "expr 支持的算子见 `python main.py factor ops`，变量见 `python main.py factor vars`。",
                "所有算子都是**因果**的（只用过去的数据），RANK 也是截至当前的百分位。",
                "",
                "source: builtin=框架自带 / user=手写的 / mined=机器学习挖的 / preset=预置经典",
            ],
            "factors": [s.to_dict() for s in self._sorted()],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(self.path)

    def _sorted(self) -> List[FactorSpec]:
        return sorted(self.specs.values(), key=lambda s: (s.category, s.name))

    # ===================================================================
    # 增删改
    # ===================================================================
    def add(self, name: str, expr: str, description: str = "",
            category: str = "自定义", source: str = SOURCE_USER,
            metrics: Optional[Dict[str, Any]] = None,
            overwrite: bool = False) -> Tuple[bool, str, Optional[FactorSpec]]:
        """新增一条因子。**先校验再入库**。

        :return: (是否成功, 说明, spec)
        """
        name = str(name or "").strip()
        expr = str(expr or "").strip()
        if not name:
            return False, "因子名不能为空", None
        if not expr:
            return False, "表达式不能为空", None
        if not _valid_name(name):
            return False, (f"因子名 {name!r} 不合法：只能用字母/数字/下划线，"
                           "且不能以数字开头"), None

        check = validate(expr)
        if not check.get("ok"):
            return False, f"表达式有错：{check.get('error')}", None

        with self._lock:
            self.load()
            exists = name in self.specs
            if exists and not overwrite:
                return False, f"因子 {name!r} 已存在（要覆盖请用 overwrite=True）", None
            now = datetime.now().isoformat(timespec="seconds")
            spec = FactorSpec(
                name=name, expr=expr, description=description,
                category=category, source=source,
                metrics=dict(metrics or {}),
                created_at=(self.specs[name].created_at if exists else now),
                updated_at=now,
            )
            self.specs[name] = spec
            self.save()
        _register_expression_factor(spec)
        return True, ("已更新" if exists else "已保存"), spec

    def remove(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            self.load()
            spec = self.specs.pop(str(name), None)
            if spec is None:
                return False, f"因子 {name!r} 不存在"
            self.save()
        _unregister_factor(str(name))
        return True, f"已删除因子 {name!r}"

    def get(self, name: str) -> Optional[FactorSpec]:
        self.load()
        return self.specs.get(str(name))

    def set_enabled(self, name: str, enabled: bool) -> Tuple[bool, str]:
        with self._lock:
            self.load()
            spec = self.specs.get(str(name))
            if spec is None:
                return False, f"因子 {name!r} 不存在"
            spec.enabled = bool(enabled)
            spec.updated_at = datetime.now().isoformat(timespec="seconds")
            self.save()
        if enabled:
            _register_expression_factor(spec)
        else:
            _unregister_factor(str(name))
        return True, f"因子 {name!r} 已{'启用' if enabled else '停用'}"

    def list(self, category: str = "", source: str = "") -> List[FactorSpec]:
        self.load()
        out = list(self.specs.values())
        if category:
            out = [s for s in out if s.category == category]
        if source:
            out = [s for s in out if s.source == source]
        return sorted(out, key=lambda s: s.name)

    def categories(self) -> List[str]:
        self.load()
        return sorted({s.category for s in self.specs.values()})

    # ===================================================================
    # 注册
    # ===================================================================
    def register_all(self) -> int:
        """把库里所有启用的因子注册进因子注册表。返回注册数量。"""
        self.load()
        n = 0
        for spec in self.specs.values():
            if spec.enabled and _register_expression_factor(spec):
                n += 1
        if n:
            logger.info(f"因子库已注册 {n} 个表达式因子（来自 {self.path.name}）")
        return n


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def _valid_name(name: str) -> bool:
    if not name:
        return False
    return bool(_NAME_RE.match(name))


# ===========================================================================
# 表达式因子 → 注册表
# ===========================================================================
def _register_expression_factor(spec: FactorSpec) -> bool:
    """把一条因子定义变成注册表里可用的因子类。

    每条因子要**各自一个子类**：注册表是 name→class，
    如果所有因子共用一个类，`create_factor(name)` 就不知道该用哪个表达式。
    """
    try:
        validate(spec.expr)
    except ExprError as exc:
        logger.warning(f"因子 {spec.name} 表达式非法，跳过注册: {exc}")
        return False

    from app.modules.factor.base import IBaseFactor
    from app.modules.factor.registry import _FACTORS, register_factor

    if spec.name in _FACTORS and getattr(_FACTORS[spec.name], "_expr", "") == spec.expr:
        return True     # 已注册且表达式没变

    class _ExprFactor(IBaseFactor):
        name = spec.name
        description = spec.description or spec.expr
        _expr = spec.expr

        def compute(self, ctx) -> Optional[float]:
            try:
                data = {
                    "open": ctx.series.open,
                    "high": ctx.series.high,
                    "low": ctx.series.low,
                    "close": ctx.series.close,
                    "volume": ctx.series.volume,
                    "amount": ctx.series.amount,
                }
                arr = evaluate_expr(spec.expr, data)
            except Exception as exc:
                logger.debug(f"因子 {spec.name} 计算失败: {exc}")
                return None
            if arr is None or len(arr) == 0:
                return None
            v = float(arr[-1])
            if v != v:          # NaN
                return None
            return v

    _ExprFactor.__name__ = f"ExprFactor_{spec.name}"
    _ExprFactor.__qualname__ = _ExprFactor.__name__
    register_factor(name=spec.name)(_ExprFactor)
    return True


def _unregister_factor(name: str) -> None:
    from app.modules.factor.registry import _FACTORS

    klass = _FACTORS.get(name)
    if klass is not None and getattr(klass, "_expr", None) is not None:
        _FACTORS.pop(name, None)


def evaluate_expr(expr: str, data: Dict[str, Sequence[float]]):
    """延迟导入，避免因子库模块被导入时就把表达式引擎拉起来。"""
    from app.modules.factor.expr import evaluate

    return evaluate(expr, data)


# ===========================================================================
# 单例
# ===========================================================================
_LIBRARY: Optional[FactorLibrary] = None


def get_library(reload: bool = False) -> FactorLibrary:
    global _LIBRARY
    if _LIBRARY is None or reload:
        lib = FactorLibrary()
        lib.load(force=True)
        lib.register_all()
        _LIBRARY = lib
    return _LIBRARY


#: 预置经典因子：随包发布，给用户一个起点（都能删）
PRESET_FACTORS: List[Dict[str, Any]] = [
    {
        "name": "mom_20", "expr": "(close / REF(close, 20) - 1) * 100",
        "description": "20 日动量（%）", "category": "动量",
    },
    {
        "name": "rev_5", "expr": "-(close / REF(close, 5) - 1) * 100",
        "description": "5 日反转（取负号：跌得多分数高）", "category": "反转",
    },
    {
        "name": "vol_ratio", "expr": "MA(volume, 5) / MA(volume, 20)",
        "description": "量能比：短期量 / 长期量", "category": "量能",
    },
    {
        "name": "volatility_20", "expr": "STD(close / REF(close, 1) - 1, 20) * 100",
        "description": "20 日波动率（%）", "category": "风险",
    },
    {
        "name": "ma_bias_20", "expr": "close / MA(close, 20) - 1",
        "description": "相对 20 日均线的乖离", "category": "均线",
    },
    {
        "name": "amplitude_10", "expr": "(TS_MAX(high, 10) - TS_MIN(low, 10)) / MA(close, 10)",
        "description": "10 日振幅/均价", "category": "波动",
    },
    {
        "name": "turnover_mom", "expr": "amount / MA(amount, 20)",
        "description": "成交额放量倍数", "category": "量能",
    },
    {
        "name": "price_pos_20", "expr": "(close - TS_MIN(low, 20)) / (TS_MAX(high, 20) - TS_MIN(low, 20))",
        "description": "20 日区间位置 0~1", "category": "位置",
    },
]


def install_presets(overwrite: bool = False) -> int:
    """写入预置因子。只补不覆盖（用户可能改过）。"""
    lib = get_library()
    n = 0
    for item in PRESET_FACTORS:
        ok, _, _ = lib.add(source=SOURCE_PRESET, overwrite=overwrite, **item)
        if ok:
            n += 1
    return n


__all__ = [
    "FACTORS_FILE",
    "PRESET_FACTORS",
    "SOURCE_BUILTIN",
    "SOURCE_MINED",
    "SOURCE_PRESET",
    "SOURCE_USER",
    "FactorLibrary",
    "FactorSpec",
    "get_library",
    "install_presets",
]
