#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : scheduler.py
# @Description : 调度组件：引擎的"心脏"——订阅事件、建任务、分发给执行池
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
调度器在整条链路里的位置
========================

    Engine ──发布事件──> EventBus ──投递──> Scheduler
                                              │
                                   订阅 → 匹配规则 → 建 EngineTask
                                              │
                                   CapabilityRouter 选队列/执行池
                                              │
        Market / Research / Strategy / Trading / Backtest / Maintenance 池

关键设计：**调度器不执行任务**。
它只回答三个问题：该不该做（规则条件）→ 做成什么任务（规则参数）→
交给谁做（能力路由）。真正的执行在 :mod:`app.runtime.workers`。

为什么必须这样分层
------------------

重构前"服务互相直接调用"：采集服务拿到数据 → 直接调因子模块 →
直接调策略 → 直接下单。这条链有三个致命缺陷：

  1. **失败无处可去**：因子算挂了，整条链断在中间，没人知道卡在哪一步；
  2. **重试无从谈起**：链上每一环都要自己写重试，且各不相同；
  3. **无法插队**：回测和撤单抢同一批线程，撤单排在回测后面。

事件 + 任务把这条链拆成"可追踪、可重试、可控制"的工作流：
每一步都是一个有 ID、有状态、有幂等键的任务，
卡在哪一步、重试了几次、为什么失败，全部可查。

回声防护
--------

调度器会**拒绝为回声事件建任务**（``task.started`` 等）。
这不是性能优化而是正确性要求：一旦有规则订阅了任务事件，
``task.started → 建任务 → task.started → …`` 会在一秒内堆满队列。
守卫见 :data:`~app.runtime.events.types.TASK_ECHO_EVENTS`。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from twisted.internet import defer, reactor
from twisted.internet.task import LoopingCall

from app.common.logging import logger
from app.engine.capability import SchedulerComponent as SchedulerBase
from app.engine.context import RunMode
from app.runtime.events import EventType, StandardEvents
from app.runtime.events.schema import EngineEvent
from app.runtime.events.types import TaskType
from app.runtime.maintenance import TICK_SECONDS, MaintenancePlanner
from app.runtime.scheduling import (
    CapabilityRouter,
    RuleRegistry,
    build_default_rules,
)
from app.runtime.tasking.models import EngineTask, Priority
from app.runtime.tasking.store import TaskStore
from app.runtime.utils import async_sleep
from app.runtime.workers import HandlerRegistry, PoolManager
from app.runtime.workers.handlers import register_default_handlers


