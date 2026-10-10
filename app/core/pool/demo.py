#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : demo.py
# @Description : 合成演示数据（离线可用）
#
#                用途只有一个：**在没有网络的环境里，也能验证"打分 → 排序 → 推荐"
#                这条链路是否真的在工作**。
#
#                刻意不用真实数据：
#                  - 数据获取（app/data/marketx.py）联网后单独用 `pool sync` 验证
#                  - 打分逻辑不该依赖网络 —— 否则每次调试都要看数据源脸色
#
#                生成的数据带**明确的强弱差异**（有几只故意做成"资金持续流入 +
#                热门题材 + 健康筹码 + 涨停"，有几只做成反面），
#                这样一眼就能看出排名是不是真的按逻辑排的。
#                如果演示里强弱排反了，那就是打分逻辑有问题，不是数据的问题。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.repository import marketx_repository as R


#: 演示用题材
THEMES = ["人工智能", "半导体", "新能源", "医药生物", "军工", "消费电子"]
#: 冷门题材（用于弱票）
COLD_THEMES = ["传统制造", "纺织服装"]


def seed_demo(count: int = 12, day: Any = None,
              seed: Optional[int] = 20261010) -> Dict[str, Any]:
    """造一批仿真的资金流/筹码/涨停/题材数据并入库。

    :return: {"symbols", "date", "strong": [...], "weak": [...]}
    """
    rng = random.Random(seed)
    d = _as_date(day) or date.today()
    count = max(2, int(count))

    strong: List[str] = []
    weak: List[str] = []
    symbols: List[str] = []

    # 一半强、一半弱，剩下的随机 —— 排名应该明显把强票排前面
    for i in range(count):
        sym = f"{600000 + i * 7:06d}"
        symbols.append(sym)
        if i % 3 == 0:
            _make_strong(sym, d, rng)
            strong.append(sym)
        elif i % 3 == 1:
            _make_weak(sym, d, rng)
            weak.append(sym)
        else:
            _make_mixed(sym, d, rng)

    _seed_themes(d, rng)
    return {
        "symbols": len(symbols),
        "date": str(d),
        "strong": strong,
        "weak": weak,
        "all": symbols,
    }


# ===========================================================================
def _flow_rows(sym: str, d: date, nets: List[float], close0: float,
               pcts: List[float]) -> List[Dict[str, Any]]:
    rows = []
    close = float(close0)
    for i, (net, pct) in enumerate(zip(nets, pcts)):
        close = round(close * (1 + pct / 100.0), 3)
        amount = 8e8 + abs(net) * 3
        rows.append({
            "symbol": sym,
            "date": d - timedelta(days=len(nets) - 1 - i),
            "close": close,
            "pct_change": pct,
            "main_net": net,
            "main_net_pct": net / amount,
            "amount": amount,
            "turnover": 5.0 + abs(pct),
        })
    return rows


def _make_strong(sym: str, d: date, rng: random.Random) -> None:
    """强势：连续净流入且加速 + 涨停 + 热门题材 + 健康筹码。"""
    nets = [0.8e8, 1.2e8, 1.8e8, 2.6e8, 4.5e8]
    pcts = [1.5, 2.0, 2.6, 3.4, 6.0]
    R.save_fund_flow(_flow_rows(sym, d, nets, 20.0, pcts))
    R.save_chip([{
        "symbol": sym, "date": d, "avg_cost": 19.0,
        "profit_ratio": round(rng.uniform(0.45, 0.68), 3),
        "concentration_90": round(rng.uniform(0.05, 0.10), 3),
        "peak_price": 21.0,
    }])
    board = rng.choice(THEMES)
    R.save_stock_themes([{"symbol": sym, "board": board, "kind": "concept",
                          "source": "demo", "date": d}])
    R.save_limitup([{
        "date": d, "symbol": sym, "name": f"演示{sym[-3:]}",
        "pct_change": 6.0, "close": 21.0, "amount": 12e8,
        "seal_amount": rng.uniform(2e8, 8e8),
        "continuous": rng.randint(1, 3), "broken_times": 0,
        "industry": board,
    }])
    # 基本面：好公司（低估值 + 高 ROE + 双增长）
    R.save_fundamentals({
        "symbol": sym, "date": d, "report_date": d - timedelta(days=40),
        "pe": round(rng.uniform(12, 22), 1),
        "pb": round(rng.uniform(1.5, 3.0), 2),
        "roe": round(rng.uniform(15, 24), 1),
        "gross_margin": round(rng.uniform(35, 55), 1),
        "net_margin": round(rng.uniform(12, 22), 1),
        "revenue_yoy": round(rng.uniform(18, 40), 1),
        "profit_yoy": round(rng.uniform(20, 45), 1),
        "debt_ratio": round(rng.uniform(25, 45), 1),
        "dividend_yield": round(rng.uniform(0.8, 2.5), 2),
        "market_cap": rng.uniform(2e11, 8e11),
    })


