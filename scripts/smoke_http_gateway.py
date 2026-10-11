#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : smoke_http_gateway.py
# @Description : 通用 HTTP 券商网关端到端冒烟
#
#                为什么单独做一个脚本：
#                  "接券商"这件事最容易出的不是逻辑错，而是**对不上** ——
#                  字段名对不上、嵌套层级对不上、状态字符串对不上。
#                  这些错在单元测试里查不出来（mock 出来的永远是"对的形状"），
#                  只有真的发一次 HTTP 请求才知道。
#
#                所以这里用标准库起一个**迷你券商服务**，字段命名刻意用
#                另一种风格（quantity / filledQuantity / orderId / 状态写 new），
#                让网关的字段映射真的被走一遍。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, ".")

# ===========================================================================
# 迷你券商（刻意用另一套字段命名）
# ===========================================================================
BOOK: Dict[str, Any] = {
    "seq": 0,
    "orders": {},        # broker_order_id -> dict
    "positions": {},     # symbol -> {quantity, avgPrice}
    "cash": 500000.0,
    "fills": [],
}


class MiniBrokerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:      # 别把请求日志打进冒烟输出
        pass

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/account":
            mv = sum(p["quantity"] * p["avgPrice"] for p in BOOK["positions"].values())
            self._send(200, {"data": {
                "cash": BOOK["cash"], "available": BOOK["cash"],
                "frozen": 0.0, "marketValue": mv, "totalAsset": BOOK["cash"] + mv,
            }})
        elif url.path == "/orders":
            self._send(200, {"data": list(BOOK["orders"].values())})
        elif url.path == "/positions":
            self._send(200, {"data": [
                {"symbol": s, "quantity": p["quantity"], "avgPrice": p["avgPrice"]}
                for s, p in BOOK["positions"].items()
            ]})
        elif url.path == "/fills":
            self._send(200, {"data": BOOK["fills"]})
        elif url.path.startswith("/instruments/"):
            sym = url.path.rsplit("/", 1)[-1]
            self._send(200, {"data": {
                "symbol": sym, "name": "迷你股份",
                "limitUp": 11.0, "limitDown": 9.0,
                "tickSize": 0.01, "lotSize": 100, "prevClose": 10.0,
            }})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/orders":
            self._send(404, {"message": "not found"})
            return
        req = self._body()
        # 校验：券商侧会拒绝非法单，这正是要验证的路径之一
        if int(req.get("quantity") or 0) <= 0:
            self._send(400, {"message": "quantity must be positive"})
            return
        BOOK["seq"] += 1
        bid = f"MB{BOOK['seq']:06d}"
        BOOK["orders"][bid] = {
            "orderId": bid,
            "clientOrderId": req.get("clientOrderId") or "",
            "symbol": req.get("symbol"),
            "side": req.get("side"),
            "quantity": int(req.get("quantity") or 0),
            "price": float(req.get("price") or 0.0),
            "status": "new",
            "filledQuantity": 0,
            "avgPrice": 0.0,
        }
        self._send(200, {"data": {"orderId": bid, "status": "new"}})

    def do_DELETE(self) -> None:
        bid = urlparse(self.path).path.rsplit("/", 1)[-1]
        o = BOOK["orders"].get(bid)
        if o is None or o["status"] in ("filled", "cancelled"):
            self._send(400, {"message": "order not cancellable"})
            return
        o["status"] = "cancelled"
        self._send(200, {"data": {"orderId": bid, "status": "cancelled"}})


def _mark_filled(bid: str) -> None:
    """模拟柜台把单撮掉，并生成一笔成交明细。"""
    o = BOOK["orders"][bid]
    px = o["price"] or 10.0
    o.update({"status": "filled", "filledQuantity": o["quantity"], "avgPrice": px})
    p = BOOK["positions"].setdefault(
        o["symbol"], {"quantity": 0, "avgPrice": 0.0}
    )
    if o["side"] == "buy":
        p["avgPrice"] = ((p["avgPrice"] * p["quantity"]) + px * o["quantity"]) / max(
            1, p["quantity"] + o["quantity"])
        p["quantity"] += o["quantity"]
        BOOK["cash"] -= px * o["quantity"]
    else:
        p["quantity"] -= o["quantity"]
        BOOK["cash"] += px * o["quantity"]
    BOOK["fills"].append({
        "fillId": f"{bid}-1", "orderId": bid, "symbol": o["symbol"],
        "side": o["side"], "quantity": o["quantity"], "price": px,
        "filledQuantity": o["quantity"], "fee": px * o["quantity"] * 0.0003,
    })


