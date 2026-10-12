#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Providers —— 一切外部系统的适配边界
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
Providers（外部系统适配层）
==========================

所有**不受我们控制**的外部依赖都收敛在这里，业务层只见接口不见实现。

    market/      行情数据源：AkShare / BaoStock / 东财 / 雪球 / TuShare / mootdx / 本地
    broker/      券商网关：网关注册、HTTP 网关、模拟柜台、端点配置
    news/        新闻源（RSS / 自定义）

规矩::

    1. provider 内部允许脏：重试、限流、熔断、脏格式适配都在这里做。
    2. provider 对外只给干净模型（providers/market/model/*）。
       外部格式 → 标准模型的转换**只允许发生在 adapters/ 里**。
    3. provider 不得反向 import modules / engine；它不知道谁在用它。
"""
