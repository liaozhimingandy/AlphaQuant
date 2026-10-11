#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : base.py
# @Description : 数据源抽象：Capability（能力）+ Provider（数据源）+ 错误分类
#
#                这一层回答三个问题，它们是整个采集层的地基：
#
#                1. **这个源能干什么？** → ``DataCapability``
#                   以前"能力"就是一个字符串 kind（"daily"/"hot_rank"），
#                   注册时手打、拼错了没人管、IDE 也不提示。
#                   现在是枚举：写错在 import 时就炸，而不是跑到半夜才发现。
#
#                2. **它为什么失败？** → ``ProviderErrorType``
#                   "被限流"、"被封"、"没这只票的数据"、"接口改版了"，
#                   这四件事的处理方式**完全不同**：
#                     - 被限流/被封 → 冷却它，别再撞；
#                     - 没这只票数据 → 源是好的，换下一个源试试；
#                     - 接口改版 → 重试一万次也没用，立刻报错让人改代码。
#                   以前只有"瞬时/确定性"两分法，所以才会出现
#                   "明明是接口改版，却重试 3 次再换源，一轮白白耗掉十几秒"。
#
#                3. **它长什么样？** → ``DataProvider`` + ``@capability``
#                   刻意**不做**万能接口（get_kline/get_financial/get_news 全写一遍
#                   然后 raise NotImplementedError）。那种写法每加一个源就要
#                   补十几个空方法，而且"到底实现了没有"只能靠读代码。
#                   这里改成**按能力注册**：实现了哪个能力就声明哪个，
#                   没声明的能力路由根本不会调用它（NOT_SUPPORTED，不是失败）。
#
#                一个 Provider 可以有**多个同名能力的不同实现**（不同接口/端点），
#                比如 akshare 的人气榜有 emappdata 与 push2 两个端点，
#                它们风控面不同，必须能独立熔断 —— 这就是 Route 的 variant。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set

import pandas as pd

from app.datasource.model.enums import Market

# ===========================================================================
# 一、能力（Capability）
# ===========================================================================
class DataCapability(str, Enum):
    """数据能力。**按能力设计，而不是按数据源设计** —— 这是整个架构的关键。

    "财联社没有 K 线行情、只有基本面新闻" 不是异常，是 **Capability 不匹配**：
    路由看到它没有 DAILY_KLINE 这个能力，压根不会去调它。
    """

    # ---- 行情 ----
    DAILY_KLINE = "daily_kline"
    MINUTE_KLINE = "minute_kline"

    # ---- 榜单 / 资金 ----
    HOT_RANK = "hot_rank"
    FLOW_RANK = "flow_rank"
    FUND_FLOW = "fund_flow"

    # ---- 基本面 ----
    STOCK_BASIC = "stock_basic"
    FINANCIAL_STATEMENT = "financial_statement"
    FINANCIAL_INDICATOR = "financial_indicator"
    VALUATION = "valuation"
    SHAREHOLDER = "shareholder"

    # ---- 另类 ----
    NEWS = "news"
    ANNOUNCEMENT = "announcement"
    INDUSTRY = "industry"
    MACRO = "macro"

    @property
    def label(self) -> str:
        return _CAP_LABELS.get(self, self.value)

    @classmethod
    def parse(cls, value: Any) -> "DataCapability":
        """解析能力。兼容旧链路的 kind 字符串（"daily"/"fund_flow"…）。

        为什么必须兼容：``app/data/marketx.py`` 那套老代码还在用字符串 kind，
        迁移期内新旧两条链路并存，能力名必须能对上，否则会出现
        "新链路注册了、老链路看不见"的分裂。
        """
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        if not text:
            raise ValueError("能力名为空")
        if text in cls._value2member_map_:
            return cls(text)
        alias = _CAP_ALIASES.get(text)
        if alias:
            return alias
        raise ValueError(
            f"未知的数据能力 {text!r}；可用: {[c.value for c in cls]}"
        )


