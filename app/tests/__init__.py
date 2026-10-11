#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试套件。

依赖方向：约定：凡触碰 熔断/健康分/权重/域名健康 这类全局状态的用例，必须在 setUp 里一并 reset，否则用例之间会互相污染。
"""

__all__: list[str] = []
