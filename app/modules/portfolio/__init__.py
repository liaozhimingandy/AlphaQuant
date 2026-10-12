#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Portfolio —— 组合聚合视图与跨任务额度预留
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""Portfolio（服务视角）：跨任务的资金聚合视图。

注意与 :mod:`app.account` 的分工::

    app.account      一个账户/任务的**账本**：Cash / Position / TradeRecord / Sizer
    app.modules.portfolio  跨任务的**预留与概览**：Total equity / reserved / available

``Reservation`` 带 TTL，超时自动回收；读路径（``reserved``/``available``/
``is_busy``）会先做一次清理，避免最后一笔过期后永久判定为忙。
"""
from app.modules.portfolio.service import PortfolioService, Reservation

__all__ = ["PortfolioService", "Reservation"]
