#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : conftest.py
# @Description : 测试全局夹具
#
#                这里只做一件事，但很重要：**关掉数据源权重的落盘**。
#
#                权重是跨重启记忆的（写在 data/source_weights.json），
#                目的是让"昨晚不靠谱的源今早别再排第一"。但测试里会刻意
#                造出大量失败（代理被拦、接口改版……），这些失败会把真实
#                权重降下去 —— 于是跑一次测试，工程里的源顺序就悄悄变了，
#                而且没人知道是测试改的。这种"测试污染生产状态"最难查。
#
#                所以测试期间统一把全局注册表的落盘关掉；
#                需要验证落盘本身的用例用临时目录另建注册表。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations


def pytest_configure(config) -> None:
    try:
        from app.data.sources import REGISTRY

        REGISTRY.persist_path = None
    except Exception:  # pragma: no cover - 注册表不可用时不拦着测试
        pass
