#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 数据源层：DataSource → Provider → Capability → Adapter → Router → DataService。

依赖方向：对外唯一出口是 service.DataService；上层禁止直接 import adapters/*。
"""

__all__: list[str] = []
