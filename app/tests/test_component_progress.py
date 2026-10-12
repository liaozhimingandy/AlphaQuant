#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_component_progress.py
# @Description : 组件任务进度（面板上的"已完成 / 全部"）
#
#                这组测试守的是：**进度必须说真话**。
#                进度条说谎的三种典型方式，这里逐一堵住：
#                  1. 计划数和实际执行数不一致 → 到不了 100% 或卡在 99%；
#                  2. 中途抛异常没收尾 → 永远显示"进行中"；
#                  3. 失败被算成成功 → 界面上一片绿，实际什么都没采到。
#
#                另外覆盖本地兜底人气榜源：外网全挂时它必须顶上，
#                但**永远排在最后**（抢跑 = 有网也一直用旧数据）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import unittest
from datetime import date, timedelta
from contextlib import contextmanager
from unittest import mock

from app.engine.component.base import IBaseComponent, Progress


class _Dummy(IBaseComponent):
    name = "dummy"

    def on_initialize(self) -> None:
        pass

    def on_start(self):
        pass

    def on_stop(self, graceful: bool = True):
        pass


class TestProgressModel(unittest.TestCase):
    """Progress 本身的语义。"""

    def test_initial_state_has_no_task(self):
        p = Progress()
        self.assertEqual(p.total, 0)
        self.assertFalse(p.running)
        # total=0 且没在跑 = "这个组件没有批次任务"，进度算满，面板据此不画进度条
        self.assertEqual(p.percent, 100.0)

    def test_percent_and_elapsed(self):
        p = Progress(total=4, running=True)
        p.done, p.failed = 2, 1
        self.assertEqual(p.percent, 75.0)
        self.assertEqual(p.finished, 3)

    def test_percent_capped_at_100(self):
        """重试导致 done+failed 超过 total 时不能画出 120% 的进度条。"""
        p = Progress(total=2, running=True)
        p.done, p.failed = 3, 1
        self.assertEqual(p.percent, 100.0)

    def test_zero_total_running_is_zero(self):
        p = Progress(total=0, running=True)
        self.assertEqual(p.percent, 0.0)


class TestComponentProgressApi(unittest.TestCase):
    """start / advance / finish 三件套。"""

    def setUp(self):
        self.c = _Dummy()

    def test_round_increments_and_resets(self):
        self.c.start_progress(3, "第一轮")
        self.c.advance_progress(2)
        self.assertEqual(self.c.progress()["round"], 1)
        self.c.start_progress(5, "第二轮")
        self.assertEqual(self.c.progress()["round"], 2)
        self.assertEqual(self.c.progress()["done"], 0)
        self.assertEqual(self.c.progress()["total"], 5)

    def test_failed_counted_separately(self):
        self.c.start_progress(3)
        self.c.advance_progress(1)
        self.c.advance_progress(1, failed=True)
        p = self.c.progress()
        self.assertEqual((p["done"], p["failed"], p["finished"]), (1, 1, 2))

    def test_finish_stops_running(self):
        self.c.start_progress(2)
        self.c.finish_progress()
        self.assertFalse(self.c.progress()["running"])

    def test_negative_advance_ignored(self):
        """防御：传 0 / 负数不该让进度倒退。"""
        self.c.start_progress(5)
        self.c.advance_progress(1)
        self.c.advance_progress(-3)
        self.c.advance_progress(0)
        self.assertEqual(self.c.progress()["done"], 1)

    def test_progress_is_json_friendly(self):
        import json

        self.c.start_progress(2, "x")
        json.dumps(self.c.progress())      # 不该抛（面板要序列化它）


