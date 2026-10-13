#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : profile.py
# @Description : 标的主表同步 —— 全市场股票列表 / 上市日期 / 板块分类入库
#
#                为什么上市日期必须联网同步，而不能本地推导
#                ============================================================
#                一个很容易想到、也很容易做错的实现是：
#                "上市日期 = 库里第一根 K 线的日期"。它看着还挺自洽，
#                实际上把一个**外部事实**替换成了**本地行为的副产品**：
#
#                    一只 2007 年上市的老票，2026 年才被纳入候选池的话，
#                    按这个算法它的上市日期会变成 2026 年 —— 于是
#                    "这只票上市首日到现在"的所有统计全部错位。
#
#                所以本模块只做一件事：**从交易所/数据源取回产权列表**，
#                写到 :class:`app.data.schema.StockInfo`。

#                为什么要排一串数据源而不是认准一个
#                ============================================================
#                没有任何一个稳定的公开接口能一次性给出
#                "全市场 × 代码 + 名称 + 上市日期"。实际情况是：
#
#                  * 交易所列表接口分别给沪深京三份，**带上市日期**，但
#                    只覆盖自己那一个交易所，且偶发改版（列改名/换 URL）；
#                  * 全市场快照接口一次给全 A 股代码+名称，**不带上市日期**，
#                    也最快、最稳；
#                  * 个股资料接口什么都有，但**一次只能查一只票**。
#
#                所以这里是**三条腿降级**，顺序讲究：
#                  1. 先铺面：全市场快照（拿到"有哪些票"并保证名称覆盖）；
#                  2. 再补质：逐个交易所列表（补上市日期与板块）；
#                  3. 最后补漏：只对**仍缺上市日期**的少量标的打个股资料接口
#                     （限额 `individual_limit`，因为它最贵）。
#
#                每一步失败都只损失那一步的信息，前面的成果不会回滚 ——
#                这是"部分可用"优于"全有或全无"的典型场景。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from app.common.logging import logger

#: 每个交易所列表接口的期望列名（按优先级）。写成候选集合而不是固定名，
#: 因为这些中文字段的命名在 akshare 各版本间并不稳定。
_COL_CODE = ("证券代码", "股票代码", "代码", "A股代码")
_COL_NAME = ("证券简称", "股票简称", "名称", "证券名称")
_COL_DATE = ("上市日期", "上市时间", "挂牌日期", "首次上市日期")
_COL_BOARD = ("板块", "证券类别", "市场")

#: 逐个交易所铺 Native 列表。(函数名, 参数, 市场, 板块, 来源标签)
_EXCHANGE_SOURCES: Tuple[Dict[str, Any], ...] = (
    {"fn": "stock_info_sh_name_code", "args": ("主板A股",), "market": "sh",
     "board": "main", "source": "sse"},
    {"fn": "stock_info_sh_name_code", "args": ("科创板",), "market": "sh",
     "board": "kcb", "source": "sse"},
    {"fn": "stock_info_sz_name_code", "args": ("A股列表",), "market": "sz",
     "board": "", "source": "szse"},
    {"fn": "stock_info_bj_name_code", "args": (), "market": "bj",
     "board": "bj", "source": "bse"},
)


def _pick(df: Any, candidates: Sequence[str]) -> Optional[str]:
    """在一堆可能的列名里挑一个真实存在的（先全等，后包含匹配）。"""
    # 注意别写成 ``getattr(df, "columns", []) or []``：
    # pandas 的 Index 不允许被当成布尔值（会抛
    # "The truth value of an Index is ambiguous"），
    # 而 ``or`` 恰好会去取它的真值 —— 一行看起来无害的兜底写法，
    # 会让**每一次**列匹配都炸在空表上。
    raw_cols = getattr(df, "columns", None)
    cols = list(raw_cols) if raw_cols is not None else []
    for want in candidates:
        for c in cols:
            if str(c).strip() == want:
                return c
    for want in candidates:
        for c in cols:
            if want in str(c):
                return c
    return None