#: 旧 kind 字符串 → 能力
_CAP_ALIASES: Dict[str, "DataCapability"] = {
    "daily": DataCapability.DAILY_KLINE,
    "kline": DataCapability.DAILY_KLINE,
    "daily_kline": DataCapability.DAILY_KLINE,
    "day": DataCapability.DAILY_KLINE,
    "minute": DataCapability.MINUTE_KLINE,
    "hot": DataCapability.HOT_RANK,
    "hot_rank": DataCapability.HOT_RANK,
    "flow": DataCapability.FLOW_RANK,
    "flow_rank": DataCapability.FLOW_RANK,
    "fund": DataCapability.FUND_FLOW,
    "fund_flow": DataCapability.FUND_FLOW,
}

_CAP_LABELS: Dict["DataCapability", str] = {
    DataCapability.DAILY_KLINE: "日线",
    DataCapability.MINUTE_KLINE: "分钟线",
    DataCapability.HOT_RANK: "人气榜",
    DataCapability.FLOW_RANK: "资金榜",
    DataCapability.FUND_FLOW: "个股资金流",
    DataCapability.STOCK_BASIC: "股票基本信息",
    DataCapability.FINANCIAL_STATEMENT: "财务报表",
    DataCapability.FINANCIAL_INDICATOR: "财务指标",
    DataCapability.VALUATION: "估值",
    DataCapability.SHAREHOLDER: "股东",
    DataCapability.NEWS: "新闻",
    DataCapability.ANNOUNCEMENT: "公告",
    DataCapability.INDUSTRY: "行业板块",
    DataCapability.MACRO: "宏观",
}


# ===========================================================================
# 二、错误分类（Failure Type）
# ===========================================================================
class ProviderErrorType(str, Enum):
    """失败类型。**不同失败 = 不同处理**，别再统统当成"重试 3 次"。"""

    NETWORK_ERROR = "network_error"      # 连不上、连接被重置、代理拦截页
    TIMEOUT = "timeout"                  # 超时
    RATE_LIMIT = "rate_limit"            # 限流（429 / 请求太频繁）
    BLOCKED = "blocked"                  # 被封 IP / 反爬 / 403
    AUTH_ERROR = "auth_error"            # token 无效 / 积分不足 / 401
    SERVER_ERROR = "server_error"        # 源站 5xx
    PARSE_ERROR = "parse_error"          # 接口改版、缺列、结构变了
    QUALITY_ERROR = "quality_error"      # 数据自相矛盾（high < close 等）
    INVALID_REQUEST = "invalid_request"  # 我们自己传错了参数
    NO_DATA = "no_data"                  # 源是通的，只是这份数据没有
    NOT_SUPPORTED = "not_supported"      # 这个源压根没这个能力
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return _ERR_LABELS.get(self, self.value)


_ERR_LABELS: Dict["ProviderErrorType", str] = {
    ProviderErrorType.NETWORK_ERROR: "网络错误",
    ProviderErrorType.TIMEOUT: "超时",
    ProviderErrorType.RATE_LIMIT: "限流",
    ProviderErrorType.BLOCKED: "被封禁",
    ProviderErrorType.AUTH_ERROR: "鉴权失败",
    ProviderErrorType.SERVER_ERROR: "源站错误",
    ProviderErrorType.PARSE_ERROR: "解析失败",
    ProviderErrorType.QUALITY_ERROR: "数据质量问题",
    ProviderErrorType.INVALID_REQUEST: "请求参数错误",
    ProviderErrorType.NO_DATA: "无数据",
    ProviderErrorType.NOT_SUPPORTED: "不支持该能力",
    ProviderErrorType.UNKNOWN: "未知错误",
}

#: **可以重试**的失败。重试的前提是"再试一次可能就好了"：
#: 网络抖动、超时、源站 5xx 都是；接口改版、参数传错不是。
RETRYABLE: FrozenSet[ProviderErrorType] = frozenset({
    ProviderErrorType.NETWORK_ERROR,
    ProviderErrorType.TIMEOUT,
    ProviderErrorType.SERVER_ERROR,
    ProviderErrorType.RATE_LIMIT,
})

