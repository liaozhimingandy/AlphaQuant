#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : http_gateway.py
# @Description : 通用 HTTP 券商网关（零第三方依赖，配置驱动）
#
#                为什么需要它：
#                  接券商最大的现实是——**每家都不一样**。
#                  CTP、恒生 UFX、各家 QMT、券商自研柜台、自建交易中台，
#                  字段命名、鉴权方式、URL 形状没有两家是一样的。
#                  如果每接一家都要写一个 Python 类，那"接券商"就永远是个
#                  需要排期开发的事，而不是配个文件就能跑的事。
#
#                所以这里提供一个**配置驱动**的 HTTP 网关：
#                  把"下单发到哪个路径、字段叫什么、返回值从哪取"
#                  全部写进 config/brokers.json，不改一行代码就能接一家
#                  提供 HTTP 接口的券商或自建中台。
#
#                用标准库 urllib 实现 —— 这个项目坚持零新依赖，
#                requests 虽然好用，但为了一个 HTTP 客户端引入一整条
#                依赖链（urllib3/certifi/charset-normalizer…）不值得。
#
#                安全约定：凭据**不写在配置里**，只写环境变量名
#                （``token_env``），与接入点配置的其它部分保持一致。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.providers.broker.gateway import (
    AccountSnapshot,
    FillEvent,
    GatewayError,
    GatewayOrder,
    IBrokerGateway,
    InstrumentInfo,
    OrderRejected,
    OrderRequest,
    PositionSnapshot,
    register_gateway,
)
from app.domain.types import OrderStatus, Side
from app.common.logging import logger

#: URL 占位符：路径里写 {broker_order_id} / {symbol}，发请求时替换
DEFAULT_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "place": {"path": "/orders", "method": "POST"},
    "cancel": {"path": "/orders/{broker_order_id}", "method": "DELETE"},
    "orders": {"path": "/orders", "method": "GET"},
    "positions": {"path": "/positions", "method": "GET"},
    "account": {"path": "/account", "method": "GET"},
    "fills": {"path": "/fills", "method": "GET"},
    "instrument": {"path": "/instruments/{symbol}", "method": "GET"},
}

#: 请求体字段映射（本地名 → 券商字段名）
DEFAULT_REQ_FIELDS: Dict[str, str] = {
    "symbol": "symbol",
    "side": "side",
    "size": "quantity",
    "price": "price",
    "order_type": "orderType",
    "client_order_id": "clientOrderId",
    "task_id": "taskId",
}

#: 响应字段取值路径（支持 "data.orderId" 这种点号路径，以及 "0" 取列表第 N 项）
DEFAULT_RESP_FIELDS: Dict[str, str] = {
    "list": "data",
    "order_id": "orderId",
    "client_order_id": "clientOrderId",
    "symbol": "symbol",
    "side": "side",
    "size": "quantity",
    "filled_size": "filledQuantity",
    "price": "price",
    "avg_price": "avgPrice",
    "status": "status",
    "message": "message",
    "cash": "cash",
    "available": "available",
    "frozen": "frozen",
    "market_value": "marketValue",
    "total_asset": "totalAsset",
}

#: 券商状态字符串 → 内部状态。接新券商时**只需要改这里**。
STATUS_MAP: Dict[str, OrderStatus] = {
    "new": OrderStatus.SUBMITTED, "submitted": OrderStatus.SUBMITTED,
    "pending": OrderStatus.SUBMITTED, "accepted": OrderStatus.SUBMITTED,
    "partial": OrderStatus.PARTIAL, "partially_filled": OrderStatus.PARTIAL,
    "filled": OrderStatus.FILLED, "dealt": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED, "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED, "refused": OrderStatus.REJECTED,
}

#: 买卖方向映射（券商侧写法五花八门）
SIDE_MAP: Dict[str, Side] = {
    "buy": Side.BUY, "b": Side.BUY, "1": Side.BUY, "long": Side.BUY,
    "sell": Side.SELL, "s": Side.SELL, "2": Side.SELL, "short": Side.SELL,
}


