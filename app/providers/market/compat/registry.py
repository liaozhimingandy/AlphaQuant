#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : sources.py
# @Description : 数据源注册表 —— "一个源挂了自动换下一个"的通用机制
#
#                为什么要抽这一层：
#                  取数失败是常态而不是例外 —— 东财 push2his 被本地代理拦、
#                  Tushare 积分不够、源站改版、非交易日返回空、某只票停牌……
#                  如果每个 fetch_xxx() 都自己写 if/else 切源，
#                  加一个新源要改 N 处代码，而且失败原因永远说不清。
#
#                这一层只负责四件事：
#                  1. **注册**：一个源 = 一个函数 + 它支持哪些类型 + 一个**权重**；
#                  2. **排序**：先按权重降序，权重相同再按
#                     settings.DATA_SOURCE_PRIORITY 决定尝试顺序；
#                  3. **切换**：逐个尝试，只有**源级故障**才计入熔断；
#                  4. **降权**：源不可用就降权（见 :data:`WEIGHT_DECAY`），
#                     让它排到后面；恢复了再慢慢涨回来。
#                     "挂了还一直排第一"是最浪费请求额度的失败模式。
#
#                一个容易搞错的点（这里专门做了区分）：
#                  "这个源连不上"（代理拦截 / 鉴权失败 / 超时）是**源坏了**，
#                  该熔断它，别让整批请求反复撞墙；
#                  "这只票今天没数据"（停牌 / 退市 / 非交易日）是**数据缺失**，
#                  源本身是好的，绝不能因为连续几只票没数据就把源熔断掉。
#                所以适配器要用不同异常表达这两种情况（见 SourceError /
#                DataUnavailable）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Set

import pandas as pd

from app.common.config import settings
from app.common.logging import log_throttled, logger

# ===========================================================================
# 权重参数
# ===========================================================================
#: 注册时的默认权重。所有源初始都是 1 —— 顺序完全由 DATA_SOURCE_PRIORITY 决定；
#: 只有"实测不可用"才会拉开差距，这样排序的初始状态是可预期的。
DEFAULT_WEIGHT: float = 1.0
#: 每次**源级**失败的降权系数（1 → 0.5 → 0.25 → ...）
WEIGHT_DECAY: float = 0.5
#: 权重下限。降到 0 就等于永久下线，那不如 unregister；
#: 留一个下限是给"它哪天又活了"留机会。
WEIGHT_MIN: float = 0.1
#: 每次成功回涨的步长。刻意**远小于**降权幅度：
#: 一个抽风的源要连续成功好几次才配回到队首，否则"时好时坏"的源会反复插队。
WEIGHT_RECOVER: float = 0.1


class SourceError(RuntimeError):
    """**源级**故障：连不上、被鉴权拒绝、接口被封、返回结构变了。

    抛出它 = 告诉注册表"这个源现在不可用"，会计入连续失败并触发熔断。
    """


class DataUnavailable(RuntimeError):
    """**数据级**缺失：源是好的，只是这份数据没有（停牌/退市/非交易日/该票无记录）。

    不算源坏，不触发熔断，只会跳到下一个源试试。
    """


@dataclass
class FetchEvent:
    """一次采集尝试的记录 —— 回答"这批数据到底是谁给的"。

    以前失败日志只有一句 "hot 获取失败"，看不出试过哪些源、各试几次、
    最后是哪个源给的。现在每次采集都留痕，面板和日志都能直接回答。
    """
    kind: str                    # 数据类型：fund_flow / hot_rank / flow_rank
    source: str                  # 实际使用的数据源名
    ok: bool                     # 是否成功
    attempts: int = 0            # 该源尝试了几次
    rows: Optional[int] = None   # 拿到多少行
    ms: int = 0                  # 耗时（毫秒）
    error: str = ""              # 失败原因（成功时为空）
    at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "source": self.source, "ok": self.ok,
            "attempts": self.attempts, "rows": self.rows, "ms": self.ms,
            "error": self.error[:300], "at": self.at,
        }


def is_transient(exc: Exception) -> bool:
    """判断异常是不是"瞬时网络类"（代理抽风 / 连接重置 / 超时 / SSL 抖动）。

    **接口变更、缺列、当日无数据**属于确定性错误，重试没有意义，
    必须立刻上报而不是反复撞墙。
    """
    name = type(exc).__name__
    if any(k in name for k in (
        "ProxyError", "Proxy", "ConnectionError", "ConnectError", "Timeout",
        "ReadTimeout", "RemoteDisconnected", "ChunkedEncoding", "SSLError",
        "HTTPError", "ConnectionReset", "BrokenPipe", "IncompleteRead",
        # 代理返回拦截页 / 反爬页时，接口拿到的是一段 HTML，
        # 解析方会报 JSONDecodeError。这类"响应不是我们想要的样子"
        # 重试大概率能拿到正常响应，所以按瞬时处理。
        "JSONDecodeError",
    )):
        return True
    # 上层常把底层原因包进自己的异常里（如 MarketXError("... ProxyError: ...")），
    # 所以再扫一遍消息 —— 否则"包装后"的代理错误会被判成确定性错误而不重试。
    msg = str(exc)
    return any(k in msg for k in (
        "ProxyError", "ConnectionError", "RemoteDisconnected", "Timeout",
        "Max retries", "Connection reset", "Connection aborted",
        "HTTPSConnectionPool", "Read timed out",
    ))


