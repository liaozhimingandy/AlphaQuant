import backtrader as bt
import pandas as pd

from app.db.database import SessionLocal
from app.repository.stock_repository import StockRepository
from app.strategy.ma_cross import MaCrossStrategy
from app.strategy.strategy import PreciseMaCrossStrategyI

# from app.strategy.trend_ma_cross import TrendMaCrossStrategy

INITIAL_CASH = 10000

def run_backtest():

    cerebro = bt.Cerebro()

    # 基础设置
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.broker.setcommission(commission=0.0003)  # 万3手续费
    cerebro.broker.set_slippage_perc(perc=0.001)  # 0.1%滑点

    df = pd.read_csv(
        "data/stock/000001.csv",
        parse_dates=["date"],
        index_col="date"
    )

    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)

    # 添加策略
    cerebro.addstrategy(MaCrossStrategy)
    # cerebro.addstrategy(TrendMaCrossStrategy)

    # ================== 核心：永久杜绝负仓位的设置 ==================
    cerebro.broker.set_shortcash(False)  # 禁止做空：没持仓绝对不能卖
    cerebro.broker.set_checksubmit(True)  # 开启资金检查：没钱不能买，禁止透支
    cerebro.broker.set_coc(True)  # 成交价格对齐收盘价，避免成交价跳空导致超支
    # ==============================================================

    # 添加回测指标
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trade")

    print("start:", cerebro.broker.getvalue())

    results = cerebro.run()

    print("end:", cerebro.broker.getvalue())
    # 打印回测结果
    for strat in results:
        # ================== 核心：输出最终盈利率+所有收益指标 ==================
        final_value = cerebro.broker.get_value()
        # 1. 核心最终盈利率
        total_profit = final_value - INITIAL_CASH
        total_profit_rate = (final_value - INITIAL_CASH) / INITIAL_CASH * 100

        # 2. 复利年化收益率（自动计算回测天数，适配任意周期）
        backtest_days = (df.index[-1] - df.index[0]).days
        annual_return = ((final_value / INITIAL_CASH) ** (365 / backtest_days) - 1) * 100 if backtest_days > 0 else 0

        # 3. 交易统计指标（安全取值，无交易也不会报错）
        trade_analyzer = strat.analyzers.trade.get_analysis()
        total_trades = trade_analyzer.get("total", {}).get("total", 0)
        win_trades = trade_analyzer.get("won", {}).get("total", 0)
        win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0
        max_drawdown = strat.analyzers.drawdown.get_analysis()["max"]["drawdown"]
        sharpe_ratio = strat.analyzers.sharpe.get_analysis().get("sharperatio", 0)

        # 格式化打印最终结果
        print("\n" + "=" * 100)
        print("📊 【最终回测结果】")
        print("=" * 100)
        print(f"最终账户总市值：{final_value:.2f}元")
        print(f"总盈亏：{total_profit:.2f}元")
        print(f"✅ 最终总盈利率：{total_profit_rate:.2f}%")
        print(f"✅ 复利年化收益率：{annual_return:.2f}%")
        print("-" * 50)
        print(f"总交易次数：{total_trades}次")
        print(f"交易胜率：{win_rate:.2f}%")
        # print(f"夏普比率：{sharpe_ratio:.2f}")
        print(f"最大回撤：{max_drawdown:.2f}%")
        print("=" * 100)

    # 绘制回测图（自动标记买卖点箭头）
    # cerebro.plot(style="candle")


def run_backtest2(
        symbol: str = "000001",
        start_date: str = "2023-01-01",
        end_date: str = "2026-05-25"
):

    db = SessionLocal()

    try:

        # ======================
        # 从数据库读取数据
        # ======================

        df = StockRepository.get_stock_df(
            db=db,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date
        )

        if df.empty:
            print("没有回测数据")
            return

        # ======================
        # 初始化 Cerebro
        # ======================

        cerebro = bt.Cerebro()

        cerebro.broker.setcash(INITIAL_CASH)

        cerebro.broker.setcommission(
            commission=0.0003
        )

        cerebro.broker.set_slippage_perc(
            perc=0.001
        )

        # 禁止做空
        cerebro.broker.set_shortcash(False)

        # 开启资金检查
        cerebro.broker.set_checksubmit(True)

        # 收盘价成交
        cerebro.broker.set_coc(True)

        # ======================
        # 加载数据
        # ======================

        data = bt.feeds.PandasData(
            dataname=df
        )

        cerebro.adddata(data)

        # ======================
        # 添加策略
        # ======================

        cerebro.addstrategy(
            MaCrossStrategy
        )

        # ======================
        # 分析器
        # ======================

        cerebro.addanalyzer(
            bt.analyzers.SharpeRatio,
            _name="sharpe"
        )

        cerebro.addanalyzer(
            bt.analyzers.DrawDown,
            _name="drawdown"
        )

        cerebro.addanalyzer(
            bt.analyzers.TradeAnalyzer,
            _name="trade"
        )

        # ======================
        # 开始回测
        # ======================

        print("=" * 100)
        print(f"开始回测: {symbol}")
        print(f"初始资金: {INITIAL_CASH:.2f}")
        print("=" * 100)

        results = cerebro.run()

        final_value = cerebro.broker.getvalue()

        strat = results[0]

        # ======================
        # 收益指标
        # ======================

        total_profit = final_value - INITIAL_CASH

        total_profit_rate = (
                total_profit / INITIAL_CASH
        ) * 100

        backtest_days = (
            df.index[-1] - df.index[0]
        ).days

        annual_return = (
            (
                final_value / INITIAL_CASH
            ) ** (
                365 / backtest_days
            ) - 1
        ) * 100 if backtest_days > 0 else 0

        trade_analysis = (
            strat.analyzers.trade
            .get_analysis()
        )

        total_trades = (
            trade_analysis
            .get("total", {})
            .get("total", 0)
        )

        win_trades = (
            trade_analysis
            .get("won", {})
            .get("total", 0)
        )

        win_rate = (
            win_trades / total_trades * 100
            if total_trades > 0 else 0
        )

        drawdown = (
            strat.analyzers.drawdown
            .get_analysis()["max"]["drawdown"]
        )

        sharpe = (
            strat.analyzers.sharpe
            .get_analysis()
            .get("sharperatio", 0)
        )

        # ======================
        # 打印结果
        # ======================

        print("\n" + "=" * 100)
        print("📊 回测结果")
        print("=" * 100)

        print(f"最终资产: {final_value:.2f}")
        print(f"总收益: {total_profit:.2f}")
        print(f"总收益率: {total_profit_rate:.2f}%")
        print(f"年化收益率: {annual_return:.2f}%")

        print("-" * 50)

        print(f"总交易次数: {total_trades}")
        print(f"胜率: {win_rate:.2f}%")
        print(f"最大回撤: {drawdown:.2f}%")
        print(f"夏普比率: {sharpe}")

        print("=" * 100)

        # ======================
        # 绘图
        # ======================

        # cerebro.plot(style="candle")

    finally:

        db.close()

