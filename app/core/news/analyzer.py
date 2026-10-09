#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : analyzer.py
# @Description : 新闻分析器：规则版（离线可用）+ 大模型版（OpenAI 兼容协议）
#               大模型是可选增强：没配 key 就自动降级到规则版，不会让服务起不来
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.core.market.types import NewsAnalysis, NewsItem
from app.utils.logger import logger

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore


class IBaseNewsAnalyzer(ABC):
    """新闻分析器接口。"""

    name: str = "base"

    @abstractmethod
    def analyze(self, news: NewsItem) -> Optional[NewsAnalysis]:
        """返回分析结论。拿不准就返回低 confidence，不要返回 None 以外的异常。"""


# ============================================================
# 规则版：离线可用，作为大模型的兜底与对照基准
# ============================================================
_BULLISH = {
    "业绩预增": 0.8, "净利润增长": 0.8, "超预期": 0.7, "涨停": 0.6,
    "中标": 0.6, "签约": 0.5, "回购": 0.5, "增持": 0.5,
    "突破": 0.4, "利好": 0.5, "创新高": 0.6, "获批": 0.6,
    "分红": 0.3, "订单": 0.4, "扩产": 0.4, "扭亏": 0.7,
}
_BEARISH = {
    "业绩预亏": -0.8, "净利润下滑": -0.7, "不及预期": -0.6, "跌停": -0.6,
    "立案": -0.9, "调查": -0.7, "减持": -0.6, "退市": -1.0,
    "亏损": -0.7, "利空": -0.5, "下滑": -0.4, "违约": -0.8,
    "诉讼": -0.4, "停工": -0.5, "召回": -0.5, "处罚": -0.7,
}


class KeywordAnalyzer(IBaseNewsAnalyzer):
    """关键词打分。

    存在的意义不只是"省 API 钱"：它给了大模型一个**可对照的基准**。
    当两者结论长期背离时，说明提示词或关键词表有问题，这个信号很有价值。
    """

    name = "keyword"

    def __init__(
        self,
        bullish: Optional[Dict[str, float]] = None,
        bearish: Optional[Dict[str, float]] = None,
        max_score: float = 1.0,
        confidence_per_hit: float = 0.3,
        max_confidence: float = 0.9,
        **kw,
    ) -> None:
        self.bullish = {**_BULLISH, **(bullish or {})}
        self.bearish = {**_BEARISH, **(bearish or {})}
        self.max_score = float(max_score)
        self.confidence_per_hit = float(confidence_per_hit)
        self.max_confidence = float(max_confidence)

    def analyze(self, news: NewsItem) -> NewsAnalysis:
        text = f"{news.title} {news.content}"
        hits: List[str] = []
        raw = 0.0
        for kw, w in self.bullish.items():
            if kw in text:
                raw += w
                hits.append(f"+{kw}")
        for kw, w in self.bearish.items():
            if kw in text:
                raw += w
                hits.append(f"-{kw}")

        score = max(-self.max_score, min(self.max_score, raw))
        confidence = min(self.max_confidence, len(hits) * self.confidence_per_hit)
        if score > 0.15:
            direction = "bullish"
        elif score < -0.15:
            direction = "bearish"
        else:
            direction = "neutral"

        return NewsAnalysis(
            news_fingerprint=news.fingerprint,
            symbols=list(news.symbols),
            score=round(score, 4),
            confidence=round(confidence, 4),
            direction=direction,
            reason="命中: " + ", ".join(hits) if hits else "未命中关键词",
            analyzer=self.name,
            raw={"hits": hits},
        )


# ============================================================
# 大模型版
# ============================================================
DEFAULT_PROMPT = """你是量化交易系统的新闻分析模块。请判断下面这条新闻对指定股票的影响。

要求：
1. 只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块
2. JSON 字段：
   - score: 数值，[-1, 1]，正=利好，负=利空，0=中性
   - confidence: 数值，[0, 1]，你的把握程度；信息不足就给低分
   - direction: "bullish" | "bearish" | "neutral"
   - reason: 一句话理由（不超过 40 字）

股票代码：{symbols}
标题：{title}
正文：{content}
"""


