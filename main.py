import backtrader as bt
import pandas as pd

from app.strategy.ma_cross import MaCrossStrategy
from app.strategy.trend_ma_cross import TrendMaCrossStrategy


def run_backtest():

    cerebro = bt.Cerebro()

    # 基础设置
    cerebro.broker.setcash(10000)
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
        print("夏普比率：", strat.analyzers.sharpe.get_analysis()["sharperatio"])
        print("最大回撤：%.2f%%" % strat.analyzers.drawdown.get_analysis()["max"]["drawdown"])
        print("交易次数：", strat.analyzers.trade.get_analysis()["total"]["total"])
        print('*'*80)

    # 绘制回测图（自动标记买卖点箭头）
    cerebro.plot(style="candle")


if __name__ == "__main__":
    run_backtest()