class TestExtDataProgress(unittest.TestCase):
    """日频截面采集（原 ExtDataComponent）：计划项数必须和实际执行的项数一致。

    它已并入 ``CollectionService`` 的 ``_cross`` 单元：参数带 ``cross_`` 前缀、
    方法带 ``_cross`` 中缀，进度契约（"计划 == 执行"）不变。
    """

    def _comp(self, **kw):
        from app.engine.component import CollectionService

        # K 线单元指向一个不存在的配置：隔离工程里的 config/collector.json
        c = CollectionService(
            cross_symbols=["000001", "600000"],
            cross_enabled=True,
            kline_config_path="/nonexistent/collector.json",
            **kw,
        )
        return c

    def test_plan_matches_execution(self):
        """盘后跑一轮，进度必须正好到 100% 且 running=False。"""
        comp = self._comp(cross_categories=["lhb", "margin"],
                          cross_with_fundamental=True)
        comp._cross_final_date = None        # 让它把基本面也算进计划
        plan = comp._cross_plan_items("post")
        self.assertEqual(
            sorted(plan),
            sorted(["fund_flow", "chip", "fundamental", "lhb", "margin",
                    "macro", "breadth"]),
        )

        def _ok(*a, **k):
            return {"ok": 2, "failed": []}

        with mock.patch("app.alpha.pool.sync_fund_flow", side_effect=_ok), \
             mock.patch("app.alpha.pool.sync_chip", side_effect=_ok), \
             mock.patch("app.alpha.pool.sync_fundamentals", side_effect=_ok), \
             mock.patch("app.alpha.pool.sync_category", side_effect=_ok), \
             mock.patch("app.feature.features.get_provider",
                        side_effect=Exception("no cache")):
            comp._run_cross_once("post")

        p = comp.progress()
        self.assertEqual(p["total"], len(plan))
        self.assertEqual(p["done"], len(plan))
        self.assertEqual(p["failed"], 0)
        self.assertEqual(p["percent"], 100.0)
        self.assertFalse(p["running"])

    def test_failed_item_counted_as_failed_not_done(self):
        """某一项采挂了 → 算失败，不能算完成（否则界面上一片绿）。"""

        def _bad(*a, **k):
            return {"ok": 0, "failed": [("000001", "超时")]}

        def _ok(*a, **k):
            return {"ok": 2, "failed": []}

        comp = self._comp(cross_categories=[], cross_with_fundamental=False)
        with mock.patch("app.alpha.pool.sync_fund_flow", side_effect=_bad), \
             mock.patch("app.alpha.pool.sync_chip", side_effect=_ok), \
             mock.patch("app.alpha.pool.sync_category", side_effect=_ok), \
             mock.patch("app.feature.features.get_provider",
                        side_effect=Exception("no cache")):
            comp._run_cross_once("post")
        p = comp.progress()
        self.assertEqual(p["failed"], 1)
        self.assertGreaterEqual(p["done"], 1)

    def test_exception_still_finishes_progress(self):
        """中途炸了也要收尾 —— 否则面板永远显示"进行中"。"""
        comp = self._comp(cross_categories=[], cross_with_fundamental=False)
        with mock.patch("app.alpha.pool.sync_fund_flow",
                        side_effect=RuntimeError("炸了")):
            with self.assertRaises(RuntimeError):
                comp._run_cross_once("post")
        self.assertFalse(comp.progress()["running"])

    def test_daily_batch_has_three_items(self):
        comp = self._comp()
        with mock.patch("app.alpha.pool.sync_limitup",
                        return_value={"ok": 10}), \
             mock.patch("app.alpha.pool.sync_boards", return_value={"ok": 5}), \
             mock.patch("app.alpha.pool.sync_concept_members",
                        return_value={"ok": 3}):
            comp._run_cross_daily()
        p = comp.progress()
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["done"], 3)
        self.assertFalse(p["running"])


