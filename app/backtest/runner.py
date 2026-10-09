#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : runner.py
# @Description : 统一回测执行器：取数 → 组装 Cerebro → 执行 → 产出 BacktestResult
#               替换掉原先散落在 main.py 里三份几乎重复的 run_backtest*
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, List, Optional

import backtrader as bt
import pandas as pd

from app.backtest.analyzers import EquityCurveAnalyzer
from app.backtest.config import BacktestConfig
from app.backtest.registry import get_strategy
from app.backtest.result import BacktestResult, build_result
from app.data.service import MarketDataService
from app.utils.logger import logger

# backtrader 的 PandasData 需要这些列（小写）
REQUIRED_FEED_COLUMNS = ["open", "high", "low", "close", "volume"]


class BacktestError(RuntimeError):
    """回测执行失败。"""


class BacktestRunner:
    """一次回测 = 一个配置 + 一个 Runner。"""

    def __init__(self, config: BacktestConfig, db=None):
        self.config = config
        self.db = db
        self.cerebro: Optional[bt.Cerebro] = None
        self.data: Optional[pd.DataFrame] = None

    # ---------------- 数据 ----------------
    def load_data(self) -> pd.DataFrame:
        df = MarketDataService.load(
            symbol=self.config.symbol,
            start_date=self.config.start_iso,
            end_date=self.config.end_iso,
            source=self.config.data_source,
            db=self.db,
        )
        df = self._prepare_feed(df)
        self.data = df
        return df

    def _prepare_feed(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并清洗成 backtrader 可直接消费的格式。"""
        if df is None or df.empty:
            raise BacktestError(
                f"{self.config.symbol} 在 {self.config.start_iso}~{self.config.end_iso} 无数据"
            )

        df = df.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        df.index.name = "date"

        missing = [c for c in REQUIRED_FEED_COLUMNS if c not in df.columns]
        if missing:
            raise BacktestError(f"行情数据缺少必需列: {missing}")

        for col in REQUIRED_FEED_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 停牌/脏数据直接丢弃，否则 backtrader 会算出 NaN 均线导致信号失真
        before = len(df)
        df = df.dropna(subset=REQUIRED_FEED_COLUMNS)
        df = df[(df["close"] > 0) & (df["volume"] >= 0)]
        df = df.sort_index()
        if len(df) < before:
            logger.warning(f"已过滤脏数据 {before - len(df)} 行")

        if len(df) < 2:
            raise BacktestError(
                f"有效数据仅 {len(df)} 行，不足以回测（需 >= 2 行）"
            )
        return df

    # ---------------- Cerebro ----------------
    def build_cerebro(self, df: pd.DataFrame) -> bt.Cerebro:
        cerebro = bt.Cerebro()

        strategy_cls = get_strategy(self.config.strategy)
        cerebro.addstrategy(strategy_cls, **self._strategy_kwargs(strategy_cls))

        feed = bt.feeds.PandasData(dataname=df)
        cerebro.adddata(feed)

        cerebro.broker.setcash(self.config.cash)
        cerebro.broker.setcommission(commission=self.config.commission)
        if self.config.slippage_perc:
            cerebro.broker.set_slippage_perc(perc=self.config.slippage_perc)

        # A 股保护：禁止裸做空 + 资金校验 + 收盘价成交，杜绝负仓位/透支
        cerebro.broker.set_shortcash(False)
        cerebro.broker.set_checksubmit(True)
        cerebro.broker.set_coc(True)

        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
        cerebro.addanalyzer(bt.analyzers.Transactions, _name="transactions")
        cerebro.addanalyzer(EquityCurveAnalyzer, _name="equity")

        self.cerebro = cerebro
        return cerebro

    def _strategy_kwargs(self, strategy_cls: type[bt.Strategy]) -> Dict[str, Any]:
        """合并策略参数。

        只注入策略**确实声明过**的 printlog，避免给不支持该参数的策略传参报错。
        """
        kwargs = dict(self.config.strategy_params)

        # backtrader 会把 params 转成 AutoInfoClass（不是 dict），所以用 hasattr 判断
        declared = getattr(strategy_cls, "params", None)
        if declared is not None:
            if hasattr(declared, "printlog"):
                kwargs.setdefault("printlog", self.config.print_log)

            # 过滤策略未声明的参数，否则 backtrader 只抛一句
            # "__init__() got an unexpected keyword argument"，排错成本很高
            unknown = [k for k in kwargs if not hasattr(declared, k)]
            for k in unknown:
                logger.warning(
                    f"策略 {self.config.strategy} 不支持参数 {k}，已忽略"
                )
                kwargs.pop(k)
        return kwargs

    # ---------------- 执行 ----------------
    def run(self) -> BacktestResult:
        df = self.load_data() if self.data is None else self.data
        cerebro = self.build_cerebro(df)

        logger.info(
            f"开始回测 | {self.config.symbol} | {self.config.strategy} | "
            f"{self.config.start_iso}~{self.config.end_iso} | "
            f"{len(df)} 根K线 | 初始资金 {self.config.cash:,.2f}"
        )

        try:
            results = cerebro.run()
        except Exception as exc:
            raise BacktestError(f"回测执行失败: {exc}") from exc

        strat = results[0]
        final_cash = float(cerebro.broker.getvalue())

        equity_curve = self._safe_get(strat, "equity")
        if equity_curve is None or equity_curve.empty:
            equity_curve = pd.DataFrame(
                {"value": [self.config.cash, final_cash], "position": [0.0, 0.0]},
                index=pd.to_datetime([df.index[0], df.index[-1]]),
            )
            equity_curve.index.name = "date"

        trade_stats = self._extract_trade_stats(strat)
        trades = self._extract_trades(strat)

        # 空头持仓检测：纯多头策略出现负持仓一定是缺陷（僵尸止损单在同一根K线
        # 撤销失败、或空仓时误卖），这类问题必须显式暴露，不能让结果静默通过。
        min_position = 0.0
        if "position" in equity_curve and not equity_curve.empty:
            min_position = float(equity_curve["position"].min())
        if min_position < 0:
            logger.warning(
                f"⚠️ 检测到空头持仓（最小持仓 {min_position:.0f} 股）。"
                f"多头策略不应出现负仓位，请检查止损单的撤销与下单时机。"
            )

        result = build_result(
            config=self.config,
            equity_curve=equity_curve,
            initial_cash=self.config.cash,
            final_cash=final_cash,
            trade_stats=trade_stats,
            trades=trades,
            min_position=min_position,
        )

        if self.config.plot:
            self._plot(cerebro)

        logger.success(
            f"回测完成 | 最终资产 {result.final_cash:,.2f} | "
            f"收益率 {result.total_return_pct:.2f}% | 交易 {result.trade_count} 次"
        )
        return result

    # ---------------- 结果解析（全部做空值保护） ----------------
    @staticmethod
    def _safe_get(strat, name: str) -> Any:
        try:
            return getattr(strat.analyzers, name).get_analysis()
        except Exception as exc:
            logger.debug(f"读取分析器 {name} 失败: {exc}")
            return None

    @staticmethod
    def _extract_trade_stats(strat) -> Dict[str, Any]:
        try:
            analysis = strat.analyzers.trades.get_analysis()
        except Exception:
            return {}

        def deep(d: dict, *keys, default=0):
            cur = d
            for k in keys:
                if not isinstance(cur, dict):
                    return default
                cur = cur.get(k)
                if cur is None:
                    return default
            return cur

        # 以"已平仓交易"为准：未平仓的浮盈不计入胜率
        closed = deep(analysis, "total", "closed", default=0)
        won = deep(analysis, "won", "total", default=0)
        lost = deep(analysis, "lost", "total", default=0)

        return {
            "total": int(closed or 0),
            "won": int(won or 0),
            "lost": int(lost or 0),
            "avg_win": float(deep(analysis, "won", "pnl", "average", default=0.0)),
            "avg_lose": float(deep(analysis, "lost", "pnl", "average", default=0.0)),
            "gross_win": float(deep(analysis, "won", "pnl", "total", default=0.0)),
            "gross_lose": float(deep(analysis, "lost", "pnl", "total", default=0.0)),
        }

    @staticmethod
    def _extract_trades(strat) -> List[Dict]:
        """从 Transactions 提取成交明细。"""
        try:
            analysis = strat.analyzers.transactions.get_analysis()
        except Exception:
            return []

        rows: List[Dict] = []
        for dt, items in analysis.items():
            if isinstance(items, dict):
                items = list(items.values())
            for item in items or []:
                try:
                    # Transactions 的元素形如 [amount, price, sid, sym, value]
                    amount, price = float(item[0]), float(item[1])
                except Exception:
                    continue
                rows.append(
                    {
                        "date": str(dt)[:10] if dt is not None else "",
                        "side": "BUY" if amount > 0 else "SELL",
                        "size": abs(amount),
                        "price": price,
                        "amount": abs(amount) * price,
                    }
                )
        return rows

    @staticmethod
    def _plot(cerebro: bt.Cerebro) -> None:
        try:
            cerebro.plot(style="candle")
        except Exception as exc:
            logger.warning(f"绘图失败(不影响回测结果): {exc}")


def run_backtest(config: BacktestConfig, db=None) -> BacktestResult:
    """便捷函数：一步跑完回测。"""
    return BacktestRunner(config, db=db).run()
