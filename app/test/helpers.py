#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : helpers.py
# @Description : 测试辅助：合成行情数据 + 临时工作区
#               测试不依赖网络，也不往工程的 output/ 目录里写东西
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from app.core.config import settings


def make_ohlcv(
    days: int = 240,
    start: str = "2023-01-01",
    seed: int = 42,
    base_price: float = 10.0,
    amplitude: float = 2.0,
    cycles: float = 6.0,
) -> pd.DataFrame:
    """生成一段确定性的人造行情（正弦波动 + 微小噪声），索引为 date。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=days, name="date")

    t = np.linspace(0, cycles * np.pi, days)
    close = base_price + amplitude * np.sin(t) + rng.normal(0, 0.05, days)
    close = np.maximum(close, 0.5)  # 价格必须为正

    # 用当日波幅构造 open/high/low，保证 low <= open/close <= high
    span = np.abs(rng.normal(0, 0.15, days))
    open_ = close + rng.normal(0, 0.08, days)
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    low = np.maximum(low, 0.1)

    volume = rng.integers(1_000_000, 5_000_000, size=days).astype(float)

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": volume * close,
        },
        index=idx,
    )


def make_uptrend(days: int = 240, start: str = "2023-01-01") -> pd.DataFrame:
    """带回调的上涨行情：用于验证买入路径会被触发。

    注意：不能用"完美线性上涨"——那种数据里 5 日线始终在 20 日线上方，
    永远不会产生金叉事件，策略自然一笔交易都不会有。
    """
    idx = pd.bdate_range(start=start, periods=days, name="date")
    t = np.linspace(0, 8 * np.pi, days)
    close = np.linspace(10.0, 20.0, days) + 1.2 * np.sin(t)
    close = np.maximum(close, 0.5)
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.full(days, 1_000_000.0),
            "amount": np.full(days, 1_000_000.0) * close,
        },
        index=idx,
    )


# ===========================================================================
# 后台清理队列
# ===========================================================================
_trash_lock = threading.Lock()
_trash_pending: List[Path] = []
_trash_thread: Optional[threading.Thread] = None


def _drain_trash() -> None:
    while True:
        with _trash_lock:
            if not _trash_pending:
                return
            target = _trash_pending.pop(0)
        shutil.rmtree(target, ignore_errors=True)


def reap_async(path: Path) -> None:
    """把删除丢到后台线程，不阻塞当前测试。

    **只用于系统临时目录下的路径。** 临时目录之外（比如工程的 output/）在某些环境里
    删除会被"回收站代理"接管：每删一个路径都要起一个外部进程，还会走一套全局的
    批量删除守卫；从后台线程并发触发会把这个守卫的锁搞死锁，之后每次删除都要等
    60 秒超时——比省下的时间贵得多。所以非临时目录请用普通的同步删除。

    为什么临时目录值得异步：删一个目录在这种机器上要几百毫秒到 1.5 秒
    （实时防护逐个文件扫描），几十个用例累计起来清理时间远超测试本身。
    而临时目录是"删不掉也无所谓"的，晚删、甚至被进程退出打断都无副作用。
    """
    global _trash_thread
    tmp_root = Path(tempfile.gettempdir()).resolve()
    try:
        target = Path(path).resolve()
        target.relative_to(tmp_root)
    except (ValueError, OSError):
        # 不在临时目录 → 老老实实同步删，别去碰回收站/守卫那套东西
        shutil.rmtree(path, ignore_errors=True)
        return

    with _trash_lock:
        _trash_pending.append(target)
        if _trash_thread is not None and _trash_thread.is_alive():
            return
        _trash_thread = threading.Thread(target=_drain_trash, name="test-reaper", daemon=True)
        _trash_thread.start()


@atexit.register
def _flush_trash() -> None:
    """进程退出前给后台清理一个短暂窗口，尽量别把垃圾留在临时目录里。

    只等很短的时间：这是"尽力而为"，不值得为了干净而让 CI 多花几十秒。
    """
    t = _trash_thread
    if t is not None and t.is_alive():
        t.join(timeout=1.0)


# ===========================================================================
# 临时工作区
# ===========================================================================
class TempWorkspaceMixin:
    """给测试类提供「类级共享的临时工作区」。

    三件事，都是为了让测试既**不污染工程**又**不被文件系统拖慢**：

    1. 工作区建在系统临时目录，类结束只删一次（而不是每个用例一个
       ``TemporaryDirectory``）——Windows 上删目录很贵。
    2. ``isolated_settings`` 里列出的全局目录会被重定向到工作区，
       测试不会往工程的 ``output/`` 里写运行时任务、快照之类的东西。
    3. 清理走 ``reap_async``：测试线程不等删除完成，套件的墙钟时间不再被
       rmtree 支配。

    用法::

        class TestXxx(TempWorkspaceMixin, unittest.TestCase):
            isolated_settings = ("RUNTIME_DIR", "SNAPSHOT_DIR")

            def setUp(self):
                self.dir = self.workdir()      # 本用例专属子目录
    """

    #: 需要重定向到临时工作区的全局目录名（app.core.config.settings 的属性）
    isolated_settings: Sequence[str] = ()
    _ws_root: Optional[Path] = None
    _saved_settings: Dict[str, Path] = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ws_root = Path(tempfile.mkdtemp(prefix="alphaquant-test-"))
        cls._saved_settings = {}
        for attr in cls.isolated_settings:
            cls._saved_settings[attr] = getattr(settings, attr)
            setattr(settings, attr, cls._ws_root / f"_{attr.lower()}")

    @classmethod
    def tearDownClass(cls):
        try:
            for attr, original in cls._saved_settings.items():
                setattr(settings, attr, original)
            cls._saved_settings = {}
            if cls._ws_root is not None:
                reap_async(cls._ws_root)
        finally:
            cls._ws_root = None
            super().tearDownClass()

    def workdir(self, name: str = "") -> Path:
        """返回一个本用例专属的子目录（按用例名隔离，互不干扰）。

        路径必须在系统临时目录下，`reap_async` 才会走异步删除——
        这也意味着 ``workdir`` 的父目录不能被重定向到别处。
        """
        root = type(self)._ws_root
        if root is None:
            raise RuntimeError("TempWorkspaceMixin 未初始化工作区（setUpClass 未执行）")
        d = Path(root) / (name or self._testMethodName)
        d.mkdir(parents=True, exist_ok=True)
        return d


__all__ = [
    "make_ohlcv",
    "make_uptrend",
    "reap_async",
    "TempWorkspaceMixin",
]
