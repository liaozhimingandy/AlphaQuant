#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_worker_pool_config.py
# @Description : 执行池 worker 数的配置化（config/runtime.json）
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""这组用例钉住的是"改了配置却不生效 / 配错了静默失效"这两类问题。

**为什么 workers 数必须来自文件**
七个池的容量是一组互相约束的数字（``strategy=1`` 是正确性要求、
``market`` 受数据源限流约束），散在七个环境变量里既没法加注释、
也没法进版本库做 review。所以走 ``config/runtime.json``。

**为什么改完必须重启**
``ThreadPoolExecutor`` 的 ``max_workers`` 在建池那一刻就固定，
运行中改数字只会得到"面板显示 8、实际并发还是 3"。
比起不支持热更新，"改了但没真的改"难排查得多，所以这里刻意不提供热更新。

**为什么配置出错不能拖住启动**
启动不起来 = 一条数据都采不到；配错一个池名 = 那个池退回默认容量继续干。
后者明显便宜得多，所以坏配置只留告警，不抛异常。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any, Dict

from app.runtime.events.types import Pool
from app.runtime.workers.pools import (
    POOL_CONFIG_KEY,
    PoolManager,
    load_worker_pool_config,
)


def _write(tmp: str, payload: Any, name: str = "runtime.json") -> str:
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh, ensure_ascii=False)
    return path


