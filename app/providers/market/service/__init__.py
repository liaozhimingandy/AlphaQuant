#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 数据服务层（对外统一 API）
#
#                定位：介于"采集基础设施"与"业务"之间的**薄**适配层。
#                它不许有任何"先试 a、再试 b"的逻辑 —— 那是 Router 的职责。
#                它只负责：请求对象化 + 调路由 + 转成标准模型。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from app.providers.market.service.data_service import DataService, data_service

__all__ = ["DataService", "data_service"]
