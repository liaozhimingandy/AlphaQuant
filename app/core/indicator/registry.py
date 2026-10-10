#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : registry.py
# @Description : 指标注册表：让"加一个指标"变成一行装饰器，不用改任何框架代码
#
#                这里还承担两件容易被忽视、但直接影响性能与正确性的事：
#
#                1. **参数化复用**：`sma` 只有一个，周期靠参数传（`sma(period=20)`），
#                   而不是注册 `sma5 / sma10 / sma20` 三个指标。
#                   参数不同只是不同的调用，不是不同的指标。
#
#                2. **结果缓存**：指标是对整条序列做向量计算的，同一根K线上
#                   多个因子都要 MA20 时，重复算 N 次纯属浪费。
#                   缓存挂在 BarSeries 上，用 `revision` 判断失效 ——
#                   注意不能用 `len()`，deque 满了之后长度不变而内容在滑。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import inspect
import threading
import weakref
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar

import numpy as np

from app.core.indicator.base import IBaseIndicator
from app.core.market.series import BarSeries
from app.utils.logger import logger

T = TypeVar("T", bound=IBaseIndicator)

_INDICATORS: Dict[str, Type[IBaseIndicator]] = {}


def register_indicator(
    cls: Optional[Type[T]] = None, *, name: Optional[str] = None
) -> Callable:
    """注册指标类。可用作装饰器或普通函数。

    用法::

        @register_indicator
        class MyIndicator(IBaseIndicator):
            name = "my_ind"
            ...
    """

    def _do(klass: Type[T]) -> Type[T]:
        key = name or getattr(klass, "name", "")
        if not key:
            raise ValueError(f"指标 {klass.__name__} 必须定义 name 或显式传入 name=")
        if key in _INDICATORS and _INDICATORS[key] is not klass:
            logger.warning(f"指标 {key} 被覆盖: {_INDICATORS[key]} -> {klass}")
        _INDICATORS[key] = klass
        return klass

    return _do(cls) if cls is not None else _do


def create_indicator(name: str, **params) -> IBaseIndicator:
    """按名称创建指标实例。未注册时抛 KeyError，调用方负责降级。"""
    klass = _INDICATORS.get(name)
    if klass is None:
        raise KeyError(
            f"未注册的指标: {name} | 可用: {', '.join(sorted(_INDICATORS))}"
        )
    return klass(**params)


def has_indicator(name: str) -> bool:
    return name in _INDICATORS


def list_indicators() -> List[str]:
    return sorted(_INDICATORS)


def describe_indicator(name: str) -> Dict[str, Any]:
    """列出一个指标的可配参数（名字/类型/默认值）。

    直接从 ``__init__`` 签名反射出来，不额外维护一份"参数说明书"——
    那种手工清单迟早会和代码不一致。
    """
    klass = _INDICATORS.get(name)
    if klass is None:
        return {}
    try:
        sig = inspect.signature(klass.__init__)
    except (TypeError, ValueError):  # pragma: no cover
        return {}
    out: Dict[str, Any] = {}
    for pname, p in sig.parameters.items():
        if pname == "self" or p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        out[pname] = {
            "default": None if p.default is inspect.Parameter.empty else p.default,
            "required": p.default is inspect.Parameter.empty,
            "type": getattr(p.annotation, "__name__", None)
            if p.annotation is not inspect.Parameter.empty
            else None,
        }
    return out


def describe_all() -> Dict[str, Dict[str, Any]]:
    return {name: describe_indicator(name) for name in list_indicators()}


def indicator_registry() -> Dict[str, Type[IBaseIndicator]]:
    return dict(_INDICATORS)


def clear_indicators() -> None:
    """仅供测试使用。"""
    _INDICATORS.clear()


# ===========================================================================
# 参数化 + 结果缓存
# ===========================================================================
# 缓存挂在序列对象上（WeakKeyDictionary，序列被回收时条目自动消失，
# 不会因为长跑服务不断建临时序列而泄漏内存）。
_CACHE: "weakref.WeakKeyDictionary[BarSeries, Dict[str, Tuple[int, np.ndarray]]]" = (
    weakref.WeakKeyDictionary()
)
_CACHE_LOCK = threading.Lock()
_STATS: Dict[str, int] = {"hits": 0, "misses": 0, "evictions": 0}


def param_key(name: str, params: Dict[str, Any]) -> str:
    """把 (指标名, 参数) 压成一个稳定的缓存键。

    参数顺序必须不影响键：``sma(period=5, field="close")`` 与
    ``sma(field="close", period=5)`` 是同一个指标。不可哈希的值退化成 repr。
    """
    if not params:
        return name
    parts = []
    for k in sorted(params):
        v = params[k]
        try:
            hash(v)
            sv = repr(v)
        except TypeError:
            sv = repr(v)
        parts.append(f"{k}={sv}")
    return f"{name}({','.join(parts)})"


def indicator_series(
    series: BarSeries, name: str, *, force: bool = False, **params: Any
) -> Optional[np.ndarray]:
    """算（或取缓存里的）整条指标序列。

    同一根K线上多个因子要同一个指标时只算一次；K线一变（revision 变）就重算。

    :return: 与序列等长的数组；指标未注册或计算出错时返回 None
    """
    klass = _INDICATORS.get(name)
    if klass is None:
        return None

    key = param_key(name, params)
    rev = getattr(series, "revision", None)

    if not force and rev is not None:
        with _CACHE_LOCK:
            bucket = _CACHE.get(series)
            hit = bucket.get(key) if bucket else None
        if hit is not None and hit[0] == rev:
            _STATS["hits"] += 1
            return hit[1]

    try:
        arr = klass(**params).compute(series)
    except Exception as exc:
        # 一个指标算不出来不该让整个任务崩掉——上层按"无信号"处理
        logger.debug(f"指标 {key} 计算失败: {exc}")
        return None

    if rev is not None:
        with _CACHE_LOCK:
            bucket = _CACHE.get(series)
            if bucket is None:
                bucket = {}
                try:
                    _CACHE[series] = bucket
                except TypeError:  # pragma: no cover - 序列不可弱引用时降级为不缓存
                    return arr
            bucket[key] = (rev, arr)
        _STATS["misses"] += 1
    return arr


def indicator_value(
    series: BarSeries, name: str, idx: int = 0, **params: Any
) -> Optional[float]:
    """取单个值（idx=0 最新，idx=1 上一根）。走缓存，不足时返回 None。"""
    arr = indicator_series(series, name, **params)
    if arr is None or arr.size == 0:
        return None
    pos = arr.size - 1 - int(idx)
    if pos < 0:
        return None
    v = arr[pos]
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return float(v)


def cache_stats() -> Dict[str, int]:
    """缓存命中情况。命中率过低往往意味着每根K线都在造新的序列对象。"""
    with _CACHE_LOCK:
        return {**_STATS, "series_tracked": len(_CACHE)}


def clear_cache() -> None:
    """清空缓存。仅供测试与人工排障使用。"""
    with _CACHE_LOCK:
        _CACHE.clear()
        _STATS["evictions"] += 1


__all__ = [
    "cache_stats",
    "clear_cache",
    "clear_indicators",
    "create_indicator",
    "describe_all",
    "describe_indicator",
    "has_indicator",
    "indicator_registry",
    "indicator_series",
    "indicator_value",
    "list_indicators",
    "param_key",
    "register_indicator",
]