def _make_weak(sym: str, d: date, rng: random.Random) -> None:
    """弱势：持续净流出 + 冷题材 + 深套筹码。"""
    nets = [-1.5e8, -1.8e8, -2.2e8, -2.0e8, -3.0e8]
    pcts = [-2.0, -2.4, -1.8, -2.6, -3.0]
    R.save_fund_flow(_flow_rows(sym, d, nets, 30.0, pcts))
    R.save_chip([{
        "symbol": sym, "date": d, "avg_cost": 34.0,
        "profit_ratio": round(rng.uniform(0.02, 0.12), 3),
        "concentration_90": round(rng.uniform(0.30, 0.55), 3),
        "peak_price": 33.0,
    }])
    board = rng.choice(COLD_THEMES)
    R.save_stock_themes([{"symbol": sym, "board": board, "kind": "concept",
                          "source": "demo", "date": d}])
    # 基本面：差公司（亏损 + 高负债 + 双降）—— 基本面是过滤器，
    # 演示里必须让"强票"在这一维也站得住，否则看不出六维是不是都通了
    R.save_fundamentals({
        "symbol": sym, "date": d, "report_date": d - timedelta(days=40),
        "pe": round(rng.uniform(-30, -5), 1),
        "pb": round(rng.uniform(4, 9), 2),
        "roe": round(rng.uniform(-12, -2), 1),
        "gross_margin": round(rng.uniform(5, 14), 1),
        "net_margin": round(rng.uniform(-20, -5), 1),
        "revenue_yoy": round(rng.uniform(-28, -8), 1),
        "profit_yoy": round(rng.uniform(-45, -15), 1),
        "debt_ratio": round(rng.uniform(72, 92), 1),
        "dividend_yield": 0.0,
        "market_cap": rng.uniform(2e10, 8e10),
    })


def _make_mixed(sym: str, d: date, rng: random.Random) -> None:
    """中性：资金小幅波动，题材一般。"""
    nets = [n * 1e8 for n in (0.6, -0.3, 0.9, 0.4, 1.1)]
    pcts = [0.8, -0.6, 1.2, 0.5, 1.4]
    R.save_fund_flow(_flow_rows(sym, d, nets, 15.0, pcts))
    R.save_chip([{
        "symbol": sym, "date": d, "avg_cost": 15.0,
        "profit_ratio": round(rng.uniform(0.30, 0.60), 3),
        "concentration_90": round(rng.uniform(0.12, 0.28), 3),
        "peak_price": 15.2,
    }])
    board = rng.choice(THEMES[2:])
    R.save_stock_themes([{"symbol": sym, "board": board, "kind": "concept",
                          "source": "demo", "date": d}])
    R.save_fundamentals({
        "symbol": sym, "date": d, "report_date": d - timedelta(days=40),
        "pe": round(rng.uniform(25, 45), 1),
        "pb": round(rng.uniform(2.5, 5.0), 2),
        "roe": round(rng.uniform(6, 13), 1),
        "gross_margin": round(rng.uniform(18, 30), 1),
        "net_margin": round(rng.uniform(4, 10), 1),
        "revenue_yoy": round(rng.uniform(-2, 12), 1),
        "profit_yoy": round(rng.uniform(-8, 14), 1),
        "debt_ratio": round(rng.uniform(45, 62), 1),
        "dividend_yield": round(rng.uniform(0.3, 1.5), 2),
        "market_cap": rng.uniform(5e10, 3e11),
    })


def _seed_themes(d: date, rng: random.Random) -> None:
    rows = []
    for i, board in enumerate(THEMES):
        rows.append({
            "date": d, "board": board, "kind": "concept",
            "pct_change": round(rng.uniform(2.0, 7.0), 2),
            "main_net": rng.uniform(3e8, 12e8),
            "up_count": rng.randint(30, 90), "down_count": rng.randint(0, 15),
            "rank": i + 1,
        })
    for j, board in enumerate(COLD_THEMES):
        rows.append({
            "date": d, "board": board, "kind": "concept",
            "pct_change": round(rng.uniform(-3.0, -0.5), 2),
            "main_net": -rng.uniform(1e8, 4e8),
            "up_count": rng.randint(2, 12), "down_count": rng.randint(30, 60),
            "rank": 150 + j,
        })
    R.save_themes(rows)


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


__all__ = ["seed_demo"]
