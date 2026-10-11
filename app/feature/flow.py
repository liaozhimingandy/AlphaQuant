#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : flow.py
# @Description : 资金流 / 筹码 / 题材 / 涨停 因子
#
#                这些因子的输入**不是 BarSeries**，而是日频截面数据
#                （由 FeatureProvider 装进 `ctx.features`）。
#
#                一个必须守住的约定：**数据缺失时返回 None**。
#                返回 0 会被规则层当成"值确实为 0"，进而判定
#                "主力大幅流出"，凭空造出一个强烈看空信号 ——
#                而真实情况是"我们没拿到数据"。两者天差地别。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Optional

from app.feature.base import IBaseFactor
from app.feature.context import FactorContext
from app.feature.registry import register_factor


class _FeatureFactor(IBaseFactor):
    """从 ctx.features 取值的因子基类。

    把"取不到就 None"这件事收敛到一处，避免每个因子各写一遍、
    某个因子漏了就变成一个静默的假信号。
    """

    #: 对应的 feature 键
    key: str = ""

    def compute(self, ctx: FactorContext) -> Optional[float]:
        if not self.key:
            return None
        value = ctx.feature(self.key)
        if value is None:
            return None
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return None if v != v else v


@register_factor
class MainNetPctFactor(_FeatureFactor):
    """主力净流入占成交额比例（当日）。

    比绝对金额可比：同样是净流入 1 亿，在小票上是巨量，在大盘股上只是毛毛雨。
    """

    name = "main_net_pct"
    description = "主力净流入 / 成交额"
    key = "main_net_pct"


@register_factor
class MainNetFactor(_FeatureFactor):
    """主力净流入绝对额（元）。判断"量级"时用。"""

    name = "main_net"
    description = "主力净流入金额"
    key = "main_net"


@register_factor
class CapitalAccelFactor(_FeatureFactor):
    """资金流加速度：近 3 日净流入均值 / 前 3 日均值。

    >1 表示在加速流入 —— 这正是"大资金正在快速进场"的量化定义。
    """

    name = "capital_accel"
    description = "近3日/前3日 主力净流入均值之比"
    key = "capital_accel"


@register_factor
class CapitalPulseFactor(_FeatureFactor):
    """资金脉冲：当日净额 / 过去 N 日净额绝对值均值。

    抓的是"突然放量进场"。配合 net_days 一起看更可靠：
    单日脉冲可能是假动作，连续净流入 + 单日脉冲才是真进场。
    """

    name = "capital_pulse"
    description = "当日主力净额 / 历史均值（脉冲强度）"
    key = "capital_pulse"


@register_factor
class NetInflowDaysFactor(_FeatureFactor):
    """连续主力净流入天数。趋势性的直接刻画。"""

    name = "net_inflow_days"
    description = "连续主力净流入天数"
    key = "net_days"


@register_factor
class ChipProfitRatioFactor(_FeatureFactor):
    """获利盘比例 0~1。

    两端都危险：>0.9 抛压重（人人浮盈随时兑现）；<0.1 套牢盘密集。
    中性偏高的位置（0.3~0.7）通常结构最好。
    """

    name = "chip_profit_ratio"
    description = "获利盘比例"
    key = "chip_profit_ratio"


@register_factor
class ChipConcentrationFactor(_FeatureFactor):
    """筹码集中度（越小越集中，主力控盘度越高）。"""

    name = "chip_concentration"
    description = "筹码集中度，越小越集中"
    key = "chip_concentration"


@register_factor
class ChipPeakGapFactor(_FeatureFactor):
    """现价相对主筹码峰的位置：(现价 - 筹码峰)/筹码峰。

    刚突破筹码峰（略大于 0）通常意味着上方套牢盘已被消化；
    远低于筹码峰意味着还在密集套牢区里挣扎。
    """

    name = "chip_peak_gap"
    description = "(现价-筹码峰)/筹码峰"
    key = "chip_peak_gap"


@register_factor
class ThemeRankFactor(_FeatureFactor):
    """所属最热题材的当日排名（越小越热）。"""

    name = "theme_rank"
    description = "所属题材当日涨幅排名"
    key = "theme_rank"


