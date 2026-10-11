#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : features.py
# @Description : 个股扩展特征装配：把"资金流 / 筹码 / 题材 / 涨停 / 评分"拼成一个
#                可以直接喂给因子层与打分引擎的结构
#
#                为什么需要这一层：
#                  这几类数据**不在 BarSeries 里**（它们是日频截面数据，不是逐根K线）。
#                  如果让每个因子自己去查库，一次打分会打几十次同样的查询 ——
#                  而且查不到时各自处理，口径必然不一致。
#
#                所以：这里**一次查全、算好派生量、缓存**，
#                因子与打分引擎只消费结果。
#
#                派生量的口径（这些才是"大资金在快速进场"的判据）：
#                  - ``net_days``      连续主力净流入天数（趋势性）
#                  - ``accel``         近 3 日均值 / 前 3 日均值（加速度）
#                  - ``pulse``         当日主力净额 / 过去 N 日均值（脉冲强度）
#                单看一天的净流入很容易被骗：一只票某天净流入 1 亿，
#                可能是主力在出货前的对倒。看"连续 + 加速 + 放量"才站得住。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from app.common.logging import logger


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        v = float(value)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


@dataclass
class StockFeatures:
    """一只票在某交易日的扩展特征。

    ``available`` 明确记录**哪些维度真的有数据**。
    没有数据就记为 False，打分引擎据此把该维度**排除**而不是按 0 算 ——
    0 分会被当成"这项很差"，那是凭空制造出来的假信号。
    """

    symbol: str
    date: Optional[date] = None

    # ---- 资金流 ----
    main_net: Optional[float] = None            # 当日主力净流入（元）
    main_net_pct: Optional[float] = None        # 占成交额比例
    net_days: int = 0                           # 连续净流入天数
    accel: Optional[float] = None               # 加速度：近3日/前3日
    pulse: Optional[float] = None               # 脉冲：今日/过去N日均值
    turnover: Optional[float] = None
    amount: Optional[float] = None

    # ---- 筹码 ----
    avg_cost: Optional[float] = None
    profit_ratio: Optional[float] = None        # 获利盘 0~1
    chip_concentration: Optional[float] = None  # 越小越集中
    peak_price: Optional[float] = None
    peak_gap: Optional[float] = None            # (现价 - 筹码峰)/筹码峰

    # ---- 题材 ----
    themes: List[str] = field(default_factory=list)
    theme_rank: Optional[int] = None            # 所属最热题材的当日排名
    theme_pct: Optional[float] = None           # 该题材当日涨幅
    theme_main_net: Optional[float] = None      # 该题材当日主力净额

    # ---- 涨停 ----
    is_limitup: bool = False
    continuous: int = 0                         # 连板数
    seal_amount: Optional[float] = None         # 封板资金
    broken_times: int = 0

    # ---- 基本面 ----
    pe: Optional[float] = None                  # 市盈率 TTM
    pb: Optional[float] = None                  # 市净率
    ps: Optional[float] = None
    dividend_yield: Optional[float] = None      # 股息率 %
    market_cap: Optional[float] = None          # 总市值（元）
    float_market_cap: Optional[float] = None
    roe: Optional[float] = None                 # 净资产收益率 %
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_yoy: Optional[float] = None         # 营收同比 %
    profit_yoy: Optional[float] = None          # 净利同比 %
    debt_ratio: Optional[float] = None          # 资产负债率 %
    bps: Optional[float] = None
    report_date: Optional[str] = None           # 财报报告期（用了哪份财报）

    # ---- 资金类（龙虎榜 / 两融 / 股东户数）----
    lhb_net_buy: Optional[float] = None         # 龙虎榜净买额（元）
    lhb_buy_amount: Optional[float] = None      # 龙虎榜买入额
    margin_balance: Optional[float] = None      # 融资余额
    short_balance: Optional[float] = None       # 融券余额
    holder_count: Optional[float] = None        # 股东户数
    holder_change_pct: Optional[float] = None   # 户数变化 %（负=集中）
    forecast_yoy: Optional[float] = None        # 业绩预告同比中值 %
    rating_score: Optional[float] = None        # 机构评级 0~3
    target_price: Optional[float] = None        # 目标价

    # ---- 舆情 ----
    news_count: Optional[float] = None          # 近 N 天新闻条数
    news_sentiment: Optional[float] = None      # 平均情感 -1~1（None=没分析过）
    news_positive_ratio: Optional[float] = None # 正面占比

    # ---- 宏观 / 市场（市场级，所有票共享）----
    macro_cpi: Optional[float] = None
    macro_pmi: Optional[float] = None
    macro_m2: Optional[float] = None
    macro_lpr: Optional[float] = None
    breadth_up: Optional[float] = None          # 上涨家数
    breadth_down: Optional[float] = None        # 下跌家数
    breadth_limit_up: Optional[float] = None    # 涨停家数
    breadth_sentiment: Optional[float] = None   # 市场情绪 -100~100

    # ---- 其它 ----
    close: Optional[float] = None
    pct_change: Optional[float] = None
    hot_rank: Optional[int] = None
    score: Optional[float] = None               # 已算好的综合分（有则直接用）

    available: Dict[str, bool] = field(default_factory=dict)

    def as_features(self) -> Dict[str, Any]:
        """转成 FactorContext.features 的扁平字典。"""
        out = {
            "main_net": self.main_net,
            "main_net_pct": self.main_net_pct,
            "net_days": self.net_days,
            "capital_accel": self.accel,
            "capital_pulse": self.pulse,
            "turnover": self.turnover,
            "amount": self.amount,
            "avg_cost": self.avg_cost,
            "chip_profit_ratio": self.profit_ratio,
            "chip_concentration": self.chip_concentration,
            "chip_peak_price": self.peak_price,
            "chip_peak_gap": self.peak_gap,
            "themes": list(self.themes),
            "theme_rank": self.theme_rank,
            "theme_pct": self.theme_pct,
            "theme_main_net": self.theme_main_net,
            "is_limitup": self.is_limitup,
            "limitup_continuous": self.continuous,
            "seal_amount": self.seal_amount,
            "broken_times": self.broken_times,
            "hot_rank": self.hot_rank,
            "composite_score": self.score,
            "pe": self.pe,
            "pb": self.pb,
            "ps": self.ps,
            "dividend_yield": self.dividend_yield,
            "market_cap": self.market_cap,
            "float_market_cap": self.float_market_cap,
            "roe": self.roe,
            "gross_margin": self.gross_margin,
            "net_margin": self.net_margin,
            "revenue_yoy": self.revenue_yoy,
            "profit_yoy": self.profit_yoy,
            "debt_ratio": self.debt_ratio,
            "bps": self.bps,
            # 资金类
            "lhb_net_buy": self.lhb_net_buy,
            "lhb_buy_amount": self.lhb_buy_amount,
            "margin_balance": self.margin_balance,
            "short_balance": self.short_balance,
            "holder_count": self.holder_count,
            "holder_change_pct": self.holder_change_pct,
            "forecast_yoy": self.forecast_yoy,
            "rating_score": self.rating_score,
            "target_price": self.target_price,
            # 舆情
            "news_count": self.news_count,
            "news_sentiment": self.news_sentiment,
            "news_positive_ratio": self.news_positive_ratio,
            # 宏观 / 市场
            "macro_cpi": self.macro_cpi,
            "macro_pmi": self.macro_pmi,
            "macro_m2": self.macro_m2,
            "macro_lpr": self.macro_lpr,
            "breadth_up": self.breadth_up,
            "breadth_down": self.breadth_down,
            "breadth_limit_up": self.breadth_limit_up,
            "breadth_sentiment": self.breadth_sentiment,
        }
        # 合成项一并注入：风控与规则直接 ctx.feature("fundamental_score") 就能用，
        # 不必每次再算一遍（打分要遍历整个候选池，重复算很贵）
        out["fundamental_score"] = fundamental_score(out)
        return out


