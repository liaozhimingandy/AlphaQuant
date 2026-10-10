#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : engine.py
# @Description : 多维度综合打分引擎
#
#                为什么不能只用单一因子：
#                  单因子策略的问题不是"不准"，而是**不稳定** ——
#                  资金流好但题材在退潮，进场就是接盘；
#                  题材热但筹码全是套牢盘，拉升就是给解套盘送钱。
#                  多维度打分的价值在于让各维度**互相牵制**：
#                  任何一个维度极端，都会被别的维度拉回来。
#
#                六个维度：
#                  capital    资金链   主力净流入占比 / 脉冲 / 连续天数 / 加速度
#                  theme      题材热度 所属题材涨幅、排名、题材资金流、是否龙头
#                  chip       筹码结构 获利盘、集中度、现价与筹码峰的关系
#                  technical  技术面   动量、趋势、区间位置（复用资金流的收盘序列）
#                  limitup    涨停效应 连板、封板资金、炸板次数
#                  risk       风险扣分 过热、高获利盘、炸板、追高（**只扣分**）
#
#                两条硬约定：
#                  1. **数据缺失不补 0**。某维度没数据就把它的权重**分摊给其它维度**，
#                     并在 available 里标出来。补 0 等于凭空造出一个"这项很差"的假象。
#                  2. **可用维度太少就不给分**（默认少于 2 个返回 None）。
#                     只有资金流一个维度就给 90 分，是最容易骗到人的假信号。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.core.data.features import FeatureProvider, StockFeatures, get_provider
from app.utils.logger import logger


# ===========================================================================
# 区间映射：把任意量纲的原始值映射成 0~100 的分
# ===========================================================================
def band(value: Optional[float], points: Sequence[Tuple[float, float]],
         default: Optional[float] = None) -> Optional[float]:
    """分段线性映射。

    :param points: 递增的 (阈值, 分数) 列表；value 低于首个阈值取首个分数，
                   高于末个阈值取末个分数，中间线性插值。
    :param default: value 为 None 时的返回值（默认 None = 不可用）

    为什么用分段线性而不是硬阈值：硬阈值在边界附近会跳变，
    一个 0.0499 和 0.0501 的票不该相差 40 分。
    """
    if value is None:
        return default
    pts = [(float(a), float(b)) for a, b in points]
    if not pts:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    if v <= pts[0][0]:
        return pts[0][1]
    if v >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= v <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return pts[-1][1]


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


# ===========================================================================
# 配置
# ===========================================================================
#: 默认权重。注意 sentiment / macro 权重偏低是**刻意**的：
#   - 舆情靠关键词规则，精度有限，给高了等于让噪音主导排名；
#   - 宏观与市场宽度对所有票同值，它只能决定"整体该进攻还是防守"，
#     不参与票与票之间的排序，权重给高会稀释掉真正的选股信息。
DEFAULT_WEIGHTS: Dict[str, float] = {
    "capital": 0.24,
    "theme": 0.18,
    "chip": 0.14,
    "technical": 0.12,
    "limitup": 0.06,
    "fundamental": 0.14,
    "sentiment": 0.06,
    "macro": 0.06,
}