#: 解析类失败的关键词。TCP 是通的、响应也拿到了，只是**结构对不上** ——
#: 与网络层失败是两回事，混在一起会把排查方向带偏（见 ``is_parse_error``）。
_PARSE_ERROR_WORDS = (
    "ValueError", "KeyError", "Length mismatch", "length mismatch",
    "缺少", "缺列", "缺少必需列", "解析", "parse_error",
)


def is_parse_error(exc: BaseException) -> bool:
    """这次失败是"源返回的东西变了"，而不是"连不上源"。

    典型：akshare 内部给 DataFrame 赋列名时抛
    ``ValueError: Length mismatch: Expected axis has 13 elements, new values have 7`` ——
    东财把返回字段数改了，库还没跟上。

    **必须和"域名不可达"分开**：解析失败时 TCP 连接是好的，
    若按网络失败处理，会顺手把域名冷却 5 分钟，并在日志里提示
    "检查代理端口"——于是真正的病因（接口改版，得改适配代码）
    被一句误导性的提示盖住，排查从"改代码"变成"查网络"。
    """
    try:
        from app.providers.market.provider.base import (
            ProviderErrorType,
            classify,
        )

        return classify(exc) is ProviderErrorType.PARSE_ERROR
    except Exception:
        text = f"{type(exc).__name__}: {exc}"
        return any(w in text for w in _PARSE_ERROR_WORDS)


def is_parse_error_text(text: str) -> bool:
    """字符串版解析失败判断（告警拼接时手里只有字符串）。

    与 :func:`is_parse_error` 用同一套关键词，避免两处各写一遍。
    """
    return any(w in (text or "") for w in _PARSE_ERROR_WORDS)


def is_network_error_text(text: str) -> bool:
    """对**已经格式化成字符串**的错误判断它是不是网络层问题。

    ``is_transient`` 吃的是异常对象，但告警拼接时手里只有字符串，
    这里把同样的关键词判断暴露出来，避免两处逻辑各写一遍。
    """
    return any(k in (text or "") for k in (
        "ProxyError", "ConnectionError", "RemoteDisconnected", "Timeout",
        "Max retries", "Connection reset", "Connection aborted",
        "HTTPSConnectionPool", "Read timed out", "JSONDecodeError",
        "SSLError", "ChunkedEncoding",
    ))


def network_hint() -> str:
    """生成一段"下一步该查什么"的提示 —— 给"所有源都失败"这种时刻用。

    只报"所有数据源均失败"等于没报：用户真正需要知道的是
    **是不是代理拦的、代理指向哪、该去哪里验证**。
    环境变量是硬事实，直接打出来比让人猜快得多。
    """
    from app.providers.market.transport import net

    parts = []
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        val = os.environ.get(var)
        if val:
            parts.append(f"{var}={val}")
    if not parts:
        # 环境变量里没有，不代表"这台机器不能走代理" ——
        # 很可能装了 Clash（7890 在监听）只是没设变量，
        # 守护进程又不继承 shell 变量。先探一下再说，别误导人去改代码。
        found = net._autodetect_proxy()
        if found:
            return (
                "排查：未设代理环境变量，但检测到本机 " + found + " 有代理在监听 "
                "（疑似 Clash）—— 直连东财会被 RST。"
                "请在 config 里设 DATA_PROXY=" + found + "，或给启动进程设 HTTP_PROXY；"
                "验证命令：alphaquant sources --probe"
            )
        return ("排查：本机未设置代理环境变量，且未在常见端口"
                f"（{'/'.join(str(p) for p in net.COMMON_PROXY_PORTS[:4])}…）"
                "发现代理。若处在公司网络/Clash 后面，请确认代理端口真实在监听"
                "（netstat -ano | findstr :端口），并设 DATA_PROXY 指向它")
    reach = net.proxy_reachable()
    tail = (" —— 端口若无程序监听即为僵尸代理，改指向可用端口或临时取消；"
            "验证命令：alphaquant sources --probe")
    if reach is False:
        return ("排查：当前代理环境变量 " + " ".join(parts)
                + " **但端口连不上（僵尸代理）**，所有请求都会卡到超时后失败。"
                " 改指向可用端口，或在配置里设 DATA_PROXY 覆盖它。" + tail)
    return "排查：当前代理环境变量 " + " ".join(parts) + tail


