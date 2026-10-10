#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : catalog.py
# @Description : 因子总目录：把所有可用因子按**类别**归拢展示
#
#                为什么需要它（注册表里明明已经有了）：
#                  注册表 `list_factors()` 返回的是一长串平铺的名字
#                  （现在 60+ 个）。用户看到的是一堆陌生的字符串，
#                  既不知道哪个是哪个类别，也不知道哪些是自己写的、
#                  哪些是采集来的数据才有值。
#
#                  结果就是"看起来有很多因子，实际只敢用自己认识的那几个"。
#                  因子的价值在于被用起来，而不是被列出来。
#
#                所以这里按类别分组，并给出每条因子的：
#                  - 所属类别（量价/题材/筹码/基本面/资金/舆情/宏观）
#                  - 来源（内置 / 因子库表达式 / 机器学习挖的）
#                  - 说明
#                  - **库里有没有数据**（没有的话用了也是 None）
#
#                最后一列最关键：很多人写完策略发现"怎么一直没信号"，
#                查半天才发现那个因子压根没采过数据。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import logger

#: 因子类别（顺序即界面展示顺序）
CAT_PRICE = "量价技术"
CAT_EXPR = "表达式因子"
CAT_THEME = "题材概念"
CAT_CHIP = "筹码结构"
CAT_CAPITAL = "资金类"
CAT_FUNDAMENTAL = "基本面"
CAT_SENTIMENT = "舆情"
CAT_MACRO = "宏观市场"
CAT_OTHER = "其它"

CATEGORY_DOCS: Dict[str, str] = {
    CAT_PRICE: "K线直接算出的技术指标（内置，只要有行情就能算）",
    CAT_EXPR: "因子库里自己写的表达式因子（config/factors.json）",
    CAT_THEME: "题材归属与题材热度（需采集题材数据）",
    CAT_CHIP: "筹码分布与获利盘（需采集筹码数据）",
    CAT_CAPITAL: "主力资金流、龙虎榜、两融、股东户数（需采集）",
    CAT_FUNDAMENTAL: "估值、成长、质量、业绩预告（需采集）",
    CAT_SENTIMENT: "新闻情感与关注度（需采集新闻）",
    CAT_MACRO: "宏观指标与市场宽度（市场级，所有票同值）",
    CAT_OTHER: "跨类别合成因子（如综合评分）",
}

#: 因子名 → 类别。没列出的归入 CAT_OTHER。
#: 这里刻意写成**显式表**而不是靠名字猜前缀：
#: 因子命名没有强制规范，靠猜迟早会分错，而分错的类别比不分类更糟。
FACTOR_CATEGORY: Dict[str, str] = {
    # ---- 量价技术 ----
    "price": CAT_PRICE, "ma": CAT_PRICE, "ma_spread": CAT_PRICE,
    "ma_bias": CAT_PRICE, "rsi": CAT_PRICE, "momentum": CAT_PRICE,
    "volatility": CAT_PRICE, "volume_ratio": CAT_PRICE,
    "drawdown": CAT_PRICE, "price_position": CAT_PRICE,
    "wma": CAT_PRICE, "wma_spread": CAT_PRICE,
    "wma_cross_up": CAT_PRICE, "wma_cross_down": CAT_PRICE,
    "wma_trend_up": CAT_PRICE, "wma_trend_down": CAT_PRICE,
    # ---- 题材 ----
    "theme_rank": CAT_THEME, "theme_pct": CAT_THEME,
    "theme_main_net": CAT_THEME,
    # ---- 筹码 ----
    "chip_profit_ratio": CAT_CHIP, "chip_concentration": CAT_CHIP,
    "chip_peak_gap": CAT_CHIP, "chip_peak_price": CAT_CHIP,
    # ---- 资金 ----
    "main_net": CAT_CAPITAL, "main_net_pct": CAT_CAPITAL,
    "capital_accel": CAT_CAPITAL, "capital_pulse": CAT_CAPITAL,
    "net_inflow_days": CAT_CAPITAL,
    "lhb_net_buy": CAT_CAPITAL, "lhb_buy_amount": CAT_CAPITAL,
    "margin_balance": CAT_CAPITAL, "short_balance": CAT_CAPITAL,
    "holder_count": CAT_CAPITAL, "holder_change_pct": CAT_CAPITAL,
    # ---- 基本面 ----
    "pe": CAT_FUNDAMENTAL, "pb": CAT_FUNDAMENTAL, "ps": CAT_FUNDAMENTAL,
    "dividend_yield": CAT_FUNDAMENTAL, "market_cap": CAT_FUNDAMENTAL,
    "float_market_cap": CAT_FUNDAMENTAL, "roe": CAT_FUNDAMENTAL,
    "gross_margin": CAT_FUNDAMENTAL, "net_margin": CAT_FUNDAMENTAL,
    "revenue_yoy": CAT_FUNDAMENTAL, "profit_yoy": CAT_FUNDAMENTAL,
    "debt_ratio": CAT_FUNDAMENTAL, "bps": CAT_FUNDAMENTAL,
    "forecast_yoy": CAT_FUNDAMENTAL, "rating_score": CAT_FUNDAMENTAL,
    "target_price": CAT_FUNDAMENTAL, "fundamental_score": CAT_FUNDAMENTAL,
    # ---- 舆情 ----
    "news_sentiment": CAT_SENTIMENT, "news_confidence": CAT_SENTIMENT,
    "news_impact": CAT_SENTIMENT, "news_count": CAT_SENTIMENT,
    "news_positive_ratio": CAT_SENTIMENT, "hot_rank": CAT_SENTIMENT,
    # ---- 宏观 / 市场 ----
    "macro_cpi": CAT_MACRO, "macro_pmi": CAT_MACRO, "macro_m2": CAT_MACRO,
    "macro_lpr": CAT_MACRO, "breadth_sentiment": CAT_MACRO,
    "breadth_up_ratio": CAT_MACRO,
    # ---- 其它 ----
    "composite_score": CAT_OTHER, "limitup_effect": CAT_THEME,
}