def fundamental_score(fields: Dict[str, Any]) -> Optional[float]:
    """基本面综合分（0~100）：估值 30% + 成长 40% + 质量 30%。

    放在这里而不是因子层，是因为**三处要用**它：
    因子（fundamental_score）、特征注入、风控闸门。
    三处各写一份，迟早会算出三个不同的分数。

    缺哪一项就跳过那一项并按剩余项重新加权 ——
    只有估值没有财报的票不该被判死。
    """
    def _num(v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            f = float(v)
            return None if f != f else f
        except (TypeError, ValueError):
            return None

    # ---- 估值 ----
    vals: List[float] = []
    pe = _num(fields.get("pe"))
    if pe is not None and pe > 0:
        vals.append(max(0.0, min(100.0, 100.0 - max(0.0, pe - 10) * 1.4)))
    pb = _num(fields.get("pb"))
    if pb is not None and pb > 0:
        vals.append(max(0.0, min(100.0, 100.0 - max(0.0, pb - 1) * 12.0)))
    value = sum(vals) / len(vals) if vals else None

    # ---- 成长 ----
    vals = []
    for key in ("revenue_yoy", "profit_yoy"):
        v = _num(fields.get(key))
        if v is not None:
            vals.append(max(0.0, min(100.0, 50.0 + v * 1.67)))
    growth = sum(vals) / len(vals) if vals else None

    # ---- 质量 ----
    vals = []
    roe = _num(fields.get("roe"))
    if roe is not None:
        vals.append(max(0.0, min(100.0, roe * 5.5)))
    gm = _num(fields.get("gross_margin"))
    if gm is not None:
        vals.append(max(0.0, min(100.0, gm * 1.5)))
    quality = sum(vals) / len(vals) if vals else None
    if quality is not None:
        debt = _num(fields.get("debt_ratio"))
        if debt is not None and debt > 60:
            quality = max(0.0, quality - min(25.0, (debt - 60) * 0.8))

    weights = ((value, 0.30), (growth, 0.40), (quality, 0.30))
    num = sum(v * w for v, w in weights if v is not None)
    den = sum(w for v, w in weights if v is not None)
    if den <= 0:
        return None
    return round(num / den, 2)


class FeatureProvider:
    """按标的装配扩展特征。**带 TTL 缓存**：

    打分要遍历整个候选池（几十到几百只票），每只都要查资金流+筹码+题材。
    不缓存的话一次刷新就是上千次查询 —— 既慢，也容易把数据源打到限流。
    """

    def __init__(self, ttl: float = 300.0, history: int = 20) -> None:
        self.ttl = float(ttl)
        self.history = int(history)
        self._cache: Dict[str, tuple] = {}
        self.stats = {"hits": 0, "misses": 0, "errors": 0}

    # ------------------------------------------------------------------
    def get(self, symbol: str, day: Any = None,
            close: Optional[float] = None) -> StockFeatures:
        key = f"{symbol}|{day or ''}"
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and (now - hit[0]) < self.ttl:
            self.stats["hits"] += 1
            return hit[1]
        self.stats["misses"] += 1
        try:
            feat = self._build(symbol, day, close)
        except Exception as exc:
            self.stats["errors"] += 1
            logger.debug(f"装配 {symbol} 扩展特征失败: {exc}")
            feat = StockFeatures(symbol=symbol)
        self._cache[key] = (now, feat)
        return feat

    def invalidate(self, symbol: str = "") -> None:
        if not symbol:
            self._cache.clear()
            return
        for k in [k for k in self._cache if k.startswith(f"{symbol}|")]:
            self._cache.pop(k, None)

    # ------------------------------------------------------------------
    def _fill_fundamental(self, feat: "StockFeatures", symbol: str,
                          day: Any) -> None:
        """填基本面。

        基本面是**慢变量**：季报一出就几个月不变。所以查的是
        "不晚于 day 的最近一条"，而不是"恰好等于 day" ——
        后者会让绝大多数日子查不到任何财务数据。
        """
        from app.storage.repo import kline_repo as R

        try:
            row = R.latest_fundamental(symbol, day)
        except Exception as exc:
            logger.debug(f"读取 {symbol} 基本面失败: {exc}")
            return
        if not row:
            feat.available["fundamental"] = False
            return
        for key in ("pe", "pb", "ps", "dividend_yield", "market_cap",
                    "float_market_cap", "roe", "gross_margin", "net_margin",
                    "revenue_yoy", "profit_yoy", "debt_ratio", "bps"):
            setattr(feat, key, _f(row.get(key)))
        feat.report_date = row.get("report_date")
        # 至少要有一项估值或质量指标，才算这个维度"有数据"
        feat.available["fundamental"] = any(
            getattr(feat, k) is not None
            for k in ("pe", "pb", "roe", "revenue_yoy", "profit_yoy")
        )

    # ------------------------------------------------------------------
    def _market_context(self, day: Any) -> Dict[str, Optional[float]]:
        """宏观 + 市场宽度。**所有标的共享**，所以单独缓存。

        不缓存的话，给 500 只票打分就要查 500 次同一行数据 ——
        那是纯浪费，而且是"看起来在跑实际在等 IO"的典型。
        """
        now = time.monotonic()
        cached = getattr(self, "_market_cache", None)
        if cached and (now - cached[0]) < max(60.0, self.ttl):
            return cached[1]
        out: Dict[str, Optional[float]] = {}
        try:
            from app.storage.repo import kline_repo as R

            macro = R.latest_macro(day) or {}
            out["macro_cpi"] = _f(macro.get("cpi"))
            out["macro_pmi"] = _f(macro.get("pmi"))
            out["macro_m2"] = _f(macro.get("m2"))
            out["macro_lpr"] = _f(macro.get("lpr"))
            b = R.latest_breadth(day) or {}
            out["breadth_up"] = _f(b.get("up_count"))
            out["breadth_down"] = _f(b.get("down_count"))
            out["breadth_limit_up"] = _f(b.get("limit_up_count"))
            out["breadth_sentiment"] = _f(b.get("sentiment"))
        except Exception as exc:
            logger.debug(f"读取宏观/市场宽度失败: {exc}")
        self._market_cache = (now, out)
        return out

    def _fill_capital_extra(self, feat: "StockFeatures", symbol: str,
                            day: Any) -> None:
        """资金类扩展：龙虎榜 / 两融 / 股东户数 / 业绩预告 / 机构评级。

        这些走**通用因子值表**，所以一次性把该票所有类别读出来，
        按名字对号入座 —— 新增类别不需要改这里。
        """
        from app.storage.repo import kline_repo as R

        try:
            vals = R.factor_values(symbol, day) or {}
        except Exception as exc:
            logger.debug(f"读取 {symbol} 通用因子值失败: {exc}")
            return
        mapping = {
            "lhb_net_buy": "lhb_net_buy", "lhb_buy_amount": "lhb_buy_amount",
            "margin_balance": "margin_balance", "short_balance": "short_balance",
            "holder_count": "holder_count",
            "holder_change_pct": "holder_change_pct",
            "forecast_yoy": "forecast_yoy", "rating_score": "rating_score",
            "target_price": "target_price",
        }
        for src, attr in mapping.items():
            if src in vals:
                setattr(feat, attr, _f(vals.get(src)))

    def _fill_sentiment(self, feat: "StockFeatures", symbol: str,
                        day: Any) -> None:
        """舆情：新闻条数、平均情感、正面占比。"""
        from app.storage.repo import kline_repo as R

        try:
            st = R.news_stats(symbol, day, days=10) or {}
        except Exception as exc:
            logger.debug(f"读取 {symbol} 舆情失败: {exc}")
            return
        feat.news_count = _f(st.get("count"))
        feat.news_sentiment = _f(st.get("avg_sentiment"))
        feat.news_positive_ratio = _f(st.get("positive_ratio"))
        # "有新闻但一条都没分析过情感"不算这个维度可用
        feat.available["sentiment"] = bool(st.get("with_sentiment"))

    def _build(self, symbol: str, day: Any, close: Optional[float]) -> StockFeatures:
        from app.storage.repo import kline_repo as R

        feat = StockFeatures(symbol=symbol)
        flows = R.latest_fund_flow(symbol, self.history)
        if flows:
            last = flows[-1]
            feat.date = _as_date(last.get("date"))
            feat.main_net = _f(last.get("main_net"))
            feat.main_net_pct = _f(last.get("main_net_pct"))
            feat.turnover = _f(last.get("turnover"))
            feat.amount = _f(last.get("amount"))
            feat.close = _f(last.get("close")) or close
            feat.pct_change = _f(last.get("pct_change"))
            feat.net_days = _net_days(flows)
            feat.accel = _accel(flows)
            feat.pulse = _pulse(flows)
        feat.available["capital"] = feat.main_net is not None

        chips = R.latest_chip(symbol, 1)
        if chips:
            c = chips[-1]
            feat.avg_cost = _f(c.get("avg_cost"))
            feat.profit_ratio = _f(c.get("profit_ratio"))
            feat.chip_concentration = _f(c.get("concentration_90")) or _f(c.get("concentration_70"))
            feat.peak_price = _f(c.get("peak_price"))
            price = feat.close or close
            if feat.peak_price and price:
                feat.peak_gap = (price - feat.peak_price) / feat.peak_price
        feat.available["chip"] = feat.profit_ratio is not None or feat.peak_price is not None

        feat.themes = [t.get("board") for t in R.stock_themes(symbol) if t.get("board")]
        feat.available["theme"] = bool(feat.themes)

        if feat.themes:
            boards = {t["board"]: t for t in R.themes(day, limit=200)}
            best = None
            for b in feat.themes:
                info = boards.get(b)
                if info is None:
                    continue
                rank = info.get("rank")
                if best is None or (rank or 999) < (best.get("rank") or 999):
                    best = info
            if best:
                feat.theme_rank = best.get("rank")
                feat.theme_pct = _f(best.get("pct_change"))
                feat.theme_main_net = _f(best.get("main_net"))
                feat.available["theme"] = True

        for lu in R.limitup_pool(day, limit=500):
            if lu.get("symbol") == symbol:
                feat.is_limitup = True
                feat.continuous = int(lu.get("continuous") or 0)
                feat.seal_amount = _f(lu.get("seal_amount"))
                feat.broken_times = int(lu.get("broken_times") or 0)
                break
        feat.available["limitup"] = feat.is_limitup

        self._fill_fundamental(feat, symbol, day)
        self._fill_capital_extra(feat, symbol, day)
        self._fill_sentiment(feat, symbol, day)
        for key, value in (self._market_context(day) or {}).items():
            setattr(feat, key, value)
        feat.available["macro"] = any(
            getattr(feat, k) is not None
            for k in ("macro_pmi", "macro_cpi", "breadth_sentiment",
                      "breadth_up")
        )

        sc = R.score_of(symbol, day)
        if sc:
            feat.score = _f(sc.get("total_score"))

        if close is not None and feat.close is None:
            feat.close = float(close)
        return feat


# ===========================================================================
# 派生量计算
# ===========================================================================
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


def _net_days(flows: Sequence[Dict[str, Any]]) -> int:
    """从最后一天往前数，连续主力净流入的天数。"""
    n = 0
    for r in reversed(list(flows)):
        v = _f(r.get("main_net"))
        if v is None or v <= 0:
            break
        n += 1
    return n


def _accel(flows: Sequence[Dict[str, Any]], window: int = 3) -> Optional[float]:
    """加速度 = 近 window 日均值 / 前 window 日均值。

    分子分母都可能为 0 或负 —— 这时"加速度"没有意义，
    返回 None 让上层当作不可用，而不是返回一个会误导的数字。
    """
    vals = [_f(r.get("main_net")) for r in flows]
    vals = [v for v in vals if v is not None]
    need = window * 2
    if len(vals) < need:
        return None
    recent = vals[-window:]
    before = vals[-need:-window]
    avg_recent = sum(recent) / len(recent)
    avg_before = sum(before) / len(before)
    if avg_before <= 0:
        return None
    return avg_recent / avg_before


def _pulse(flows: Sequence[Dict[str, Any]], window: int = 20) -> Optional[float]:
    """脉冲 = 当日主力净额 / 过去 window 日"净额绝对值"的平均。

    用绝对值做分母：一只票长期净流出、突然净流入，这个比值才有意义；
    用纯均值做分母会出现"均值接近 0 → 比值爆炸"的假信号。
    """
    vals = [_f(r.get("main_net")) for r in flows]
    vals = [v for v in vals if v is not None]
    if len(vals) < 3:
        return None
    today = vals[-1]
    history = vals[:-1][-window:]
    if not history:
        return None
    base = sum(abs(v) for v in history) / len(history)
    if base <= 0:
        return None
    return today / base


#: 进程内默认 provider。打分/因子共用，避免重复装配。
_default: Optional[FeatureProvider] = None


def get_provider(ttl: Optional[float] = None) -> FeatureProvider:
    global _default
    if _default is None:
        from app.common.config import settings

        _default = FeatureProvider(
            ttl=float(ttl if ttl is not None else settings.EXT_FEATURE_TTL)
        )
    return _default


__all__ = ["FeatureProvider", "StockFeatures", "get_provider"]
