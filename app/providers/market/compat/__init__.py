#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 兼容期保留的旧注册表实现（旧的 REGISTRY + 各 *_source 适配器）。

依赖方向：新写入的取数路径请一律走 datasource.provider/router；本包会随迁移完成逐步下线，请勿新增依赖。
"""

__all__: list[str] = []
