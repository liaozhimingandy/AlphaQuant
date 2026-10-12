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

# --- 临时目录重定向（必须放在最前面，早于任何测试用到 tempfile） ---
#
# 受管控的 Windows 环境里，系统 temp 目录（%TEMP%）下的文件操作
# （os.remove / os.rmdir / sqlite 关闭连接）会被无限期阻塞。
# 放到项目内的 .pytest_tmp 下就正常。这里统一重定向，
# 让"跑测试"这件事不要被宿主环境的文件锁绑架。
import os as _os
import tempfile as _tempfile

_PROJECT_TMP = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".pytest_tmp")
_os.makedirs(_PROJECT_TMP, exist_ok=True)
_tempfile.tempdir = _PROJECT_TMP


def pytest_configure(config) -> None:
    # --- 关掉任务落盘 ---
    #
    # 任务流水账默认写 ``output/runtime/tasks.db``。测试里每个用例都在
    # 造任务、造失败，如果它们真的落盘，跑一次测试就会在真实输出目录里
    # 留下一堆"没干完的活"，下次启动引擎会把它们全捞回来重跑 ——
    # 也就是"跑测试"污染了"生产状态"，而且没人会想到去查任务表。
    #
    # 需要验证落盘本身的用例会显式传 journal 实例，不受这里影响。
    _os.environ["AQ_TASK_PERSIST"] = "0"

    try:
        from app.providers.market.compat.registry import REGISTRY

        REGISTRY.persist_path = None
    except Exception:  # pragma: no cover - 注册表不可用时不拦着测试
        pass

    _tolerate_locked_tempdirs()


def _tolerate_locked_tempdirs() -> None:
    """绕开"临时目录删不掉导致测试假失败"的环境问题。

    某些受管控的 Windows 环境里，``os.rmdir`` 对刚创建的目录会**无限期阻塞**
    （不是抛异常，而是不返回）。后果很误导人：用例本身早已跑完并通过，
    却卡在 ``TemporaryDirectory.__exit__`` 的清理上，最后被 pytest-timeout
    判成超时失败 —— 看起来像代码缺陷，实际是环境的文件锁。

    注意 ``ignore_errors=True`` 解决不了它：那个参数只吞**异常**，
    对"调用根本不返回"无效。所以这里直接跳过清理，把目录留给系统回收。

    代价是临时目录会被留下（在系统 temp 下，可定期清理）。
    如果你的环境删除正常，删掉本函数即可恢复原行为。
    """
    import os
    import tempfile

    if os.environ.get("ALPHAQUANT_REAL_TEMPDIR_CLEANUP"):
        return

    def _no_cleanup(self, *args, **kwargs) -> None:  # noqa: ANN001
        self._finalizer.detach()

    if getattr(tempfile.TemporaryDirectory, "_alphaquant_nocleanup", False):
        return
    _no_cleanup._alphaquant_nocleanup = True
    tempfile.TemporaryDirectory.cleanup = _no_cleanup

    _tolerate_locked_sqlite_dispose()


def _tolerate_locked_sqlite_dispose() -> None:
    """``Engine.dispose()`` 默认 ``close=True``，会真的去关 SQLite 连接。

    在这台机器上**关 SQLite 连接会无限期阻塞** —— 和测试跑在哪个临时目录无关
    （系统 temp 和项目内 .pytest_tmp 都中招），阻塞点在 sqlite3 的句柄释放上。
    症状是 tearDown 里 ``dispose()`` 不返回，整个测试文件被判超时。

    实测 ``dispose(close=False)`` 立即返回：它换掉连接池但不去关底层连接，
    句柄留给进程退出时由 OS 回收。测试场景下这没有副作用。

    与上面的临时目录清理共用同一个环境变量开关，避免出现"堵一半"的死锁。
    """
    try:
        from sqlalchemy.engine import Engine
    except ImportError:  # pragma: no cover - 没装 SQLAlchemy 就无从谈起
        return

    if getattr(Engine, "_alphaquant_noclose", False):
        return

    original = Engine.dispose

    def _dispose_without_closing(self, close: bool = True) -> None:
        return original(self, False)

    _dispose_without_closing._alphaquant_noclose = True
    Engine.dispose = _dispose_without_closing
