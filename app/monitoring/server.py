#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内嵌网页监控组件：在引擎自己的 reactor 上挂一个 Twisted Web Site。

直接读引擎内存状态（零拷贝延迟），控制指令即时生效，零额外进程；
通过 /api/* 暴露 JSON，既给面板用，也给 CLI / hub 用。

本文件原本 1871 行——一个组件类里塞了生命周期、快照落盘、事件订阅
和 40 多个业务视图方法，改一条「因子列表」要翻一千行。现在：

    server.py       组件核心（生命周期 / 快照 / 事件）—— 本文件
    resource.py     MonitorResource：HTTP 传输层
    api/*.py        按业务域划分的视图 mixin（overview/live/brokers/
                    factors/pool/collect/strategies/backtest）

视图用** mixin **而非自由函数：方法体重度依赖 self._xxx，改成传参
等于把 40 多个方法全改一遍。mixin 保持 self 语义，是纯代码搬移。

对外接口未变：from app.monitoring.server import MonitorWebComponent / MonitorResource"""
from __future__ import annotations


from collections import deque
from datetime import date, datetime
from typing import Any, Deque, Dict, List, Optional
import time

from twisted.internet import defer, reactor, task, threads
from twisted.internet import reactor
from twisted.web.server import Site

from app.common.config import settings
from app.common.logging import logger
from app.engine.component import IBaseComponent
from app.runtime.event_bus import StandardEvents
from app.monitoring.api.backtest import get_backtest_service
from app.monitoring.snapshot import SnapshotStore, content_fingerprint

from app.monitoring.api.overview import OverviewViewsMixin
from app.monitoring.api.live import LiveViewsMixin
from app.monitoring.api.brokers import BrokerViewsMixin
from app.monitoring.api.factors import FactorViewsMixin
from app.monitoring.api.pool import PoolViewsMixin
from app.monitoring.api.collect import CollectViewsMixin
from app.monitoring.api.strategies import StrategyViewsMixin
from app.monitoring.api.backtest import BacktestViewsMixin
from app.monitoring.resource import MonitorResource  # noqa: F401  对外再导出

# MonitorWebComponent 里方法被调用的顺序（MRO）：先各视图 mixin，最后 IBaseComponent。
class MonitorWebComponent(OverviewViewsMixin, LiveViewsMixin, BrokerViewsMixin, FactorViewsMixin, PoolViewsMixin, CollectViewsMixin, StrategyViewsMixin, BacktestViewsMixin, IBaseComponent):
    name = "monitor"

    # ============================================================
    # 快照
    # ============================================================
    #: 快照策略
    #:   on_event  —— 有"操作"才落盘（默认）。没操作 = 状态没变 = 不需要新快照
    #:   on_change —— 定期检查内容指纹，真变了才落盘
    #:   interval  —— 到点就写，不管有没有变化（想要完整时间轴时用）
    #:   off       —— 不写历史快照（面板仍能实时看，读的是内存状态）
    SNAPSHOT_MODES = ("on_event", "on_change", "interval", "off")

    #: 什么算"操作"。列表之外的事件（比如 BAR_RECEIVED 逐根K线、
    #: POSITION_UPDATED 由成交派生）**不算** —— 否则行情一跑，
    #: 状态就在持续变化，等于又回到"按时间刷盘"了。
    TRIGGER_EVENTS = (
        StandardEvents.ORDER_CREATED,
        StandardEvents.ORDER_FILLED,
        StandardEvents.ORDER_CANCELLED,
        StandardEvents.SIGNAL_REJECTED,
        StandardEvents.TASK_ADDED,
        StandardEvents.TASK_REMOVED,
        StandardEvents.TASK_STARTED,
        StandardEvents.TASK_STOPPED,
        StandardEvents.TASK_PAUSED,
        StandardEvents.TASK_RESUMED,
        StandardEvents.COMPONENT_ENABLED,
        StandardEvents.COMPONENT_DISABLED,
        StandardEvents.CONTROL_COMMAND,
    )

    def __init__(
        self,
        engine: Any = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        snapshot_interval: Optional[float] = None,
        snapshot_enabled: Optional[bool] = None,
        snapshot_mode: str = "",
        snapshot_min_gap: Optional[float] = None,
        snapshot_debounce: Optional[float] = None,
        snapshot_dir: Optional[str] = None,
        keep: Optional[int] = None,
        namespace: str = "default",
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.bound_engine = engine
        self.host = str(host or settings.MONITOR_HOST)
        self.port = int(port if port is not None else settings.MONITOR_PORT)
        # 检查间隔：只对 on_change 有意义（"多久看一眼有没有变"），对 on_event 不生效
        self.snapshot_interval = float(
            snapshot_interval if snapshot_interval is not None else settings.MONITOR_SNAPSHOT_INTERVAL
        )
        self.snapshot_enabled = bool(
            settings.MONITOR_SNAPSHOT_ENABLED if snapshot_enabled is None else snapshot_enabled
        )
        mode = str(snapshot_mode or settings.MONITOR_SNAPSHOT_MODE).strip().lower()
        self.snapshot_mode = mode if mode in self.SNAPSHOT_MODES else "on_event"
        self.snapshot_min_gap = float(
            snapshot_min_gap if snapshot_min_gap is not None else settings.MONITOR_SNAPSHOT_MIN_GAP
        )
        #: on_event 的抖动窗口（秒）：操作后等这么久再落盘，把同一批操作并成一份
        self.snapshot_debounce = float(
            snapshot_debounce if snapshot_debounce is not None
            else settings.MONITOR_SNAPSHOT_DEBOUNCE
        )
        self.snapshot_base = str(snapshot_dir) if snapshot_dir else None
        self.keep = keep
        self.namespace = namespace or "default"

        self.store: Optional[SnapshotStore] = None
        self.resource: Optional[MonitorResource] = None
        #: 回测作业管理器（面板上「新建回测」走它）
        self.backtests = get_backtest_service()
        self.audit: Deque[Dict[str, Any]] = deque(maxlen=100)

        self._site: Optional[Site] = None
        self._listening: Any = None
        self._loop: Optional[task.LoopingCall] = None
        self._writing = False
        #: 上一份已落盘内容的指纹。只在"内容变了才写"模式下有意义。
        self._last_fingerprint: str = ""
        self._last_write_at: float = 0.0
        #: on_event 模式的"脏"标记 + 触发原因（合并突发用）
        self._dirty = False
        self._dirty_reasons: Deque[str] = deque(maxlen=20)
        self._flush_call: Any = None
        self.stats: Dict[str, Any] = {
            "writes": 0,
            "skipped_unchanged": 0,
            "skipped_too_soon": 0,
            "skipped_noop": 0,
            "triggers": 0,
            "last_trigger": "",
            "orders": 0,
            "rejections": 0,
            "last_snapshot_at": None,
            "listen_error": "",
            "api_calls": 0,
        }

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        self.host = str(cfg.get("host") or self.host)
        self.port = int(cfg.get("port") or self.port)
        self.snapshot_interval = float(cfg.get("snapshot_interval") or self.snapshot_interval)
        if "snapshot_enabled" in cfg:
            self.snapshot_enabled = bool(cfg["snapshot_enabled"])
        if cfg.get("snapshot_mode"):
            m = str(cfg["snapshot_mode"]).strip().lower()
            if m in self.SNAPSHOT_MODES:
                self.snapshot_mode = m
        if cfg.get("snapshot_min_gap") is not None:
            self.snapshot_min_gap = float(cfg["snapshot_min_gap"])
        if cfg.get("snapshot_debounce") is not None:
            self.snapshot_debounce = float(cfg["snapshot_debounce"])
        self.snapshot_base = str(cfg.get("snapshot_dir") or self.snapshot_base or settings.SNAPSHOT_DIR)
        if cfg.get("namespace"):
            self.namespace = str(cfg["namespace"])
        if cfg.get("keep"):
            self.keep = int(cfg["keep"])

        self.store = SnapshotStore(
            run_id=self.run_id, base_dir=self.snapshot_base, keep=self.keep
        )
        self.resource = MonitorResource(self)

        self.subscribe_event(StandardEvents.ORDER_FILLED, self._on_order)
        self.subscribe_event(StandardEvents.SIGNAL_REJECTED, self._on_rejected)
        self.subscribe_event(StandardEvents.CONTROL_COMMAND, self._on_control)
        # 状态变更类事件 → 标记"该落一份快照了"。
        # **这是 on_event 模式的全部触发来源**：没人动它，就不写盘。
        for event_name in self.TRIGGER_EVENTS:
            self.subscribe_event(event_name, self._make_trigger(event_name))

        logger.info(
            f"监控组件就绪 | 面板 {self.host}:{self.port} | "
            f"快照目录 {self.store.root} | 策略 {self.snapshot_mode}"
        )

    def on_start(self) -> defer.Deferred:
        self._site = Site(self.resource)
        try:
            self._listening = reactor.listenTCP(self.port, self._site, interface=self.host)
            self.stats["listen_error"] = ""
            logger.info(f"🖥️  监控面板已启动: http://{self.host}:{self.port}/")
        except Exception as exc:
            # 端口被占用不该让引擎起不来——监控是"可选增强"，不是"关键路径"
            self._listening = None
            self.stats["listen_error"] = str(exc)
            logger.error(f"监控面板端口 {self.host}:{self.port} 绑定失败，引擎继续运行: {exc}")

        if not self.snapshot_enabled or self.snapshot_mode == "off":
            logger.info(
                f"快照落盘已关闭（模式={self.snapshot_mode}，启用={self.snapshot_enabled}）"
                " | 面板仍可实时查看内存状态"
            )
            return defer.succeed(None)

        if self.snapshot_mode == "on_event":
            # **不装定时器**：没有操作就永远不写盘。
            # 这里同步写一份基线快照，让 run 目录一建立就有 latest.json，
            # 否则重启后翻目录会以为"什么都没发生"。
            self._write_snapshot(reason="engine_start", async_persist=False)
            logger.info(
                "快照策略: on_event（有操作才落盘） | 抖动窗口 "
                f"{self.snapshot_debounce}s | 触发事件: 下单/成交/撤单/拒单、"
                "任务增删改、组件启停、控制指令、引擎启停"
            )
            return defer.succeed(None)

        self._loop = task.LoopingCall(self._tick_snapshot)
        self._loop.start(self.snapshot_interval, now=True)
        logger.info(
            f"快照策略: {self.snapshot_mode} | 检查间隔 {self.snapshot_interval}s | "
            f"最小写入间隔 {self.snapshot_min_gap}s"
            + ("（仅内容变化时落盘）" if self.snapshot_mode == "on_change" else "（到点即写）")
        )
        return defer.succeed(None)

    @defer.inlineCallbacks
    def on_stop(self, graceful: bool = True):
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None
        if self._listening is not None:
            yield defer.maybeDeferred(self._listening.stopListening)
            self._listening = None
        logger.info("监控面板已停止监听")

    def is_busy(self) -> bool:
        """监控组件不参与"引擎是否空闲"的判定。"""
        return False

    def _make_trigger(self, event_name: str):
        """事件 → 标记"该落盘了"。带上事件名是为了让快照里能看出**为什么**写它。"""
        def _handler(**kwargs: Any) -> None:
            self.mark_dirty(event_name)
        return _handler

    def mark_dirty(self, reason: str = "") -> None:
        """标记"状态变了，该落一份快照"。

        **可以从任意线程调用**：成交回报是在工作线程里处理完再发布的。
        真正的排程必须回到 reactor 线程（callLater 不是线程安全的）。
        """
        self._dirty = True
        self.stats["triggers"] += 1
        if reason:
            self._dirty_reasons.append(reason)
        if self.snapshot_mode != "on_event" or not self.snapshot_enabled:
            return
        try:
            reactor.callFromThread(self._schedule_flush)
        except Exception as exc:  # pragma: no cover - reactor 未就绪时兜底
            logger.debug(f"快照排程失败（忽略）: {exc}")

    def _schedule_flush(self) -> None:
        """（reactor 线程）安排一次落盘。已在等待中的话什么都不做 ——

        这就是"合并突发"：一次回放可能瞬间产生上百笔订单，
        等抖动窗口过去只写一份，而不是写一百份。
        """
        if self._flush_call is not None and self._flush_call.active():
            return
        self._flush_call = reactor.callLater(
            max(0.0, self.snapshot_debounce), self._do_event_flush
        )

    def _do_event_flush(self) -> None:
        self._flush_call = None
        if not self._dirty or not self.snapshot_enabled or self.snapshot_mode != "on_event":
            return
        if self.store is None or self.bound_engine is None:
            return
        if self._writing:
            # 上一份还没落完：保持脏标记，稍后再试（不丢，只是晚一点）
            self._schedule_flush()
            return
        self._dirty = False
        reasons = sorted(set(self._dirty_reasons))
        self._dirty_reasons.clear()
        self._write_snapshot(reason="+".join(reasons) or "event")

    def _snapshot_ready(self) -> bool:
        return bool(
            self.snapshot_enabled
            and self.snapshot_mode != "off"
            and self.store is not None
            and self.bound_engine is not None
        )

    def _write_snapshot(self, reason: str = "", force: bool = False,
                        async_persist: bool = True):
        """采集 + 落盘。**采集必须在 reactor 线程**（那时遍历任务字典才安全），

        落盘是纯 IO，默认丢线程池，避免磁盘慢的时候卡住交易链路。
        """
        if not self._snapshot_ready():
            return None
        try:
            snap = self.store.capture(self.bound_engine, extra=self._extra())
        except Exception as exc:
            logger.error(f"快照采集失败: {exc}", exc_info=True)
            return None

        # 指纹必须在注入 reason/seq **之前**算：那些字段每份都不同，
        # 先注入再算的话"内容一样就不写"永远不成立。
        fp = content_fingerprint(snap)
        if not force and fp == self._last_fingerprint:
            self.stats["skipped_unchanged"] += 1
            self.stats["skipped_noop"] += 1
            return None

        self._last_fingerprint = fp
        self._last_write_at = time.monotonic()
        snap["engine"]["snapshot_seq"] = self.store.bump_seq()
        snap["engine"]["snapshot_reason"] = reason or "unspecified"
        self.stats["writes"] += 1
        self.stats["last_snapshot_at"] = snap.get("captured_at")
        self.stats["last_trigger"] = reason

        if async_persist:
            self._writing = True
            try:
                d = threads.deferToThread(self.store.persist, snap)
            except Exception as exc:
                self._writing = False
                logger.error(f"快照落盘派发失败: {exc}", exc_info=True)
                return None
            d.addBoth(self._after_write)
            return d

        self._writing = False
        try:
            path = self.store.persist(snap)
        except Exception as exc:
            logger.error(f"快照落盘失败: {exc}", exc_info=True)
            return None
        self._after_write(path)
        return path

    def _tick_snapshot(self):
        """定时检查点（on_change / interval 模式用）。**不等于"每隔 N 秒写一份"**。"""
        if self._writing:
            logger.debug("上一份快照尚未落盘，跳过本轮")
            return defer.succeed(None)
        if self.snapshot_mode == "interval":
            return self._write_snapshot(reason="interval", force=True)
        # on_change：内容没变就不写（判断在 _write_snapshot 里）
        now = time.monotonic()
        gap = self.snapshot_min_gap
        if gap > 0 and (now - self._last_write_at) < gap:
            self.stats["skipped_too_soon"] += 1
            return defer.succeed(None)
        return self._write_snapshot(reason="on_change")

    def _after_write(self, result: Any) -> Any:
        self._writing = False
        if isinstance(result, Exception):
            logger.error(f"快照落盘失败: {result}")
            return None
        try:
            self.event_bus.publish(
                StandardEvents.SNAPSHOT_SAVED,
                path=str(result),
                run_id=self.run_id,
            )
        except Exception:
            pass
        return result

    def flush(self, force: bool = True):
        """同步写一份快照（引擎停止时调用，保证最后一次状态不丢）。

        :param force: True=不管有没有变化都写（默认，用于停止时收尾）。
                      False=遵守变更检测，仅在内容变化时写。
        """
        if self._flush_call is not None and self._flush_call.active():
            try:
                self._flush_call.cancel()
            except Exception:
                pass
            self._flush_call = None
        self._dirty = False
        self._dirty_reasons.clear()
        return self._write_snapshot(
            reason="engine_stop", force=force, async_persist=False
        )

    def set_snapshot_mode(self, mode: str, enabled: Optional[bool] = None) -> Dict[str, Any]:
        """运行时切换快照策略（面板/CLI 可调，不用重启）。"""
        m = str(mode or "").strip().lower()
        if m not in self.SNAPSHOT_MODES:
            return {"ok": False, "error": f"未知快照模式 {mode!r}，可用: {self.SNAPSHOT_MODES}"}
        self.snapshot_mode = m
        if enabled is not None:
            self.snapshot_enabled = bool(enabled)

        # 让 Loop 的启停跟上新策略：on_event 模式**不该有定时器**
        # （有定时器就会有人误以为"它还在按时间写"）。
        want_loop = self.snapshot_enabled and m in ("on_change", "interval")
        if want_loop and self._loop is None:
            self._loop = task.LoopingCall(self._tick_snapshot)
            self._loop.start(self.snapshot_interval, now=False)
        elif not want_loop and self._loop is not None:
            if self._loop.running:
                self._loop.stop()
            self._loop = None

        if m == "on_event":
            # 切到事件驱动时，把"当前状态"先写一份作为基线
            self._write_snapshot(reason="mode_switch", async_persist=False)

        logger.info(
            f"快照策略已切换 | 模式={m} | 启用={self.snapshot_enabled} | "
            f"检查间隔={self.snapshot_interval}s | 抖动窗口={self.snapshot_debounce}s"
        )
        return {
            "ok": True, "mode": m, "enabled": self.snapshot_enabled,
            "interval": self.snapshot_interval, "min_gap": self.snapshot_min_gap,
            "debounce": self.snapshot_debounce,
        }

    def _extra(self) -> Dict[str, Any]:
        return {
            "service": self.namespace,
            "monitor_port": self.port,
            "listen_ok": self._listening is not None,
        }

    # ============================================================
    # 事件订阅
    # ============================================================
    def _on_order(self, order: Any = None, **kwargs: Any) -> None:
        if order is None:
            return
        self.stats["orders"] += 1
        if self.store is not None:
            self.store.note_order(order)

    def _on_rejected(self, order: Any = None, reason: str = "", **kwargs: Any) -> None:
        self.stats["rejections"] += 1
        if order is not None and self.store is not None:
            self.store.note_order(order)

    def _on_control(self, action: str = "", detail: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self.audit.append({
            "at": datetime.now().isoformat(),
            "action": action,
            "detail": dict(detail or {}),
        })

    # ============================================================
    # 对外数据
    # ============================================================
    @property
    def run_id(self) -> str:
        """本次运行的标识。

        **单一事实来源**：以引擎上下文的 run_id 为准。面板上的 run_id、快照目录名、
        SNAPSHOT_SAVED 事件里的 run_id 必须永远是同一个值——否则页面上会同时出现
        两个 run_id（一个来自引擎快照、一个来自监控组件），运维根本没法判断该去看哪个目录。
        组件自身的 context 只在拿不到引擎时兜底。
        """
        ctx = getattr(self.bound_engine, "context", None) if self.bound_engine is not None else None
        run_id = getattr(ctx, "run_id", "") if ctx is not None else ""
        if run_id:
            return str(run_id)
        return self.context.run_id if self.context else ""

    @property
    def control(self):
        return self.bound_engine.control if self.bound_engine is not None else None

    def snapshot(self) -> Dict[str, Any]:
        return self.stats_dict()