def run_backtest3(
symbol: str = "000001",
        start_date: str = "2023-01-01",
        end_date: str = "2026-05-25"
):
    db = SessionLocal()

    try:

        # ======================
        # 从数据库读取数据
        # ======================

        df = StockRepository.get_stock_df(
            db=db,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date
        )

        if df.empty:
            print("没有回测数据")
            return
            # ======================
            # 初始化 Cerebro
            # ======================

        cerebro = bt.Cerebro()

        cerebro.broker.setcash(INITIAL_CASH)

        cerebro.broker.setcommission(
            commission=0.0003
        )

        cerebro.broker.set_slippage_perc(
            perc=0.001
        )

        # 禁止做空
        cerebro.broker.set_shortcash(False)

        # 开启资金检查
        cerebro.broker.set_checksubmit(True)

        # 收盘价成交
        cerebro.broker.set_coc(True)

        # ======================
        # 加载数据
        # ======================

        data = bt.feeds.PandasData(
            dataname=df
        )

        cerebro.adddata(data)

        # ======================
        # 添加策略
        # ======================

        cerebro.addstrategy(
            PreciseMaCrossStrategyI
        )

        # ======================
        # 分析器
        # ======================

        cerebro.addanalyzer(
            bt.analyzers.SharpeRatio,
            _name="sharpe"
        )

        cerebro.addanalyzer(
            bt.analyzers.DrawDown,
            _name="drawdown"
        )

        cerebro.addanalyzer(
            bt.analyzers.TradeAnalyzer,
            _name="trade"
        )

        # ======================
        # 开始回测
        # ======================

        print("=" * 100)
        print(f"开始回测: {symbol}")
        print(f"初始资金: {INITIAL_CASH:.2f}")
        print("=" * 100)

        results = cerebro.run()

        final_value = cerebro.broker.getvalue()

        strat = results[0]

        # ======================
        # 收益指标
        # ======================

        total_profit = final_value - INITIAL_CASH

        total_profit_rate = (
                                    total_profit / INITIAL_CASH
                            ) * 100

        backtest_days = (
                df.index[-1] - df.index[0]
        ).days

        annual_return = (
                                (
                                        final_value / INITIAL_CASH
                                ) ** (
                                        365 / backtest_days
                                ) - 1
                        ) * 100 if backtest_days > 0 else 0

        trade_analysis = (
            strat.analyzers.trade
            .get_analysis()
        )

        total_trades = (
            trade_analysis
            .get("total", {})
            .get("total", 0)
        )

        win_trades = (
            trade_analysis
            .get("won", {})
            .get("total", 0)
        )

        win_rate = (
            win_trades / total_trades * 100
            if total_trades > 0 else 0
        )

        drawdown = (
            strat.analyzers.drawdown
            .get_analysis()["max"]["drawdown"]
        )

        sharpe = (
            strat.analyzers.sharpe
            .get_analysis()
            .get("sharperatio", 0)
        )

        # ======================
        # 打印结果
        # ======================

        print("\n" + "=" * 100)
        print("📊 回测结果")
        print("=" * 100)

        print(f"最终资产: {final_value:.2f}")
        print(f"总收益: {total_profit:.2f}")
        print(f"总收益率: {total_profit_rate:.2f}%")
        print(f"年化收益率: {annual_return:.2f}%")

        print("-" * 50)

        print(f"总交易次数: {total_trades}")
        print(f"胜率: {win_rate:.2f}%")
        print(f"最大回撤: {drawdown:.2f}%")
        print(f"夏普比率: {sharpe}")

        print("=" * 100)

        # ======================
        # 绘图
        # ======================

        cerebro.plot(style="candle")
    except Exception as e:
        print(e)
        raise e

if __name__ == "__main__":
    run_backtest3()