#: 哪些因子在库里有数据（按通用因子值表/其它表的类别判断）
_DATA_SOURCE: Dict[str, tuple] = {
    # 类别 → (检查函数用的参数)
    "lhb": ("lhb",), "margin": ("margin",), "forecast": ("forecast",),
    "holders": ("holders",), "rating": ("rating",),
}


def _library_names() -> Dict[str, Any]:
    """因子库里的表达式因子（name → spec）。"""
    try:
        from app.core.factor.library import get_library

        return {s.name: s for s in get_library().list()}
    except Exception:
        return {}


def _registry() -> Dict[str, Any]:
    try:
        from app.core.factor.registry import factor_registry

        return factor_registry()
    except Exception:
        return {}


#: 存在 StockFundamental 表里的字段（而不是通用因子值表）
FUNDAMENTAL_TABLE_FIELDS = {
    "pe", "pb", "ps", "dividend_yield", "market_cap", "float_market_cap",
    "roe", "gross_margin", "net_margin", "revenue_yoy", "profit_yoy",
    "debt_ratio", "bps",
}
#: 由资金流表派生的因子
FLOW_DERIVED = {"main_net", "main_net_pct", "capital_accel", "capital_pulse",
                "net_inflow_days"}


def _data_status() -> Dict[str, Any]:
    """一次性探明"哪些表里有数据"。

    每个表只查 1 行（limit 1），不做 count —— 只关心"有没有"，
    不关心有多少。这样即使库很大也是常数时间。
    """
    out: Dict[str, Any] = {
        "flow": False, "chip": False, "fundamental": False, "theme": False,
        "news": False, "macro": False, "breadth": False, "generic": set(),
    }
    try:
        from sqlalchemy import func as _func
        from sqlalchemy import select

        from app.db.database import SessionLocal
        from app.db.models import (
            ChipDistribution,
            FundFlow,
            MacroIndicator,
            MarketBreadth,
            MarketNews,
            StockFundamental,
            StockTheme,
            ThemeFlow,
        )

        with SessionLocal() as s:
            for key, model in (("flow", FundFlow), ("chip", ChipDistribution),
                               ("fundamental", StockFundamental),
                               ("theme", ThemeFlow), ("news", MarketNews),
                               ("macro", MacroIndicator),
                               ("breadth", MarketBreadth)):
                out[key] = s.execute(select(model).limit(1)).first() is not None
            # 题材归属单独一张表
            out["theme"] = out["theme"] or (
                s.execute(select(StockTheme).limit(1)).first() is not None)
            rows = s.execute(select(_func.count()).select_from(
                __import__("app.db.models", fromlist=["StockFactorValue"]
                           ).StockFactorValue).limit(1)).scalar()
            out["generic_count"] = int(rows or 0)
    except Exception as exc:
        logger.debug(f"探测数据可用性失败: {exc}")
    try:
        from app.repository import marketx_repository as R

        out["generic"] = {row["name"] for row in R.factor_categories()}
    except Exception as exc:
        logger.debug(f"读取通用因子值类别失败: {exc}")
    return out