# ===========================================================================
# 冒烟
# ===========================================================================
PASSED = 0
FAILED: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"[OK ] {name}" + (f" {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"[FAIL] {name}" + (f" {detail}" if detail else ""))


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), MiniBrokerHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"迷你券商已启动: http://127.0.0.1:{port}\n")

    from app.execution.gateway import (
        GatewayError,
        OrderRejected,
        OrderRequest,
        create_gateway,
    )
    from app.domain.types import OrderStatus, Side

    print("=" * 66)
    print("① 构造与连接")
    print("=" * 66)
    gw = create_gateway(
        "http",
        base_url=f"http://127.0.0.1:{port}",
        account="MINI-001",
        timeout=5.0,
    )
    gw.connect()
    check("已连接", gw.is_connected())
    check("能力声明可读", gw.capabilities()["query_instrument"])

    print("\n" + "=" * 66)
    print("② 查账户 / 查标的（字段映射真的被走一遍）")
    print("=" * 66)
    acc = gw.query_account()
    check("账户资金读到了（嵌套 data 结构）", acc.cash == 500000.0, f"cash={acc.cash}")
    inst = gw.query_instrument("000001")
    check("标的信息读到了", inst is not None and inst.limit_up == 11.0,
          f"limit_up={inst.limit_up if inst else None}")

    print("\n" + "=" * 66)
    print("③ 下单 → 回报 → 成交明细")
    print("=" * 66)
    bid = gw.place_order(OrderRequest(
        symbol="000001", side=Side.BUY, size=1000, price=10.0,
        client_order_id="LOCAL-1", task_id="t1",
    ))
    check("下单返回券商订单号", bool(bid), f"broker_order_id={bid}")

    orders = gw.query_orders(only_open=False)
    check("能查到订单", any(o.broker_order_id == bid for o in orders))
    if orders:
        o = next(x for x in orders if x.broker_order_id == bid)
        check("方向被正确映射", o.side == Side.BUY, f"side={o.side}")
        check("状态被正确映射（new→SUBMITTED）",
              o.status == OrderStatus.SUBMITTED, f"status={o.status}")
        check("数量与价格正确", o.size == 1000 and o.price == 10.0)

    _mark_filled(bid)
    fills = gw.poll_fills()
    check("收到成交回报", len(fills) >= 1, f"{len(fills)} 笔")
    if fills:
        f = fills[0]
        check("成交价有效（非 0）", f.price > 0, f"price={f.price}")
        check("成交量正确", f.size == 1000)
    # 去重：再 poll 一次不该重复给同一笔
    again = gw.poll_fills()
    check("成交回报不重复", len(again) == 0, f"第二次 {len(again)} 笔")

    print("\n" + "=" * 66)
    print("④ 持仓与资金随成交变化")
    print("=" * 66)
    pos = gw.query_positions()
    check("持仓已建立", pos.get("000001") is not None and pos["000001"].size == 1000,
          f"{pos.get('000001')}")
    acc2 = gw.query_account()
    check("资金已扣减", acc2.cash < 500000.0, f"{acc.cash} → {acc2.cash}")

    print("\n" + "=" * 66)
    print("⑤ 撤单与被拒绝的路径")
    print("=" * 66)
    bid2 = gw.place_order(OrderRequest(
        symbol="000002", side=Side.BUY, size=500, price=20.0,
        client_order_id="LOCAL-2",
    ))
    check("第二笔下单成功", bool(bid2))
    check("撤单受理", gw.cancel_order(bid2) is True)
    st = next((o for o in gw.query_orders(only_open=False)
               if o.broker_order_id == bid2), None)
    check("撤单后状态为 CANCELLED",
          st is not None and st.status == OrderStatus.CANCELLED,
          f"{st.status if st else None}")

    try:
        gw.place_order(OrderRequest(symbol="000001", side=Side.BUY, size=0, price=10.0))
        check("非法数量被拒绝", False, "居然没抛异常")
    except OrderRejected as exc:
        check("非法数量被拒绝（OrderRejected，不可重试）", True, str(exc)[:60])
    except Exception as exc:
        check("非法数量被拒绝", False, f"抛的是 {type(exc).__name__}")

    print("\n" + "=" * 66)
    print("⑥ 连不上的时候要说清楚（通信失败 ≠ 被拒绝）")
    print("=" * 66)
    bad = create_gateway("http", base_url="http://127.0.0.1:1", timeout=2.0)
    try:
        bad.query_account()
        check("连不上会抛 GatewayError", False, "没抛异常")
    except GatewayError as exc:
        check("连不上抛 GatewayError（可重试）", True, str(exc)[:60])
    except Exception as exc:
        check("连不上会抛 GatewayError", False, f"抛的是 {type(exc).__name__}")

    gw.disconnect()
    server.shutdown()
    print("\n" + "=" * 66)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print(f"✅ HTTP 券商网关冒烟全部通过（{PASSED} 项）")
    print("   下单 → 查询 → 成交回报 → 持仓资金 → 撤单 → 拒绝/断网 全链路已验证")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