class TestCollectorProgress(unittest.TestCase):
    """K 线采集：被"非交易时段"跳过的任务不该算进总数。

    原 ``DataCollectorComponent`` 现在是 ``CollectionService`` 的 ``_kline`` 单元。
    """

    @staticmethod
    def _comp():
        """只装配 K 线单元的采集服务（配置指向不存在的文件，隔离工程配置）。"""
        from app.engine.component import CollectionService

        return CollectionService(kline_config_path="/nonexistent/collector.json")

    def test_skipped_jobs_not_counted(self):
        comp = self._comp()
        job_ok = mock.MagicMock()
        job_ok.interval = 60.0
        job_ok.enabled = True
        job_ok.keep_days = 0
        job_ok.key = "ok-1d"
        job_skip = mock.MagicMock()
        job_skip.interval = 60.0
        job_skip.enabled = True
        job_skip.keep_days = 0
        job_skip.key = "skip-1d"

        comp.kline_spec.should_run = lambda j: j is job_ok
        with mock.patch.object(comp, "_collect_one_kline", return_value=5), \
             mock.patch.object(comp, "_record_kline_success"), \
             mock.patch.object(comp, "_record_kline_failure"):
            comp._run_kline_bucket([job_ok, job_skip])

        p = comp.progress()
        # 只跑 1 个：被跳过的那个不能出现在"全部"里，
        # 否则进度条显示 1/2 然后永远不动
        self.assertEqual(p["total"], 1)
        self.assertEqual(p["done"], 1)
        self.assertFalse(p["running"])

    def test_failed_job_counted_as_failed(self):
        comp = self._comp()
        job = mock.MagicMock()
        job.interval = 60.0
        job.enabled = True
        job.keep_days = 0
        job.key = "bad-1d"
        comp.kline_spec.should_run = lambda j: True
        with mock.patch.object(comp, "_collect_one_kline",
                               side_effect=RuntimeError("网络炸了")), \
             mock.patch.object(comp, "_record_kline_failure"), \
             mock.patch.object(comp, "_record_kline_success"):
            comp._run_kline_bucket([job])
        self.assertEqual(comp.progress()["failed"], 1)
        self.assertEqual(comp.progress()["done"], 0)


class TestDataGuardProgress(unittest.TestCase):
    """数据校验（原 DataGuardComponent）：逐只回调驱动进度，一轮结束要收尾。

    现在是 ``CollectionService`` 的 ``_guard`` 单元。
    """

    @staticmethod
    def _comp():
        from app.engine.component import CollectionService

        return CollectionService(
            guard_enabled=True, kline_config_path="/nonexistent/collector.json"
        )

    def test_on_step_starts_and_finishes(self):
        comp = self._comp()
        comp._on_guard_step(1, 3, False)
        self.assertTrue(comp.progress()["running"])
        self.assertEqual(comp.progress()["total"], 3)
        comp._on_guard_step(2, 3, True)        # 这只异常
        comp._on_guard_step(3, 3, False)
        p = comp.progress()
        self.assertEqual((p["done"], p["failed"], p["finished"]), (2, 1, 3))
        self.assertFalse(p["running"])

    def test_second_round_restarts(self):
        comp = self._comp()
        comp._on_guard_step(1, 2, False)
        comp._on_guard_step(2, 2, False)
        self.assertEqual(comp.progress()["round"], 1)
        comp._on_guard_step(1, 4, False)       # 新一轮：总数变了
        p = comp.progress()
        self.assertEqual(p["round"], 2)
        self.assertEqual(p["total"], 4)
        self.assertEqual(p["finished"], 1)

    def test_scan_reports_progress_callback(self):
        """扫描器必须真的回调，而不是只在组件里自己数。"""
        from app.engine.integrity import DataIntegrityChecker

        checker = DataIntegrityChecker(lookback_days=3, auto_repair=False,
                                       max_symbols=5)
        seen = []

        def _cb(done, total, bad):
            seen.append((done, total, bad))

        with mock.patch.object(checker, "check_symbol",
                               return_value=mock.MagicMock(
                                   missing=[], anomalies=[], error="",
                                   repaired=0, ok=True, symbol="x")):
            checker.scan(["000001", "000002"], on_progress=_cb)
        self.assertEqual([s[0] for s in seen], [1, 2])
        self.assertTrue(all(s[1] == 2 for s in seen))