def _has_data(name: str, category: str, status: Dict[str, Any]) -> Optional[bool]:
    """这个因子现在能不能算出值。

    ``False`` 意味着"用了也是 None" —— 这一列能省掉大量
    "为什么我的策略一直没信号"的排查时间。

    量价技术类只要有K线就能算，不该被标成"无数据" ——
    把明明能用的因子标灰，会让人以为系统坏了。
    """
    if category in (CAT_PRICE, CAT_EXPR):
        return True
    if category == CAT_OTHER:
        return None
    if name in FLOW_DERIVED:
        return status.get("flow", False)
    if name.startswith("chip_"):
        return status.get("chip", False)
    if name.startswith("theme_"):
        return status.get("theme", False)
    if name.startswith("news_"):
        return status.get("news", False)
    if name.startswith("macro_"):
        return status.get("macro", False)
    if name.startswith("breadth_"):
        return status.get("breadth", False)
    if name in FUNDAMENTAL_TABLE_FIELDS:
        return status.get("fundamental", False)
    if name in (status.get("generic") or set()):
        return True
    return False


def factor_catalog(with_data: bool = True) -> Dict[str, Any]:
    """按类别归拢的因子总目录。

    :param with_data: 是否检查"库里有没有数据"（会多查一次库，稍慢）
    """
    reg = _registry()
    lib = _library_names()
    status = _data_status() if with_data else {}

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for name in sorted(reg):
        klass = reg[name]
        spec = lib.get(name)
        sub = ""
        if spec is not None:
            # 库里的因子统一归到"表达式因子"，子分类只当标签 ——
            # 否则"动量/反转/量能…"会各成一个只有一个因子的小分组，
            # 界面上比不分组还难看。
            category = CAT_EXPR
            sub = spec.category or ""
            source = spec.source
            expr = spec.expr
            metrics = spec.metrics or {}
        else:
            category = FACTOR_CATEGORY.get(name, CAT_OTHER)
            source = "builtin"
            expr = getattr(klass, "_expr", "") or ""
            metrics = {}

        desc = getattr(klass, "description", "") or ""
        if with_data:
            has_data: Optional[bool] = _has_data(name, category, status)
        else:
            has_data = None

        groups.setdefault(category, []).append({
            "name": name,
            "description": desc,
            "source": source,
            "sub": sub,
            "expr": expr,
            "score": metrics.get("score"),
            "ic": metrics.get("ic"),
            "has_data": has_data,
        })

    out = {
        "categories": [
            {"key": k, "doc": CATEGORY_DOCS.get(k, ""),
             "factors": groups.get(k, [])}
            for k in _ordered_keys(groups)
        ],
        "total": sum(len(v) for v in groups.values()),
    }
    return out


def _ordered_keys(groups: Dict[str, List[Any]]) -> List[str]:
    """按预定义的类别顺序排，未知类别放最后。"""
    order = [CAT_PRICE, CAT_EXPR, CAT_THEME, CAT_CHIP, CAT_CAPITAL,
             CAT_FUNDAMENTAL, CAT_SENTIMENT, CAT_MACRO, CAT_OTHER]
    known = [k for k in order if k in groups]
    rest = sorted(k for k in groups if k not in order)
    return known + rest


def flat_names() -> List[str]:
    """给下拉框用的全部因子名（按类别排序，便于分 optgroup）。"""
    cat = factor_catalog(with_data=False)
    out: List[str] = []
    for g in cat["categories"]:
        out.extend(f["name"] for f in g["factors"])
    return out


__all__ = [
    "CATEGORY_DOCS",
    "FACTOR_CATEGORY",
    "factor_catalog",
    "flat_names",
]