#: **要计入源的健康度**（触发降分/熔断）的失败。
#: 注意 NO_DATA / NOT_SUPPORTED / INVALID_REQUEST **不在里面**：
#: 没这只票的数据、这个源没这个能力、我们自己参数写错 ——
#: 都不是源的错，惩罚它会让好源被误杀。
PENALIZE: FrozenSet[ProviderErrorType] = frozenset({
    ProviderErrorType.NETWORK_ERROR,
    ProviderErrorType.TIMEOUT,
    ProviderErrorType.RATE_LIMIT,
    ProviderErrorType.BLOCKED,
    ProviderErrorType.AUTH_ERROR,
    ProviderErrorType.SERVER_ERROR,
    ProviderErrorType.PARSE_ERROR,
    ProviderErrorType.QUALITY_ERROR,
})

#: 命中后要**额外冷却**更久的失败（限流/封禁/鉴权，重试只会让情况更糟）
COOLDOWN_SECONDS: Dict[ProviderErrorType, float] = {
    ProviderErrorType.RATE_LIMIT: 600.0,
    ProviderErrorType.BLOCKED: 900.0,
    ProviderErrorType.AUTH_ERROR: 1800.0,
}

#: 判定用的关键词。顺序 = 优先级，先匹配先命中。
_KEYWORDS: Sequence[tuple] = (
    # ---- 无数据（源是好的） ----
    (ProviderErrorType.NO_DATA, (
        "未返回", "无数据", "无此数据", "没有数据", "no data", "empty",
        "停牌", "退市", "未上市", "解析后为空", "返回空数据", "not found",
    )),
    # ---- 限流 / 封禁 / 鉴权 ----
    (ProviderErrorType.RATE_LIMIT, (
        "限流", "频率", "too many requests", "429", "请求过于频繁",
        "访问频率", "rate limit",
    )),
    (ProviderErrorType.BLOCKED, (
        "403", "forbidden", "被封", "封禁", "反爬", "风控", "黑名单",
        "captcha", "验证",
    )),
    (ProviderErrorType.AUTH_ERROR, (
        "401", "unauthorized", "token", "积分", "鉴权", "授权", "权限",
        "auth", "api key", "未登录",
    )),
    # ---- 网络 / 超时 / 源站 ----
    (ProviderErrorType.TIMEOUT, (
        "timeout", "timed out", "read timed out", "超时",
    )),
    (ProviderErrorType.NETWORK_ERROR, (
        "proxyerror", "connectionerror", "connectionreset", "connection reset",
        "connection aborted", "remotedisconnected", "chunkedencoding",
        "incompleteread", "brokenpipe", "sslerror", "max retries",
        "httpsconnectionpool", "name or service not known", "连接被重置",
        "网络", "dns",
    )),
    (ProviderErrorType.SERVER_ERROR, (
        "500", "502", "503", "504", "bad gateway", "server error",
        "internal error",
    )),
    # ---- 解析 / 参数 ----
    (ProviderErrorType.PARSE_ERROR, (
        "缺少", "缺列", "缺少必需列", "keyerror", "valueerror", "解析",
        "json", "expecting value", "列不存在", "columns",
    )),
    (ProviderErrorType.INVALID_REQUEST, (
        "invalid", "参数错误", "参数非法", "multiple values for argument",
        "unexpected keyword",
    )),
)


def classify(exc: BaseException, default: ProviderErrorType = ProviderErrorType.UNKNOWN
             ) -> ProviderErrorType:
    """把任意异常归成一个 :class:`ProviderErrorType`。

    为什么不能只看异常类型：第三方库（akshare/baostock）会把底层原因
    **包进自己的异常里**（``MarketXError("... ProxyError: ...")``），
    光看类型就是普通的 RuntimeError。所以类型 + 消息一起判。
    """
    if isinstance(exc, ProviderError):
        return exc.type

    name = type(exc).__name__.lower()
    text = str(exc).lower()

    # 明确的语义类型优先
    if "dataunavailable" in name:
        return ProviderErrorType.NO_DATA
    if isinstance(exc, NotImplementedError) or "notsupported" in name:
        return ProviderErrorType.NOT_SUPPORTED
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return ProviderErrorType.TIMEOUT
    if "jsondecode" in name:
        # 代理返回拦截页时，接口拿到的是一段 HTML，解析方报 JSONDecodeError。
        # 这类"响应不是我们想要的样子"重试大概率能拿到正常响应。
        return ProviderErrorType.NETWORK_ERROR
    if isinstance(exc, KeyError):
        return ProviderErrorType.PARSE_ERROR
    if isinstance(exc, TypeError):
        return ProviderErrorType.INVALID_REQUEST

    for etype, words in _KEYWORDS:
        for w in words:
            if w in text:
                return etype
    for etype, words in _KEYWORDS:
        for w in words:
            if w in name:
                return etype
    return default


