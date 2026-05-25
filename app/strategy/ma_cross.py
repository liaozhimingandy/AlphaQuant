import backtrader as bt


class MaCrossStrategy(bt.Strategy):

    params = (
        ("fast", 5),
        ("slow", 20),
    )

    def __init__(self):

        self.ma_fast = bt.indicators.SMA(
            self.data.close,
            period=self.p.fast
        )

        self.ma_slow = bt.indicators.SMA(
            self.data.close,
            period=self.p.slow
        )

        self.crossover = bt.indicators.CrossOver(
            self.ma_fast,
            self.ma_slow
        )

    def next(self):
        if not self.position:
            if self.crossover > 0:
                self.buy(size=100)
        else:
            if self.crossover < 0:
                self.sell(size=100)