#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : score_strategy.py
# @Description : 综合评分回测策略（backtrader 链路）
#
#                与实盘 ScoreStrategy 用**同一套评分数据**（stock_score 表）：
#                这样"回测里选出来的票"和"实盘要买的票"是同一批，
#                而不是回测用一套打分、实盘用另一套 —— 那是最常见的自欺。
#
#                评分是**日频**的：一根K线上只有一个分。
#                所以这里不做任何"日内"逻辑，只在每日收盘决策。
#
#                取不到当天评分时**不动手**（不买也不卖）。
#                这是与实盘一致的保守选择。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

import backtrader as bt

from app.utils.logger import logger

STRATEGY_NAME = "score"


class ScoreStrategy(bt.Strategy):
    """按多维度综合分决策的回测策略。"""

    STRATEGY_NAME = STRATEGY_NAME

    params = dict(
        entry_score=70.0,
        exit_score=55.0,
        lot_size=100,
        cash_buffer=0.95,
        printlog=False,
        symbol="",
    )

    def __init__(self) -> None:
        self.order = None
        self._scores: Dict[str, float] = self._load_scores()
        self._used = 0
        self._missed = 0

    # ------------------------------------------------------------------
    def _load_scores(self) -> Dict[str, float]:
        """一次性把该标的的历史评分读进内存。

        在 __init__ 里读而不是每根K线查库：后者会让一次回测变成几千次查询，
        而且回测期间数据是静态的，没必要反复读。
        """
        symbol = str(self.p.symbol or getattr(self.data, "_name", "") or "")
        if not symbol:
            return {}
        try:
            from app.db.database import SessionLocal
            from app.db.models import StockScore

            with SessionLocal() as s:
                rows = (s.query(StockScore)
                        .filter(StockScore.symbol == symbol)
                        .all())
            return {str(r.date): float(r.total_score)
                    for r in rows if r.total_score is not None}
        except Exception as exc:
            logger.warning(f"[score] 读取历史评分失败（该策略不会触发交易）: {exc}")
            return {}

    def _score_on(self, day: date) -> Optional[float]:
        return self._scores.get(str(day))

    # ------------------------------------------------------------------
    def log(self, msg: str) -> None:
        if self.p.printlog:
            print(f"{self.data.datetime.date(0)} | {msg}")

    def notify_order(self, order) -> None:
        if order.status in (order.Submitted, order.Accepted):
            return
        self.order = None

    def next(self) -> None:
        if self.order is not None:
            return
        day = self.data.datetime.date(0)
        score = self._score_on(day)
        if score is None:
            self._missed += 1
            return
        self._used += 1

        if not self.position:
            if score >= float(self.p.entry_score):
                price = float(self.data.close[0])
                if price <= 0:
                    return
                size = int(self.broker.getcash() * float(self.p.cash_buffer)
                           / price / int(self.p.lot_size)) * int(self.p.lot_size)
                if size > 0:
                    self.log(f"评分 {score:.1f} ≥ {self.p.entry_score} → 买入 {size}")
                    self.order = self.buy(size=size)
            return

        if score <= float(self.p.exit_score):
            self.log(f"评分 {score:.1f} ≤ {self.p.exit_score} → 平仓")
            self.order = self.close()

    def stop(self) -> None:
        if not self._scores:
            logger.warning(
                "[score] 该标的没有历史评分数据，回测不会产生任何交易。"
                "先跑 `python main.py pool refresh` 生成评分。"
            )
        else:
            logger.info(
                f"[score] 回测结束 | 评分覆盖 {self._used} 天 / 缺失 {self._missed} 天 | "
                f"期末权益 {self.broker.getvalue():,.2f}"
            )


__all__ = ["ScoreStrategy"]