class ProviderError(RuntimeError):
    """一次失败的完整描述：谁、什么能力、什么类型、原因。

    带上 ``type`` 的意义：路由器靠它决定"重试 / 冷却 / 换源 / 直接放弃"，
    而不是靠字符串匹配（那是这轮之前最脆弱的一环）。
    """

    def __init__(self, message: str, *,
                 type: ProviderErrorType = ProviderErrorType.UNKNOWN,
                 provider: str = "",
                 capability: Optional[DataCapability] = None,
                 route: str = "",
                 cause: Optional[BaseException] = None):
        super().__init__(message)
        self.type = type if isinstance(type, ProviderErrorType) else ProviderErrorType(type)
        self.provider = provider
        self.capability = capability
        self.route = route
        self.cause = cause

    @property
    def retryable(self) -> bool:
        return self.type in RETRYABLE

    @property
    def penalize(self) -> bool:
        return self.type in PENALIZE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": str(self)[:300],
            "type": self.type.value,
            "type_label": self.type.label,
            "provider": self.provider,
            "capability": self.capability.value if self.capability else "",
            "route": self.route,
            "retryable": self.retryable,
            "penalize": self.penalize,
        }


def as_provider_error(exc: BaseException, *, provider: str = "",
                      capability: Optional[DataCapability] = None,
                      route: str = "") -> ProviderError:
    """把任意异常包成 :class:`ProviderError`（已是则补上缺失的上下文）。"""
    if isinstance(exc, ProviderError):
        exc.provider = exc.provider or provider
        exc.capability = exc.capability or capability
        exc.route = exc.route or route
        return exc
    return ProviderError(str(exc), type=classify(exc), provider=provider,
                         capability=capability, route=route, cause=exc)


# ===========================================================================
# 三、Provider / Route
# ===========================================================================
@dataclass(slots=True)
class Route:
    """一条**可执行**的取数路径 = Provider 的某个能力的某个实现。

    为什么路由单元不是 Provider：akshare 的人气榜有 emappdata 与 push2
    两个端点，它们风控面不同（实测 push2 被代理拦、emappdata 通）。
    合成一个 Provider 就没法"只熔断挂掉的那个端点"，
    合成两个 Provider 又丢掉了"它们同属 akshare"这个事实。
    所以：Provider 管身份（名字/域名/连接复用），Route 管取数与熔断。
    """

    name: str                        # 路由 id（如 akshare_daily / emapp_hot）
    provider: str                    # 数据源身份（akshare / baostock / …）
    capability: DataCapability
    handler: Callable[..., pd.DataFrame]
    variant: str = ""
    priority: int = 50               # 配置优先级，越大越先
    enabled: bool = True
    fallback: bool = False           # 兜底源：恒排最后，只在全部失败后才试
    hosts: tuple = ()
    market: Market = Market.CN
    description: str = ""

    @property
    def key(self) -> tuple:
        """熔断/健康度的键：**能力级**，不是 Provider 级。

        akshare 的 K 线被限流，不代表它的人气榜也用不了 ——
        所以键里必须有 capability。
        """
        return (self.provider, self.capability.value, self.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "provider": self.provider,
            "capability": self.capability.value,
            "capability_label": self.capability.label,
            "variant": self.variant, "priority": self.priority,
            "enabled": self.enabled, "fallback": self.fallback,
            "hosts": list(self.hosts), "market": self.market.value,
            "description": self.description,
        }


