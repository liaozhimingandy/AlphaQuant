#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_event_task_runtime.py
# @Description : 事件 → 任务 → 执行池 全链路测试
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
这套测试守的是**架构不变式**，不是某个函数的返回值：

  * 事件与任务不能互相污染（旧事件名必须单射映射到新名）；
  * 任务系统自己发出的事件不能再生成任务（回声防护）；
  * 执行池之间必须互不影响（回测跑满不能挤死实盘）；
  * 下单类任务永远不允许自动重试；
  * 没有处理器的任务必须进死信，不能假装成功。

任何一条破了，系统都能"看起来在跑"而实际什么都没做 ——
那正是这类架构最危险的失败模式，所以必须钉死在测试里。
"""
from __future__ import annotations

import time
import unittest
from typing import Any, List

from app.engine.context import EngineContext, RunMode
from app.runtime.events import EventBus, StandardEvents
from app.runtime.events.schema import EngineEvent
from app.runtime.events.types import (
    CANONICAL_TO_LEGACY,
    LEGACY_EVENT_ALIASES,
    EventType,
    Pool,
    Queue,
    TaskType,
)
from app.runtime.scheduler import SchedulerComponent
from app.runtime.scheduling import (
    CapabilityRouter,
    Rule,
    RuleRegistry,
    build_default_rules,
)
from app.runtime.tasking.models import EngineTask, Priority, TaskState
from app.runtime.tasking.store import TaskStore
from app.runtime.workers import PoolManager
from app.runtime.workers.handlers import _publish


# =============================================================================
# 工具
# =============================================================================
def _wait(predicate, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _scheduler(**kw: Any) -> SchedulerComponent:
    """一个不挂引擎、不连 reactor 的调度器（规则与执行池都齐全）。"""
    sch = SchedulerComponent(**kw)
    sch.initialize(EngineContext(), EventBus(), {})
    sch._running = True
    sch._subscribe_rules()
    return sch


def _pump(sch: SchedulerComponent, seconds: float = 3.0, predicate=None) -> bool:
    """手动驱动分发循环（测试里没有 reactor 在跑）。"""
    end = time.time() + seconds
    while time.time() < end:
        sch._dispatch_once()
        if predicate is not None and predicate():
            return True
        time.sleep(0.02)
    return bool(predicate()) if predicate is not None else False


# =============================================================================
# 1. 事件契约
# =============================================================================
class TestEngineEvent(unittest.TestCase):
    def test_create_canonicalizes_legacy_name(self):
        evt = EngineEvent.create("bar_received", {"symbol": "000001"})
        self.assertEqual(evt.event_type, EventType.MARKET_BAR_RECEIVED)
        self.assertTrue(evt.event_id)

    def test_child_inherits_correlation_and_points_causation(self):
        parent = EngineEvent.create(
            EventType.MARKET_KLINE_UPDATED, correlation_id="chain-1"
        )
        child = parent.child(EventType.FACTOR_SNAPSHOT_UPDATED, {"symbols": ["1"]})
        self.assertEqual(child.causation_id, parent.event_id)
        self.assertEqual(child.correlation_id, "chain-1")

    def test_roundtrip_dict(self):
        evt = EngineEvent.create(
            EventType.ENGINE_READY, {"a": 1}, source="engine", correlation_id="c1"
        )
        back = EngineEvent.from_dict(evt.to_dict())
        self.assertEqual(back.event_id, evt.event_id)
        self.assertEqual(back.event_type, evt.event_type)
        self.assertEqual(back.correlation_id, "c1")
        self.assertEqual(back.data["a"], 1)

    def test_legacy_alias_map_is_injective(self):
        """回归：曾经 control_command 与 task_added 都映射到 task.created，
        导致控制面板的审计流收到幽灵指令。映射必须是单射。"""
        values = list(LEGACY_EVENT_ALIASES.values())
        self.assertEqual(len(values), len(set(values)))
        for canonical, legacy in CANONICAL_TO_LEGACY.items():
            self.assertEqual(len(legacy), 1, f"{canonical} 被多个旧名共享")


# =============================================================================
# 2. 事件总线
# =============================================================================
class TestEventBus(unittest.TestCase):
    def test_legacy_publish_reaches_canonical_subscriber(self):
        bus = EventBus()
        got: List[str] = []
        bus.subscribe(StandardEvents.BAR_RECEIVED, lambda **kw: got.append("legacy"))
        bus.subscribe(
            EventType.MARKET_BAR_RECEIVED, lambda **kw: got.append("canonical")
        )
        bus.publish(StandardEvents.BAR_RECEIVED, symbol="000001")
        self.assertEqual(sorted(got), ["canonical", "legacy"])

    def test_legacy_publish_does_not_crosstalk(self):
        """回归：一个旧事件不能被投递给**另一个**旧事件的订阅者。"""
        bus = EventBus()
        seen: dict = {}
        bus.subscribe(
            StandardEvents.CONTROL_COMMAND,
            lambda **kw: seen.setdefault("control", []).append(kw.get("action")),
        )
        bus.subscribe(
            StandardEvents.TASK_ADDED,
            lambda **kw: seen.setdefault("added", []).append(kw.get("task_id")),
        )
        bus.publish(StandardEvents.TASK_ADDED, task_id="t-1")
        self.assertEqual(seen.get("added"), ["t-1"])
        self.assertIsNone(seen.get("control"))

    def test_wildcard_subscription(self):
        bus = EventBus()
        got: List[str] = []
        bus.subscribe("market.*", lambda **kw: got.append(kw.get("event").event_type))
        bus.publish(EventType.MARKET_KLINE_UPDATED, symbols=["1"])
        bus.publish(EventType.MARKET_KLINE_GAP_DETECTED, symbols=["1"])
        bus.publish(EventType.ENGINE_READY)
        self.assertEqual(got, [EventType.MARKET_KLINE_UPDATED,
                               EventType.MARKET_KLINE_GAP_DETECTED])

    def test_global_wildcard(self):
        bus = EventBus()
        count = []
        bus.subscribe("*", lambda **kw: count.append(1))
        bus.publish(EventType.ENGINE_READY)
        bus.publish(EventType.TASK_CREATED)
        self.assertEqual(len(count), 2)

    def test_duplicate_event_id_is_dropped(self):
        bus = EventBus()
        hits = []
        bus.subscribe(EventType.ENGINE_READY, lambda **kw: hits.append(1))
        evt = EngineEvent.create(EventType.ENGINE_READY)
        self.assertEqual(bus.publish_event(evt), 1)
        self.assertEqual(bus.publish_event(evt), 0)
        self.assertEqual(len(hits), 1)
        self.assertEqual(bus.stats["duplicate_dropped"], 1)

    def test_non_mapping_context_keeps_its_key_name(self):
        """``publish(..., context=obj)`` 必须让订阅者拿到 ``context=obj``。

        把 EngineContext 塞进 ``value=`` 会让老订阅者静默收到 None，
        这类"参数名漂移"的错极难排查。
        """
        bus = EventBus()
        marker = object()
        got: List[Any] = []
        bus.subscribe(EventType.ENGINE_STARTED, lambda **kw: got.append(kw))
        bus.publish(EventType.ENGINE_STARTED, context=marker)
        self.assertEqual(len(got), 1)
        self.assertIs(got[0].get("context"), marker)

    def test_payload_event_key_wins_over_envelope(self):
        """载荷里的 ``event`` 是业务对象时，不能被 EngineEvent 顶掉。"""
        bus = EventBus()
        marker = object()
        got: List[Any] = []
        bus.subscribe(
            EventType.NEWS_ARTICLE_COLLECTED,
            lambda **kw: got.append(kw.get("event")),
        )
        bus.publish(StandardEvents.NEWS_RECEIVED, news="n", event=marker)
        self.assertIs(got[0], marker)

    def test_handler_exception_is_isolated_and_counted(self):
        bus = EventBus()
        got: List[str] = []
        bus.subscribe(EventType.ENGINE_READY, lambda **kw: (_ for _ in ()).throw(
            RuntimeError("boom")))
        bus.subscribe(EventType.ENGINE_READY, lambda **kw: got.append("second"))
        bus.publish(EventType.ENGINE_READY)
        self.assertEqual(got, ["second"])
        self.assertEqual(bus.stats["handler_errors"], 1)


# =============================================================================
# 3. 任务存储与状态机
# =============================================================================
class TestTaskStore(unittest.TestCase):
    def test_priority_order(self):
        store = TaskStore()
        low = EngineTask(task_type=TaskType.LEGACY_JOB, priority=Priority.IDLE)
        high = EngineTask(task_type=TaskType.LEGACY_JOB, priority=Priority.CRITICAL)
        store.enqueue(low)
        store.enqueue(high)
        first = store.pop(Queue.DEFAULT)
        self.assertIs(first, high)

    def test_dedup_blocks_duplicate_while_inflight(self):
        store = TaskStore()
        a = EngineTask(task_type=TaskType.FACTOR_CALCULATE, dedup_key="k1")
        b = EngineTask(task_type=TaskType.FACTOR_CALCULATE, dedup_key="k1")
        self.assertEqual(store.enqueue(a)[0], True)
        ok, reason = store.enqueue(b)
        self.assertFalse(ok)
        self.assertIn("幂等键", reason)

    def test_dedup_key_released_after_terminal(self):
        store = TaskStore()
        a = EngineTask(task_type=TaskType.FACTOR_CALCULATE, dedup_key="k1")
        store.enqueue(a)
        store.mark_success(a)
        again = EngineTask(task_type=TaskType.FACTOR_CALCULATE, dedup_key="k1")
        self.assertTrue(store.enqueue(again)[0])

    def test_retry_then_dead_letter(self):
        store = TaskStore()
        task = EngineTask(task_type=TaskType.LEGACY_JOB, max_retries=1)
        store.enqueue(task)
        store.pop(Queue.DEFAULT)
        store.mark_running(task, "default")
        self.assertEqual(store.mark_failure(task, "boom"), "retry")
        self.assertTrue(store.requeue(task))
        store.pop(Queue.DEFAULT)
        store.mark_running(task, "default")
        self.assertEqual(store.mark_failure(task, "boom"), "dead_letter")
        self.assertEqual(task.status, TaskState.DEAD_LETTER)

    def test_dead_letter_shortcut_skips_retry(self):
        store = TaskStore()
        task = EngineTask(task_type=TaskType.LEGACY_JOB, max_retries=5)
        store.enqueue(task)
        store.dead_letter(task, "没有处理器")
        self.assertEqual(task.status, TaskState.DEAD_LETTER)
        self.assertEqual(store.stats["dead_letter"], 1)

    def test_non_terminal_for_recovery(self):
        store = TaskStore()
        queued = EngineTask(task_type=TaskType.LEGACY_JOB)
        done = EngineTask(task_type=TaskType.LEGACY_JOB)
        store.enqueue(queued)
        store.enqueue(done)
        store.mark_success(done)
        self.assertEqual([t.task_id for t in store.non_terminal()], [queued.task_id])


# =============================================================================
# 4. 任务模型
# =============================================================================
class TestEngineTask(unittest.TestCase):
    def test_queue_defaults_from_task_type(self):
        self.assertEqual(EngineTask(task_type=TaskType.FACTOR_CALCULATE).queue,
                         Queue.RESEARCH)
        self.assertEqual(EngineTask(task_type=TaskType.TRADE_EXECUTE).queue,
                         Queue.TRADING)

    def test_trade_task_never_retries(self):
        task = EngineTask(task_type=TaskType.TRADE_EXECUTE, max_retries=3)
        self.assertEqual(task.max_retries, 0)
        task.mark_failed("x")
        self.assertFalse(task.can_retry())

    def test_retry_backoff_has_jitter(self):
        task = EngineTask(task_type=TaskType.LEGACY_JOB, retry_interval=10.0)
        delays = {round(task.next_retry_delay(), 4) for _ in range(8)}
        self.assertGreater(len(delays), 1, "退避必须带抖动，否则失败任务会同时重试")


# =============================================================================
# 5. 能力路由
# =============================================================================
class TestCapabilityRouter(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CapabilityRouter()

    def test_route_to_expected_pool(self):
        self.assertEqual(self.router.pool_for(TaskType.MARKET_KLINE_COLLECT),
                         Pool.MARKET)
        self.assertEqual(self.router.pool_for(TaskType.FACTOR_CALCULATE),
                         Pool.RESEARCH)
        self.assertEqual(self.router.pool_for(TaskType.STRATEGY_RUN),
                         Pool.STRATEGY)
        self.assertEqual(self.router.pool_for(TaskType.TRADE_EXECUTE),
                         Pool.TRADING)
        self.assertEqual(self.router.pool_for(TaskType.BACKTEST_RUN),
                         Pool.BACKTEST)
        self.assertEqual(self.router.pool_for(TaskType.MAINTENANCE_LOG_CLEANUP),
                         Pool.MAINTENANCE)

    def test_unknown_task_type_is_unroutable(self):
        self.assertIsNone(self.router.pool_for("no.such.task"))
        self.assertTrue(self.router.unroutable("no.such.task"))

    def test_override_pins_task_to_pool(self):
        self.router.set_task_pool(TaskType.FACTOR_CALCULATE, Pool.BACKTEST)
        self.assertEqual(self.router.pool_for(TaskType.FACTOR_CALCULATE),
                         Pool.BACKTEST)

    def test_trading_and_strategy_are_single_slot(self):
        """串行是**正确性要求**：并发改账本 / 并发下单都会出事。"""
        self.assertEqual(Pool.DEFAULT_CAPACITY[Pool.TRADING], 1)
        self.assertEqual(Pool.DEFAULT_CAPACITY[Pool.STRATEGY], 1)


# =============================================================================
# 6. 执行池隔离
# =============================================================================
class TestPoolIsolation(unittest.TestCase):
    def test_each_pool_has_own_executor(self):
        mgr = PoolManager()
        market = mgr.pool("market")
        backtest = mgr.pool("backtest")
        self.assertIsNot(market._executor, backtest._executor)

    def test_full_pool_returns_none_not_error(self):
        mgr = PoolManager(capacities={"trading": 1})
        d1 = mgr.dispatch("trading", "t1", time.sleep, 0.2)
        self.assertIsNotNone(d1)
        d2 = mgr.dispatch("trading", "t2", time.sleep, 0.1)
        self.assertIsNone(d2, "池满应返回 None（正常调度状态），不是异常")
        _wait(lambda: mgr.inflight("trading") == 0)

    def test_backtest_busy_does_not_block_market(self):
        mgr = PoolManager(capacities={"backtest": 1, "market": 1})
        mgr.dispatch("backtest", "b1", time.sleep, 0.3)
        self.assertFalse(mgr.has_capacity("backtest"))
        self.assertTrue(mgr.has_capacity("market"))
        _wait(lambda: mgr.inflight("backtest") == 0)


# =============================================================================
# 7. 调度规则
# =============================================================================
class TestRules(unittest.TestCase):
    def test_condition_exception_means_skip(self):
        def boom(event, ctx):
            raise RuntimeError("配置读取失败")
        rule = Rule(name="boom", event_type="x.y",
                    task_type=TaskType.FACTOR_CALCULATE, condition=boom)
        self.assertFalse(rule.should_run(EngineEvent.create("x.y"), None))

    def test_disabled_rule_never_runs(self):
        rule = Rule(name="off", event_type="x.y",
                    task_type=TaskType.FACTOR_CALCULATE, enabled=False)
        self.assertFalse(rule.should_run(EngineEvent.create("x.y"), None))

    def test_build_task_carries_provenance(self):
        rules = {r.name: r for r in build_default_rules()}
        rule = rules["kline_updated_factor"]
        evt = EngineEvent.create(
            EventType.MARKET_KLINE_UPDATED,
            {"symbols": ["000001"], "day": "2026-10-12"},
            source="collection_service",
            correlation_id="chain-1",
        )
        task = rule.build_task(evt, None)
        self.assertEqual(task.source_event_id, evt.event_id)
        self.assertEqual(task.causation_id, evt.event_id)
        self.assertEqual(task.correlation_id, "chain-1")
        self.assertEqual(task.params["symbols"], ["000001"])
        self.assertTrue(task.dedup_key)

    def test_registry_matches_by_event_type(self):
        reg = RuleRegistry()
        reg.register_all(build_default_rules())
        matched = reg.match(EventType.ENGINE_READY)
        self.assertTrue(matched)
        self.assertTrue(all(r.event_type == EventType.ENGINE_READY for r in matched))
        self.assertEqual(reg.match("no.such.event"), ())

    def test_rule_can_be_toggled(self):
        reg = RuleRegistry()
        reg.register_all(build_default_rules())
        self.assertTrue(reg.set_enabled("kline_updated_factor", False))
        rule = reg.get("kline_updated_factor")
        self.assertFalse(rule.enabled)

    def test_signal_rule_creates_risk_task(self):
        rules = {r.name: r for r in build_default_rules()}
        rule = rules["signal_generated_risk"]
        evt = EngineEvent.create(
            EventType.STRATEGY_SIGNAL_GENERATED,
            {"signal": object(), "task_id": "t-1"},
        )
        task = rule.build_task(evt, None)
        self.assertEqual(task.task_type, TaskType.RISK_EVALUATE)
        self.assertEqual(task.priority, Priority.CRITICAL)


# =============================================================================
# 8. 调度器全链路
# =============================================================================
class TestSchedulerFlow(unittest.TestCase):
    def test_echo_events_create_nothing(self):
        sch = _scheduler()
        before = sch.store.pending_count()
        for echo in (EventType.TASK_STARTED, EventType.TASK_COMPLETED,
                     EventType.TASK_FAILED):
            self.assertEqual(sch.handle_event(EngineEvent.create(echo)), 0)
        self.assertEqual(sch.store.pending_count(), before)
        self.assertGreater(sch.stats["events_echo"], 0)

    def test_engine_ready_creates_startup_tasks(self):
        sch = _scheduler()
        created = sch.handle_event(EngineEvent.create(EventType.ENGINE_READY))
        self.assertGreaterEqual(created, 2)
        types = [t.task_type for t in sch.store.pending_tasks()]
        self.assertIn(TaskType.SYSTEM_RECOVER, types)
        self.assertIn(TaskType.MAINTENANCE_VALIDATE, types)

    def test_kline_updated_creates_factor_task_once(self):
        sch = _scheduler()
        payload = {"symbols": ["000001"], "day": "2026-10-12"}
        self.assertEqual(
            sch.handle_event(
                EngineEvent.create(
                    EventType.MARKET_KLINE_UPDATED, dict(payload), source="collection"
                )
            ),
            1,
        )
        # 同一批 K 线再来一次（事件重投）不该再建任务
        self.assertEqual(
            sch.handle_event(
                EngineEvent.create(
                    EventType.MARKET_KLINE_UPDATED, dict(payload), source="collection"
                )
            ),
            0,
        )
        self.assertEqual(sch.store.qsize(Queue.RESEARCH), 1)

    def test_kline_without_symbols_creates_nothing(self):
        """没有标的就跑"因子更新"= 对全市场重算，必须被条件挡住。"""
        sch = _scheduler()
        self.assertEqual(
            sch.handle_event(EngineEvent.create(EventType.MARKET_KLINE_UPDATED)),
            0,
        )

    def test_dispatch_runs_handler(self):
        sch = _scheduler()
        done: List[Any] = []
        sch.handlers.register(
            TaskType.FACTOR_CALCULATE,
            lambda task, ctx: done.append(task.params.get("symbols")),
            override=True,
        )
        ok, reason = sch.submit_engine_task(
            TaskType.FACTOR_CALCULATE, params={"symbols": ["000001"]}
        )
        self.assertTrue(ok, reason)
        self.assertTrue(_pump(sch, predicate=lambda: bool(done)))
        self.assertEqual(done[0], ["000001"])
        self.assertEqual(sch.store.stats["success"], 1)

    def test_task_without_handler_goes_to_dead_letter(self):
        # 用 sweep 而不是 backtest.run：后者已经注册了处理器
        # （回测接进任务体系后它就是个正常任务，不再能当"没处理器"的样本）
        sch = _scheduler()
        sch.submit_engine_task(TaskType.BACKTEST_SWEEP, params={})
        sch._dispatch_once()
        self.assertEqual(sch.store.stats["dead_letter"], 1)
        letters = sch.store.dead_letters()
        self.assertIn("没有注册处理器", letters[0]["error"])

    def test_backtest_run_has_handler(self):
        """回测必须是"能跑的任务"，不是"声明了但没人接"的占位。

        它曾经被列在 UNIMPLEMENTED 里，理由是"建议显式调用"；
        但那套显式调用是自己另起炉灶，结果回测既不在任务明细里、
        失败了也不进死信。这个用例钉住它已经接通。
        """
        sch = _scheduler()
        self.assertTrue(sch.handlers.has(TaskType.BACKTEST_RUN))
        self.assertEqual(sch.router.pool_for(TaskType.BACKTEST_RUN), Pool.BACKTEST)

        # 缺 symbol 要失败进死信，而不是"跑成功但什么都没做"
        sch.submit_engine_task(TaskType.BACKTEST_RUN, params={})
        sch._dispatch_once()
        _wait(lambda: sch.store.stats["dead_letter"] == 1, 6.0)
        self.assertEqual(sch.store.stats["dead_letter"], 1)
        self.assertIn("symbol", sch.store.dead_letters()[0]["error"])

    def test_failure_is_retried_then_dead_lettered(self):
        sch = _scheduler()
        calls = []

        def flaky(task, ctx):
            calls.append(1)
            raise RuntimeError("数据源 500")

        sch.handlers.register(TaskType.FACTOR_CALCULATE, flaky, override=True)
        sch.submit_engine_task(
            TaskType.FACTOR_CALCULATE,
            params={"symbols": ["000001"]},
            max_retries=1,
            retry_interval=0.1,
        )
        self.assertTrue(_pump(sch, 4.0, lambda: len(calls) >= 2))
        _wait(lambda: sch.store.stats["dead_letter"] == 1, 6.0)
        self.assertEqual(sch.store.stats["dead_letter"], 1)

    def test_kline_to_factor_to_strategy_chain(self):
        """最小闭环：K 线更新 → 因子任务 → 因子快照 → 策略任务。"""
        sch = _scheduler()
        sch.run_mode = RunMode.SIMULATE  # 回测模式下策略链刻意不自动跑
        seen: List[str] = []

        def factor(task, ctx):
            seen.append("factor")
            _publish(
                ctx,
                EventType.FACTOR_SNAPSHOT_UPDATED,
                symbols=task.params.get("symbols") or [],
                day="2026-10-12",
            )
            return {"ok": True}

        def strategy(task, ctx):
            seen.append("strategy")
            return {"ok": True}

        sch.handlers.register(TaskType.FACTOR_CALCULATE, factor, override=True)
        sch.handlers.register(TaskType.STRATEGY_RUN, strategy, override=True)

        sch.handle_event(
            EngineEvent.create(
                EventType.MARKET_KLINE_UPDATED,
                {"symbols": ["000001"], "day": "2026-10-12"},
                source="collection_service",
            )
        )
        self.assertTrue(_pump(sch, 6.0, lambda: "strategy" in seen))
        self.assertEqual(seen, ["factor", "strategy"])

    def test_engine_stopping_blocks_new_tasks(self):
        sch = _scheduler()
        sch._on_engine_stopping()
        self.assertEqual(sch.handle_event(EngineEvent.create(EventType.ENGINE_READY)), 0)

    def test_legacy_submit_task_still_works(self):
        sch = _scheduler()
        done: List[str] = []
        tid = sch.submit_task("一次性作业", lambda: done.append("ran"))
        self.assertTrue(tid)
        self.assertTrue(_pump(sch, predicate=lambda: bool(done)))
        self.assertEqual(done, ["ran"])

    def test_health_reports_dead_letters(self):
        sch = _scheduler()
        sch.submit_engine_task(TaskType.BACKTEST_SWEEP, params={})
        sch._dispatch_once()
        ok, detail = sch.health_check()
        self.assertTrue(ok)
        self.assertIn("死信", detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