def _to_date(value: Any) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    txt = str(value).strip()
    if not txt or txt.lower() in ("nan", "none", "-"):
        return None
    txt = txt.split(" ")[0].replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def _rows_from_frame(df: Any, market: str = "", board: str = "",
                     source: str = "") -> List[Dict[str, Any]]:
    """把一个交易所列表 DataFrame 规整成若干条标的信息。"""
    if df is None:
        return []
    empty = getattr(df, "empty", None)
    if empty is None or bool(empty):
        return []
    c_code = _pick(df, _COL_CODE)
    if c_code is None:
        return []
    c_name = _pick(df, _COL_NAME)
    c_date = _pick(df, _COL_DATE)
    c_board = _pick(df, _COL_BOARD) if not board else None

    out: List[Dict[str, Any]] = []
    for rec in df.to_dict("records"):
        try:
            from app.domain.symbol import normalize_symbol

            sym = normalize_symbol(str(rec.get(c_code) or "").strip())
        except Exception:
            continue
        row: Dict[str, Any] = {"symbol": sym, "source": source}
        if c_name:
            row["name"] = str(rec.get(c_name) or "").strip()
        if c_date:
            row["list_date"] = _to_date(rec.get(c_date))
        if board:
            row["board"] = board
        elif c_board:
            row["board"] = str(rec.get(c_board) or "").strip()[:64]
        if market:
            row["market"] = market
        out.append(row)
    return out