def _dig(obj: Any, path: str) -> Any:
    """按点号路径取值，取不到返回 None（不抛异常）。

    为什么不用 JSONPath 之类的：那要引入新依赖，而这里要的只是
    "取 data.list.0.orderId" 这种最常见的几层嵌套。
    """
    if obj is None or not path:
        return None
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _dig2(obj: Any, path: str) -> Any:
    """先按原路径取，取不到再钻进 ``data`` 里取一次。

    券商返回形状就两大类：平铺 ``{"orderId": ...}`` 和包一层
    ``{"data": {...}}``。写映射的人不该为了这个去改配置 ——
    两种都认，配置里写 ``data.orderId`` 也能正常工作。
    """
    v = _dig(obj, path)
    if v is None and isinstance(obj, dict) and not path.startswith("data."):
        inner = obj.get("data")
        if isinstance(inner, dict):
            v = _dig(inner, path)
    return v


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return default if v != v else v
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@register_gateway
class HttpGateway(IBrokerGateway):
    """配置驱动的 HTTP 券商网关。

    最小可用配置（config/brokers.json 的一个接入点）::

        {
          "id": "mybroker",
          "gateway": "http",
          "params": {
            "base_url": "http://127.0.0.1:8080",
            "token_env": "BROKER_TOKEN"
          }
        }

    字段对不上时再补 ``endpoints`` / ``req_fields`` / ``resp_fields``
    三个映射表，不用改代码。
    """

    name = "http"

    def __init__(
        self,
        base_url: str = "",
        timeout: float = 10.0,
        account: str = "",
        readonly: bool = False,
        config: Optional[Dict[str, Any]] = None,
        endpoint_id: str = "",
        token: str = "",
        token_env: str = "",
        headers: Optional[Dict[str, str]] = None,
        endpoints: Optional[Dict[str, Any]] = None,
        req_fields: Optional[Dict[str, str]] = None,
        resp_fields: Optional[Dict[str, str]] = None,
        status_map: Optional[Dict[str, str]] = None,
        side_map: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> None:
        cfg: Dict[str, Any] = dict(config or {})
        self.base_url = str(cfg.get("base_url") or base_url or "").rstrip("/")
        self.timeout = float(cfg.get("timeout") or timeout or 10.0)
        self.account = str(account or cfg.get("account") or "") or "HTTP"
        self.readonly = bool(readonly if readonly else cfg.get("readonly", False))
        self.endpoint_id = str(endpoint_id or cfg.get("endpoint_id") or "http")
        self.config = cfg

        self._token_env = str(cfg.get("token_env") or token_env or "")
        self._token = str(cfg.get("token") or token or "")
        if not self._token and self._token_env:
            self._token = os.getenv(self._token_env, "")
        self._headers: Dict[str, str] = {
            k: str(v) for k, v in (cfg.get("headers") or headers or {}).items()
        }

        self.endpoints: Dict[str, Dict[str, str]] = {
            k: dict(v) for k, v in DEFAULT_ENDPOINTS.items()
        }
        for k, v in (cfg.get("endpoints") or endpoints or {}).items():
            self.endpoints[str(k)] = {"path": str(v.get("path", "")),
                                      "method": str(v.get("method", "GET")).upper()}
        self.req = dict(DEFAULT_REQ_FIELDS)
        self.req.update({str(k): str(v)
                         for k, v in (cfg.get("req_fields") or req_fields or {}).items()})
        self.resp = dict(DEFAULT_RESP_FIELDS)
        self.resp.update({str(k): str(v)
                          for k, v in (cfg.get("resp_fields") or resp_fields or {}).items()})

        self._status_map = dict(STATUS_MAP)
        for k, v in (cfg.get("status_map") or status_map or {}).items():
            try:
                self._status_map[str(k).lower()] = OrderStatus(str(v))
            except ValueError:
                logger.warning(f"网关 {self.name} 状态映射非法: {k}={v}")
        self._side_map = dict(SIDE_MAP)
        self._side_map.update({str(k).lower(): (Side(str(v)) if str(v) in
                                                {s.value for s in Side} else Side.BUY)
                               for k, v in (cfg.get("side_map") or side_map or {}).items()})

        self._connected = False
        self._seq = 0
        self.last_error = ""
        self.stats = {"requests": 0, "errors": 0, "placed": 0, "cancelled": 0}

    # ===================================================================
    # HTTP 基础
    # ===================================================================
    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _request(self, op: str, body: Optional[Dict[str, Any]] = None,
                 query: Optional[Dict[str, Any]] = None,
                 **placeholders: Any) -> Any:
        ep = self.endpoints.get(op)
        if not ep or not ep.get("path"):
            raise GatewayError(f"接入点未配置 {op} 的路径（endpoints.{op}.path）")
        if not self.base_url and not ep["path"].startswith("http"):
            raise GatewayError("未配置 base_url，无法确定券商接口地址")

        path = ep["path"]
        for k, v in placeholders.items():
            path = path.replace("{" + k + "}", urllib.parse.quote(str(v)))
        url = self._url(path)
        if query:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(query)}"

        method = (ep.get("method") or "GET").upper()
        data = None
        headers = {"Accept": "application/json", **self._headers}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None and method not in ("GET", "DELETE"):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        self.stats["requests"] += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            self.stats["errors"] += 1
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            self.last_error = f"HTTP {exc.code}: {detail}"
            # 4xx 里属于业务拒绝的（400/403/422）不当通信失败处理：
            # 重试没有意义，必须让上层把它当成"被券商拒绝"。
            if exc.code in (400, 403, 422):
                raise OrderRejected(f"券商拒绝: {detail or exc.reason}") from exc
            raise GatewayError(f"券商接口错误 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            self.stats["errors"] += 1
            self.last_error = f"网络错误: {exc.reason}"
            raise GatewayError(f"无法连接券商接口 {url}: {exc.reason}") from exc
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = str(exc)
            raise GatewayError(f"请求券商接口失败: {exc}") from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 有些柜台返回的是表单编码或纯文本，别把整个链路搞挂
            return {"_raw": raw}

    @staticmethod
    def _rows(payload: Any, list_path: str) -> List[Dict[str, Any]]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return [dict(x) for x in payload if isinstance(x, dict)]
        got = _dig(payload, list_path or "data")
        if got is None:
            got = payload if isinstance(payload, dict) else None
        if isinstance(got, list):
            return [dict(x) for x in got if isinstance(x, dict)]
        if isinstance(got, dict):
            return [got]
        return []

    def _status(self, raw: Any) -> OrderStatus:
        if raw is None:
            return OrderStatus.SUBMITTED
        return self._status_map.get(str(raw).strip().lower(), OrderStatus.SUBMITTED)

    def _side(self, raw: Any) -> Side:
        if raw is None:
            return Side.BUY
        got = self._side_map.get(str(raw).strip().lower())
        if got is not None:
            return got
        try:
            return Side(str(raw))
        except ValueError:
            return Side.BUY

    # ===================================================================
    # 连接
    # ===================================================================
    def connect(self) -> None:
        if not self.base_url:
            raise GatewayError("未配置 base_url")
        # 连一次账户接口当作登录校验：能读到账号才说明鉴权没问题。
        # 只在"配了 account 端点"时做，避免接一家只提供下单接口的中台就连不上。
        if self.endpoints.get("account", {}).get("path"):
            try:
                self._request("account")
            except OrderRejected:
                raise
            except GatewayError as exc:
                raise GatewayError(f"券商接口连通性检查失败: {exc}") from exc
        self._connected = True
        logger.info(
            f"[HTTP网关] 已连接 | 接入点={self.endpoint_id} | {self.base_url} | "
            f"账号={self.account}" + ("（只读）" if self.readonly else "")
        )

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def ensure_connected(self) -> bool:
        if self._connected:
            return True
        try:
            self.connect()
        except Exception as exc:
            logger.warning(f"HTTP 网关重连失败: {exc}")
            return False
        return self._connected

    def health(self) -> Dict[str, Any]:
        detail = "" if self._connected else "未连接"
        if self._connected and self.endpoints.get("account", {}).get("path"):
            try:
                self._request("account")
                detail = "账户接口响应正常"
            except Exception as exc:
                detail = f"账户接口异常: {str(exc)[:120]}"
                return {"connected": False, "detail": detail}
        return {"connected": self._connected, "detail": detail}

    def capabilities(self) -> Dict[str, bool]:
        has = lambda op: bool(self.endpoints.get(op, {}).get("path"))  # noqa: E731
        return {
            "market_order": has("place"),
            "cancel": has("cancel"),
            "query_trades": has("fills"),
            "query_instrument": has("instrument"),
            "reconnect": True,
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "gateway": self.name,
            "endpoint_id": self.endpoint_id,
            "account": self.account,
            "readonly": self.readonly,
            "base_url": self.base_url,
        }

    # ===================================================================
    # 交易
    # ===================================================================
    def place_order(self, req: OrderRequest) -> str:
        if not self._connected:
            raise GatewayError("HTTP 网关未连接")
        if self.readonly:
            raise OrderRejected("接入点为只读档位（readonly=true），已阻止下单")
        if req.size <= 0:
            raise OrderRejected("下单数量必须为正")

        body = {
            self.req["symbol"]: req.symbol,
            self.req["side"]: "buy" if req.side == Side.BUY else "sell",
            self.req["size"]: int(req.size),
            self.req["price"]: float(req.price or 0.0),
            self.req["order_type"]: str(req.order_type or "LIMIT").upper(),
        }
        if req.client_order_id:
            body[self.req["client_order_id"]] = req.client_order_id
        if req.task_id:
            body[self.req["task_id"]] = req.task_id

        payload = self._request("place", body=body)
        bid = _dig2(payload, self.resp["order_id"])
        if not bid:
            msg = str(_dig2(payload, self.resp["message"]) or payload or "")
            raise OrderRejected(f"券商未返回订单号: {msg[:200]}")
        self.stats["placed"] += 1
        return str(bid)

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self._request("cancel", broker_order_id=broker_order_id)
        except GatewayError:
            raise
        except Exception as exc:
            logger.warning(f"撤单失败 {broker_order_id}: {exc}")
            return False
        self.stats["cancelled"] += 1
        return True

    # ===================================================================
    # 查询
    # ===================================================================
    def query_orders(self, only_open: bool = True) -> List[GatewayOrder]:
        payload = self._request("orders", query={"open": "true"} if only_open else None)
        out: List[GatewayOrder] = []
        for r in self._rows(payload, self.resp["list"]):
            raw_status = r.get(self.resp["status"].split(".")[-1])
            out.append(GatewayOrder(
                broker_order_id=str(r.get(self.resp["order_id"].split(".")[-1]) or ""),
                client_order_id=str(r.get(self.resp["client_order_id"].split(".")[-1]) or ""),
                symbol=str(r.get(self.resp["symbol"].split(".")[-1]) or ""),
                side=self._side(r.get(self.resp["side"].split(".")[-1])),
                size=_i(r.get(self.resp["size"].split(".")[-1])),
                price=_f(r.get(self.resp["price"].split(".")[-1])),
                filled_size=_i(r.get(self.resp["filled_size"].split(".")[-1])),
                avg_price=_f(r.get(self.resp["avg_price"].split(".")[-1])),
                status=self._status(raw_status),
                message=str(r.get(self.resp["message"].split(".")[-1]) or ""),
                updated_at=datetime.now(),
            ))
        return [o for o in out if o.broker_order_id]

    def query_positions(self) -> Dict[str, PositionSnapshot]:
        payload = self._request("positions")
        out: Dict[str, PositionSnapshot] = {}
        for r in self._rows(payload, self.resp["list"]):
            sym = str(r.get(self.resp["symbol"].split(".")[-1]) or "")
            if not sym:
                continue
            out[sym] = PositionSnapshot(
                symbol=sym,
                size=_i(r.get(self.resp["size"].split(".")[-1])),
                avg_price=_f(r.get(self.resp["avg_price"].split(".")[-1])),
                frozen_size=_i(r.get("frozen") if "frozen" in r
                               else r.get("frozenQuantity")),
            )
        return out

    def query_account(self) -> AccountSnapshot:
        payload = self._request("account")
        # 多数柜台的账户结构是 {"data": {...}}，少数直接平铺。两种都认 ——
        # 平铺时取到 0 会让"对账不一致"，而这种错最难查（看起来只是数据不对）。
        r = payload if isinstance(payload, dict) else {}
        inner = r.get("data") if isinstance(r.get("data"), dict) else None
        pick = lambda key: (  # noqa: E731
            r.get(key) if key in r
            else (inner.get(key) if inner else None)
        )
        return AccountSnapshot(
            cash=_f(pick("cash")),
            available=_f(pick("available")),
            frozen=_f(pick("frozen")),
            market_value=_f(pick("market_value")),
            total_asset=_f(pick("total_asset")),
        )

    def query_trades(self, since: Any = None) -> List[FillEvent]:
        if not self.endpoints.get("fills", {}).get("path"):
            return []
        query = {}
        if since is not None:
            query["since"] = since.isoformat() if hasattr(since, "isoformat") else str(since)
        payload = self._request("fills", query=query or None)
        out: List[FillEvent] = []
        for r in self._rows(payload, self.resp["list"]):
            out.append(FillEvent(
                fill_id=str(r.get("fillId") or r.get("tradeId") or ""),
                broker_order_id=str(r.get("orderId") or r.get(self.resp["order_id"]) or ""),
                client_order_id=str(r.get("clientOrderId") or ""),
                symbol=str(r.get(self.resp["symbol"]) or ""),
                side=self._side(r.get(self.resp["side"])),
                size=_i(r.get(self.resp["size"])),
                price=_f(r.get(self.resp["price"])),
                fee=_f(r.get("fee") or r.get("commission")),
                filled_size=_i(r.get(self.resp["filled_size"])),
            ))
        return out

    def query_instrument(self, symbol: str) -> Optional[InstrumentInfo]:
        if not self.endpoints.get("instrument", {}).get("path"):
            return None
        try:
            payload = self._request("instrument", symbol=symbol)
        except Exception as exc:
            logger.debug(f"查询 {symbol} 标的信息失败: {exc}")
            return None
        r = payload if isinstance(payload, dict) else {}
        r = _dig(payload, self.resp["list"]) or r
        if not isinstance(r, dict):
            return None
        return InstrumentInfo(
            symbol=str(r.get("symbol") or symbol),
            name=str(r.get("name") or ""),
            limit_up=_f(r.get("limitUp") or r.get("upperLimit")),
            limit_down=_f(r.get("limitDown") or r.get("lowerLimit")),
            tick_size=_f(r.get("tickSize") or 0.01),
            lot_size=_i(r.get("lotSize") or 100),
            is_suspended=bool(r.get("suspended") or r.get("isSuspended")),
            prev_close=_f(r.get("prevClose")),
        )

    def poll_fills(self) -> List[FillEvent]:
        """HTTP 网关用"查成交"代替"收回报"。

        语义差别要说清：``query_trades`` 是查，可能被重复调用；
        ``poll_fills`` 只应该返回**上次之后新增的**。
        这里按 fill_id 去重来近似，真实接入时建议券商侧支持游标。
        """
        fills = self.query_trades()
        if not fills:
            return []
        seen = getattr(self, "_seen_fills", None)
        if seen is None:
            self._seen_fills = set()
            seen = self._seen_fills
        out = []
        for f in fills:
            key = f.fill_id or f"{f.broker_order_id}:{f.filled_size}"
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(f)
        return out

    def poll_order_updates(self) -> List[GatewayOrder]:
        """HTTP 网关没有推送，用"查订单"代替：返回当前全部订单状态。

        上层按 (order_id, status) 幂等处理，重复收到同样状态不会出问题。
        """
        try:
            return self.query_orders(only_open=False)
        except GatewayError:
            return []


__all__ = ["HttpGateway", "DEFAULT_ENDPOINTS", "STATUS_MAP", "SIDE_MAP"]
