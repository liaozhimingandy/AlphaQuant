#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Trading —— 订单 → 网关 → 成交 → 账本
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""Trading：把信号变成真实持仓，是"实盘事故"最集中的地方。

    service.py      TradingService：多任务网关托管 + 订单/成交/权益落库
    live_broker.py  LiveBroker：单网关的订单生命周期（含超时回收 sweep）

已知边界：进程重启后未结订单与额度预留均为内存态，需靠 ``sweep_stale_orders``
与券商端回查对齐，详见 docs/EXTENDING.md 的待办清单。
"""
from app.modules.trading.live_broker import LiveBroker
from app.modules.trading.service import TradingService

__all__ = ["TradingService", "LiveBroker"]
