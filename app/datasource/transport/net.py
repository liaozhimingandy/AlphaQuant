"""统一网络传输层 —— 所有外网取数都应该走这里。

为什么需要这一层（这轮问题的根因）
--------------------------------
之前每个适配器各写各的 ``requests.get``，甚至直接调 akshare 让它自己发请求，
结果是同一件事被重复踩了三遍坑：

1. **没有请求头**：东财/腾讯这类站点不带 ``User-Agent`` / ``Referer`` 会直接
   断连（表现为 ``RemoteDisconnected``），看着像"被限流"，其实是自己没伪装；
2. **没有通道兜底**：只认环境变量里的代理。代理是**僵尸端口**（没人监听）或者
   **白名单代理**（只放行部分域名）时，所有源一起死，而且每个源都要等满超时，
   一次采集耗掉几十秒最后还是 0 条；
3. **没有域名级记忆**：同一个坏域名在一轮里被重试 N 次 —— 源链里 3 个源有 2 个
   依赖 ``push2.eastmoney.com``，那个域名挂了等于 3 个源全挂，却还要各等一遍。

这一层把三件事收敛到一个地方：

- **请求头**：按域名自动配 ``Referer``，统一浏览器 UA；
- **通道**：先按环境配置（走代理）试，失败自动换直连；某个域名上次是靠哪条通道
  成功的，下次优先走那条（``_host_pref``）；
- **域名健康度**：连续失败到阈值就判"该域名暂不可达"，冷却期内直接快速失败，
  不再让它把整条源链的超时预算吃光。

用法::

    from app.datasource.transport import net
    data = net.post_json(url, json=payload)
    df   = net.get_text("https://qt.gtimg.cn/q=sh600000")

不想联网的场景（单测）可以 ``net.offline(True)`` 让它全部立即失败。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from app.common.logging import logger

# ============================================================
# 配置
# ============================================================

DEFAULT_TIMEOUT: float = float(os.getenv("DATA_HTTP_TIMEOUT", "10"))
#: 连续失败几次就把这个域名判为"暂不可达"
BLOCK_THRESHOLD: int = int(os.getenv("DATA_HOST_BLOCK_THRESHOLD", "2"))
#: 判不可达后的冷却秒数：冷却内对该域名的请求直接快速失败
BLOCK_COOLDOWN: float = float(os.getenv("DATA_HOST_BLOCK_COOLDOWN", "300"))

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

#: 按域名给 Referer —— 站点靠它判断"是不是从行情页点进来的"。
#: 少了这个，东财系会直接断连，报错长得很像被限流，极易误判。
_REFERER_BY_SUFFIX: Tuple[Tuple[str, str], ...] = (
    (".eastmoney.com", "https://quote.eastmoney.com/"),
    (".gtimg.cn", "https://gu.qq.com/"),
    (".qq.com", "https://gu.qq.com/"),
    (".sina.com.cn", "https://finance.sina.com.cn/"),
    (".xueqiu.com", "https://xueqiu.com/"),
    (".baidu.com", "https://gushitong.baidu.com/"),
    (".10jqka.com.cn", "https://stockpage.10jqka.com.cn/"),
)


class NetworkError(Exception):
    """网络层失败：域名不可达、被代理拦、连接被重置等。

    与业务层的 ``DataUnavailable`` 区分开：**网络坏了要重试/换源，
    数据没有则不该重试**。混在一起会让"停牌"这种正常情况被反复重试。
    """


class HostBlocked(NetworkError):
    """域名处于冷却期（刚连续失败过），本次直接跳过。"""


# ============================================================
# 域名健康度
# ============================================================

_lock = threading.RLock()
_hosts: Dict[str, Dict[str, Any]] = {}
#: host -> 上次成功的通道名（"proxy" / "direct"），下次优先走它
_host_pref: Dict[str, str] = {}
_offline = False


def host_of(url: str) -> str:
    """从 URL 取域名（诊断日志里打这个比打整条 URL 可读得多）。"""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def headers_for(url: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """按域名生成请求头。"""
    host = host_of(url)
    h = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
    }
    for suffix, ref in _REFERER_BY_SUFFIX:
        if host.endswith(suffix):
            h["Referer"] = ref
            break
    if extra:
        h.update(extra)
    return h


def mark(host: str, ok: bool, error: str = "") -> None:
    """记录一次该域名的成败，用于冷却与通道偏好。"""
    if not host:
        return
    with _lock:
        st = _hosts.setdefault(host, {"ok": 0, "fail": 0, "blocked_until": 0.0,
                                      "last_error": "", "last_ok": None})
        if ok:
            st["ok"] += 1
            st["fail"] = 0
            st["blocked_until"] = 0.0
            st["last_error"] = ""
            st["last_ok"] = time.time()
        else:
            st["fail"] += 1
            st["last_error"] = str(error)[:200]
            if st["fail"] >= BLOCK_THRESHOLD:
                st["blocked_until"] = time.monotonic() + BLOCK_COOLDOWN
                logger.warning(
                    f"域名 {host} 连续失败 {st['fail']} 次，{int(BLOCK_COOLDOWN)}s 内"
                    f"不再请求（最后一次: {st['last_error']}）"
                )


def is_blocked(host: str) -> bool:
    """该域名是否处于"暂不可达"冷却期。"""
    if not host:
        return False
    with _lock:
        st = _hosts.get(host)
        if not st:
            return False
        return time.monotonic() < float(st.get("blocked_until") or 0.0)


def blocked_reason(hosts: Optional[List[str]]) -> str:
    """给定一组域名，返回"哪些不可用、为什么"——给跳过源时的日志用。"""
    if not hosts:
        return ""
    out = []
    for h in hosts:
        with _lock:
            st = _hosts.get(h) or {}
        if time.monotonic() < float(st.get("blocked_until") or 0.0):
            left = int(float(st.get("blocked_until")) - time.monotonic())
            out.append(f"{h}({st.get('last_error') or '不可达'}，{left}s 后重试)")
    return "；".join(out)


def host_state() -> Dict[str, Dict[str, Any]]:
    """所有域名的健康快照（面板/CLI 排障用）。"""
    now = time.monotonic()
    with _lock:
        return {
            h: {
                "ok": s.get("ok", 0),
                "fail": s.get("fail", 0),
                "blocked": now < float(s.get("blocked_until") or 0.0),
                "blocked_remaining": max(0, int(float(s.get("blocked_until") or 0) - now)),
                "last_error": s.get("last_error", ""),
                "last_ok": s.get("last_ok"),
                "prefer": _host_pref.get(h, ""),
            }
            for h, s in _hosts.items()
        }


def reset() -> None:
    """清空域名健康度与通道偏好（测试 / 手动恢复时用）。"""
    with _lock:
        _hosts.clear()
        _host_pref.clear()


def offline(flag: Optional[bool] = None) -> bool:
    """读写"离线开关"：打开后所有请求立即失败，用于单测禁止真实联网。"""
    global _offline
    if flag is None:
        return _offline
    _offline = bool(flag)
    return _offline


# ============================================================
# 代理通道
# ============================================================

def _env_proxy() -> str:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return ""


def proxy_configured() -> bool:
    return bool(_env_proxy())


def proxy_reachable(timeout: float = 1.5) -> Optional[bool]:
    """代理端口到底有没有程序在监听。

    **僵尸代理**是最难查的一类故障：环境变量写了端口，但没人监听 ——
    表现是每次请求都卡到超时然后失败，看起来像"被限流"。
    这里直接 TCP 连通一下，1.5 秒就能给出确定答案。
    返回 None = 没配代理（无此一说）。
    """
    raw = _env_proxy()
    if not raw:
        return None
    try:
        body = raw.split("//", 1)[-1].rstrip("/")
        host, _, port = body.rpartition(":")
        if not host or not port:
            return None
        import socket

        with socket.socket() as s:
            s.settimeout(timeout)
            s.connect((host, int(port)))
        return True
    except Exception as exc:
        logger.debug(f"代理 {raw} 不可连: {type(exc).__name__}")
        return False


_proxy_checked = False
_proxy_state: Optional[bool] = None


def sanitize_proxy(force: bool = False) -> Optional[bool]:
    """代理是**僵尸端口**时，直接把代理环境变量摘掉。

    为什么必须做在环境变量这一层：akshare / baostock 这些第三方库在自己内部
    发请求，根本不走 ``net.request`` —— 传输层再怎么"代理失败改直连"也救不了它们。
    而它们又统一读 ``HTTP_PROXY`` 环境变量，所以把变量摘掉是唯一对它们也生效的做法。

    判定用的是**一次 TCP 连接**（约 1.5s，只做一次）：
    连不上 = 没有任何程序在监听，那这个代理就是死的，留着只会让每个请求都卡满超时。

    :return: True 代理可用 / False 已摘除 / None 本来就没配代理
    """
    global _proxy_checked, _proxy_state
    if _proxy_checked and not force:
        return _proxy_state
    _proxy_checked = True

    if not proxy_configured():
        _proxy_state = None
        return None

    if proxy_reachable() is False:
        raw = _env_proxy()
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(var, None)
        _proxy_state = False
        logger.warning(
            f"检测到代理 {raw} 无程序监听（僵尸代理），已临时取消代理环境变量，"
            f"本次进程将直连；若要恢复请修正端口后重启"
        )
        return False

    _proxy_state = True
    return True


def _transports(host: str) -> List[Tuple[str, Optional[Dict[str, str]]]]:
    """本次请求要依次尝试的通道。

    - ``proxy``  = ``None``，即让 requests 自己读环境变量（走代理）；
    - ``direct`` = 显式置空，绕过代理。

    若某个域名上次是靠直连成功的，这次先直连 —— 省掉一次必失败的代理等待。
    """
    pref = _host_pref.get(host)
    modes: List[Tuple[str, Optional[Dict[str, str]]]] = []
    if proxy_configured():
        modes.append(("proxy", None))
        modes.append(("direct", {"http": "", "https": ""}))
    else:
        modes.append(("direct", None))
    if pref:
        modes.sort(key=lambda m: 0 if m[0] == pref else 1)
    return modes


# ============================================================
# 请求
# ============================================================

def request(method: str, url: str, *, params: Optional[Dict] = None,
            json_body: Optional[Any] = None, data: Optional[Any] = None,
            headers: Optional[Dict[str, str]] = None,
            timeout: Optional[float] = None,
            encoding: Optional[str] = None) -> requests.Response:
    """发一次请求，自动带请求头、自动换通道、自动记域名健康度。"""
    if offline():
        raise NetworkError("离线模式：禁止真实联网（net.offline(True)）")

    host = host_of(url)
    if is_blocked(host):
        raise HostBlocked(f"域名 {host} 处于冷却期，本次直接跳过")

    tmo = float(timeout or DEFAULT_TIMEOUT)
    h = headers_for(url, headers)
    errors: List[str] = []

    for mode, proxies in _transports(host):
        t0 = time.monotonic()
        try:
            resp = requests.request(
                method.upper(), url, params=params, json=json_body, data=data,
                headers=h, timeout=tmo, proxies=proxies,
            )
            if encoding:
                resp.encoding = encoding
            resp.raise_for_status()
        except Exception as exc:
            why = f"{type(exc).__name__}: {str(exc)[:120]}"
            errors.append(f"[{mode}] {why}")
            logger.debug(f"{method.upper()} {host} 经 {mode} 失败（{int((time.monotonic()-t0)*1000)}ms）: {why}")
            mark(host, False, f"{mode} {why}")
            continue
        mark(host, True)
        _host_pref[host] = mode
        logger.debug(f"{method.upper()} {host} 经 {mode} 成功（{int((time.monotonic()-t0)*1000)}ms）")
        return resp

    raise NetworkError(f"{method.upper()} {host} 失败 -> " + " | ".join(errors))


def get_text(url: str, **kw: Any) -> str:
    return request("GET", url, **kw).text


def get_json(url: str, **kw: Any) -> Any:
    resp = request("GET", url, **kw)
    try:
        return resp.json()
    except Exception as exc:
        raise NetworkError(f"{host_of(url)} 返回的不是 JSON: {type(exc).__name__}") from exc


def post_json(url: str, **kw: Any) -> Any:
    resp = request("POST", url, **kw)
    try:
        return resp.json()
    except Exception as exc:
        raise NetworkError(f"{host_of(url)} 返回的不是 JSON: {type(exc).__name__}") from exc


__all__ = [
    "NetworkError", "HostBlocked", "DEFAULT_TIMEOUT",
    "host_of", "headers_for", "mark", "is_blocked", "blocked_reason",
    "host_state", "reset", "offline",
    "proxy_configured", "proxy_reachable", "sanitize_proxy",
    "request", "get_text", "get_json", "post_json",
]
