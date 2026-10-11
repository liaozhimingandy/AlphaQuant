#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 执行层：撮合、订单生命周期、券商接入点
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from app.execution.broker import IBaseBroker, SimulatedBroker
from app.execution.endpoints import (
    BrokerEndpoint,
    EndpointsConfig,
    describe_endpoints,
    load_endpoints,
    mask_secret,
)
# 导入即注册：网关靠装饰器进注册表，不 import 就永远搜不到它。
# 放在这里而不是等谁用到再导 —— "这家券商能用"不该取决于
# 某个模块碰巧被谁先 import 了。
from app.execution import http_gateway as _http_gateway  # noqa: F401
from app.execution.gateway import (  # noqa: F401
    IBrokerGateway,
    SimulatedGateway,
)

__all__ = [
    "BrokerEndpoint",
    "EndpointsConfig",
    "IBaseBroker",
    "IBrokerGateway",
    "SimulatedBroker",
    "SimulatedGateway",
    "describe_endpoints",
    "load_endpoints",
    "mask_secret",
]