class LLMAnalyzer(IBaseNewsAnalyzer):
    """调用 OpenAI 兼容的 chat/completions 接口。

    两个硬性约束：
      1. **必须在线程池里调用**——这是同步阻塞 HTTP，直接在 reactor 线程跑会卡死引擎
      2. **失败必须降级**，绝不能抛异常打穿调用链；拿不到结论时返回 confidence=0，
         下游的 news_confidence 风控会把这类信号拦掉
    """

    name = "llm"

    def __init__(
        self,
        api_base: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout: float = 20.0,
        temperature: float = 0.0,
        max_content: int = 1200,
        prompt_template: str = DEFAULT_PROMPT,
        fallback: Optional[IBaseNewsAnalyzer] = None,
        enabled: bool = True,
        **kw,
    ) -> None:
        self.api_base = str(api_base).rstrip("/")
        self.api_key = str(api_key)
        self.model = str(model)
        self.timeout = float(timeout)
        self.temperature = float(temperature)
        self.max_content = int(max_content)
        self.prompt_template = prompt_template
        self.fallback = fallback or KeywordAnalyzer()
        # 没配 key 就自动禁用：服务照常跑，只是不出 LLM 结论
        self.enabled = bool(enabled) and bool(self.api_key)

        if not self.enabled:
            logger.warning(
                "LLMAnalyzer 未启用（缺少 api_key），新闻分析将回退到规则版"
            )

    @property
    def available(self) -> bool:
        return self.enabled and requests is not None

    def analyze(self, news: NewsItem) -> Optional[NewsAnalysis]:
        if not self.available:
            return self.fallback.analyze(news)
        try:
            payload = self._build_payload(news)
            resp = requests.post(
                f"{self.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            parsed = self._parse_json(content)
            if parsed is None:
                logger.warning(f"LLM 返回无法解析，回退规则版: {content[:120]!r}")
                return self.fallback.analyze(news)
            return self._to_analysis(news, parsed)
        except Exception as exc:
            logger.warning(f"LLM 分析失败，回退规则版: {exc}")
            return self.fallback.analyze(news)

    # ---------------- 内部 ----------------
    def _build_payload(self, news: NewsItem) -> Dict[str, Any]:
        prompt = self.prompt_template.format(
            symbols=",".join(news.symbols) or "未标注",
            title=news.title,
            content=(news.content or "")[: self.max_content],
        )
        return {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        """从模型输出里抠出 JSON。模型爱加 ```json 围栏，这里统一容忍。"""
        if not text:
            return None
        text = text.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None

    def _to_analysis(self, news: NewsItem, parsed: Dict[str, Any]) -> NewsAnalysis:
        score = float(parsed.get("score", 0.0) or 0.0)
        score = max(-1.0, min(1.0, score))
        conf = float(parsed.get("confidence", 0.0) or 0.0)
        conf = max(0.0, min(1.0, conf))
        direction = str(parsed.get("direction") or "").lower()
        if direction not in ("bullish", "bearish", "neutral"):
            direction = "bullish" if score > 0.15 else ("bearish" if score < -0.15 else "neutral")
        return NewsAnalysis(
            news_fingerprint=news.fingerprint,
            symbols=list(news.symbols) or [],
            score=round(score, 4),
            confidence=round(conf, 4),
            direction=direction,
            reason=str(parsed.get("reason", ""))[:200],
            analyzer=f"{self.name}:{self.model}",
            raw=parsed,
        )


_ANALYZER_REGISTRY: Dict[str, type] = {
    "keyword": KeywordAnalyzer,
    "llm": LLMAnalyzer,
}


def register_news_analyzer(cls: type) -> type:
    _ANALYZER_REGISTRY[getattr(cls, "name", cls.__name__)] = cls
    return cls


def create_news_analyzer(spec: Any, **defaults: Any) -> IBaseNewsAnalyzer:
    if spec is None:
        spec = {"type": "keyword"}
    if isinstance(spec, IBaseNewsAnalyzer):
        return spec
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise ValueError(f"分析器配置必须是 dict/str，收到 {type(spec).__name__}")

    stype = str(spec.get("type") or "keyword")
    params = dict(defaults)
    params.update(spec.get("params") or {})
    for k, v in spec.items():
        if k not in ("type", "params"):
            params.setdefault(k, v)

    klass = _ANALYZER_REGISTRY.get(stype)
    if klass is None:
        raise ValueError(f"未知分析器: {stype}，可用: {sorted(_ANALYZER_REGISTRY)}")
    return klass(**params)


__all__ = [
    "IBaseNewsAnalyzer",
    "KeywordAnalyzer",
    "LLMAnalyzer",
    "create_news_analyzer",
    "register_news_analyzer",
]
