import backtrader as bt


class MaCrossStrategy(bt.Strategy):

    params = (
        ("fast", 5), # 5 日均线
        ("slow", 20),# 20 日均线
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
        self.order = None
        self.buy_price = None
        # 新增：A股实盘资金冻结逻辑
        self.frozen_cash = 0.0  # 挂单冻结的资金，下单立刻扣减可用

    @property
    def available_cash(self):
        """核心：真实可用资金 = 系统现金 - 挂单冻结资金，和A股实盘完全一致"""
        return self.broker.get_cash() - self.frozen_cash

    def next(self):
        # 打印当前账户总仓位比例，超过100%就是加了杠杆
        current_date = self.datetime.date(0)
        position_value = self.position.size * self.data.close[0]
        total_equity = self.broker.get_value()
        position_ratio = position_value / total_equity * 100
        print(f"【每日监控】日期：{current_date} | 股价：{self.data.close[0]:.2f}元 | 可用资金：{self.available_cash:.2f}元 "
              f"| 仓位：{position_ratio:.1f}% | 冻结资金：{self.frozen_cash:.2f}元")

        # 如果没有持仓
        if not self.position:
            if self.crossover > 0:
                # 用真实可用资金计算仓位，预留15%缓冲覆盖手续费滑点
                commission_rate = 0.0003 + 0.001
                max_use_cash = self.available_cash * 0.85
                max_hand = int(max_use_cash * (1 - commission_rate) / (self.data.close[0] * 100))
                buy_size = max_hand * 100

                if buy_size > 0:
                    # 核心：下单瞬间立刻冻结资金，可用资金马上减少
                    need_cash = buy_size * self.data.close[0] * (1 + commission_rate)
                    self.frozen_cash = need_cash

                    print(
                        f'\n🔔 金叉买入：日期：{current_date} | 当前股价{self.data.close[0]:.2f}元，买入{max_hand}手共{buy_size}股，冻结资金{need_cash:.2f}元')
                    self.order = self.buy(size=buy_size, exectype=bt.Order.Close)
        else:
            if self.crossover < 0:
                print(f'\n🔔 死叉卖出：日期：{current_date} | 当前股价{self.data.close[0]:.2f}元，全部平仓')
                if self.order:
                    self.cancel(self.order)
                self.order = self.close(exectype=bt.Order.Close)
                self.frozen_cash = 0

                return