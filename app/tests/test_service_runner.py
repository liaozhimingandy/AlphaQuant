#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_service_runner.py
# @Description : 后台服务启动脚本的回归测试
#
#                这里钉的是两个「引擎装配成功、却在开机第一屏就崩」的坑：
#
#                1. 启动脚本打印采集概况时按旧结构取 ``col.spec.jobs``。
#                   采集三合一后服务上只有 ``kline_spec``，于是抛
#                   AttributeError —— 进程在打印日志这一步就退出，
#                   外部表现是「监控面板打不开」，实际引擎压根没起来。
#
#                2. ``IQuantEngine.async_sleep`` 用
#                   ``callLater(callable=d.callback)`` 排程，缺 result 参数。
#                   Twisted 回调时抛 TypeError，等它的协程永远等不到结果，
#                   停服务时表现为 timed call 报错后卡住不退出。
#
#                两条都是「非关键路径把关键路径带崩」，所以单独成文件钉住。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.engine import service_runner as sr
from app.engine.utils import async_sleep as util_async_sleep


class _FakeReactor:
    """记下 callLater 的入参，不真的排程（测试里跑真 reactor 太重）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def callLater(self, delay=None, callable=None, **kwargs):  # noqa: A002 - 对齐 Twisted 签名
        self.calls.append({"delay": delay, "callable": callable, "kwargs": kwargs})
        return SimpleNamespace(active=lambda: False, cancel=lambda: None)


class TestCollectionSummary(unittest.TestCase):
    """启动日志的采集概况：取不到不能崩。"""

    def _service(self, intervals):
        jobs = [SimpleNamespace(interval=i) for i in intervals]
        return SimpleNamespace(kline_spec=SimpleNamespace(jobs=jobs))

    def test_buckets_by_interval(self):
        buckets = sr.collection_job_buckets(self._service([300.0, 300.0, 1800.0]))
        self.assertEqual(buckets, {300.0: 2, 1800.0: 1})

    def test_missing_service_returns_empty(self):
        """服务没装配 —— 返回空表，而不是抛异常。"""
        self.assertEqual(sr.collection_job_buckets(None), {})
        self.assertEqual(sr.collection_job_buckets(object()), {})

    def test_legacy_service_without_kline_spec(self):
        """旧结构（只有裸 spec / 什么都没有）也必须兜住。"""
        self.assertEqual(sr.collection_job_buckets(SimpleNamespace()), {})
        legacy = SimpleNamespace(spec=SimpleNamespace(jobs=[SimpleNamespace(interval=60.0)]))
        self.assertEqual(sr.collection_job_buckets(legacy), {})

    def test_log_summary_never_raises(self):
        """日志这行曾经让整个进程退出：无服务 / 空任务 / 正常任务都不能抛。"""
        for col in (None, object(), SimpleNamespace(),
                    self._service([]), self._service([60.0, 60.0])):
            sr.log_collection_summary(col)  # 不抛即通过


class TestAsyncSleep(unittest.TestCase):
    """async_sleep 必须真的把 Deferred 回调掉。"""

    def _assert_callback_with_result(self, sleep_fn):
        # 接口方法已委托给 utils.async_sleep，所以排程发生在 utils 模块里
        from app.engine import utils as engine_utils

        patch_target = engine_utils
        fake = _FakeReactor()
        original = patch_target.reactor
        patch_target.reactor = fake
        try:
            from twisted.internet import defer

            d = sleep_fn(0.01)
            self.assertIsInstance(d, defer.Deferred)
            self.assertEqual(len(fake.calls), 1)
            call = fake.calls[0]
            # 关键：必须带 result，否则 Twisted 回调时缺参数抛 TypeError
            self.assertIn("result", call["kwargs"])
            # 模拟 Twisted 到点调用
            call["callable"](**call["kwargs"])
            self.assertTrue(d.called, "Deferred 未被回调，等待方会永久挂起")
            self.assertIsNone(d.result)
        finally:
            patch_target.reactor = original

    def test_interface_async_sleep(self):
        from app.engine.base import IQuantEngine

        self._assert_callback_with_result(IQuantEngine.async_sleep)

    def test_util_async_sleep(self):
        self._assert_callback_with_result(util_async_sleep)


if __name__ == "__main__":
    unittest.main()
