import backtrader as bt
import pandas as pd

from app.strategy.ma_cross import MaCrossStrategy
from app.strategy.trend_ma_cross import TrendMaCrossStrategy


def run_backtest():

    cerebro = bt.Cerebro()

    cerebro.addstrategy(MaCrossStrategy)
    cerebro.addstrategy(TrendMaCrossStrategy)

    cerebro.broker.setcash(10000)

    df = pd.read_csv(
        "data/stock/000001.csv",
        parse_dates=["date"],
        index_col="date"
    )

    data = bt.feeds.PandasData(dataname=df)

    cerebro.adddata(data)

    print("start:", cerebro.broker.getvalue())

    cerebro.run()

    print("end:", cerebro.broker.getvalue())

    cerebro.plot()


if __name__ == "__main__":
    run_backtest()