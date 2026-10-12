#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : commission.py
# @Description : A 股交易成本模型（佣金 + 印花税 + 过户费）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
A 股交易成本模型
================

为什么不能直接用 ``cerebro.broker.setcommission(commission=rate)``
-------------------------------------------------------------------

backtrader 的 stocklike 佣金是**买卖对称**的单一费率，而 A 股的关键特征
恰恰是"不对称"：

==========  ========  ============  ==================================
项目        方向      费率          说明
==========  ========  ============  ==================================
佣金        双边      万三          券商收，单笔最低 5 元
印花税      **卖出**  万五          只在卖出时收，2023-08 起减半
过户费      双边      十万分之一    沪深均按成交金额收
==========  ========  ============  ==================================

**印花税（0.05%）比佣金（0.03%）还高**，是卖出侧的主要成本。用对称费率
要么高估买入成本、要么低估卖出成本；而短线策略的净值对卖出成本**极敏感**——
漏掉印花税会系统性高估策略收益，结果是回测好看、实盘亏钱。

因此这里用 :class:`backtrader.CommInfoBase` 自定义一个"按方向计费"的方案，
让回测口径跟真实券商一致。

用法::

    from app.research.commission import AStockCommissionInfo
    cerebro.broker.addcommissioninfo(AStockCommissionInfo())
"""
from __future__ import annotations

import backtrader as bt


class AStockCommissionInfo(bt.CommInfoBase):
    """按 A 股真实规则计费的佣金方案。

    :param commission: 佣金费率（默认万三）
    :param stamp_duty: 印花税率，**仅卖出收取**（默认万五）
    :param transfer_fee: 过户费率（默认十万分之一）
    :param min_commission: 单笔最低佣金（元），默认 5 元
    """

    params = (
        ("commission", 0.0003),      # 佣金率（双边）
        ("stamp_duty", 0.0005),      # 印花税率（仅卖出）
        ("transfer_fee", 0.00001),   # 过户费率（双边）
        ("min_commission", 5.0),     # 单笔最低佣金（元）
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
    )

    def _getcommission(self, size: float, price: float,
                       pseudoexec: bool) -> float:
        """backtrader 回调：返回这笔成交的总费用（元）。

        ``size > 0`` 为买入，``size < 0`` 为卖出。
        """
        if not size:
            return 0.0
        amount = abs(size) * float(price or 0.0)
        if amount <= 0:
            return 0.0

        comm = amount * float(self.p.commission or 0.0)
        floor = float(self.p.min_commission or 0.0)
        if floor > 0:
            comm = max(comm, floor)

        transfer = amount * float(self.p.transfer_fee or 0.0)
        # 印花税：只有卖出这条腿收，买入不收
        stamp = amount * float(self.p.stamp_duty or 0.0) if size < 0 else 0.0
        return comm + transfer + stamp


__all__ = ["AStockCommissionInfo"]