@register_factor
class ThemeHeatFactor(_FeatureFactor):
    """所属最热题材的当日涨幅(%)。"""

    name = "theme_pct"
    description = "所属题材当日涨幅"
    key = "theme_pct"


@register_factor
class LimitUpEffectFactor(IBaseFactor):
    """涨停效应分：连板数 + 封板质量 - 炸板惩罚。

    不是一个原始字段，而是把涨停池里的几个字段合成一个"质量分"：
    单纯"涨停过"信息量太低（一字板和尾盘偷袭板完全不是一回事）。
    """

    name = "limitup_effect"
    description = "涨停质量：连板加成 + 封板加成 - 炸板惩罚"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        if not ctx.feature("is_limitup"):
            return None
        continuous = float(ctx.feature("limitup_continuous") or 0)
        broken = float(ctx.feature("broken_times") or 0)
        seal = ctx.feature("seal_amount") or 0.0
        try:
            seal = float(seal)
        except (TypeError, ValueError):
            seal = 0.0
        # 封板资金按亿为单位给一个温和的加成，避免大盘股天然占优
        seal_bonus = min(2.0, seal / 1e8) if seal > 0 else 0.0
        return 1.0 + continuous + seal_bonus - broken * 0.5


# ===========================================================================
# 基本面因子
# ===========================================================================
# 基本面是**慢变量**：季报几个月才更新一次。所以它不适合当"择时"信号，
# 而是当**过滤器**用 —— 决定"这只票值不值得进池子"。
# 用法上要注意：ROE 30% 的公司和 ROE 3% 的公司，遇到同样的资金流脉冲，
# 后续的持续性完全不是一回事。

@register_factor
class PeFactor(_FeatureFactor):
    """市盈率 TTM。亏损股为负或缺失，规则层要用"有值且 < X"来筛。"""

    name = "pe"
    description = "市盈率 TTM"
    key = "pe"


@register_factor
class PbFactor(_FeatureFactor):
    """市净率。重资产行业天然低 PB，横比才有效。"""

    name = "pb"
    description = "市净率"
    key = "pb"


@register_factor
class DividendYieldFactor(_FeatureFactor):
    """股息率 %。防御型配置的锚。"""

    name = "dividend_yield"
    description = "股息率 %"
    key = "dividend_yield"


@register_factor
class MarketCapFactor(_FeatureFactor):
    """总市值（元）。既是规模因子，也是流动性代理变量。"""

    name = "market_cap"
    description = "总市值（元）"
    key = "market_cap"


@register_factor
class RoeFactor(_FeatureFactor):
    """净资产收益率 %。巴菲特最看重的单一指标，跨行业可比性最好。"""

    name = "roe"
    description = "净资产收益率 %"
    key = "roe"


@register_factor
class GrossMarginFactor(_FeatureFactor):
    """毛利率 %。护城河的代理变量：能长期维持高毛利率才有定价权。"""

    name = "gross_margin"
    description = "毛利率 %"
    key = "gross_margin"


@register_factor
class NetMarginFactor(_FeatureFactor):
    """净利率 %。"""

    name = "net_margin"
    description = "净利率 %"
    key = "net_margin"


@register_factor
class RevenueYoyFactor(_FeatureFactor):
    """营收同比 %。成长性的第一手证据。"""

    name = "revenue_yoy"
    description = "营收同比增长 %"
    key = "revenue_yoy"


@register_factor
class ProfitYoyFactor(_FeatureFactor):
    """净利同比 %。

    必须和营收同比**一起看**：营收涨、净利跌，是靠降价换量；
    营收平、净利涨，可能是卖资产或降本 —— 质地完全不同。
    """

    name = "profit_yoy"
    description = "净利同比增长 %"
    key = "profit_yoy"


@register_factor
class DebtRatioFactor(_FeatureFactor):
    """资产负债率 %。金融地产行业天然高，横比没意义。"""

    name = "debt_ratio"
    description = "资产负债率 %"
    key = "debt_ratio"