def capability(cap: DataCapability, *, name: str = "", variant: str = "",
               priority: Optional[int] = None, fallback: Optional[bool] = None,
               hosts: Optional[Sequence[str]] = None,
               description: str = "") -> Callable:
    """把一个方法声明为"某个能力的实现"。

    ::

        class AkShareProvider(DataProvider):
            name = "akshare"

            @capability(DataCapability.DAILY_KLINE, name="akshare_daily")
            def _daily(self, symbol, start=None, end=None, adjust="qfq"):
                ...

    :param name: 路由 id，**默认 = provider 名**。同一个 Provider 有多个
        同能力实现时必须显式取名（如 emapp_hot / akshare_spot）。
    :param priority: 覆盖 Provider 的默认优先级。
    :param fallback: 标为兜底（本地库兜底用）。
    :param hosts: 该实现依赖的外网域名，用于域名级快速跳过。
    """
    if not isinstance(cap, DataCapability):
        cap = DataCapability.parse(cap)

    def _deco(fn: Callable) -> Callable:
        meta = {
            "capability": cap, "name": name, "variant": variant,
            "priority": priority, "fallback": fallback,
            "hosts": tuple(hosts or ()), "description": description,
            "attr": fn.__name__, "doc": (fn.__doc__ or "").strip().split("\n")[0],
        }
        setattr(fn, "__data_capability__", meta)
        return fn

    return _deco


class DataProvider:
    """数据源基类。**只声明自己支持的能力**，不做万能接口。

    子类里被 :func:`capability` 装饰的方法会被自动收集（``__init_subclass__``），
    实例化后通过 :meth:`routes` 交给注册表。
    """

    #: 数据源身份（小写英文，如 akshare / baostock / mootdx）
    name: str = ""
    market: Market = Market.CN
    #: 本 Provider 默认依赖的域名（可被 capability 级覆盖）
    hosts: tuple = ()
    #: 默认优先级（配置里没写时用这个）
    priority: int = 50
    #: 是否兜底源（本地库兜底这类）
    fallback: bool = False
    description: str = ""

    #: 子类收集到的能力声明（由 __init_subclass__ 填）
    _CAP_META_: List[Dict[str, Any]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        meta: List[Dict[str, Any]] = []
        for attr, value in list(vars(cls).items()):
            m = getattr(value, "__data_capability__", None)
            if isinstance(m, dict):
                item = dict(m)
                item["attr"] = attr
                meta.append(item)
        # 继承父类的声明（子类覆盖同名 attr 时以子类为准）
        parents: Dict[str, Dict[str, Any]] = {}
        for base in cls.__mro__[1:]:
            for m in getattr(base, "_CAP_META_", []):
                parents.setdefault(m.get("attr", ""), m)
        for attr, m in parents.items():
            if not any(x.get("attr") == attr for x in meta):
                meta.append(m)
        cls._CAP_META_ = meta

    # ---------------- 能力 ----------------
    @property
    def capabilities(self) -> Set[DataCapability]:
        return {m["capability"] for m in type(self)._CAP_META_}

    def supports(self, cap: Any) -> bool:
        try:
            cap = DataCapability.parse(cap)
        except ValueError:
            return False
        return cap in self.capabilities

    def routes(self) -> List[Route]:
        """把类声明实例化成 Route 列表（handler 绑定到 self）。"""
        out: List[Route] = []
        for m in type(self)._CAP_META_:
            fn = getattr(self, m["attr"], None)
            if fn is None:
                continue
            name = m["name"] or (f"{self.name}_{m['variant']}" if m["variant"]
                                 else self.name)
            out.append(Route(
                name=name,
                provider=self.name,
                capability=m["capability"],
                handler=fn,
                variant=m["variant"] or "",
                priority=self.priority if m["priority"] is None else int(m["priority"]),
                enabled=True,
                fallback=self.fallback if m["fallback"] is None else bool(m["fallback"]),
                hosts=tuple(m["hosts"]) or tuple(self.hosts),
                market=self.market,
                description=m["description"] or m.get("doc", ""),
            ))
        return out

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "market": self.market.value,
            "hosts": list(self.hosts),
            "priority": self.priority,
            "fallback": self.fallback,
            "description": self.description,
            "capabilities": sorted(c.value for c in self.capabilities),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        caps = ",".join(sorted(c.value for c in self.capabilities))
        return f"<Provider {self.name or '?'} capabilities=[{caps}]>"


__all__ = [
    "COOLDOWN_SECONDS",
    "DataCapability",
    "DataProvider",
    "PENALIZE",
    "ProviderError",
    "ProviderErrorType",
    "RETRYABLE",
    "Route",
    "as_provider_error",
    "capability",
    "classify",
]
