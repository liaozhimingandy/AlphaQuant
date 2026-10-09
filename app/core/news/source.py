#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : source.py
# @Description : 可插拔新闻源：RSS/Atom 抓取 + 解析 + 标的识别
#               不依赖 feedparser（环境里没装），用 lxml 自解析，够用且可控
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.core.market.types import NewsItem
from app.utils.logger import logger

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

try:
    from lxml import etree
except Exception:  # pragma: no cover
    etree = None  # type: ignore


#: A股代码：6 位数字，可选 sh/sz 前缀
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
#: "平安银行(000001)" / "000001.SZ" 这类带前缀的写法
_PREFIX_RE = re.compile(r"\b(?:sh|sz|bj)\.?(\d{6})\b", re.IGNORECASE)

_TIME_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",   # RFC 822 (RSS 2.0)
    "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S%z",        # ISO 8601 (Atom)
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


class IBaseNewsSource(ABC):
    """新闻源接口。"""

    name: str = "base"

    @abstractmethod
    def fetch(self) -> List[NewsItem]:
        """拉取新闻。允许抛异常，由 NewsCenter 统一兜底。"""


def extract_symbols(text: str, keyword_map: Optional[Dict[str, str]] = None) -> List[str]:
    """从标题/正文里抽取股票代码。

    两级策略：
      1. 关键词映射（"平安银行" -> 000001），这是最可靠的，用户可以自己配
      2. 正则兜底抓 6 位数字
    """
    syms: List[str] = []
    if keyword_map:
        for kw, code in keyword_map.items():
            if kw and kw in text:
                syms.append(str(code))
    syms.extend(_PREFIX_RE.findall(text or ""))
    syms.extend(_CODE_RE.findall(text or ""))
    # 去重保序
    seen, out = set(), []
    for s in syms:
        s = str(s).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def parse_published(value: Optional[str]) -> Optional[datetime]:
    """尽力解析时间；解析不出来返回 None（不要让一条时间格式异常的新闻污染整批）。"""
    if not value:
        return None
    value = value.strip()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:  # 最后交给 fromisoformat
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        logger.debug(f"无法解析新闻时间: {value!r}")
        return None


def _local(tag: str) -> str:
    """去掉 XML 命名空间。"""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _find_text(node: Any, names: Iterable[str]) -> str:
    """在子节点里按 local-name 找第一个有文本的。"""
    wanted = set(names)
    for child in node.iter():
        if child is node:
            continue
        if _local(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _find_link(node: Any) -> str:
    for child in node.iter():
        if child is node:
            continue
        if _local(child.tag) == "link":
            href = child.get("href")
            if href:
                return href.strip()
            if child.text:
                return child.text.strip()
    return ""


def parse_feed(xml_bytes: bytes, source_name: str, keyword_map=None) -> List[NewsItem]:
    """同时支持 RSS 2.0 与 Atom 1.0。"""
    if etree is None:
        raise RuntimeError("缺少 lxml，无法解析 RSS/Atom")
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(xml_bytes, parser=parser)

    items: List[NewsItem] = []
    for node in root.iter():
        lname = _local(node.tag)
        if lname not in ("item", "entry"):
            continue
        title = _find_text(node, ("title",))
        if not title:
            continue
        content = _find_text(node, ("description", "summary", "content", "encoded"))
        # 正文里的 HTML 标签直接剥掉，只留文本
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
        items.append(
            NewsItem(
                title=title,
                content=content,
                url=_find_link(node),
                source=source_name,
                published_at=parse_published(
                    _find_text(node, ("pubDate", "published", "updated", "date"))
                ),
                symbols=extract_symbols(f"{title} {content}", keyword_map),
            )
        )
    return items


class RssNewsSource(IBaseNewsSource):
    """RSS/Atom 新闻源。

    抓取是**阻塞 IO**，必须在线程池里调用（NewsCenter 已处理），
    绝不能直接在 reactor 线程里跑——否则整个引擎会被一个慢源卡死。
    """

    name = "rss"

    def __init__(
        self,
        urls: Iterable[str] | str,
        timeout: float = 10.0,
        keyword_map: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        label: str = "rss",
    ) -> None:
        if isinstance(urls, str):
            urls = [urls]
        self.urls: List[str] = [u for u in urls if u]
        self.timeout = float(timeout)
        self.keyword_map = keyword_map or {}
        self.headers = headers or {
            "User-Agent": "Mozilla/5.0 (compatible; AlphaQuant/1.0)"
        }
        self.label = label

    def fetch(self) -> List[NewsItem]:
        if requests is None:
            raise RuntimeError("缺少 requests，无法抓取 RSS")
        out: List[NewsItem] = []
        for url in self.urls:
            try:
                resp = requests.get(url, timeout=self.timeout, headers=self.headers)
                resp.raise_for_status()
                items = parse_feed(resp.content, self.label or url, self.keyword_map)
                logger.info(f"[{self.label}] {url} 抓到 {len(items)} 条")
                out.extend(items)
            except Exception as exc:
                # 单个源失败不能影响其它源
                logger.warning(f"[{self.label}] 抓取失败 {url}: {exc}")
        return out


class MockNewsSource(IBaseNewsSource):
    """测试/演示用：按脚本吐新闻。"""
    name = "mock"

    def __init__(self, items: Optional[List[NewsItem]] = None) -> None:
        self._items: List[NewsItem] = list(items or [])
        self._cursor = 0

    def push(self, item: NewsItem) -> None:
        self._items.append(item)

    def fetch(self) -> List[NewsItem]:
        batch = self._items[self._cursor:]
        self._cursor = len(self._items)
        return batch


_NEWS_SOURCE_REGISTRY: Dict[str, type] = {"rss": RssNewsSource, "mock": MockNewsSource}


def register_news_source(cls: type) -> type:
    _NEWS_SOURCE_REGISTRY[getattr(cls, "name", cls.__name__)] = cls
    return cls


def create_news_source(spec: Any, **defaults: Any) -> IBaseNewsSource:
    """从配置构造新闻源。spec 可以是 str / dict / 实例。"""
    if isinstance(spec, IBaseNewsSource):
        return spec
    if isinstance(spec, str):
        spec = {"type": spec, "urls": [spec]}
    if not isinstance(spec, dict):
        raise ValueError(f"新闻源配置必须是 dict/str，收到 {type(spec).__name__}")

    stype = str(spec.get("type") or "rss")
    params = dict(defaults)
    params.update(spec.get("params") or {})
    for k, v in spec.items():
        if k not in ("type", "params"):
            params.setdefault(k, v)

    klass = _NEWS_SOURCE_REGISTRY.get(stype)
    if klass is None:
        raise ValueError(
            f"未知新闻源: {stype}，可用: {sorted(_NEWS_SOURCE_REGISTRY)}"
        )
    return klass(**params)


__all__ = [
    "IBaseNewsSource",
    "RssNewsSource",
    "MockNewsSource",
    "create_news_source",
    "register_news_source",
    "parse_feed",
    "extract_symbols",
    "parse_published",
]