@register_factor
class FundamentalScoreFactor(IBaseFactor):
    """基本面综合分（0~100）：估值 30% + 成长 40% + 质量 30%。

    合成一个分而不是暴露一堆原始字段，是为了**让规则好写**：
    "基本面 > 60" 比 "ROE>15 且 净利增长>20 且 PE<40 且 负债率<60" 好维护得多，
    而且缺某一项时按可用项重新加权，不会直接判死。
    """

    name = "fundamental_score"
    description = "基本面综合分：估值+成长+质量"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        # 特征层已经算好并注入了，直接用 —— 算分口径只有一份实现
        # （app.feature.features.fundamental_score），
        # 因子、特征注入、风控三处拿到的必然是同一个数。
        cached = ctx.feature("fundamental_score")
        if cached is not None:
            try:
                return float(cached)
            except (TypeError, ValueError):
                pass
        from app.feature.features import fundamental_score

        keys = ("pe", "pb", "roe", "gross_margin", "net_margin",
                "revenue_yoy", "profit_yoy", "debt_ratio", "dividend_yield")
        return fundamental_score({k: ctx.feature(k) for k in keys})


# ===========================================================================
# 资金类扩展因子（龙虎榜 / 两融 / 股东户数 / 业绩预告 / 机构评级）
# ===========================================================================
# 这一类回答的是"钱从哪来、谁在买"：
#   主力资金流只知道"大单在流入"，龙虎榜能看出是机构还是游资；
#   两融余额反映杠杆资金的风险偏好；股东户数反映筹码在集中还是分散。

@register_factor
class LhbNetBuyFactor(_FeatureFactor):
    """龙虎榜净买额（元）。机构席位为正通常比游资更有持续性。"""

    name = "lhb_net_buy"
    description = "龙虎榜净买入额"
    key = "lhb_net_buy"


@register_factor
class MarginBalanceFactor(_FeatureFactor):
    """融资余额（元）。杠杆资金的温度计。"""

    name = "margin_balance"
    description = "融资余额"
    key = "margin_balance"


@register_factor
class HolderChangeFactor(_FeatureFactor):
    """股东户数变化 %。**负值才是好事**（户数减少 = 筹码集中）。

    刻意不做符号翻转：因子的职责是"报告事实"，
    方向由规则层决定。翻转一次，三个月后没人记得为什么。
    """

    name = "holder_change_pct"
    description = "股东户数变化 %（负=集中）"
    key = "holder_change_pct"


@register_factor
class ForecastYoyFactor(_FeatureFactor):
    """业绩预告同比中值 %。财报之前唯一的业绩前瞻。"""

    name = "forecast_yoy"
    description = "业绩预告同比 %"
    key = "forecast_yoy"


@register_factor
class RatingScoreFactor(_FeatureFactor):
    """机构评级 0~3（买入3/增持2/中性1/减持0）。

    单一评级不该直接当信号（卖方长期偏乐观），
    真正有价值的是**评级变化的密度**。
    """

    name = "rating_score"
    description = "机构评级分 0~3"
    key = "rating_score"


# ===========================================================================
# 舆情因子
# ===========================================================================
# 注意：news_sentiment / news_count 在 builtin 里已经有了
# （它们读的是事件通道注入的同一份特征），这里**不再重复注册** ——
# 同名注册会静默覆盖，而覆盖顺序取决于谁先被 import，
# 那是"重启一次行为就变了"的经典来源。


@register_factor
class NewsPositiveRatioFactor(_FeatureFactor):
    """正面新闻占比 0~1。"""

    name = "news_positive_ratio"
    description = "正面新闻占比"
    key = "news_positive_ratio"


# ===========================================================================
# 宏观 / 市场宽度因子（市场级，所有票同值）
# ===========================================================================
# 这类因子**不用于选股排序**（所有票一样，排序没意义），
# 而是当"环境开关"：决定当前环境下该偏进攻还是偏防守。

@register_factor
class MacroPmiFactor(_FeatureFactor):
    """PMI。>50 扩张、<50 收缩；拐点往往领先周期股。"""

    name = "macro_pmi"
    description = "制造业 PMI"
    key = "macro_pmi"


@register_factor
class MacroCpiFactor(_FeatureFactor):
    """CPI 同比 %。过高→货币收紧预期，过低→需求疲弱。"""

    name = "macro_cpi"
    description = "CPI 同比 %"
    key = "macro_cpi"