@contextmanager
def _temp_db():
    """临时 in-memory 库，并把**所有**会用到 SessionLocal 的地方都指过去。

    两处必须一起换、一起还原：``app.storage.engine.SessionLocal``（日线/模型）
    和 ``kline_repo.SessionLocal``（榜单表）。
    只换一处会让本地兜底源读到半套数据；忘了还原更糟 ——
    后续测试会连到一个已经 dispose 的内存库上，报
    "no such table: xxx"，而且报错位置和真正的元凶隔了整个测试文件。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.storage.engine as dbmod
    from app.storage.engine import apply_sqlite_pragmas
    from app.storage.schema import Base
    from app.storage.repo import kline_repo as R

    eng = apply_sqlite_pragmas(create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(bind=eng)
    S = sessionmaker(bind=eng, autocommit=False, autoflush=False)
    old_db, old_repo = dbmod.SessionLocal, R.SessionLocal
    dbmod.SessionLocal = S
    R.SessionLocal = S
    try:
        yield S
    finally:
        dbmod.SessionLocal = old_db
        R.SessionLocal = old_repo
        eng.dispose()


class TestSnapshotExposesProgress(unittest.TestCase):
    """引擎快照必须把进度带出去 —— 否则面板拿得到接口也渲染不出来。"""

    def test_engine_snapshot_includes_progress(self):
        from app.engine.base import BaseQuantEngine

        eng = BaseQuantEngine({"RUN_MODE": "LIVE"})
        comp = _Dummy()
        comp.start_progress(4, "体检")
        comp.advance_progress(1)
        eng._components = {"dummy": comp}
        eng._component_order = ["dummy"]
        snap = eng.snapshot()
        p = snap["components"]["dummy"].get("progress")
        self.assertIsNotNone(p, "snapshot 里必须带 progress")
        self.assertEqual(p["total"], 4)
        self.assertEqual(p["done"], 1)
        self.assertTrue(p["running"])

    def test_control_component_states_includes_progress(self):
        """面板 /api/overview 走的是 control.component_states()，同样要带。"""
        from app.engine.control import ControlCenter

        comp = _Dummy()
        comp.start_progress(2, "x")
        ctrl = ControlCenter.__new__(ControlCenter)
        ctrl.engine = mock.MagicMock()
        ctrl.engine.component_names.return_value = ["dummy"]
        ctrl.engine.get_component.return_value = comp
        rows = ctrl.component_states()
        self.assertEqual(rows[0]["progress"]["total"], 2)


class TestLocalHotFallback(unittest.TestCase):
    """本地兜底人气榜：外网全挂时的最后一道防线。"""

    def test_registered_as_fallback_and_last(self):
        import app.datasource.service.facade  # noqa: F401  触发注册
        from app.datasource.compat.registry import REGISTRY

        names = [s.name for s in REGISTRY.sources_for("hot_rank")]
        self.assertIn("local_hot", names)
        # 兜底源恒排最后：它几乎不会失败，参与权重竞争迟早会抢到队首
        self.assertEqual(names[-1], "local_hot")
        spec = [s for s in REGISTRY.sources_for("hot_rank")
                if s.name == "local_hot"][0]
        self.assertTrue(spec.fallback)

    def test_fallback_never_overtakes_by_weight(self):
        """本地源权重涨到很高也不能超过普通源。"""
        from app.datasource.compat.registry import REGISTRY, SourceRegistry

        reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0)
        reg.register("net", supports=["k"], fetch=lambda: None)
        reg.register("local", supports=["k"], fetch=lambda: None,
                     fallback=True)
        reg.set_weight("local", 99)      # 极端情况
        self.assertEqual([s.name for s in reg.sources_for("k")],
                         ["net", "local"])

    def test_synthesizes_from_daily_when_no_hot_table(self):
        """库里没人气榜时，用日线合成一份（成交额口径）。"""
        from app.datasource.compat import local_source
        from app.storage.schema import StockDaily

        day = date(2026, 10, 9)
        with _temp_db() as S:
            with S() as s:
                # 000002 当天停牌（无成交也无成交量）→ 不该进榜
                for sym, amt, vol in (("000001", 1e9, 1e5),
                                      ("600000", 5e8, 5e4),
                                      ("000002", 0.0, 0.0)):
                    s.add(StockDaily(symbol=sym, date=day, close=10.0,
                                     amount=amt, volume=vol))
                s.commit()
            df = local_source._synthesize_from_daily(day)

        # 停牌票（成交额与成交量都为 0）不该进榜
        self.assertEqual(list(df["symbol"]), ["000001", "600000"])
        self.assertEqual(list(df["rank"]), [1, 2])
        self.assertIn("pct_change", df.columns)

    def test_unavailable_when_db_empty(self):
        """连日线都没有 → 报 DataUnavailable（源没坏，是没数据，不该降权）。"""
        from app.datasource.compat import local_source
        from app.datasource.compat.registry import DataUnavailable

        with _temp_db():
            with self.assertRaises(DataUnavailable):
                local_source._hot_rank_local()


class TestNetworkHint(unittest.TestCase):
    """全源失败时的排查提示。"""

    def test_hint_mentions_proxy_vars(self):
        from app.datasource.compat.registry import network_hint

        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:59258"}):
            hint = network_hint()
        self.assertIn("127.0.0.1:59258", hint)
        self.assertIn("--probe", hint)

    def test_hint_without_proxy(self):
        from app.datasource.compat.registry import network_hint

        with mock.patch.dict("os.environ", {}, clear=True):
            hint = network_hint()
        self.assertIn("未设置代理", hint)

    def test_exhausted_error_carries_hint(self):
        """'所有数据源均失败'必须带上排查方向，否则无法行动。"""
        from app.datasource.compat.registry import REGISTRY, SourceError, SourceRegistry

        reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0)

        def _boom():
            raise ConnectionError("Connection aborted.")

        reg.register("a", supports=["k"], fetch=_boom)
        with self.assertRaises(SourceError) as ctx:
            reg.fetch("k")
        self.assertIn("所有数据源均失败", str(ctx.exception))
        self.assertIn("排查", str(ctx.exception))
        self.assertIsNotNone(REGISTRY)   # 全局注册表仍然存在

    def test_probe_reports_each_source(self):
        from app.datasource.compat.registry import DataUnavailable, SourceRegistry

        reg = SourceRegistry(retries=0)

        def _ok():
            return None

        def _boom():
            raise RuntimeError("炸")

        def _nodata():
            raise DataUnavailable("停牌")

        reg.register("good", supports=["k"], fetch=_ok)
        reg.register("bad", supports=["k"], fetch=_boom)
        reg.register("empty", supports=["k"], fetch=_nodata)
        rows = reg.probe("k")
        self.assertEqual([r["name"] for r in rows], ["good", "bad", "empty"])
        self.assertTrue(rows[0]["ok"])
        self.assertFalse(rows[1]["ok"])
        # 能连上但没数据 = 源是好的，不能报红
        self.assertTrue(rows[2]["ok"])
        self.assertIn("DataUnavailable", rows[2]["error"])


class TestRunModeSingleSource(unittest.TestCase):
    """``RunMode`` 只能有一份定义。

    踩过的坑：``engine/settings.py`` 自己定义了一份同名枚举，
    于是 ``builder``（从 domain 导入）造出来的 ``RunMode.BACKTEST``
    和 ``base``（从 settings 导入）用来比较的是**两个不同的类**，
    ``==`` 恒为 False —— 直接后果是回测的"空闲自动停止看门狗"
    永远装不上，回测跑完数据后 reactor 永久阻塞，命令不返回。
    """

    def test_both_import_paths_are_the_same_class(self):
        from app.domain.types import RunMode as DomainMode
        from app.engine.settings import RunMode as EngineMode

        self.assertIs(DomainMode, EngineMode)

    def test_cross_module_comparison_holds(self):
        """跨模块比较必须成立 —— 这正是当初失效的那条判断。"""
        from app.domain.types import RunMode as DomainMode
        from app.engine.settings import RunMode as EngineMode

        built = DomainMode("BACKTEST")          # builder 造出来的值
        self.assertEqual(built, EngineMode.BACKTEST)   # base 里的比较
        self.assertTrue(built == EngineMode.BACKTEST)

    def test_values_stable(self):
        from app.engine.settings import RunMode

        self.assertEqual(
            {m.value for m in RunMode}, {"BACKTEST", "SIMULATE", "LIVE"})


if __name__ == "__main__":
    unittest.main()
