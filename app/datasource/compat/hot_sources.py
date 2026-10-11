"""人气榜的**可用**数据源 —— 不依赖被拦的 push2 域名。

背景（这轮真正修掉的问题）
--------------------------
``app/data/marketx.py`` 里的 ``akshare_em`` 走的是 ``akshare.stock_hot_rank_em``，
而这个函数是**两段式**的：

1. POST ``emappdata.eastmoney.com/stockrank/getAllCurrentList`` → 拿到代码与名次；
2. GET  ``push2.eastmoney.com/api/qt/ulist.np/get``            → 补名称与涨跌幅。

第 1 段一直是通的，**第 2 段（push2）被代理拦死**，于是整个函数抛异常 ——
表现就是"明明数据就在门口，却报所有数据源均失败"。
``akshare_spot`` 同样压在 ``82.push2`` 上，等于三个源里有两个共用一条死路。

本模块只用第 1 段（实测稳定的域名），名称和涨跌幅改用腾讯批量报价补齐。
这样人气榜就有了一个**实际能出数**的源，而不是"三个源一起超时"。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import pandas as pd

from app.datasource.transport import net
from app.domain.symbol import normalize_symbol
from app.datasource.compat.registry import DataUnavailable
from app.common.logging import logger

EMAPP_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EMAPP_HOSTS = ["emappdata.eastmoney.com"]

#: 腾讯批量报价：一次可带几十个代码，用来补名称与涨跌幅
TX_QUOTE_URL = "https://qt.gtimg.cn/q="
TX_HOSTS = ["qt.gtimg.cn"]

_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
_CODE_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$", re.I)


def _emapp_rank(top: int = 50) -> List[Dict[str, Any]]:
    """取东财人气榜原始名次（只有代码与名次，没有名称/涨跌幅）。"""
    payload = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "pageNo": 1,
        "pageSize": max(int(top or 50), 20),
    }
    try:
        body = net.post_json(EMAPP_URL, json_body=payload, timeout=10)
    except net.NetworkError as exc:
        # 域名/网络故障 → 让注册表换下一个源
        raise type(exc)(f"东财人气榜接口不可用: {exc}") from exc

    data = (body or {}).get("data")
    if not data:
        raise DataUnavailable("东财人气榜接口返回空榜单")

    rows: List[Dict[str, Any]] = []
    for item in data:
        sc = str(item.get("sc") or "").strip().upper()
        m = _CODE_RE.match(sc)
        if not m:
            continue                      # 港股/美股等不在 A 股候选池里
        try:
            rk = int(item.get("rk"))
        except (TypeError, ValueError):
            continue
        rows.append({"symbol": m.group(2), "rank": rk, "market": m.group(1)})
    if not rows:
        raise DataUnavailable("东财人气榜解析后为空")
    return rows


def fetch_tx_quotes(symbols: List[str], market_hint: Optional[Dict[str, str]] = None
                    ) -> Dict[str, Dict[str, Any]]:
    """腾讯批量报价 → {symbol: {name, price, pct_change}}。

    一次请求能带几十个代码（实测 60 个没问题），所以补名称不会变成 N 次请求。
    失败只降级（名称留空、涨跌幅留空），**不抛异常** —— 榜单本身已经拿到了，
    不该因为补不了名称就把整份数据丢掉。
    """
    out: Dict[str, Dict[str, Any]] = {}
    syms = [s for s in symbols if s][:60]
    if not syms:
        return out

    def _q(sym: str) -> str:
        mk = (market_hint or {}).get(sym)
        if mk:
            return _PREFIX.get(mk.upper(), "sh") + sym
        # 没提示就按代码前缀猜：6/9 开头沪市，其余深/北
        return ("sh" if sym[:1] in ("6", "9") else "sz") + sym

    url = TX_QUOTE_URL + ",".join(_q(s) for s in syms)
    try:
        text = net.get_text(url, timeout=10, encoding="gbk")
    except Exception as exc:
        logger.debug(f"腾讯批量报价不可用，名称/涨跌幅留空: {exc}")
        return out

    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        try:
            key, _, payload = line.partition("=")
            fields = payload.strip().strip('"').split("~")
            if len(fields) < 6:
                continue
            code = fields[2].strip()
            name = fields[1].strip()
            price = float(fields[3])
            prev = float(fields[4])
        except (ValueError, IndexError):
            continue
        out[code] = {
            "name": name,
            "price": price,
            "pct_change": round((price - prev) / prev * 100, 2) if prev else None,
        }
    return out


def _hot_rank_emapp(top: Optional[int] = None) -> pd.DataFrame:
    """人气榜源：东财人气榜（emappdata 域名）+ 腾讯报价补全。"""
    want = int(top or 50)
    rows = _emapp_rank(want)

    hint = {r["symbol"]: r["market"] for r in rows}
    quotes = fetch_tx_quotes([r["symbol"] for r in rows], hint)

    # 腾讯没覆盖到的（极少数退市/新股），退回库里的名称对照表
    missing = [r["symbol"] for r in rows if r["symbol"] not in quotes]
    local_names: Dict[str, str] = {}
    if missing:
        try:
            from app.storage.repo.kline_repo import resolve_names

            local_names = resolve_names(missing) or {}
        except Exception:
            local_names = {}

    records = []
    for r in rows:
        q = quotes.get(r["symbol"]) or {}
        records.append({
            "symbol": normalize_symbol(r["symbol"]),
            "name": q.get("name") or local_names.get(r["symbol"], ""),
            "rank": r["rank"],
            "pct_change": q.get("pct_change"),
        })

    out = pd.DataFrame(records)
    out = out[out["symbol"].astype(bool)]
    if out.empty:
        raise DataUnavailable("东财人气榜解析后为空")
    out = out.sort_values("rank").reset_index(drop=True)
    return out.head(want) if want else out


__all__ = ["EMAPP_URL", "EMAPP_HOSTS", "TX_QUOTE_URL", "TX_HOSTS",
           "_emapp_rank", "fetch_tx_quotes", "_hot_rank_emapp"]