@register_factor
class MacroM2Factor(_FeatureFactor):
    """M2 同比 %。流动性总量。"""

    name = "macro_m2"
    description = "M2 同比 %"
    key = "macro_m2"


@register_factor
class MacroLprFactor(_FeatureFactor):
    """LPR 报价利率 %。上行周期高估值成长股系统性承压。"""

    name = "macro_lpr"
    description = "贷款市场报价利率 %"
    key = "macro_lpr"


@register_factor
class BreadthSentimentFactor(_FeatureFactor):
    """市场情绪 -100~100（涨跌家数差占比）。

    同一只票在普涨日和普跌日里，同样的形态胜率完全不同 ——
    这是所有个股策略的共同背景。
    """

    name = "breadth_sentiment"
    description = "市场情绪 -100~100"
    key = "breadth_sentiment"


@register_factor
class BreadthUpRatioFactor(IBaseFactor):
    """上涨家数占比 0~1（由涨跌家数算出，不额外存字段）。"""

    name = "breadth_up_ratio"
    description = "全市场上涨家数占比"

    def compute(self, ctx: FactorContext) -> Optional[float]:
        up = ctx.feature("breadth_up")
        down = ctx.feature("breadth_down")
        if up is None or down is None:
            return None
        try:
            total = float(up) + float(down)
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None
        return float(up) / total


@register_factor
class CompositeScoreFactor(_FeatureFactor):
    """多维度综合评分（0~100）。由打分引擎产出后回填，策略直接用。"""

    name = "composite_score"
    description = "多维度综合评分"
    key = "composite_score"


#: 供 CLI/面板列出（与 BUILTIN_FACTORS 同风格）
FLOW_FACTORS = [
    MainNetPctFactor,
    MainNetFactor,
    CapitalAccelFactor,
    CapitalPulseFactor,
    NetInflowDaysFactor,
    ChipProfitRatioFactor,
    ChipConcentrationFactor,
    ChipPeakGapFactor,
    ThemeRankFactor,
    ThemeHeatFactor,
    LimitUpEffectFactor,
    PeFactor,
    PbFactor,
    DividendYieldFactor,
    MarketCapFactor,
    RoeFactor,
    GrossMarginFactor,
    NetMarginFactor,
    RevenueYoyFactor,
    ProfitYoyFactor,
    DebtRatioFactor,
    FundamentalScoreFactor,
    LhbNetBuyFactor,
    MarginBalanceFactor,
    HolderChangeFactor,
    ForecastYoyFactor,
    RatingScoreFactor,
    NewsPositiveRatioFactor,
    MacroPmiFactor,
    MacroCpiFactor,
    MacroM2Factor,
    MacroLprFactor,
    BreadthSentimentFactor,
    BreadthUpRatioFactor,
    CompositeScoreFactor,
]


__all__ = [
    "FLOW_FACTORS",
    "CapitalAccelFactor",
    "CapitalPulseFactor",
    "ChipConcentrationFactor",
    "ChipPeakGapFactor",
    "ChipProfitRatioFactor",
    "CompositeScoreFactor",
    "DebtRatioFactor",
    "DividendYieldFactor",
    "FundamentalScoreFactor",
    "BreadthSentimentFactor",
    "BreadthUpRatioFactor",
    "ForecastYoyFactor",
    "GrossMarginFactor",
    "HolderChangeFactor",
    "LhbNetBuyFactor",
    "LimitUpEffectFactor",
    "MacroCpiFactor",
    "MacroLprFactor",
    "MacroM2Factor",
    "MacroPmiFactor",
    "MarginBalanceFactor",
    "NewsPositiveRatioFactor",
    "RatingScoreFactor",
    "MainNetFactor",
    "MainNetPctFactor",
    "MarketCapFactor",
    "NetInflowDaysFactor",
    "NetMarginFactor",
    "PeFactor",
    "PbFactor",
    "ProfitYoyFactor",
    "RevenueYoyFactor",
    "RoeFactor",
    "ThemeHeatFactor",
    "ThemeRankFactor",
]
