import backtrader as bt


class TrendMaCrossStrategy(bt.Strategy):

    params = dict(
        fast=5,
        mid=20,
        slow=60
    )

    def __init__(self):

        # 短期
        self.ma_fast = bt.ind.SMA(self.data.close, period=self.p.fast)

        # 中期
        self.ma_mid = bt.ind.SMA(self.data.close, period=self.p.mid)

        # 长期（趋势过滤）
        self.ma_slow = bt.ind.SMA(self.data.close, period=self.p.slow)

        # 金叉
        self.cross = bt.ind.CrossOver(self.ma_fast, self.ma_mid)

    def next(self):

        # ====== 1. 定义上升趋势 ======
        up_trend = (
                self.ma_slow[0] < self.ma_mid[0] < self.data.close[0]
        )

        # ====== 2. 没持仓 ======
        if not self.position:

            # 只有在上升趋势中才允许交易
            if up_trend and self.cross > 0:
                print(f'触发买操作')
                self.buy(size=100)

        # ====== 3. 持仓 ======
        else:

            # 趋势破坏 or 死叉 → 出场
            if (not up_trend) or self.cross < 0:
                print(f'触发卖操作')
                self.sell(size=100)