#: 默认分段。所有阈值都写在配置里，改权重/阈值不用动代码。
DEFAULT_BANDS: Dict[str, List[List[float]]] = {
    # 主力净流入占成交额比例
    "main_net_pct": [[-0.10, 0], [-0.02, 25], [0.0, 45], [0.05, 70], [0.15, 90], [0.30, 100]],
    # 脉冲：今日净额 / 历史均值
    "pulse": [[0, 20], [1, 45], [2, 70], [4, 90], [8, 100]],
    # 连续净流入天数
    "net_days": [[0, 30], [1, 50], [3, 75], [5, 90], [8, 100]],
    # 加速度（近3日/前3日）
    "accel": [[0, 20], [0.8, 40], [1.0, 55], [1.5, 80], [2.5, 100]],
    # 题材当日涨幅 %
    "theme_pct": [[-5, 10], [-1, 30], [0, 45], [2, 70], [5, 90], [9, 100]],
    # 题材排名（越小越热，所以分数随排名下降）
    "theme_rank": [[1, 100], [10, 90], [30, 75], [60, 60], [120, 40], [9999, 20]],
    # 获利盘比例：两端都危险，中间最好 → 用 peak 型曲线单独处理
    "profit_ratio": [[0.0, 25], [0.15, 45], [0.35, 80], [0.60, 95], [0.80, 80], [0.95, 45], [1.0, 30]],
    # 筹码集中度：越小越集中
    "concentration": [[0.02, 100], [0.08, 85], [0.15, 65], [0.30, 40], [0.60, 20]],
    # 现价相对筹码峰
    "peak_gap": [[-0.20, 25], [-0.05, 45], [0.0, 70], [0.05, 90], [0.20, 80], [0.50, 55]],
    # 5 日动量 %
    "momentum": [[-15, 10], [-5, 30], [0, 45], [5, 70], [12, 90], [25, 100]],
    # 20 日区间位置 0~1
    "position": [[0.0, 20], [0.25, 40], [0.5, 60], [0.75, 80], [1.0, 95]],
    # 连板数
    "continuous": [[0, 40], [1, 65], [2, 80], [3, 90], [5, 100]],
    # ---------------- 基本面 ----------------
    # PE：0 与负值视为无效（亏损股的 PE 没有可比性），20 倍以下给高分
    "pe": [[0, 60], [10, 90], [20, 80], [35, 60], [60, 35], [120, 10]],
    # PB
    "pb": [[0, 70], [1, 90], [3, 70], [6, 45], [12, 20]],
    # ROE %：15% 是经典"优秀"门槛
    "roe": [[-10, 5], [0, 25], [5, 45], [10, 65], [15, 85], [25, 100]],
    # 营收同比 %
    "revenue_yoy": [[-30, 5], [-10, 25], [0, 45], [10, 65], [25, 85], [50, 100]],
    # 净利同比 %
    "profit_yoy": [[-50, 5], [-20, 20], [0, 42], [15, 65], [30, 85], [60, 100]],
    # 毛利率 %
    "gross_margin": [[0, 10], [10, 30], [20, 50], [35, 70], [50, 85], [70, 100]],
    # 资产负债率 %（越高越差）
    "debt_ratio": [[20, 90], [40, 80], [60, 60], [75, 35], [90, 10]],
    # 股息率 %
    "dividend_yield": [[0, 40], [1, 55], [2, 70], [4, 85], [6, 95]],
    # ---------------- 宏观 / 市场 ----------------
    # PMI：50 是荣枯线，55 以上强扩张，48 以下收缩明显
    "pmi": [[44, 10], [48, 30], [50, 50], [52, 70], [55, 85], [58, 95]],
    # M2 同比 %：过低是通缩压力，过高是放水（短期利好但透支未来）
    "m2_yoy": [[4, 30], [7, 55], [9, 70], [12, 80], [16, 60], [20, 35]],
    # LPR %：越低越利好权益
    "lpr": [[2.5, 95], [3.0, 85], [3.5, 70], [4.0, 55], [5.0, 35], [6.0, 15]],
}


@dataclass
class ScoreConfig:
    """打分配置。来自 config/score.json，改配置不用改代码。"""

    weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    bands: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    #: 至少要有几个维度有数据才给分
    min_dims: int = 2
    #: 等级门槛
    grade_a: float = 80.0
    grade_b: float = 65.0
    grade_c: float = 50.0
    #: 风险项阈值
    risk_hot_turnover: float = 25.0      # 换手率超过此值扣分（过热）
    risk_high_profit: float = 0.92       # 获利盘超过此值扣分（抛压）
    risk_chase_pct: float = 9.0          # 当日涨幅超过此值扣分（追高）
    risk_max_penalty: float = 25.0       # 风险扣分上限
    source: str = ""

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ScoreConfig":
        target = Path(path or settings.BASE_DIR / "config" / "score.json")
        cfg = cls(source=str(target))
        # 先把默认 bands 装上，文件里只覆盖提到的部分
        cfg.bands = {k: [(float(a), float(b)) for a, b in v]
                     for k, v in DEFAULT_BANDS.items()}
        if not target.exists():
            return cfg
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"打分配置解析失败（用默认值）: {exc}")
            return cfg
        if not isinstance(raw, dict):
            return cfg
        w = raw.get("weights")
        if isinstance(w, dict) and w:
            merged = dict(DEFAULT_WEIGHTS)
            merged.update({str(k): float(v) for k, v in w.items()})
            cfg.weights = merged
        b = raw.get("bands")
        if isinstance(b, dict):
            for k, v in b.items():
                if isinstance(v, list) and len(v) >= 2:
                    cfg.bands[str(k)] = [(float(a), float(bb)) for a, bb in v]
        for key, attr in (("min_dims", "min_dims"), ("grade_a", "grade_a"),
                          ("grade_b", "grade_b"), ("grade_c", "grade_c"),
                          ("risk_hot_turnover", "risk_hot_turnover"),
                          ("risk_high_profit", "risk_high_profit"),
                          ("risk_chase_pct", "risk_chase_pct"),
                          ("risk_max_penalty", "risk_max_penalty")):
            if key in raw:
                try:
                    setattr(cfg, attr, type(getattr(cfg, attr))(raw[key]))
                except Exception:
                    pass
        if isinstance(raw.get("min_dims"), bool):  # 防手滑写成 true
            cfg.min_dims = 2
        return cfg

    def band(self, key: str) -> List[Tuple[float, float]]:
        return self.bands.get(key) or [(0.0, 50.0), (1.0, 50.0)]