class LoadWorkerPoolConfig(unittest.TestCase):
    """配置读取本身：缺失 / 损坏 / 非法值都要有确定行为。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file_falls_back_to_defaults(self):
        cfg = load_worker_pool_config(os.path.join(self.tmp, "nope.json"))
        self.assertIsNone(cfg["capacities"])
        self.assertIsNone(cfg["enabled"])
        self.assertEqual(cfg["warnings"], [])

    def test_broken_json_is_a_warning_not_a_crash(self):
        path = _write(self.tmp, "{not json,,,")
        cfg = load_worker_pool_config(path)
        self.assertIsNone(cfg["capacities"])
        self.assertEqual(len(cfg["warnings"]), 1)
        self.assertIn("解析失败", cfg["warnings"][0])

    def test_missing_section_uses_defaults(self):
        path = _write(self.tmp, {"something_else": 1})
        cfg = load_worker_pool_config(path)
        self.assertIsNone(cfg["capacities"])
        self.assertEqual(cfg["warnings"], [])

    def test_capacities_are_read(self):
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"capacities": {"market": 8}}})
        cfg = load_worker_pool_config(path)
        self.assertEqual(cfg["capacities"], {"market": 8})

    def test_underscore_keys_are_comments_not_config(self):
        raw: Dict[str, Any] = {
            POOL_CONFIG_KEY: {
                "_comment": ["hi"],
                "capacities": {"_note": 1, "market": 4},
            }
        }
        cfg = load_worker_pool_config(_write(self.tmp, raw))
        self.assertEqual(cfg["capacities"], {"market": 4})

    def test_unknown_pool_name_is_rejected_with_warning(self):
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"capacities": {"quantum": 4}}})
        cfg = load_worker_pool_config(path)
        self.assertIsNone(cfg["capacities"])
        self.assertEqual(len(cfg["warnings"]), 1)
        self.assertIn("未知执行池", cfg["warnings"][0])

    def test_non_integer_capacity_is_ignored(self):
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"capacities": {"market": "ten"}}})
        cfg = load_worker_pool_config(path)
        self.assertIsNone(cfg["capacities"])
        self.assertIn("不是整数", cfg["warnings"][0])

    def test_capacity_below_one_is_clamped_to_one(self):
        # 0 个 worker = 活永远没人干，比配错更危险，必须纠回来并告知
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"capacities": {"research": 0}}})
        cfg = load_worker_pool_config(path)
        self.assertEqual(cfg["capacities"], {"research": 1})
        self.assertIn("< 1", cfg["warnings"][0])

    def test_empty_enabled_list_is_treated_as_unset(self):
        # 配错成 [] 就把所有池 disable 掉：引擎起来一个任务都跑不了，
        # 而报错信息还跟池容量毫无关系 —— 这类坑按"没配"处理最安全。
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"enabled": []}})
        cfg = load_worker_pool_config(path)
        self.assertIsNone(cfg["enabled"])

    def test_enabled_filters_unknown_names(self):
        path = _write(self.tmp, {POOL_CONFIG_KEY: {
            "enabled": ["market", "quantum", "research"],
        }})
        cfg = load_worker_pool_config(path)
        self.assertEqual(cfg["enabled"], ["market", "research", "default"])
        self.assertIn("quantum", cfg["warnings"][0])

    def test_default_pool_always_exists(self):
        """``default`` 是兜底池，不能因为配置里没列它就消失。

        少建它的后果不是报错，而是路由判断不出归属的任务（``legacy.job``）
        没人领、悄悄进死信 —— 那种失败要到很后面才看得见。
        """
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"enabled": ["market"]}})
        cfg = load_worker_pool_config(path)
        self.assertIn(Pool.DEFAULT, cfg["enabled"])


class ScheduledPools(unittest.TestCase):
    """调度器是不是真的把配置用上了。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _scheduler(self, **kw):
        from app.runtime.scheduler import SchedulerComponent

        sch = SchedulerComponent(**kw)
        self.addCleanup(sch.pools.shutdown)
        return sch

    def test_default_scheduler_reads_repo_runtime_json(self):
        sch = self._scheduler()
        info = sch.pool_config_info()
        self.assertTrue(info["config_source"].endswith("runtime.json"))
        by_name = {p["pool"]: p["capacity"] for p in info["pools"]}
        self.assertEqual(by_name["market"], 3)
        self.assertEqual(by_name["strategy"], 1)
        self.assertEqual(info["config_warnings"], [])

    def test_file_capacity_overrides_builtin_default(self):
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"capacities": {"market": 6}}})
        sch = self._scheduler(pool_config_path=path)
        pools = {p["pool"]: p for p in sch.pool_config_info()["pools"]}
        self.assertEqual(pools["market"]["capacity"], 6)
        # 没配的池退回内置默认，而不是被兜底成同一个数
        self.assertEqual(pools["trading"]["capacity"],
                         Pool.DEFAULT_CAPACITY[Pool.TRADING])

    def test_enabled_list_controls_which_pools_exist(self):
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"enabled": ["market"]}})
        sch = self._scheduler(pool_config_path=path)
        # default 是兜底池，永远会被补回来
        self.assertEqual(sorted(sch.pools.names()), ["default", "market"])

    def test_explicit_capacity_wins_over_file(self):
        # 测试里"我要一个 8 并发的研究池"不该被用户的配置悄悄盖掉
        path = _write(self.tmp, {POOL_CONFIG_KEY: {"capacities": {"market": 6}}})
        sch = self._scheduler(capacities={"market": 2}, pool_config_path=path)
        pools = {p["pool"]: p for p in sch.pool_config_info()["pools"]}
        self.assertEqual(pools["market"]["capacity"], 2)
        self.assertEqual(sch.pool_config_info()["config_source"], "代码显式传参")

    def test_bad_config_does_not_block_startup(self):
        path = _write(self.tmp, "{oops")
        sch = self._scheduler(pool_config_path=path)
        self.assertTrue(sch.pools.names())
        self.assertEqual(len(sch.pool_config_info()["config_warnings"]), 1)

    def test_pool_manager_reports_config_source(self):
        pm = PoolManager(capacities={"market": 5}, enabled=["market"],
                         source="/x/runtime.json", warnings=["boom"])
        snap = pm.snapshot()
        self.assertEqual(snap["config_source"], "/x/runtime.json")
        self.assertEqual(snap["config_warnings"], ["boom"])
        self.addCleanup(pm.shutdown)


if __name__ == "__main__":
    unittest.main()
