#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目级共享异常。

放这里而不是散落在各业务模块，是因为"存储后端故障"这类错误需要被
跨层捕获：落库层（``app.data.repo``）抛出 → 同步层（``app.modules.*``）
冒泡 → 任务处理器（``app.runtime.workers``）捕获并决定"进死信 / 重放"。
如果异常定义在最底层，上层导入就会成环，所以提一层到 ``app.common``。
"""
from __future__ import annotations


class AlphaQuantError(Exception):
    """所有业务异常的基类。"""


class StorageError(AlphaQuantError):
    """存储后端故障：落库整批失败（磁盘满 / 库锁 / 权限 / 连接断开）。

    与"某一行数据不合法"严格区分：

    * 数据问题（某只票退市、接口返回残缺）是**常态**，应进 ``failed``
      列表、单只失败不影响整批，不该进错误队列；
    * 存储故障（整批 + 逐条都写不进）说明"写库这条路断了"，
      必须冒泡成任务失败 → 进死信 → 可人工重放，否则会被静默吞掉，
      第二天才发现"昨天根本没存进数据"。
    """


__all__ = ["AlphaQuantError", "StorageError"]