class StockProfileSync:
    """标的主表同步器。

    :param fetcher:  数据取数函数 ``fn(name, *args, **kwargs) -> DataFrame``。
                     默认走 :func:`facade.call_akshare`（有限流 + 退避重试）。
                     **留这个注入口的全部理由是为了离线测试** ——
                     上市日期这种"同步了才知道"的能力，如果只能在联网环境下
                     被测，那么它的回归测试就等于没有。
    :param individual_limit: 单次最多用个股资料接口补全多少只标的。
                     这个接口一只票一次请求，是整个链路里最贵的一步。
    :param min_interval:     两次请求之间的最小间隔（秒），**额外**给一条
                     手动退避。默认 0 = 完全交给 facade 的限速器 ——
                     正常情况下那里已经档好了，不需要再来一层。
    """

    def __init__(
        self,
        fetcher: Optional[Callable[..., Any]] = None,
        individual_limit: int = 50,
        min_interval: float = 0.0,
    ) -> None:
        self._fetcher = fetcher
        self.individual_limit = max(0, int(individual_limit))
        self.min_interval = max(0.0, float(min_interval or 0.0))

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------
    def _call_ak(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._fetcher is not None:
            return self._fetcher(name, *args, **kwargs)
        from app.providers.market.service import facade as MX

        return MX.call_akshare(name, *args, limit_key="profile", **kwargs)

    def _throttle(self) -> None:
        if self.min_interval > 0:
            time.sleep(self.min_interval)

    # ------------------------------------------------------------------
    # 三步同步
    # ------------------------------------------------------------------
    def _sync_spot(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """第一步：全市场快照 —— 先把"有哪些票、叫什么"铺满。"""
        try:
            rows = self._fetcher_spot()
        except Exception as exc:
            return [], f"{type(exc).__name__}: {str(exc)[:160]}"
        out = [{"symbol": r.get("symbol"), "name": r.get("name") or "",
                "source": "spot"} for r in rows]
        return out, None

    def _fetcher_spot(self) -> List[Dict[str, Any]]:
        """取全市场快照的行列表。单独拆出来是为了让测试能绕过网络。"""
        if self._fetcher is not None:
            df = self._fetcher("stock_zh_a_spot_em")
            cols = {str(c).strip(): c for c in getattr(df, "columns", [])}
            c_code = cols.get("代码") or cols.get("股票代码")
            c_name = cols.get("名称") or cols.get("股票简称")
            if c_code is None:
                raise ValueError("全市场快照缺少代码列")
            out = []
            for rec in df.to_dict("records"):
                out.append({
                    "symbol": str(rec.get(c_code) or "").strip(),
                    "name": str(rec.get(c_name) or "").strip(),
                })
            return out
        from app.providers.market.service import facade as MX

        df = MX.fetch_spot()
        return [{"symbol": r.get("symbol"), "name": r.get("name") or ""}
                for r in df.to_dict("records")]

    def _sync_exchanges(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """第二步：逐个交易所拉清单 —— 这一步才有上市日期。"""
        rows: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []
        for spec in _EXCHANGE_SOURCES:
            try:
                self._throttle()
                df = self._call_ak(spec["fn"], *spec["args"])
                got = _rows_from_frame(
                    df, market=spec["market"], board=spec["board"],
                    source=spec["source"],
                )
                if got:
                    rows.extend(got)
                    logger.info(
                        f"标的主表 | {spec['source']} 列表 {spec['fn']}"
                        f"{spec['args'] or ''} 取到 {len(got)} 只"
                    )
                else:
                    failed.append({
                        "source": f"{spec['source']}:{spec['fn']}{spec['args'] or ''}",
                        "error": "返回为空或缺少代码列（接口改名？）",
                    })
            except Exception as exc:
                # 一个交易所挂了只影响那个交易所，其它照旧
                failed.append({
                    "source": f"{spec['source']}:{spec['fn']}{spec['args'] or ''}",
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                })
                logger.warning(f"标的主表 | {spec['fn']} 取数失败: {exc}")
        return rows, failed

    def _sync_individual(self, symbols: Sequence[str]
                         ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """第三步：对仍缺上市日期的少量标的，逐只查个股资料。

        这一步是**补漏**而不是主流程：它一次只能查一只票，拿它的额度去铺
        全市场（5000+ 只）既不现实也不礼貌。所以只在"前两步做完还有缺口"
        时按 :attr:`individual_limit` 限量执行，剩下的下一轮继续 ——
        多轮之后自然补齐，且每轮的请求量都可预期。
        """
        rows: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []
        for sym in list(symbols)[: self.individual_limit]:
            try:
                self._throttle()
                df = self._call_ak("stock_individual_info_em", symbol=sym)
            except Exception as exc:
                failed.append({"source": f"individual:{sym}",
                               "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
                continue
            rows.append(self._parse_individual(sym, df))
        return rows, failed

    @staticmethod
    def _parse_individual(symbol: str, df: Any) -> Dict[str, Any]:
        """个股资料接口返回的是 (item, value) 两列的长表，把它拍平。"""
        item_c = _pick(df, ("item", "指标", "项目"))
        val_c = _pick(df, ("value", "值", "数值"))
        meta: Dict[str, Any] = {}
        if item_c and val_c:
            for rec in df.to_dict("records"):
                key = str(rec.get(item_c) or "").strip()
                if key:
                    meta[key] = rec.get(val_c)
        row: Dict[str, Any] = {"symbol": symbol, "source": "individual"}
        for key in _COL_DATE:
            if key in meta:
                row["list_date"] = _to_date(meta.get(key))
                break
        if "行业" in meta:
            row["industry"] = str(meta.get("行业") or "").strip()[:64]
        if "股票简称" in meta:
            row["name"] = str(meta.get("股票简称") or "").strip()

        def _num(key: str) -> Optional[float]:
            try:
                v = float(str(meta.get(key)).replace(",", ""))
                return v
            except Exception:
                return None

        for src_key, field in (("总股本", "total_share"), ("流通股", "float_share")):
            got = _num(src_key)
            if got is not None:
                row[field] = got
        return row

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------
    def _save(self, rows: List[Dict[str, Any]], report: Dict[str, Any]) -> int:
        """``save_profiles`` 的薄封装：失败记进报告而不是让整轮同步崩掉。"""
        from app.data.repo import stock_info_repo as P

        try:
            return int(P.save_profiles(rows) or 0)
        except Exception as exc:
            report["failed"].append({"source": "save", "error": str(exc)[:200]})
            logger.error(f"标的主表落库失败: {exc}")
            return 0

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def sync(self, force: bool = False, board_meta: bool = True,
             individual: bool = True) -> Dict[str, Any]:
        """同步标的主表。

        :param force:      True = 无视"要不要同步"的判断，全量重拉一遍
        :param board_meta: 是否用**代码规则**补齐 market/board
                           （不需要联网，几乎不可能失败，默认开）
        :param individual: 是否走第三步的逐只补漏
        :return: 报告字典。``ok`` 表示"有没有写入"，**不代表全部成功** ——
                 三个源里挂掉一个仍然算部分成功，`sources` 里记得清清楚楚。
        """
        from app.data.repo import stock_info_repo as P

        t0 = time.monotonic()
        P.ensure_schema()
        report: Dict[str, Any] = {
            "ok": False,
            "written": 0,
            "rows": 0,
            "mode": "full" if force else "auto",
            "sources": [],
            "failed": [],
            "skipped": False,
            "reason": "",
        }

        before_missing = 0
        total_now = P.count()
        if not force:
            # 要不要同步：**缺口数量**优先于"上次同步时间"。
            # 主表有一千只票没有上市日期，比主表晚同步三天严重得多 ——
            # 前者会让K线任务跳过或误采一千只标的，后者只是晚几天。
            before_missing = len(P.missing_list_date())
            if total_now and not before_missing:
                report["skipped"] = True
                report["reason"] = f"主表已覆盖 {total_now} 只且无缺漏，跳过"
                report["duration_ms"] = int((time.monotonic() - t0) * 1000)
                report["ok"] = True
                return report

        rows: List[Dict[str, Any]] = []

        spot_rows, spot_err = self._sync_spot()
        if spot_err:
            report["failed"].append({"source": "spot", "error": spot_err})
            logger.warning(f"标的主表 | 全市场快照失败: {spot_err}")
        else:
            rows.extend(spot_rows)
            report["sources"].append({"source": "spot", "rows": len(spot_rows),
                                      "with_list_date": False})

        ex_rows, ex_failed = self._sync_exchanges()
        rows.extend(ex_rows)
        n_with_date = sum(1 for r in ex_rows if r.get("list_date"))
        report["sources"].append({
            "source": "exchange", "rows": len(ex_rows), "with_list_date": n_with_date,
        })
        report["failed"].extend(ex_failed)

        # 为什么分两批落库，而不是攒到最后一次性写
        # ============================================================
        # 第三步的"还差谁"是**查库**得来的，而库里此刻还没有本轮刚取到的
        # 标的 —— 攒到最后一次性写的话，``missing_list_date()`` 永远查不到
        # 它们，补漏这一步就成了死代码（首轮同步尤其明显：库是空的，
        # 于是"缺漏清单"也是空的，个股资料接口一次都不会被调用）。
        # 所以先把前两步的成果落库，再据此算缺口。
        if rows:
            if board_meta:
                rows = _fill_board(rows)
            report["written"] += self._save(rows, report)

        if individual:
            known = {str(r.get("symbol")) for r in rows if r.get("symbol")}
            still_missing: List[str] = []
            try:
                still_missing = [
                    s for s in P.missing_list_date()
                    # 本轮碰到过的标的优先补；本轮一无所获（known 为空）时
                    # 才去啃历史存量 —— 否则一次首轮同步就会拿个股资料接口
                    # 去铺全市场 5000 只票。
                    if (force or s in known or not known)
                ]
            except Exception as exc:  # pragma: no cover
                logger.warning(f"标的主表 | 查询缺漏失败: {exc}")
            still_missing = still_missing[: self.individual_limit]
            if still_missing:
                ind_rows, ind_failed = self._sync_individual(still_missing)
                if ind_rows:
                    if board_meta:
                        ind_rows = _fill_board(ind_rows)
                    report["written"] += self._save(ind_rows, report)
                report["sources"].append({
                    "source": "individual",
                    "rows": len(ind_rows),
                    "with_list_date": sum(1 for r in ind_rows if r.get("list_date")),
                })
                report["failed"].extend(ind_failed)

        report["rows"] = len(rows)
        report["ok"] = int(report.get("written") or 0) > 0

        try:
            report["coverage"] = P.summary()
        except Exception:  # pragma: no cover
            report["coverage"] = {}
        report["duration_ms"] = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"标的主表同步 | 写入 {report['written']}/{report['rows']} 只 | "
            f"覆盖 {(report.get('coverage') or {}).get('total', 0)} 只 | "
            f"失败 {len(report['failed'])} 个源"
        )
        return report


#: 板块 → 市场。北交所自成一家，其余按板块归属沪深两个交易所。
_MARKET_OF_BOARD: Dict[str, str] = {
    "sh": "sh", "kcb": "sh", "sz": "sz", "cyb": "sz", "bj": "bj",
}


def _fill_board(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """用**代码规则**定 market/board —— 不联网、不会失败。

    为什么代码规则是权威、接口标签只是兜底
    ============================================================
    交易所列表是**按板块分次调用**的：上证要调两次（主板A股 / 科创板），
    每次返回的每一行都被盖上这次调用的板块标签。这在接口正常时没问题
    —— 一只票只属于一个板块，只会出现在一次调用里。

    但它有一个很不显眼的失效模式：**接口改版或参数失效时，两次调用可能
    返回同一张全量清单** —— 于是同一只票先被打上 ``main``、再被打上
    ``kcb``，后写覆盖先写，600000 的板块就变成了科创板。
    而代码前缀是交易所的固定编码（60/68/30/00/8），永远不会变，
    也不会因为某次调用返回了什么而改变。

    所以这里让 :func:`classify_board` 说了算，接口自带的板块标签只在
    代码规则推不出来（返回 ``other``，比如 B 股）时才兜底使用。
    """
    try:
        from app.modules.universe.pool.pool import classify_board
    except Exception:  # pragma: no cover - 极端情况下不补也照样能存
        return rows
    for r in rows:
        sym = str(r.get("symbol") or "")
        if not sym:
            continue
        derived = classify_board(sym)
        hint = str(r.get("board") or "").strip()
        r["board"] = derived if derived != "other" else (hint or "other")
        if not r.get("market"):
            r["market"] = _MARKET_OF_BOARD.get(r["board"], "other")
    return rows


def sync_full(force: bool = False, **kw: Any) -> Dict[str, Any]:
    """便捷入口（任务处理器与 CLI 用）。"""
    return StockProfileSync(**kw).sync(force=force)


__all__ = ["StockProfileSync", "sync_full", "_rows_from_frame"]