@dataclass
class SourceSpec:
    """一个数据源的登记信息。"""
    name: str
    fetch: Callable[..., pd.DataFrame]
    supports: Set[str] = field(default_factory=set)
    enabled: bool = True

    #: **兜底源**：永远排在最后，不管权重多高。
    #: 本地库兜底（用库里已有数据顶上）必须标这个 —— 它不会失败，
    #: 权重会一路涨回 1，若按权重排序它迟早抢到队首，
    #: 于是"明明有网也一直用本地旧数据"，比拿不到数据更难发现。
    #: 兜底的意义是"别人都不行时顶上"，不是"参与竞争"。
    fallback: bool = False

    #: 该源**依赖的外网域名**（如 ``["push2.eastmoney.com"]``）。
    #: 之前三个源里有两个都压在同一个被拦的域名上，却还要各自等满超时才失败，
    #: 一轮采集白白耗掉十几秒。声明域名后，注册表在动手前先问一次
    #: ``net.is_blocked``，域名已在冷却期就直接跳过这个源（并记一条可读的
    #: 采集事件），把超时预算留给真正还有希望的源。
    #: 留空 = 不限制（本地源、未知域名的源）。
    hosts: Set[str] = field(default_factory=set)

    # ---- 权重 ----
    #: 当前权重：**越大越先被尝试**。运行时会随成败上下浮动。
    weight: float = DEFAULT_WEIGHT
    #: 基准权重 = 注册时给定的初值（默认 1）。恢复最多回到这里，
    #: 不会"用得多了就越涨越高"。
    base_weight: float = DEFAULT_WEIGHT

    # ---- 运行时健康状态（由注册表维护，注册时不用填） ----
    fail_count: int = 0          # 连续源级失败次数
    dead_until: float = 0.0      # 熔断到期时刻（time.monotonic）
    last_ok: Optional[float] = None
    last_error: str = ""


