#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 统一网络传输层：UA/Referer、代理→直连回退、域名健康、僵尸代理摘除。

依赖方向：所有走 HTTP 的适配器都应经由这里，便于统一埋点与故障降级。
"""

__all__: list[str] = []