class SchedulerComponent(SchedulerBase):
    """任务调度 + 周期定时器（事件驱动版）。

    它决定**什么时候该跑哪个任务**：订阅事件 → 匹配调度规则 →
    生成 :class:`EngineTask` → 按能力路由交给对应执行池。

    运行模式差异
    ------------
    ``BACKTEST`` 下调度器仍然工作，但内置规则里的实盘链
    （如 因子 → 策略）带 ``_mode_not("BACKTEST")`` 条件会自动不生效。
    所以不需要维护"回测专用"的另一套调度逻辑。
    """

    name = "scheduler"

    #: 调度主循环的节拍（秒）。10ms 太密会空转烧 CPU，
    #: 100ms 对实盘又太钝 —— 50ms 是实测下来的平衡点。
    tick_interval: float = 0.05

    #: 恢复上来的任务最多等多久还认为"值得再跑一次"（小时）。
    #: 行情类的活隔夜就没意义了（今天的 K 线才是今天的）；
    #: 而"三天前没算完的因子"重跑一遍也换不回当时的上下文。
    recover_max_age_hours: float = 72.0
    #: 单次最多捞回多少条。防的是"磁盘上攒了十万条没干完的活，
    #: 启动时一次性灌爆队列"——那会把真正紧急的今日任务挤到后面。
    recover_limit: int = 500

    def __init__(
        self,
        capacities: Optional[Dict[str, int]] = None,
        pools: Optional[List[str]] = None,
        with_default_rules: bool = True,
        recover: bool = True,
        journal_path: str = "",
    ) -> None:
        super().__init__()
        #: 引擎引用（builder 用 set_engine 注入），规则条件靠它查服务状态
        self.engine: Any = None
        self.run_mode: RunMode = RunMode.BACKTEST
        self.max_concurrent: int = 0

        # ---- 事件 → 任务 ----
        self.store = TaskStore(journal_path=journal_path)
        #: 启动是否从流水账捞回未完成的活。关掉它只该有一个理由：
        #: 明确想让这台引擎"从零开始"（比如换了一份配置重跑）。
        self.recover_on_start = bool(recover)
        self.recovery_report: Dict[str, Any] = {}
        self.router = CapabilityRouter()
        self.rules = RuleRegistry()
        if with_default_rules:
            self.rules.register_all(build_default_rules())

        # ---- 周期维护（日志清理 / 归档 / 校验）----
        # 维护任务的时间源：没有它，``system.maintenance.due`` 没人发，
        # 那三条运维规则就只是三行配置，永远不会被触发。
        self.maintenance = MaintenancePlanner()
        # 这里绑的是**惰性**发布器：事件总线此刻可能还没有（组件未初始化），
        # 到真正发事件时才解析 self.bus。否则"启动前手动跑一次维护"会因为
        # 拿不到总线而失败，而报错还只是干巴巴的一句"发布失败"。
        self.maintenance.bind(lambda *a, **kw: self.bus.publish(*a, **kw))

        # ---- 执行 ----
        self.pools = PoolManager(capacities=capacities, enabled=pools)
        self.handlers = HandlerRegistry()
        register_default_handlers(self.handlers)
        # 旧式"函数型任务"的兜底通道：面板/CLI 仍在用它投递一次性作业
        self.handlers.register(
            TaskType.LEGACY_JOB, self._run_legacy_job,
            name="run_legacy_job", description="旧式函数型任务", override=True,
        )

        self._running = False
        #: 是否还接受新任务。引擎进入 STOPPING 时置 False ——
        #: 否则"等剩余任务收尾"的过程中还会不断有新任务进来，永远等不完。
        self._accept_new = True
        self._lock = threading.RLock()

        # ---- 定时器（兼容旧节拍） ----
        self._timers: Dict[str, LoopingCall] = {}
        self._is_stopping = False
        self._pending_retries: set = set()
        self._dispatch_call: Optional[Any] = None

        #: 在途任务 → (池名, 超时时刻 monotonic)
        self._deadlines: "Dict[str, Tuple[str, float]]" = {}
        #: 事件监听器（保持引用，组件停止时才能精确退订）
        self._listeners: "Dict[str, Callable[..., Any]]" = {}

        self.stats: Dict[str, int] = {
            "events_seen": 0, "events_echo": 0, "tasks_created": 0,
            "tasks_deduped": 0, "dispatched": 0, "timeout": 0,
            "no_handler": 0, "unroutable": 0,
        }

    # ============================================================
    # 引擎装配
    # ============================================================
    def set_engine(self, engine: Any) -> None:
        """注入引擎引用。规则条件靠它判断"某个服务启用没有"。"""
        self.engine = engine

    @property
    def bus(self) -> Any:
        """事件总线别名。

        处理器（``handlers.py``）用 ``ctx.bus`` 发后续事件来推进工作流，
        这里把组件基类的 ``event_bus`` 暴露成统一的名字，
        处理器就不必知道"我到底被谁驱动"。
        """
        return self.event_bus

    # ---------------- 延迟回调 ----------------
    def _later(self, delay: float, fn: Callable[..., Any], *args: Any) -> Any:
        """延迟执行。reactor 未运行时退回 ``threading.Timer``。

        为什么必须退回：``reactor.callLater`` 在 reactor 没跑起来时
        **永远不会触发**。于是"任务失败 → 安排重试"这条路径在
        单元测试与非 reactor 场景里直接变成"任务凭空消失"——
        而消失的方式还特别安静：没有报错、没有日志、任务就停在 RETRYING。
        """
        try:
            if reactor.running:
                return reactor.callLater(delay, fn, *args)
        except Exception:  # pragma: no cover
            pass
        timer = threading.Timer(delay, fn, args=args)
        timer.daemon = True
        timer.start()
        return timer

    @staticmethod
    def _cancel_later(handle: Any) -> None:
        try:
            if hasattr(handle, "active") and not handle.active():
                return
            handle.cancel()
        except Exception:  # pragma: no cover
            pass

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        self.max_concurrent = int(
            cfg.get("SCHEDULER_MAX_CONCURRENT", 5)
        )
        self.run_mode = self.context.run_mode
        self.tick_interval = float(cfg.get("SCHEDULER_TICK", self.tick_interval))
        self.recover_on_start = bool(
            cfg.get("RECOVER_TASKS", self.recover_on_start)
        )
        self.recover_max_age_hours = float(
            cfg.get("RECOVER_MAX_AGE_HOURS", self.recover_max_age_hours)
        )
        self.recover_limit = int(cfg.get("RECOVER_LIMIT", self.recover_limit))

        # 池容量可按组件配置覆盖
        caps = cfg.get("pools") or {}
        if caps:
            for name, cap in caps.items():
                pool = self.pools.pool(name)
                if pool is not None:
                    pool.capacity = max(0, int(cap))
        logger.info(
            f"调度组件初始化完成 | 规则 {len(self.rules.all_rules())} 条 "
            f"| 处理器 {len(self.handlers.task_types())} 种 "
            f"| 执行池 {len(self.pools.names())} 个"
        )

    def on_start(self) -> None:
        self._running = True
        self._accept_new = True
        logger.info(
            f"调度组件已启动 | 模式: {self.run_mode.value} "
            f"| 执行池: {','.join(self.pools.names())}"
        )
        # 0) **先恢复，再订阅**：顺序反了会漏掉恢复期间产生的事件
        #    （比如"恢复到一半来了条风控事件"），而恢复本身不需要订阅者。
        self._recover_from_journal()
        # 1) 订阅所有规则关心的事件
        self._subscribe_rules()
        # 2) 旧式任务提交通道
        self.subscribe_event(StandardEvents.TASK_SUBMIT, self._on_task_submit)
        # 2.5) 引擎要停了 → 立刻停止建新任务
        self.subscribe_event(EventType.ENGINE_STOPPING, self._on_engine_stopping)
        # 3) 默认节拍（沿用原定时器组件行为，供存量订阅者使用）
        self.start_timer(
            interval=5,
            event_name=StandardEvents.TIMER_3MIN,
            timer_id="default_3min",
        )
        # 3.5) 运维节拍：计划器拿到 tick 后自己判断"哪个维护任务到点了"，
        #      再发 system.maintenance.due。计划不发任务，只发"该干活了"。
        self._bind_maintenance()
        self.start_timer(
            interval=TICK_SECONDS,
            event_name=EventType.SYSTEM_MAINTENANCE_TICK,
            timer_id="maintenance_tick",
        )
        # 4) 启动分发循环
        self._dispatch_call = reactor.callLater(
            self.tick_interval, self._dispatch_loop
        )

    # ============================================================
    # 崩溃恢复 / 停机落盘
    # ============================================================
    def _recover_from_journal(self) -> Dict[str, Any]:
        """启动恢复：把上次没干完的活捞回来。

        为什么放在 start 而不是 initialize：初始化阶段组件还没齐，
        恢复出来的任务一旦被领走就会去调还没准备好的服务。
        而 start 是"一切就绪"的时刻。
        """
        if not self.recover_on_start:
            self.recovery_report = {"ok": False, "reason": "已配置为启动不恢复"}
            return self.recovery_report

        try:
            report = self.store.recover(
                max_age_hours=self.recover_max_age_hours,
                limit=self.recover_limit,
            )
        except Exception as exc:
            report = {"ok": False, "reason": str(exc)}
            logger.error(f"任务恢复失败: {exc}", exc_info=True)
        self.recovery_report = report

        if not report.get("ok"):
            logger.info(
                f"未执行任务恢复 | 原因: {report.get('reason') or '无流水账'}"
            )
            return report

        resumed = report.get("queued", 0)
        if resumed or report.get("dead_restored"):
            logger.warning(
                f"已从流水账恢复任务 | 重新入队 {resumed} 条"
                f"（其中被中断 {report.get('interrupted', 0)} 条）"
                f"| 超龄转死信 {report.get('expired', 0)} 条"
                f"| 重试耗尽转死信 {report.get('exhausted', 0)} 条"
                f"| 错误队列恢复 {report.get('dead_restored', 0)} 条"
            )
        else:
            logger.info("流水账中未发现未完成任务")
        try:
            if self.event_bus is not None:
                self.event_bus.publish(
                    EventType.SYSTEM_TASKS_RECOVERED,
                    source="scheduler",
                    **{k: v for k, v in report.items() if k != "items"},
                )
        except Exception as exc:
            logger.debug(f"发布恢复事件失败（已忽略）: {exc}")
        return report

    def _persist_on_stop(self) -> None:
        """停机落盘：把"干了一半"的活标成中断并写进流水账。

        不做这件事的后果：任务在盘上仍是 ``running``，下次启动恢复时
        看不出它是"被中断"还是"正在别处跑"，而这两者的处置完全不同
        （前者要重跑，后者不该碰）。
        """
        try:
            for task in self.store.running_tasks():
                task.mark_interrupted()
                self.store._persist(task)
            self.store.flush()
        except Exception as exc:
            logger.error(f"停机落盘失败: {exc}", exc_info=True)

    def dead_retry(self, task_id: str) -> "Tuple[bool, str]":
        """人工重投一条死信。"""
        ok, reason = self.store.retry_dead(task_id)
        if ok:
            logger.info(f"人工重投死信 {task_id[:8]}: {reason}")
            self._publish_dead_resolved(task_id, "retry")
        return ok, reason

    def dead_discard(self, task_id: str) -> "Tuple[bool, str]":
        """认掉一条死信（不重跑）。"""
        ok, reason = self.store.discard_dead(task_id)
        if ok:
            self._publish_dead_resolved(task_id, "discard")
        return ok, reason

    def dead_retry_all(self, task_type: str = "") -> "Tuple[int, int]":
        """批量重投。"""
        done, skipped = self.store.retry_all_dead(task_type)
        if done:
            logger.info(f"批量重投死信: 成功 {done} 条，跳过 {skipped} 条")
            self._publish_dead_resolved("", f"retry_all:{task_type or 'all'}")
        return done, skipped

    def _publish_dead_resolved(self, task_id: str, resolution: str) -> None:
        bus = self.event_bus
        if bus is None:
            return
        try:
            bus.publish(
                EventType.SYSTEM_DEAD_RESOLVED,
                source="scheduler", task_id=task_id, resolution=resolution,
            )
        except Exception as exc:
            logger.debug(f"发布死信处置事件失败（已忽略）: {exc}")

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True) -> Any:
        """停止调度器。

        顺序有讲究：**先停事件源，再等任务收尾**。
        反过来会出现"队列刚排空又来一批定时事件"的窗口。

        等待必须异步：``on_stop`` 跑在 reactor 线程里，用 ``time.sleep``
        会把事件循环整个冻住，而任务自己又在等这些回调 ——
        ``_active_tasks`` 永远归不了零，形成"停止 = 挂死"。
        因此这里异步让出，并**带 deadline**：宁可超时告警，也不无限等待。
        """
        self._running = False

        for timer_id, lc in self._timers.items():
            try:
                if lc.running:
                    lc.stop()
            except Exception as exc:
                logger.error(f"停止定时器 {timer_id} 异常: {exc}")
        self._timers.clear()

        # 取消尚未触发的重试，避免停止后继续往队列里投任务
        for later in list(self._pending_retries):
            self._cancel_later(later)
        self._pending_retries.clear()

        if self._dispatch_call is not None:
            try:
                if self._dispatch_call.active():
                    self._dispatch_call.cancel()
            except Exception:
                pass
            self._dispatch_call = None

        if graceful:
            deadline = time.monotonic() + self._shutdown_wait()
            while not self.store.is_idle():
                if time.monotonic() >= deadline:
                    logger.warning(
                        f"停止调度器时仍有任务未收尾 | 待执行 "
                        f"{self.store.pending_count()} | 在跑 "
                        f"{self.store.running_count()} | 超时放弃"
                    )
                    break
                yield async_sleep(0.1)

        # 收尾：不管有没有等干净，都把现场写进流水账。
        # 超时放弃的那批任务此刻仍是 running —— 它们必须被记成"中断"，
        # 而不是留给下次启动去猜。
        self._persist_on_stop()
        self.pools.shutdown(wait=False)
        logger.info("调度组件已停止")

    def _shutdown_wait(self) -> float:
        try:
            cfg = getattr(self.context, "config", None) or {}
            return float(cfg.get("SHUTDOWN_TASK_WAIT", 5))
        except Exception:
            return 5.0

    # ============================================================
    # 事件订阅
    # ============================================================
    def _subscribe_rules(self) -> None:
        """为每条规则关注的事件挂一个监听器。"""
        for event_type in self.rules.event_types():
            self.subscribe_event(event_type, self._make_listener(event_type))

    def _bind_maintenance(self) -> None:
        """把维护计划器接到事件总线上。

        计划器通过 ``bus.publish`` 发事件，而不是直接 ``submit_engine_task``：
        维护任务同样要过"规则 → 队列 → 池"这条正规链路，
        否则它会绕过死信与重试，坏了也没人知道。
        """
        bus = self.bus
        if bus is not None:
            self.subscribe_event(
                EventType.SYSTEM_MAINTENANCE_TICK, self._on_maintenance_tick
            )

    def _on_maintenance_tick(self, **kwargs: Any) -> None:
        """运维节拍 → 检查哪些维护任务到点了。"""
        try:
            self.maintenance.tick()
        except Exception as exc:
            logger.error(f"维护计划检查失败: {exc}")

    def _make_listener(self, event_type: str) -> Callable[..., Any]:
        """生成一个**知道自己订阅了什么事件**的监听器。

        为什么需要包一层：总线把事件展开成关键字参数调用订阅者，
        而资讯服务发布时把业务对象挂在了 ``event`` 键上（``NewsEvent``），
        于是监听器收到的 ``event`` 可能是业务对象而不是
        :class:`EngineEvent`。闭包记住事件名，就永远知道该怎么还原信封。
        """
        def _listener(**kwargs: Any) -> None:
            evt = kwargs.get("event")
            if isinstance(evt, EngineEvent):
                self.handle_event(evt)
                return
            data = {k: v for k, v in kwargs.items() if k != "source"}
            self.handle_event(
                EngineEvent.create(
                    event_type=event_type,
                    data=data,
                    source=str(kwargs.get("source") or ""),
                )
            )
        self._listeners[event_type] = _listener
        return _listener

    def handle_event(self, event: EngineEvent) -> int:
        """事件 → 任务。返回本次建出的任务数。

        这是"调度"这件事的全部：读规则、判条件、造任务、入队。
        它**不做任何耗时的事**，所以可以安全地跑在事件总线的同步投递里。
        """
        if event is None or not self._accept_new:
            return 0
        bus = self.event_bus
        if bus is not None and bus.is_echo_event(event.event_type):
            # 回声事件：任务系统自己发出的，不能反过来再建任务
            self.stats["events_echo"] += 1
            return 0
        matched = self.rules.match(event.event_type)
        if not matched:
            return 0

        self.stats["events_seen"] += 1
        created = 0
        for rule in matched:
            if not rule.should_run(event, self):
                continue
            task = rule.build_task(event, self)
            ok, reason = self.store.enqueue(task)
            if ok:
                created += 1
                self.stats["tasks_created"] += 1
                self._publish_task_event(EventType.TASK_CREATED, task)
                logger.debug(
                    f"规则[{rule.name}] 由 {event.event_type} 生成任务 "
                    f"{task.task_type}#{task.task_id[:8]} → 队列 {task.queue}"
                )
            else:
                self.stats["tasks_deduped"] += 1
                logger.debug(f"规则[{rule.name}] 生成任务被跳过: {reason}")
        return created

    # ============================================================
    # 分发循环
    # ============================================================
    def _dispatch_loop(self) -> None:
        if not self._running:
            return
        try:
            self._dispatch_once()
            self._sweep_timeouts()
        except Exception as exc:
            logger.error(f"调度循环异常: {exc}", exc_info=True)
        finally:
            self._dispatch_call = reactor.callLater(
                self.tick_interval, self._dispatch_loop
            )

    def _dispatch_once(self) -> int:
        """扫一遍所有池，有空槽就从对应队列领活。"""
        dispatched = 0
        for pool_name in self.pools.names():
            pool = self.pools.pool(pool_name)
            if pool is None:
                continue
            queue_name = pool.queue
            if not queue_name:
                continue
            while pool.has_slot():
                task = self.store.pop(queue_name)
                if task is None:
                    break
                if self._start_task(task, pool_name):
                    dispatched += 1
                else:
                    # 领出来却发不出去（无处理器/无路由）时不能死循环，
                    # 该分支内部已把任务终结掉，继续领下一个。
                    continue
        return dispatched

    def _start_task(self, task: EngineTask, pool_name: str) -> bool:
        """把一个任务交给执行池。返回是否成功起飞。"""
        handler = self.handlers.get(task.task_type)
        if handler is None:
            err = (
                f"任务类型 {task.task_type} 没有注册处理器"
                f"（已在 handlers.py 声明为待实现？）"
            )
            self.stats["no_handler"] += 1
            self.store.dead_letter(task, err)
            self._publish_task_event(EventType.TASK_DEAD_LETTERED, task, err)
            logger.error(err)
            return False

        target = self.router.pool_for(task.task_type)
        if target is None:
            err = f"没有执行池声明能处理 {task.task_type}"
            self.stats["unroutable"] += 1
            self.store.dead_letter(task, err)
            self._publish_task_event(EventType.TASK_DEAD_LETTERED, task, err)
            logger.error(err)
            return False
        if target != pool_name:
            # 队列与池不是一一对应时（手工改过路由），按路由结果重新入队
            task.queue = self.pools.queue_of(target) or task.queue
            self.store.requeue(task)
            return False

        self.store.mark_running(task, pool_name)
        self._deadlines[task.task_id] = (
            pool_name, time.monotonic() + max(1.0, float(task.timeout_seconds)),
        )
        d = self.pools.dispatch(pool_name, task.task_id, handler, task, self)
        if d is None:
            # 竞态：槽位在检查与投递之间被占。放回队列下次再领，
            # 这不是失败，不该进重试统计。
            self._deadlines.pop(task.task_id, None)
            self.store.requeue(task)
            return False

        self.stats["dispatched"] += 1
        self._publish_task_event(EventType.TASK_STARTED, task)
        d.addCallbacks(
            lambda result: self._on_task_done(task, result),
            lambda failure: self._on_task_failed(task, failure),
        )
        return True

    # ---------------- 完成 / 失败 ----------------
    def _on_task_done(self, task: EngineTask, result: Any) -> None:
        self._deadlines.pop(task.task_id, None)
        self.store.mark_success(task, result)
        self._publish_task_event(EventType.TASK_COMPLETED, task)
        logger.debug(
            f"任务完成 {task.task_type}#{task.task_id[:8]} "
            f"（{task.elapsed() or 0:.2f}s）"
        )

    def _on_task_failed(self, task: EngineTask, failure: Any) -> None:
        self._deadlines.pop(task.task_id, None)
        message = str(
            failure.getErrorMessage()
            if hasattr(failure, "getErrorMessage") else failure
        )
        decision = self.store.mark_failure(task, message)
        if decision == "retry":
            delay = task.next_retry_delay()
            logger.warning(
                f"任务失败，{delay:.1f}s 后第 {task.attempts} 次重试 | "
                f"{task.task_type}#{task.task_id[:8]} | {message}"
            )
            self._publish_task_event(
                EventType.TASK_RETRY_SCHEDULED, task, message
            )
            later = self._later(delay, self._requeue, task)
            self._pending_retries.add(later)
        else:
            logger.error(
                f"任务进入死信 | {task.task_type}#{task.task_id[:8]} "
                f"| 重试 {task.attempts}/{task.max_retries} 次 | {message}"
            )
            self._publish_task_event(
                EventType.TASK_DEAD_LETTERED, task, message
            )

    def _requeue(self, task: EngineTask) -> None:
        """重试到期：放回队列。"""
        if not self._running:
            return
        if not self.store.requeue(task):
            return
        self._publish_task_event(EventType.TASK_QUEUED, task)

    def _sweep_timeouts(self) -> None:
        """超时巡检。

        Python 没有安全的线程强杀，所以这里只能做**记账层面的超时**：
        释放槽位、把任务判失败（进而重试或进死信），让系统继续运转。
        那个跑飞的线程真跑完时，结果会被丢弃（Deferred 已 fire 过）。
        这比"一个任务卡死，整个池永久少一个槽位"好得多。
        """
        limits = {
            tid: max(0.0, deadline - time.monotonic())
            for tid, (_pool, deadline) in self._deadlines.items()
        }
        if not limits:
            return
        for pool_name, task_id in self.pools.timed_out(limits):
            task = self.store.get(task_id)
            self._deadlines.pop(task_id, None)
            self.pools.release(pool_name, task_id)
            if task is None:
                continue
            self.stats["timeout"] += 1
            message = f"执行超时（>{task.timeout_seconds:.0f}s），已放弃本次执行"
            logger.warning(
                f"任务超时 | {task.task_type}#{task_id[:8]} | 池 {pool_name}"
            )
            decision = self.store.mark_failure(task, message)
            if decision == "retry":
                delay = task.next_retry_delay()
                later = self._later(delay, self._requeue, task)
                self._pending_retries.add(later)
                self._publish_task_event(
                    EventType.TASK_RETRY_SCHEDULED, task, message
                )
            else:
                self._publish_task_event(
                    EventType.TASK_DEAD_LETTERED, task, message
                )

    # ============================================================
    # 契约一：提交任务（旧式函数型任务）
    # ============================================================
    def submit(self, *args: Any, **kwargs: Any) -> Any:
        """``SchedulerComponent`` 契约实现，转发到 :meth:`submit_task`。"""
        return self.submit_task(*args, **kwargs)

    def submit_task(
        self,
        task_name: str,
        handler: Any,
        priority: Any = Priority.NORMAL,
        max_retry: int = 0,
        retry_interval: float = 1.0,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """旧接口：直接提交一个"函数型"任务。

        新代码应该用 :meth:`submit_engine_task` 提交带类型的任务
        （才能被路由到正确的池、才能被规则与面板识别）。
        这里保留是为了面板/CLI 的既有调用不中断。
        """
        if not self._running:
            raise RuntimeError("调度器未启动，无法提交任务")

        task = EngineTask(
            task_type=TaskType.LEGACY_JOB,
            params={
                "handler": handler,
                "args": tuple(args),
                "kwargs": dict(kwargs),
                "name": task_name,
            },
            priority=self._as_priority(priority),
            max_retries=max(0, int(max_retry)),
            retry_interval=max(0.1, float(retry_interval)),
            source="submit_task",
        )
        ok, reason = self.store.enqueue(task)
        if not ok:
            logger.warning(f"任务[{task_name}] 入队失败: {reason}")
        self._publish_task_event(EventType.TASK_CREATED, task)
        return task.task_id

    def submit_engine_task(
        self,
        task_type: str,
        params: Optional[Dict[str, Any]] = None,
        priority: int = Priority.NORMAL,
        queue_name: str = "",
        dedup_key: str = "",
        max_retries: int = 0,
        retry_interval: float = 5.0,
        timeout_seconds: float = 60.0,
        correlation_id: str = "",
    ) -> Tuple[bool, str]:
        """新接口：按**任务类型**提交一个任务（走完整路由）。

        返回 ``(是否入队, 原因)``。被幂等键挡下时返回 False 并说明原因 ——
        这是"已经有人在做"的正常信号，不是错误。
        """
        if not self._running:
            return False, "调度器未启动"
        task = EngineTask(
            task_type=task_type,
            params=dict(params or {}),
            priority=int(priority),
            queue=queue_name,
            dedup_key=dedup_key,
            max_retries=int(max_retries),
            retry_interval=max(0.05, float(retry_interval)),
            timeout_seconds=float(timeout_seconds),
            correlation_id=correlation_id,
            source="submit_engine_task",
        )
        ok, reason = self.store.enqueue(task)
        if ok:
            self.stats["tasks_created"] += 1
            self._publish_task_event(EventType.TASK_CREATED, task)
        return ok, reason

    @staticmethod
    def _as_priority(priority: Any) -> int:
        """兼容旧的 :class:`TaskPriority` 枚举与新的整数优先级。"""
        code = getattr(priority, "code", None)
        if code is not None:
            # HIGHEST=0 … LOW=3 → 映射到 1/3/5/7 的整数档
            return int(code) * 2 + 1
        try:
            return int(priority)
        except (TypeError, ValueError):
            return Priority.NORMAL

    @staticmethod
    def _run_legacy_job(task: EngineTask, ctx: Any) -> Any:
        params = task.params or {}
        fn = params.get("handler")
        if not callable(fn):
            raise RuntimeError("旧式任务缺少可执行函数")
        return fn(*(params.get("args") or ()), **(params.get("kwargs") or {}))

    def _on_engine_stopping(self, **kwargs: Any) -> None:
        """引擎进入停止流程：**立刻**停止建新任务。"""
        self._accept_new = False
        logger.info("调度器已停止接收新任务（引擎正在停止）")

    def _on_task_submit(self, **kwargs: Any) -> None:
        """旧事件通道：``TASK_SUBMIT`` → 提交函数型任务。"""
        handler = kwargs.get("handler")
        if not callable(handler):
            logger.warning("TASK_SUBMIT 事件缺少可执行的 handler，忽略")
            return
        try:
            self.submit_task(
                str(kwargs.get("task_name") or kwargs.get("name") or "legacy"),
                handler,
                kwargs.get("priority", Priority.NORMAL),
                int(kwargs.get("max_retry") or 0),
                float(kwargs.get("retry_interval") or 1.0),
                *(kwargs.get("args") or ()),
                **(kwargs.get("kwargs") or {}),
            )
        except Exception as exc:
            logger.error(f"TASK_SUBMIT 提交失败: {exc}", exc_info=True)

    # ============================================================
    # 契约二：周期定时器
    # ============================================================
    def start_timer(
        self,
        interval: float,
        event_name: str,
        event_data: Optional[Dict[str, Any]] = None,
        run_immediately: bool = False,
        timer_id: str = "",
    ) -> str:
        """注册一个周期性事件源。

        :param timer_id: 省略时按 ``事件名@间隔`` 自动生成，
                         同一个 (事件名, 间隔) 重复注册会被忽略，
                         避免多处调用悄悄把节拍翻倍。
        """
        tid = timer_id or f"{event_name}@{interval}"
        if tid in self._timers:
            return tid
        lc = LoopingCall(self._publish_timer_event, event_name, event_data or {})
        lc.start(max(0.01, float(interval)), now=run_immediately)
        self._timers[tid] = lc
        logger.info(f"定时器[{tid}]已启动 → {interval}s/次")
        return tid

    def stop_timer(self, timer_id: str) -> bool:
        lc = self._timers.pop(timer_id, None)
        if lc is None:
            return False
        try:
            if lc.running:
                lc.stop()
        except Exception as exc:
            logger.error(f"停止定时器 {timer_id} 异常: {exc}")
        return True

    def _publish_timer_event(self, event_name: str, event_data: dict) -> None:
        try:
            if self.event_bus is not None:
                self.event_bus.publish(
                    event_name=event_name, context=event_data, source="timer"
                )
        except Exception as exc:
            logger.error(f"定时器事件执行失败: {exc}", exc_info=True)

    # ============================================================
    # 契约三：空闲判定
    # ============================================================
    @property
    def is_idle(self) -> bool:
        """队列空 + 无在跑任务，引擎才能判定空闲退出。"""
        return self.store.is_idle()

    def is_busy(self) -> bool:
        return not self.store.is_idle()

    # ============================================================
    # 内部：事件发布
    # ============================================================
    def _publish_task_event(
        self, event_type: str, task: EngineTask, error: str = ""
    ) -> None:
        bus = self.event_bus
        if bus is None:
            return
        try:
            bus.publish(
                event_type,
                source="scheduler",
                task_id=task.task_id,
                task_type=task.task_type,
                queue=task.queue,
                status=task.status.value,
                attempts=task.attempts,
                correlation_id=task.correlation_id,
                error=error,
            )
        except Exception as exc:
            logger.error(f"发布任务事件 {event_type} 失败: {exc}")

    # ============================================================
    # 可观测
    # ============================================================
    def get_pending_tasks(self) -> List[EngineTask]:
        with self._lock:
            return self.store.pending_tasks()

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update({
            "running": self._running,
            "run_mode": self.run_mode.value,
            "stats": dict(self.stats),
            "queues": self.store.queues(),
            "store": self.store.snapshot(),
            "pools": self.pools.snapshot(),
            "router": self.router.snapshot(),
            "handlers": self.handlers.snapshot(),
            "rules": self.rules.snapshot(),
            "timers": sorted(self._timers.keys()),
            "maintenance": self.maintenance.snapshot(),
            "recovery": dict(self.recovery_report or {}),
        })
        return base

    def health_check(self) -> Tuple[bool, str]:
        if not self.enabled:
            return True, "disabled"
        # 用"当前还有几条"而不是累计计数：死信被运维清掉之后，
        # 健康度就该恢复正常，否则告警一旦响过就永久响下去。
        dead = self.store.dead_letter_count()
        if dead:
            detail = f"死信 {dead} 条"
            # 死信里含交易类任务是**必须立刻处理**的事件：
            # 它意味着一笔该下的单没下出去，或者一笔不确定的单。
            trading_dead = [
                t for t in self.store.dead_letters(limit=50)
                if t["task_type"] == "trade.execute"
            ]
            if trading_dead:
                return False, f"{detail}（含交易任务 {len(trading_dead)} 条）"
            return True, detail
        return True, f"队列 {self.store.pending_count()} 待执行 / {self.store.running_count()} 在跑"


__all__ = ["SchedulerComponent"]
