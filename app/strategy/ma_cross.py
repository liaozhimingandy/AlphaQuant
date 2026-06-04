import backtrader as bt

from app.utils.logger import logger

class MaCrossStrategy(bt.Strategy):
    params = dict(
        fast=5,               # 短均线
        slow=20,              # 长均线
        trend_slow=60,        # 趋势过滤均线
        stop_loss_pct=0.05,   # 止损比例
        commission_rate=0.0013,  # 预估交易成本：佣金 + 其他成本
        cash_buffer=0.85,     # 预留资金缓冲
        lot_size=100,         # A股一手100股
        printlog=True,

    )

    def __init__(self):
        self.ma_fast = bt.indicators.SMA(self.data.close, period=self.p.fast)
        self.ma_slow = bt.indicators.SMA(self.data.close, period=self.p.slow)
        self.ma_trend = bt.indicators.SMA(self.data.close, period=self.p.trend_slow)
        self.crossover = bt.indicators.CrossOver(self.ma_fast, self.ma_slow)

        self.order = None
        self.stop_order = None
        self.buy_price = None
        self.frozen_cash = 0.0

    @property
    def available_cash(self):
        return self.broker.getcash() - self.frozen_cash

    def log(self, txt):
        if not self.p.printlog:
            return
        dt = self.data.datetime.date(0)
        logger.info(f"{dt} | {txt}")

    def _trend_up(self):
        """上升趋势：长均线向上 + 价格站上趋势均线"""
        if len(self) < self.p.trend_slow:
            return False
        return self.ma_trend[-1] < self.ma_trend[0] < self.data.close[0]

    def _calc_buy_size(self, price: float) -> int:
        """按A股一手100股计算买入股数"""
        usable_cash = self.available_cash * self.p.cash_buffer
        one_lot_cost = price * self.p.lot_size * (1 + self.p.commission_rate)
        lots = int(usable_cash / one_lot_cost)
        return max(0, lots) * self.p.lot_size

    def _cancel_stop_order(self):
        """取消止损单订单"""
        if self.stop_order is not None:
            # 取消之前的止损单
            self.cancel(self.stop_order)
            self.stop_order = None

    def _close_position(self, reason: str):
        """平仓时先撤止损单，再市价/收盘平仓"""
        self._cancel_stop_order()
        self.log(f"🔔 {reason} | 全部平仓")
        self.order = self.sell(size=self.position.size, exectype=bt.Order.Close)

    def notify_order(self, order):
        """处理订单状态，统一清理挂单/冻结资金/止损单

        | 状态        | 含义    | 什么时候触发              |
        | --------- | ----- | ------------------- |
        | Created   | 已创建   | 你刚调用 `buy()/sell()` |
        | Submitted | 已提交   | 提交给 broker          |
        | Accepted  | 已接受   | broker 接受订单         |
        | Partial   | 部分成交  | 只成交了一部分             |
        | Completed | 完全成交  | 全部成交完成              |
        | Canceled  | 已取消   | 主动 cancel           |
        | Expired   | 已过期   | 订单过期                |
        | Margin    | 保证金不足 | 资金不够                |
        | Rejected  | 被拒绝   | broker 拒单           |

        """
        if order.status in [order.Submitted, order.Accepted]:
            return

        # 订单终态，清掉“当前主订单”标记
        self.order = None

        if order.status == order.Completed:
            executed_size = abs(order.executed.size)
            executed_price = order.executed.price

            if order.isbuy():
                self.buy_price = executed_price
                self.frozen_cash = 0.0

                self.log(
                    f"✅ 买入成交 | 价格={executed_price:.2f} | 数量={executed_size:.0f}股 | "
                    f"手续费={order.executed.comm:.2f}"
                )

                if self.p.stop_loss_pct > 0:
                    stop_price = self.buy_price * (1 - self.p.stop_loss_pct)
                    self.stop_order = self.sell(
                        size=self.position.size,
                        exectype=bt.Order.Stop,
                        price=stop_price
                    )
                    self.log(f"🛡️ 止损单已挂出 | 止损价={stop_price:.2f}")

            else:
                # 如果这是止损单成交，清掉止损引用；如果是手动卖出，也一起清掉
                if order == self.stop_order:
                    self.log(f"🛡️ 止损条件触发 | 止损价={self.data.close[0]:.2f}")
                    self.stop_order = None

                self.frozen_cash = 0.0
                self.buy_price = None

                self.log(
                    f"❌ 卖出成交 | 价格={executed_price:.2f} | 数量={executed_size:.0f}股 | "
                    f"手续费={order.executed.comm:.2f}"
                )

        elif order.status in [order.Canceled, order.Rejected, order.Margin]:
            # 订单失败，冻结资金退回
            self.frozen_cash = 0.0
            if order == self.stop_order:
                self.stop_order = None

            self.log(f"⚠️ 订单异常(取消之前的止损单) | 状态={order.getstatusname()} | 冻结资金已退回")

    def next(self):
        current_price = self.data.close[0]

        position_size = self.position.size
        position_value = position_size * current_price
        total_equity = self.broker.getvalue()
        position_ratio = (position_value / total_equity * 100) if total_equity else 0.0

        self.log(
            f"每日监控 | 价格={current_price:.2f} | 持仓={position_size:.0f}股 | "
            f"持仓市值={position_value:.2f} | 总资产={total_equity:.2f} | "
            f"可用资金={self.available_cash:.2f} | 仓位={position_ratio:.1f}% | "
            f"冻结资金={self.frozen_cash:.2f}"
        )

        # 有未完成主订单时，避免重复下单
        if self.order is not None:
            return

        # 先处理持仓中的退出逻辑
        if self.position:
            # 死叉退出
            if self.crossover < 0:
                self._close_position("死叉信号")
                return

            # 额外手工止损（防止止损单失效时兜底）
            if self.buy_price is not None and self.p.stop_loss_pct > 0:
                pnl_pct = (current_price - self.buy_price) / self.buy_price
                if pnl_pct <= -self.p.stop_loss_pct:
                    self._close_position(f"手工止损触发 | 浮亏={pnl_pct * 100:.1f}%")
            return

        # 没有持仓时，只在上升趋势里找机会
        # 当前K线数量还不够 slow 周期
        if len(self) < self.p.slow:
            return

        if not self._trend_up():
            return

        # 金叉买入
        if self.crossover > 0:
            buy_size = self._calc_buy_size(current_price)

            if buy_size <= 0:
                self.log("买入信号出现，但可用资金不足，跳过")
                return

            need_cash = buy_size * current_price * (1 + self.p.commission_rate)
            self.frozen_cash = need_cash

            self.log(
                f"🔔 买入信号 | 价格={current_price:.2f} | 买入={buy_size}股 | "
                f"预计冻结资金={need_cash:.2f}"
            )

            self.order = self.buy(size=buy_size, exectype=bt.Order.Close)