# ===========================================================================
# 结果
# ===========================================================================
@dataclass
class ScoreResult:
    symbol: str
    date: Optional[date] = None
    total: Optional[float] = None
    grade: str = "N/A"
    rank: Optional[int] = None
    dims: Dict[str, Optional[float]] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    available: Dict[str, bool] = field(default_factory=dict)
    penalties: List[Tuple[str, float]] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "date": str(self.date) if self.date else None,
            "total": self.total,
            "grade": self.grade,
            "rank": self.rank,
            "dims": dict(self.dims),
            "weights": dict(self.weights),
            "reasons": list(self.reasons),
            "available": dict(self.available),
            "penalties": [list(p) for p in self.penalties],
            "source": self.source,
        }


# ===========================================================================
# 引擎
# ===========================================================================
class ScoreEngine:
    """多维度综合打分。"""

    #: 维度中文名（面板/CLI 展示用）
    DIM_NAMES = {
        "capital": "资金链",
        "theme": "题材热度",
        "chip": "筹码结构",
        "technical": "技术面",
        "limitup": "涨停效应",
        "fundamental": "基本面",
        "sentiment": "舆情",
        "macro": "宏观环境",
    }

    def __init__(self, config: Optional[ScoreConfig] = None,
                 provider: Optional[FeatureProvider] = None) -> None:
        self.config = config or ScoreConfig.load()
        self.provider = provider or get_provider()

    # ------------------------------------------------------------------
    def score(self, symbol: str, day: Any = None,
              close: Optional[float] = None,
              source: str = "") -> ScoreResult:
        feat = self.provider.get(symbol, day, close)
        return self.score_features(feat, source=source)

    # ------------------------------------------------------------------
    def score_features(self, feat: StockFeatures, source: str = "") -> ScoreResult:
        cfg = self.config
        res = ScoreResult(symbol=feat.symbol, date=feat.date, source=source)

        dims: Dict[str, Optional[float]] = {}
        reasons: List[str] = []
        available: Dict[str, bool] = {}

        # ---- 资金链 ----
        dims["capital"], ok_cap, why_cap = self._capital(feat, cfg)
        available["capital"] = ok_cap
        reasons.extend(why_cap)

        # ---- 题材热度 ----
        dims["theme"], ok_theme, why_theme = self._theme(feat, cfg)
        available["theme"] = ok_theme
        reasons.extend(why_theme)

        # ---- 筹码结构 ----
        dims["chip"], ok_chip, why_chip = self._chip(feat, cfg)
        available["chip"] = ok_chip
        reasons.extend(why_chip)

        # ---- 技术面 ----
        dims["technical"], ok_tech, why_tech = self._technical(feat, cfg)
        available["technical"] = ok_tech
        reasons.extend(why_tech)

        # ---- 涨停效应 ----
        dims["limitup"], ok_lu, why_lu = self._limitup(feat, cfg)
        available["limitup"] = ok_lu
        reasons.extend(why_lu)

        # ---- 基本面 ----
        dims["fundamental"], ok_fund, why_fund = self._fundamental(feat, cfg)
        available["fundamental"] = ok_fund
        reasons.extend(why_fund)

        # ---- 舆情 ----
        dims["sentiment"], ok_sent, why_sent = self._sentiment(feat, cfg)
        available["sentiment"] = ok_sent
        reasons.extend(why_sent)

        # ---- 宏观 / 市场环境 ----
        dims["macro"], ok_macro, why_macro = self._macro(feat, cfg)
        available["macro"] = ok_macro
        reasons.extend(why_macro)

        # ---- 权重再分配：缺哪个维度，它的权重按比例分给其它 ----
        usable = [k for k in cfg.weights if available.get(k) and dims.get(k) is not None]
        if len(usable) < int(cfg.min_dims):
            res.dims = {k: (round(v, 1) if v is not None else None) for k, v in dims.items()}
            res.available = available
            res.reasons = reasons + [
                f"可用维度不足（{len(usable)} < {cfg.min_dims}），拒绝给分 —— "
                "维度太少时打分只会骗人"
            ]
            res.total = None
            res.grade = "N/A"
            return res

        total_w = sum(cfg.weights[k] for k in usable)
        used_w: Dict[str, float] = {}
        weighted = 0.0
        for k in usable:
            w = (cfg.weights[k] / total_w) if total_w else 0.0
            used_w[k] = round(w, 4)
            weighted += float(dims[k]) * w

        # ---- 风险扣分 ----
        penalty, penalty_notes = self._risk(feat, cfg)
        total = clamp(weighted - penalty)

        res.total = round(total, 2)
        res.grade = self._grade(total, cfg)
        res.dims = {k: (round(v, 1) if v is not None else None) for k, v in dims.items()}
        res.weights = used_w
        res.available = available
        res.penalties = penalty_notes
        res.reasons = reasons + [f"风险扣分 -{p:.1f}：{n}" for n, p in penalty_notes]
        return res

    # ------------------------------------------------------------------
    @staticmethod
    def _grade(total: float, cfg: ScoreConfig) -> str:
        if total >= cfg.grade_a:
            return "A"
        if total >= cfg.grade_b:
            return "B"
        if total >= cfg.grade_c:
            return "C"
        return "D"

    # ---- 各维度 ----
    def _capital(self, f: StockFeatures, cfg: ScoreConfig):
        parts: List[Tuple[float, float]] = []   # (score, weight)
        why: List[str] = []
        if f.main_net_pct is not None:
            s = band(f.main_net_pct, cfg.band("main_net_pct"))
            if s is not None:
                parts.append((s, 0.40))
                why.append(f"主力净流入占比 {f.main_net_pct:+.2%} → {s:.0f}分")
        if f.pulse is not None:
            s = band(f.pulse, cfg.band("pulse"))
            if s is not None:
                parts.append((s, 0.25))
                why.append(f"资金脉冲 {f.pulse:.2f}× → {s:.0f}分")
        if f.net_days:
            s = band(float(f.net_days), cfg.band("net_days"))
            if s is not None:
                parts.append((s, 0.20))
                why.append(f"连续净流入 {f.net_days} 天 → {s:.0f}分")
        if f.accel is not None:
            s = band(f.accel, cfg.band("accel"))
            if s is not None:
                parts.append((s, 0.15))
                why.append(f"流入加速度 {f.accel:.2f} → {s:.0f}分")
        return _combine(parts), bool(parts), why

    def _theme(self, f: StockFeatures, cfg: ScoreConfig):
        parts: List[Tuple[float, float]] = []
        why: List[str] = []
        if f.theme_pct is not None:
            s = band(f.theme_pct, cfg.band("theme_pct"))
            if s is not None:
                parts.append((s, 0.40))
                why.append(f"题材涨幅 {f.theme_pct:+.2f}% → {s:.0f}分")
        if f.theme_rank is not None:
            s = band(float(f.theme_rank), cfg.band("theme_rank"))
            if s is not None:
                parts.append((s, 0.35))
                why.append(f"题材热度排名第 {f.theme_rank} → {s:.0f}分")
        if f.theme_main_net is not None:
            # 题材资金流按"亿"计分：净流入 5 亿以上给满分
            s = band(f.theme_main_net / 1e8, cfg.band("pulse"))
            if s is not None:
                parts.append((s, 0.25))
                why.append(f"题材主力净额 {f.theme_main_net/1e8:+.2f}亿 → {s:.0f}分")
        if not parts and f.themes:
            why.append(f"有题材归属（{'、'.join(f.themes[:3])}）但无当日题材行情数据")
        return _combine(parts), bool(parts), why

    def _chip(self, f: StockFeatures, cfg: ScoreConfig):
        parts: List[Tuple[float, float]] = []
        why: List[str] = []
        if f.profit_ratio is not None:
            s = band(f.profit_ratio, cfg.band("profit_ratio"))
            if s is not None:
                parts.append((s, 0.40))
                why.append(f"获利盘 {f.profit_ratio:.0%} → {s:.0f}分")
        if f.chip_concentration is not None:
            s = band(f.chip_concentration, cfg.band("concentration"))
            if s is not None:
                parts.append((s, 0.30))
                why.append(f"筹码集中度 {f.chip_concentration:.3f} → {s:.0f}分")
        if f.peak_gap is not None:
            s = band(f.peak_gap, cfg.band("peak_gap"))
            if s is not None:
                parts.append((s, 0.30))
                why.append(f"现价距筹码峰 {f.peak_gap:+.1%} → {s:.0f}分")
        return _combine(parts), bool(parts), why

    def _technical(self, f: StockFeatures, cfg: ScoreConfig):
        """技术面。复用资金流序列里自带的 close / pct_change，不额外查库。"""
        parts: List[Tuple[float, float]] = []
        why: List[str] = []
        if f.pct_change is not None:
            s = band(f.pct_change, cfg.band("momentum"))
            if s is not None:
                parts.append((s, 0.45))
                why.append(f"当日涨幅 {f.pct_change:+.2f}% → {s:.0f}分")
        if f.turnover is not None:
            # 换手率适中最好：太低没人气，太高是过热
            s = band(f.turnover, [(0, 30), (2, 55), (6, 80), (15, 70), (30, 45)])
            if s is not None:
                parts.append((s, 0.30))
                why.append(f"换手率 {f.turnover:.2f}% → {s:.0f}分")
        if f.amount:
            # 成交额太小流动性差，实盘上买不进也卖不出
            s = band(f.amount / 1e8, [(0, 20), (0.5, 45), (2, 70), (10, 90), (50, 100)])
            if s is not None:
                parts.append((s, 0.25))
                why.append(f"成交额 {f.amount/1e8:.2f}亿 → {s:.0f}分")
        return _combine(parts), bool(parts), why

    def _limitup(self, f: StockFeatures, cfg: ScoreConfig):
        if not f.is_limitup:
            return None, False, []
        parts: List[Tuple[float, float]] = []
        why: List[str] = [f"今日涨停（{f.continuous} 连板）"]
        s = band(float(f.continuous), cfg.band("continuous"))
        if s is not None:
            parts.append((s, 0.55))
            why.append(f"连板数 {f.continuous} → {s:.0f}分")
        if f.seal_amount:
            s = band(f.seal_amount / 1e8, [(0, 30), (0.5, 55), (2, 75), (5, 90), (20, 100)])
            if s is not None:
                parts.append((s, 0.30))
                why.append(f"封板资金 {f.seal_amount/1e8:.2f}亿 → {s:.0f}分")
        # 炸板越多越差
        s = band(float(f.broken_times), [(0, 100), (1, 65), (2, 40), (4, 15)])
        if s is not None:
            parts.append((s, 0.15))
            why.append(f"炸板 {f.broken_times} 次 → {s:.0f}分")
        return _combine(parts), bool(parts), why

    def _fundamental(self, f: StockFeatures, cfg: ScoreConfig):
        """基本面：估值 30% + 成长 40% + 质量 30%。

        为什么基本面权重不能太高：
          它是**慢变量**，一份财报要用一个季度。如果给它 40% 权重，
          打分结果会被几个月前的数据主导，而选股要的是"现在谁在被资金推动"。
          12% 的定位是**过滤器**：把垃圾股挡在池子外面，而不是决定排名先后。

        PE 为负（亏损）时直接判 0 分而不是忽略 ——
        "亏损"是确定的坏消息，不是"没数据"。
        """
        parts: List[Tuple[float, float]] = []
        why: List[str] = []

        # ---- 估值 ----
        if f.pe is not None:
            if f.pe <= 0:
                parts.append((0.0, 0.30))
                why.append(f"PE {f.pe:.1f}（亏损）→ 0分")
            else:
                s = band(f.pe, cfg.band("pe"))
                if s is not None:
                    parts.append((s, 0.30))
                    why.append(f"PE {f.pe:.1f} → {s:.0f}分")
        if f.pb is not None and f.pb > 0:
            s = band(f.pb, cfg.band("pb"))
            if s is not None:
                parts.append((s, 0.15))
                why.append(f"PB {f.pb:.2f} → {s:.0f}分")

        # ---- 成长 ----
        if f.revenue_yoy is not None:
            s = band(f.revenue_yoy, cfg.band("revenue_yoy"))
            if s is not None:
                parts.append((s, 0.20))
                why.append(f"营收同比 {f.revenue_yoy:+.1f}% → {s:.0f}分")
        if f.profit_yoy is not None:
            s = band(f.profit_yoy, cfg.band("profit_yoy"))
            if s is not None:
                parts.append((s, 0.25))
                why.append(f"净利同比 {f.profit_yoy:+.1f}% → {s:.0f}分")

        # ---- 质量 ----
        if f.roe is not None:
            s = band(f.roe, cfg.band("roe"))
            if s is not None:
                parts.append((s, 0.20))
                why.append(f"ROE {f.roe:.1f}% → {s:.0f}分")
        if f.gross_margin is not None:
            s = band(f.gross_margin, cfg.band("gross_margin"))
            if s is not None:
                parts.append((s, 0.10))
                why.append(f"毛利率 {f.gross_margin:.1f}% → {s:.0f}分")
        if f.dividend_yield is not None:
            s = band(f.dividend_yield, cfg.band("dividend_yield"))
            if s is not None:
                parts.append((s, 0.08))
                why.append(f"股息率 {f.dividend_yield:.2f}% → {s:.0f}分")

        if not parts:
            return None, False, ["无基本面数据"]
        if f.report_date:
            why.append(f"（财报报告期 {f.report_date}）")
        return _combine(parts), True, why

    def _sentiment(self, f: StockFeatures, cfg: ScoreConfig):
        """舆情：新闻情感 + 关注度 + 正面占比。

        两个必须说清的边界：
          1. **没有情感分就不算这个维度可用**。
             "有新闻但没分析"和"新闻是中性"是两回事，
             前者按 0 分处理会凭空造出看空信号。
          2. **关注度是双刃剑**：新闻突然暴增常常伴随的是风险事件
             （问询函、业绩暴雷），所以只给温和的加分，且设上限。
        """
        if f.news_sentiment is None and f.news_positive_ratio is None:
            return None, False, []
        parts: List[Tuple[float, float]] = []
        why: List[str] = []
        if f.news_sentiment is not None:
            # -1~1 → 0~100（0 分对应 50）
            s = clamp(50.0 + f.news_sentiment * 50.0)
            parts.append((s, 0.60))
            why.append(f"新闻情感 {f.news_sentiment:+.2f} → {s:.0f}分")
            if f.news_sentiment < -0.3:
                why.append("负面舆情偏多，注意事件性风险")
        if f.news_positive_ratio is not None:
            s = clamp(f.news_positive_ratio * 100.0)
            parts.append((s, 0.25))
            why.append(f"正面新闻占比 {f.news_positive_ratio:.0%} → {s:.0f}分")
        if f.news_count:
            # 关注度：10 条左右最好，太少无人问津，太多往往是出事了
            s = band(float(f.news_count),
                     [(0, 20), (3, 50), (10, 75), (25, 60), (60, 35)])
            if s is not None:
                parts.append((s, 0.15))
                why.append(f"近10日新闻 {int(f.news_count)} 条 → {s:.0f}分")
        return _combine(parts), bool(parts), why

    def _macro(self, f: StockFeatures, cfg: ScoreConfig):
        """宏观 / 市场环境：**所有标的同值**，所以它衡量的是"现在该不该出手"。

        刻意不当成选股维度（所有票一样，排序没有意义），
        但它是唯一能表达"大盘环境不好，再好的票也该降低仓位"的地方。

        PMI 是权重最高的一项：它是少数**领先**的宏观指标，
        拐点通常早于企业盈利的拐点。
        """
        parts: List[Tuple[float, float]] = []
        why: List[str] = []

        if f.macro_pmi is not None:
            # 50 是荣枯线：之下收缩、之上扩张
            s = band(f.macro_pmi, cfg.band("pmi"))
            if s is not None:
                parts.append((s, 0.45))
                why.append(f"PMI {f.macro_pmi:.1f} → {s:.0f}分")
        if f.macro_m2 is not None:
            # M2 同比：8% 左右算正常偏高（口径可能是小数也可能是百分数）
            v = f.macro_m2 * 100.0 if abs(f.macro_m2) < 1 else f.macro_m2
            s = band(v, cfg.band("m2_yoy"))
            if s is not None:
                parts.append((s, 0.20))
                why.append(f"M2 同比 {v:.1f}% → {s:.0f}分")
        if f.macro_lpr is not None:
            # 利率越低越利好权益（贴现率下降）
            s = band(f.macro_lpr, cfg.band("lpr"))
            if s is not None:
                parts.append((s, 0.15))
                why.append(f"LPR {f.macro_lpr:.2f}% → {s:.0f}分")
        if f.breadth_sentiment is not None:
            # -100~100 → 0~100
            s = clamp(50.0 + f.breadth_sentiment / 2.0)
            parts.append((s, 0.20))
            why.append(f"市场情绪 {f.breadth_sentiment:+.0f} → {s:.0f}分")
            if f.breadth_sentiment < -30:
                why.append("全市场普跌环境，个股策略胜率系统性下降")
        elif f.breadth_up and f.breadth_down:
            total = f.breadth_up + f.breadth_down
            if total > 0:
                s = clamp(f.breadth_up / total * 100.0)
                parts.append((s, 0.20))
                why.append(f"上涨家数占比 {f.breadth_up/total:.0%} → {s:.0f}分")

        if not parts:
            return None, False, []
        return _combine(parts), True, why

    def _risk(self, f: StockFeatures, cfg: ScoreConfig):
        """风险扣分。只扣分，不加分 —— 风险项的作用是"踩刹车"。"""
        penalty = 0.0
        notes: List[Tuple[str, float]] = []
        if f.profit_ratio is not None and f.profit_ratio >= cfg.risk_high_profit:
            p = min(10.0, (f.profit_ratio - cfg.risk_high_profit) * 100)
            penalty += p
            notes.append((f"获利盘 {f.profit_ratio:.0%} 偏高，兑现压力大", p))
        if f.turnover is not None and f.turnover >= cfg.risk_hot_turnover:
            p = min(8.0, (f.turnover - cfg.risk_hot_turnover) * 0.5)
            penalty += p
            notes.append((f"换手率 {f.turnover:.1f}% 过热", p))
        if f.pct_change is not None and f.pct_change >= cfg.risk_chase_pct:
            p = min(8.0, (f.pct_change - cfg.risk_chase_pct) * 1.0)
            penalty += p
            notes.append((f"当日已涨 {f.pct_change:.1f}%，追高风险", p))
        if f.broken_times and f.broken_times >= 2:
            p = min(6.0, f.broken_times * 2.0)
            penalty += p
            notes.append((f"炸板 {f.broken_times} 次，封板不牢", p))
        return min(penalty, cfg.risk_max_penalty), notes

    # ------------------------------------------------------------------
    def score_many(self, symbols: Sequence[str], day: Any = None,
                   source: str = "") -> List[ScoreResult]:
        """批量打分并排名。**没有分的（None）排在最后**，不参与排名竞争。"""
        out: List[ScoreResult] = []
        for sym in dict.fromkeys(symbols):
            try:
                out.append(self.score(sym, day, source=source))
            except Exception as exc:
                logger.debug(f"打分失败 {sym}: {exc}")
                r = ScoreResult(symbol=sym)
                r.reasons = [f"打分失败: {exc}"]
                out.append(r)
        scored = [r for r in out if r.total is not None]
        scored.sort(key=lambda r: r.total, reverse=True)
        for i, r in enumerate(scored, 1):
            r.rank = i
        for r in out:
            if r.total is None:
                r.rank = None
        return out


def _combine(parts: List[Tuple[float, float]]) -> Optional[float]:
    """按子项权重合成一个维度分。parts = [(score, weight), ...]"""
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    if total_w <= 0:
        return None
    return round(clamp(sum(s * w for s, w in parts) / total_w), 1)


__all__ = ["DEFAULT_BANDS", "DEFAULT_WEIGHTS", "ScoreConfig", "ScoreEngine", "ScoreResult", "band"]