class SourceRegistry:
    """数据源注册表：注册 → 排序 → 重试 → 自动故障切换 → 告警。

    一次采集的完整流程（也是排查"取不到数据"时的阅读顺序）::

        采集[hot_rank] 开始：可用数据源 3 个 -> akshare_em → xueqiu_hot → akshare_spot
        采集[hot_rank] · akshare_em 第1/4次失败（可重试）: ProxyError ... → 0.5s 后重试
        采集[hot_rank] · akshare_em 已重试 4 次仍失败，切换下一个源 → xueqiu_hot
        采集[hot_rank] 成功 · xueqiu_hot（第 1 次尝试，耗时 1.2s，100 行）
    """

    def __init__(self, circuit_threshold: int = 3, cooldown: float = 300.0,
                 retries: int = 3, backoff_base: float = 0.5,
                 weight_decay: float = WEIGHT_DECAY,
                 weight_min: float = WEIGHT_MIN,
                 weight_recover: float = WEIGHT_RECOVER,
                 persist_path: Optional[str] = None):
        self._specs: Dict[str, SourceSpec] = {}
        self._order: List[str] = []
        self.circuit_threshold = int(circuit_threshold)
        self.cooldown = float(cooldown)
        # 权重的三个参数做成实例属性：单测里可以用夸张的衰减系数
        # 一步看出"降权 → 排序变化"，不用真的失败十几次。
        self.weight_decay = float(weight_decay)
        self.weight_min = float(weight_min)
        self.weight_recover = float(weight_recover)
        #: 权重落盘位置。None = 不持久化（单测与临时注册表用）。
        self.persist_path = persist_path
        #: 已经落盘但对应源尚未注册的权重。注册发生在 import 期，
        #: 而文件是构造时读的 —— 先存着，register 时再套用。
        self._pending_weights: Dict[str, float] = {}
        if persist_path:
            self._pending_weights = self._load_weights()
        # 每个源的重试次数：失败先原地重试 N 次（仅瞬时错误），再换下一个源。
        # 取不到配置时兜底 3 次。设 0 = 不重试、失败立刻换源。
        try:
            self.retries = int(retries if retries is not None
                               else (settings.DATA_FETCH_MAX_RETRY or 0))
        except Exception:
            self.retries = 3
        self.backoff_base = float(backoff_base)
        # 最近采集事件（供面板/日志回答"这批数据来自哪个源"）
        self.events: Deque[FetchEvent] = deque(maxlen=100)
        # 注入点：测试里替换成假 sleep，不用真等退避时间
        self._sleep: Callable[[float], None] = time.sleep

    # ---------------- 注册 ----------------
    def register(self,
                 name: str,
                 supports: Sequence[str],
                 fetch: Optional[Callable[..., pd.DataFrame]] = None,
                 enabled: bool = True,
                 weight: Optional[float] = None,
                 fallback: bool = False,
                 hosts: Optional[Sequence[str]] = None):
        """注册一个数据源。可当装饰器用，也可直接调用。

        :param weight: 初始权重，**默认 1**。权重越大越先被尝试；
            实测不可用会自动降权。想让某个源常驻队尾就给它 0.5，
            想让它常驻队首就给 2 —— 但更推荐用 `DATA_SOURCE_PRIORITY`，
            权重留给"运行时根据健康度自动调整"。
        :param fallback: 标为**兜底源**：恒排在所有普通源之后，
            不参与权重竞争。给"本地库兜底"这类源用——
            它几乎不会失败，按权重排迟早会抢到队首，那反而更糟。
        :param hosts: 该源依赖的外网域名。声明后，若这些域名已被网络层
            判定为"暂不可达"，本源会被**直接跳过**（不再等超时）。

        ::

            @REGISTRY.register("my_source", supports=["fund_flow"])
            def _fetch(symbol, days=None):
                ...

            # 或
            REGISTRY.register("my_source", supports=["fund_flow"], fetch=_fetch)
        """
        base, current = self._resolve_weight(name, weight)

        def _wrap(fn: Callable[..., pd.DataFrame]) -> Callable[..., pd.DataFrame]:
            self._specs[name] = SourceSpec(
                name=name, fetch=fn, supports=set(supports), enabled=enabled,
                weight=current, base_weight=base, fallback=bool(fallback),
                hosts=set(hosts or ()),
            )
            if name not in self._order:
                self._order.append(name)
            logger.debug(
                f"注册数据源 {name}，支持: {sorted(self._specs[name].supports)}"
                f"，权重 {current:g}（基准 {base:g}）"
                + ("，兜底源" if fallback else "")
                + (f"，依赖域名 {sorted(self._specs[name].hosts)}" if hosts else "")
            )
            return fn

        if fetch is not None:
            _wrap(fetch)
            return fetch
        return _wrap

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)
        if name in self._order:
            self._order.remove(name)

    # ---------------- 权重 ----------------
    def _resolve_weight(self, name: str,
                        explicit: Optional[float]) -> tuple[float, float]:
        """决定一个源注册时的权重，返回 ``(基准, 当前)``。

        两者为什么要分开：**基准**是"它值多少"（人写的初值），
        **当前**是"它现在值多少"（实测跑出来的）。显式指定时二者相同；
        从落盘恢复时基准仍是 1（那是它应有的地位），当前值是上次跑出来的
        健康度 —— 于是"曾经挂过但已在恢复"的源表现为 0.5/1.0，
        既能排到后面，也还能慢慢涨回 1。

        优先级：**显式参数 > 上次落盘的权重 > 环境变量配置 > 默认 1**。
        把"落盘"放在环境变量前面，是因为落盘值是**实测跑出来的**，
        比人写死的配置更贴近当前网络状况。
        """
        if explicit is not None:
            try:
                value = max(0.0, float(explicit))
                return value, value
            except (TypeError, ValueError):
                logger.warning(f"数据源 {name} 的权重 {explicit!r} 非法，按默认 1 处理")
                return DEFAULT_WEIGHT, DEFAULT_WEIGHT
        if name in self._pending_weights:
            return DEFAULT_WEIGHT, self._pending_weights.pop(name)
        try:
            raw = settings.data_source_weights().get(name)
        except Exception:
            raw = None
        if raw is None:
            return DEFAULT_WEIGHT, DEFAULT_WEIGHT
        try:
            return DEFAULT_WEIGHT, max(0.0, float(raw))
        except (TypeError, ValueError):
            logger.warning(f"数据源 {name} 的配置权重 {raw!r} 非法，按默认 1 处理")
            return DEFAULT_WEIGHT, DEFAULT_WEIGHT

    def set_weight(self, name: str, weight: float) -> float:
        """手动设定权重，并把它设为新的基准（之后恢复不会超过这个值）。"""
        spec = self._specs.get(name)
        value = max(0.0, float(weight))
        if spec is None:
            self._pending_weights[name] = value
            self._save_weights()
            return value
        spec.weight = value
        spec.base_weight = value
        self._pending_weights.pop(name, None)
        self._save_weights()
        logger.info(f"数据源 {name} 权重设为 {value:g}")
        return value

    def reset_weight(self, name: Optional[str] = None) -> None:
        """把权重恢复到基准值（默认 1），同时清掉熔断。

        排查完"代理端口写错"这类环境问题后，用它让所有源重新公平竞争 ——
        否则一个被降权的源会一直排在后面，明明修好了却看不出来。
        """
        for spec in self._specs.values():
            if name and spec.name != name:
                continue
            spec.weight = spec.base_weight
            spec.fail_count = 0
            spec.dead_until = 0.0
            spec.last_error = ""
        self._pending_weights.pop(name or "", None)
        self._save_weights()

    def weights(self) -> Dict[str, float]:
        """{源名: 当前权重}（面板 / 排障用）。"""
        return {n: round(self._specs[n].weight, 4) for n in self._order
                if n in self._specs}

    def _decay_weight(self, spec: SourceSpec) -> None:
        before = spec.weight
        spec.weight = max(self.weight_min, round(spec.weight * self.weight_decay, 6))
        if spec.weight < before:
            logger.info(
                f"数据源 {spec.name} 不可用，权重 {before:g} → {spec.weight:g}"
            )
            self._save_weights()

    def _recover_weight(self, spec: SourceSpec) -> None:
        if spec.weight >= spec.base_weight:
            return
        before = spec.weight
        spec.weight = min(spec.base_weight,
                          round(spec.weight + self.weight_recover, 6))
        if spec.weight > before:
            logger.debug(
                f"数据源 {spec.name} 恢复，权重 {before:g} → {spec.weight:g}"
            )
            self._save_weights()

    def _save_weights(self) -> None:
        """权重落盘。目的是**跨重启记住哪个源不靠谱**。

        不落盘的话，每次重启坏源又排回第一位，头几批采集全撞墙 ——
        恰恰是"昨晚网络坏了、今早起来看"最需要避免的情况。
        写失败（只读盘 / 权限）无所谓，只是丢了这个便利，不影响取数。
        """
        if not self.persist_path:
            return
        try:
            path = Path(self.persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "_comment": "数据源权重：运行时根据成败自动调整，初始默认 1。"
                            "手工改也可以；想全部恢复默认就删掉本文件。",
                "weights": self.weights(),
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug(f"数据源权重落盘失败（忽略）: {exc}")

    def _load_weights(self) -> Dict[str, float]:
        if not self.persist_path:
            return {}
        try:
            path = Path(self.persist_path)
            if not path.exists():
                return {}
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            weights = raw.get("weights") or {}
            if not isinstance(weights, dict):
                return {}
            out: Dict[str, float] = {}
            for k, v in weights.items():
                try:
                    out[str(k)] = max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
            if out:
                logger.info(f"已加载数据源权重: {out}")
            return out
        except Exception as exc:
            logger.debug(f"数据源权重读取失败（用默认值）: {exc}")
            return {}

    # ---------------- 查询 / 排序 ----------------
    def sources_for(self, kind: str) -> List[SourceSpec]:
        """返回支持该数据类型的数据源，**按权重降序**、同权重再按优先级。

        顺序怎么定（这条规则要能被一眼复述出来）：
          0. **兜底源恒排最后** —— 它不参与竞争，只在别人全挂时顶上；
          1. **权重高的先试** —— 权重是运行时跑出来的健康度，比人写的配置准；
          2. 权重相同才看 `DATA_SOURCE_PRIORITY` 里的先后；
          3. 配置里没提到、但又注册了的源排在后面当兜底 ——
             这样"加新源"不必强制改配置就能生效。

        所有源初始权重都是 1，所以**不改任何配置时顺序 == 优先级顺序**，
        权重只在真的出现失败后才开始起作用。
        """
        preferred = settings.priority_list()

        def _rank(name: str):
            spec = self._specs[name]
            try:
                prio = (0, preferred.index(name))
            except ValueError:
                prio = (1, self._order.index(name) if name in self._order else 999)
            # 注意：权重取负号实现降序，同时保留稳定的次级排序键
            return (1 if spec.fallback else 0,
                    -round(spec.weight, 6)) + prio

        names = [n for n in self._specs
                 if kind in self._specs[n].supports and self._specs[n].enabled]
        return [self._specs[n] for n in sorted(names, key=_rank)]

    def is_healthy(self, name: str) -> bool:
        spec = self._specs.get(name)
        if spec is None or not spec.enabled:
            return False
        return time.monotonic() >= spec.dead_until

    # ---------------- 取数（重试 → 自动切换 → 告警） ----------------
    def fetch(self, kind: str, retries: Optional[int] = None,
              **kwargs: Any) -> pd.DataFrame:
        """按优先级依次尝试所有支持该类型的源：

        **每个源先重试 N 次（只重试瞬时错误），仍失败才换下一个源**；
        所有源都用尽时打 WARNING 告警并抛出 `SourceError`（带各源原因）。

        日志会明确写出：用了哪个源、试了几次、耗时多久、拿到几行 ——
        这是"取不到数据时能不能自己定位"的关键。

        :param retries: 覆盖每个源的重试次数（默认取注册表配置，即
            `DATA_FETCH_MAX_RETRY`）
        :raises SourceError: 所有源都失败
        """
        if "kind" in kwargs:
            # fetch() 的第一个位置参数就叫 kind，适配器参数再叫 kind 会撞车
            # （TypeError: got multiple values for argument 'kind'），
            # 与其让它在运行时炸，不如这里直接说清楚。
            raise TypeError(
                "fetch() 的第一个位置参数就是 kind，适配器的参数请换个名字"
                "（如 rank_kind / trade_date），别再叫 kind"
            )

        # 采集前先确认代理不是"僵尸端口"。第三方库（akshare/baostock）自己发请求，
        # 不走我们的传输层，只有从环境变量层摘掉代理才能救它们。
        try:
            from app.providers.market.transport import net

            net.sanitize_proxy()
        except Exception:
            pass

        specs = self.sources_for(kind)
        if not specs:
            raise SourceError(
                f"{kind}: 没有注册任何支持该类型的数据源"
                f"（已注册: {sorted({s for sp in self._specs.values() for s in sp.supports})}）"
            )

        tries = self.retries if retries is None else int(retries)
        tries = max(0, tries)
        now = time.monotonic()
        errors: List[str] = []
        order = [s.name for s in specs]
        # 把权重写进这行日志：排障时"为什么先试它"必须一眼可见，
        # 否则看到顺序变了却不知道是权重在起作用，会误判成配置改错了。
        logger.info(f"采集[{kind}] 开始：可用数据源 {len(specs)} 个 -> "
                    + " → ".join(f"{s.name}(w{s.weight:g})" for s in specs)
                    + f"（每个源最多重试 {tries} 次）")

        for idx, spec in enumerate(specs):
            if now < spec.dead_until:
                why = f"{spec.name}: 熔断中(剩{int(spec.dead_until - now)}s)"
                errors.append(why)
                logger.info(f"采集[{kind}] · {spec.name} 处于熔断冷却，跳过")
                continue

            # ---- 域名级快速跳过 ----
            # 源自己不知道网络状况，但注册表知道：这个源依赖的域名刚刚连续
            # 失败过。与其让它再等一遍超时（几十秒），不如此刻就判死。
            # 三个源里两个共用 push2 时，这一句能省掉整轮采集的大半时间。
            bad = self._blocked_hosts(spec)
            if bad:
                why = f"{spec.name}: 依赖域名不可达({bad})"
                errors.append(why)
                self._log_event(kind, spec.name, False, why, 0.0, 0)
                logger.info(f"采集[{kind}] · {spec.name} 依赖的域名暂不可达，"
                            f"直接跳过 -> {bad}")
                continue

            last_exc: Optional[BaseException] = None
            for attempt in range(tries + 1):
                t0 = time.monotonic()
                try:
                    df = spec.fetch(**kwargs)
                except DataUnavailable as exc:
                    # 源是好的，只是这份数据没有 —— 不重试、不惩罚，直接换源
                    spec.last_error = str(exc)
                    errors.append(f"{spec.name}: {exc}")
                    logger.info(f"采集[{kind}] · {spec.name} 无此数据（{exc}），"
                                f"换下一个源")
                    last_exc = None
                    break
                except Exception as exc:  # SourceError 也在这里（它是 Exception 子类）
                    last_exc = exc
                    if attempt < tries and is_transient(exc):
                        wait = self._backoff(attempt)
                        logger.warning(
                            f"采集[{kind}] · {spec.name} 第{attempt + 1}/{tries + 1}"
                            f"次失败（瞬时·可重试）: {type(exc).__name__}: "
                            f"{_brief(exc, 110)} → {wait:.1f}s 后重试"
                        )
                        self._sleep(wait)
                        continue
                    if attempt == 0:
                        logger.info(
                            f"采集[{kind}] · {spec.name} 失败（确定性错误·不重试）: "
                            f"{type(exc).__name__}: {_brief(exc, 110)}"
                        )
                    break

                if df is None or (hasattr(df, "empty") and df.empty):
                    spec.last_error = "返回空数据"
                    errors.append(f"{spec.name}: 返回空数据")
                    logger.info(f"采集[{kind}] · {spec.name} 返回空数据，换下一个源")
                    last_exc = None
                    break

                # ---- 成功 ----
                ms = int((time.monotonic() - t0) * 1000)
                spec.fail_count = 0
                spec.dead_until = 0.0
                spec.last_ok = time.time()
                spec.last_error = ""
                self._recover_weight(spec)
                self._mark_hosts(spec, True)
                rows = len(df) if hasattr(df, "__len__") else None
                self.events.append(FetchEvent(
                    kind=kind, source=spec.name, ok=True,
                    attempts=attempt + 1, rows=rows, ms=ms))
                logger.info(f"采集[{kind}] 成功 · 数据源 **{spec.name}**"
                            f"（第{attempt + 1}次尝试，耗时 {ms}ms，{rows} 行）")
                return df

            # 该源用尽（重试后仍失败 / 确定性错误直接放弃）
            if last_exc is not None:
                n_tries = attempt + 1
                self._penalize(spec, f"{type(last_exc).__name__}: "
                                     f"{_brief(last_exc, 160)}", exc=last_exc)
                errors.append(f"{spec.name}: {type(last_exc).__name__}: "
                              f"{_brief(last_exc, 160)}")
                nxt = order[idx + 1] if idx + 1 < len(order) else "（已无更多数据源）"
                self.events.append(FetchEvent(
                    kind=kind, source=spec.name, ok=False, attempts=n_tries,
                    error=f"{type(last_exc).__name__}: {_brief(last_exc, 160)}"))
                logger.warning(
                    f"采集[{kind}] · {spec.name} 尝试 {n_tries} 次后仍不可用，"
                    f"切换下一个源 → {nxt}；原因: "
                    f"{type(last_exc).__name__}: {_brief(last_exc, 110)}"
                )

        # ---- 所有数据源都用尽：必须告警，不能只留一句"获取失败" ----
        detail = " | ".join(errors) if errors else "无可用数据源"
        # 大多数源是"连不上"而不是"数据不对"时，问题多半在网络/代理，
        # 这时候把排查方向一起打出来，省一轮"到底哪里坏了"的猜谜。
        net = sum(1 for e in errors if is_network_error_text(e))
        parsed = sum(1 for e in errors if is_parse_error_text(e))
        hint = ""
        if errors and parsed >= max(1, len(errors) // 2) and parsed >= net:
            # 数据拿到了、但解析不了：这时提示"查代理"是把人往沟里带，
            # 真正的处置是改适配代码（列名/列数/akshare 版本）。
            hint = (f" || {len(errors)} 个源中 {parsed} 个是解析失败"
                    f"（接口返回结构与本地解析不匹配，多为 akshare/接口改版）；"
                    f"排查：核对列名与 akshare 版本，网络层无异常")
        elif errors and net >= max(1, len(errors) // 2):
            hint = f" || {len(errors)} 个源中 {net} 个是网络层失败（连接被重置/超时/非JSON响应）；{network_hint()}"
        else:
            hint = f" || {network_hint()}"
        log_throttled("WARNING",
                      f"采集[{kind}] 失败：{len(specs)} 个数据源已全部用尽 -> {detail}{hint}",
                      key=f"src-exhausted-{kind}")
        raise SourceError(f"{kind}: 所有数据源均失败 -> {detail}{hint}")

    def _backoff(self, attempt: int) -> float:
        """指数退避：0.5s、1s、2s …… + 0~0.3s 抖动，错开重试节奏。"""
        import random

        return (2 ** attempt) * self.backoff_base + random.uniform(0, 0.3)

    def _blocked_hosts(self, spec: SourceSpec) -> str:
        """该源声明的域名里，有哪些当前被网络层判定为"暂不可达"。"""
        if not spec.hosts:
            return ""
        try:
            from app.providers.market.transport import net
        except Exception:
            return ""
        try:
            return net.blocked_reason(sorted(spec.hosts))
        except Exception:
            return ""

    def _log_event(self, kind: str, source: str, ok: bool, error: str,
                   ms: int, attempts: int) -> None:
        """记一条采集事件（跳过/失败都记，否则日志里看不到被跳过的原因）。"""
        self.events.append(FetchEvent(
            kind=kind, source=source, ok=ok, attempts=attempts,
            ms=ms, error=error))

    def _mark_hosts(self, spec: SourceSpec, ok: bool, why: str = "") -> None:
        """把源的成败回写给网络层的域名健康度。

        **这一步是必须的**：akshare 之类的第三方库在自己内部发请求，
        不走我们的 ``net.request``，网络层根本看不到那些失败。
        不回写的话，``hosts`` 声明就只是个摆设 —— 域名永远不会被判死，
        下一轮还得把每个源各等一遍超时。
        """
        if not spec.hosts:
            return
        try:
            from app.providers.market.transport import net
        except Exception:
            return
        for host in spec.hosts:
            try:
                net.mark(host, ok, why)
            except Exception:
                pass

    def _penalize(self, spec: SourceSpec, why: str,
                  exc: Optional[BaseException] = None) -> None:
        spec.fail_count += 1
        spec.last_error = why
        # 解析类失败**不回写域名健康度**：域名是通的，错的是返回结构。
        # 回写了就等于把"接口改版"记成"域名不可达"，
        # 后面几批请求会因为域名冷却被整批跳过，症状看起来像网络断了。
        if exc is None or not is_parse_error(exc):
            self._mark_hosts(spec, False, why)
        # 降权和熔断是两件事，别合并：
        #   熔断 = 短期内**根本不再试**它（防整批请求撞墙）；
        #   降权 = 冷却结束后再试时，它也得**排在后面**（防它立刻又占掉第一批请求）。
        self._decay_weight(spec)
        if spec.fail_count >= self.circuit_threshold:
            spec.dead_until = time.monotonic() + self.cooldown
            log_throttled(
                "WARNING",
                f"数据源 {spec.name} 连续 {spec.fail_count} 次源级失败，"
                f"熔断 {int(self.cooldown)}s（最后原因: {why[:80]}）",
                key=f"src-dead-{spec.name}",
            )

    # ---------------- 可观测 ----------------
    def recent_events(self, n: int = 20) -> List[Dict[str, Any]]:
        """最近 N 次采集事件（用于面板显示"这批数据来自哪个源"）。"""
        return [e.to_dict() for e in list(self.events)[-int(n):]]

    def used_sources(self, n: int = 20) -> Dict[str, str]:
        """每种数据类型最近一次**成功**使用的数据源：{kind: source}。"""
        out: Dict[str, str] = {}
        for e in self.events:
            if e.ok:
                out[e.kind] = e.source
        return dict(list(out.items())[-int(n):])

    def health(self) -> List[Dict[str, Any]]:
        """给面板 / CLI / 日志用：每个源的当前状态。"""
        now = time.monotonic()
        out = []
        for name in self._order:
            spec = self._specs.get(name)
            if spec is None:
                continue
            out.append({
                "name": name,
                "supports": sorted(spec.supports),
                "enabled": spec.enabled,
                "fallback": bool(spec.fallback),
                "healthy": self.is_healthy(name),
                "circuit_remaining": max(0, int(spec.dead_until - now)),
                "fail_count": spec.fail_count,
                "weight": round(spec.weight, 4),
                "base_weight": round(spec.base_weight, 4),
                "last_ok": spec.last_ok,
                "last_error": spec.last_error[:200],
            })
        return out

    def probe(self, kind: Optional[str] = None,
              **kwargs: Any) -> List[Dict[str, Any]]:
        """**实测**每个源现在通不通（不重试、不切源、不改权重）。

        和 ``health()`` 的区别：health 是"记忆里的状态"，probe 是"现在打一枪"。
        "所有数据源均失败"时第一件事就该跑它 —— 一眼看出是**单个源挂了**
        还是**整台机器出不去网**（后者表现为所有源都是连接类错误）。

        :param kind: 只探测支持该类型的源；不传则探测全部已注册源。
        """
        specs = (self.sources_for(kind) if kind
                 else [self._specs[n] for n in self._order if self._specs[n].enabled])
        out: List[Dict[str, Any]] = []
        for spec in specs:
            t0 = time.monotonic()
            entry: Dict[str, Any] = {
                "name": spec.name,
                "supports": sorted(spec.supports),
                "weight": round(spec.weight, 4),
            }
            try:
                df = spec.fetch(**kwargs)
                rows = len(df) if hasattr(df, "__len__") else None
                entry.update(ok=True, rows=rows,
                             ms=int((time.monotonic() - t0) * 1000), error="")
            except DataUnavailable as exc:
                # 能连上、但这份数据没有 → 源是好的
                entry.update(ok=True, rows=0,
                             ms=int((time.monotonic() - t0) * 1000),
                             error=f"DataUnavailable: {_brief(exc, 120)}")
            except Exception as exc:
                entry.update(ok=False, rows=0,
                             ms=int((time.monotonic() - t0) * 1000),
                             error=f"{type(exc).__name__}: {_brief(exc, 120)}")
            # 探测也是一次真实的成败，要回写给网络层：
            # 否则"跑了 probe 发现 push2 挂了"，正式采集却还要再等一遍超时。
            self._mark_hosts(spec, bool(entry["ok"]), entry["error"])
            entry["hosts"] = sorted(spec.hosts)
            out.append(entry)
            logger.info(f"探测数据源 {spec.name}: "
                        f"{'可用' if entry['ok'] else '不可用'} "
                        f"({entry['ms']}ms) {entry['error'][:80]}")
        return out

    def reset(self) -> None:
        """清空所有熔断状态并**把权重恢复默认**（测试 / 手动恢复时用）。

        连带恢复权重是有意的：排查环境问题（比如代理端口写错）时，
        用户要的是"让所有源重新公平竞争"，只清熔断不清权重的话，
        修好了的源仍会一直排在后面，问题看起来像没修好。
        """
        for spec in self._specs.values():
            spec.fail_count = 0
            spec.dead_until = 0.0
            spec.last_error = ""
            spec.weight = spec.base_weight
        self.events.clear()
        self._save_weights()


def _brief(exc: BaseException, n: int = 160) -> str:
    """把异常压成一行可读的短句。

    代理/东财这类错误会带上几百字符的 URL（含 secids、fields 等参数），
    原样打进日志会把"为什么失败"整个淹没掉 —— 只留类型和前 N 个字符。
    """
    text = " ".join(str(exc).split())
    return text[:n] + ("…" if len(text) > n else "")


# 全局注册表：各内置源在 marketx.py（或你自己的模块）里 import 时注册进来。
# 权重落盘到 data/source_weights.json —— 跨重启记住"哪个源实测不靠谱"。
# 单测要干净环境时用 `SourceRegistry(persist_path=None)` 另建一个。
REGISTRY = SourceRegistry(
    persist_path=(settings.DATA_DIR / "source_weights.json").as_posix()
)


__all__ = [
    "DEFAULT_WEIGHT",
    "DataUnavailable",
    "FetchEvent",
    "REGISTRY",
    "SourceError",
    "SourceRegistry",
    "SourceSpec",
    "WEIGHT_DECAY",
    "WEIGHT_MIN",
    "WEIGHT_RECOVER",
    "is_transient",
]
