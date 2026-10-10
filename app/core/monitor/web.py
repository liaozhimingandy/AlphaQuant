#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : web.py
# @Description : 内嵌网页监控组件：在引擎自己的 reactor 上挂一个 Twisted Web Site
#               直接读引擎内存状态（零拷贝延迟），控制指令即时生效，零额外进程
#               通过 /api/* 暴露 JSON，既给面板用，也给 CLI / hub 用
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from twisted.internet import defer, reactor, task, threads
from twisted.web.resource import Resource
from twisted.web.server import Site

from app.core.config import settings
from app.core.engine.components.ibase import IBaseComponent
from app.core.engine.event import StandardEvents
from app.core.monitor.backtest_api import get_backtest_service
from app.core.monitor.dashboard import DASHBOARD_HTML
from app.core.monitor.snapshot import SnapshotStore, content_fingerprint
from app.repository.trading_repository import TradingRepository, get_repository
from app.utils.jsonio import dumps, tail_lines
from app.utils.logger import logger


class MonitorResource(Resource):
    """面板的路由与 JSON API。

    单 Resource 手工分发路径，而不是拆成几十个 Resource 类：
    接口数量有限，手工分发的可读性和可调试性都更好。
    """

    isLeaf = True

    def __init__(self, component: "MonitorWebComponent") -> None:
        Resource.__init__(self)
        self.component = component

    # ---------------- 输出助手 ----------------
    def _json(self, request, payload: Any, status: int = 200) -> bytes:
        request.setResponseCode(status)
        request.setHeader(b"content-type", b"application/json; charset=utf-8")
        request.setHeader(b"cache-control", b"no-store")
        return dumps(payload).encode("utf-8")

    def _text(self, request, text: str, ctype: bytes, status: int = 200) -> bytes:
        request.setResponseCode(status)
        request.setHeader(b"content-type", ctype)
        request.setHeader(b"cache-control", b"no-store")
        return text.encode("utf-8")

    @staticmethod
    def _segments(request) -> List[str]:
        path = request.path.decode("utf-8", "replace")
        return [s for s in path.split("/") if s]

    @staticmethod
    def _q(request, name: str, default: Any = None) -> Any:
        vals = request.args.get(name.encode("utf-8"))
        if not vals:
            return default
        return vals[0].decode("utf-8", "replace")

    # ---------------- GET ----------------
    def render_GET(self, request):  # noqa: N802
        seg = self._segments(request)
        try:
            return self._route_get(request, seg)
        except Exception as exc:
            logger.error(f"面板 GET {request.path!r} 处理失败: {exc}", exc_info=True)
            return self._json(request, {"error": str(exc)}, 500)

    def _route_get(self, request, seg: List[str]):
        c = self.component

        if not seg:
            return self._text(request, DASHBOARD_HTML, b"text/html; charset=utf-8")
        if seg[0] == "favicon.ico":
            request.setResponseCode(204)
            return b""

        if seg[0] != "api":
            return self._json(request, {"error": "not found", "path": request.path.decode()}, 404)

        rest = seg[1:]
        if not rest:
            return self._json(request, {"endpoints": [
                "/api/overview", "/api/tasks", "/api/tasks/<id>", "/api/orders",
                "/api/logs", "/api/snapshots", "/api/audit", "/api/health",
                "/api/collector",
            ]})

        head = rest[0]
        if head == "health":
            return self._json(request, {"ok": True, "run_id": c.run_id})
        if head == "overview":
            return self._json(request, c.build_overview())
        if head == "tasks":
            if len(rest) >= 2:
                detail = c.control.task_detail(rest[1])
                if detail is None:
                    return self._json(request, {"error": f"任务不存在: {rest[1]}"}, 404)
                return self._json(request, detail)
            return self._json(request, {"tasks": c.control.list_tasks()})
        if head == "orders":
            limit = int(self._q(request, "limit", 100) or 100)
            return self._json(request, {"orders": c.order_feed(limit)})
        if head == "logs":
            n = int(self._q(request, "n", 200) or 200)
            lines, source = c.log_tail(n)
            return self._json(request, {"lines": lines, "source": source})
        if head == "snapshots":
            return self._json(request, {
                "current": c.run_id,
                "run_dir": str(c.store.root) if c.store else None,
                "runs": SnapshotStore.runs(c.snapshot_base),
            })
        if head == "audit":
            return self._json(request, {"audit": list(c.audit)})
        if head == "collector":
            return self._json(request, c.control.collector_status())

        # ---- 回测监控 ----
        if head == "backtest":
            if len(rest) >= 2 and rest[1].isdigit():
                detail = TradingRepository.backtest_detail(int(rest[1]))
                if detail is None:
                    return self._json(request, {"error": f"回测记录不存在: {rest[1]}"}, 404)
                return self._json(request, detail)
            return self._json(request, {
                "jobs": c.backtests.jobs(int(self._q(request, "limit", 30) or 30)),
                "results": TradingRepository.backtests(
                    limit=int(self._q(request, "limit", 30) or 30),
                    symbol=self._q(request, "symbol", "") or "",
                    strategy=self._q(request, "strategy", "") or "",
                ),
                "stats": c.backtests.stats_dict(),
            })

        # ---- 实盘监控 ----
        if head == "live":
            return self._json(request, c.build_live_overview())

        # ---- 数据库查询（订单/成交/事件/运行）----
        if head == "db":
            return self._json(request, self._db_query(request, rest[1:]))

        # ---- 券商接入点（脱敏后的配置视图）----
        if head == "endpoints":
            return self._json(request, c.endpoints_view())

        # ---- 配置（日志级别、快照策略等）----
        if head == "config":
            return self._json(request, c.build_config_view())

        # ---- 能力清单（面板的下拉框从这里取，不再硬编码）----
        if head == "strategies":
            return self._json(request, c.build_capabilities())

        return self._json(request, {"error": f"未知接口: /{'/'.join(seg)}"}, 404)

    def _db_query(self, request, rest: List[str]) -> Dict[str, Any]:
        """查 SQLite 里的交易数据。面板上"这一笔到底发生了什么"靠它。"""
        import app.core.monitor.web as _self  # noqa: F401 - 保持符号可 monkeypatch

        if not rest:
            return {"endpoints": ["orders", "trades", "events", "equity", "runs", "stats"]}
        what = rest[0]
        task_id = self._q(request, "task_id", "") or ""
        run_id = self._q(request, "run_id", "") or ""
        limit = int(self._q(request, "limit", 200) or 200)
        try:
            if what == "orders":
                rows = TradingRepository.recent_orders(
                    task_id=task_id, run_id=run_id,
                    status=self._q(request, "status", "") or "", limit=limit,
                )
                return {"orders": rows, "stats": TradingRepository.order_stats(task_id, run_id)}
            if what == "trades":
                return {"trades": TradingRepository.recent_trades(
                    task_id=task_id, run_id=run_id, limit=limit)}
            if what == "events":
                return {"events": TradingRepository.events(
                    run_id=run_id,
                    category=self._q(request, "category", "") or "",
                    level=self._q(request, "level", "") or "",
                    limit=limit,
                )}
            if what == "equity":
                return {"points": TradingRepository.equity_curve(task_id, limit=limit)}
            if what == "runs":
                return {"runs": TradingRepository.runs(limit=limit)}
            if what == "stats":
                return {
                    "orders": TradingRepository.order_stats(task_id, run_id),
                    "persist": get_repository().health(),
                }
        except Exception as exc:
            return {"error": f"查询失败: {exc}"}
        return {"error": f"未知数据: {what}"}

    # ---------------- POST ----------------
    def render_POST(self, request):  # noqa: N802
        seg = self._segments(request)
        body = self._read_body(request)
        if isinstance(body, dict) and body.get("__parse_error__"):
            return self._json(request, {"ok": False, "error": body["__parse_error__"]}, 400)

        try:
            return self._route_post(request, seg, body)
        except Exception as exc:
            logger.error(f"面板 POST {request.path!r} 处理失败: {exc}", exc_info=True)
            return self._json(request, {"ok": False, "error": str(exc)}, 500)

    @staticmethod
    def _read_body(request) -> Dict[str, Any]:
        raw = request.content.read()
        if not raw:
            return {}
        import json

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return {"__parse_error__": f"请求体不是合法 JSON: {exc}"}
        if isinstance(data, list):
            return {"spec": data} if data else {}
        return data if isinstance(data, dict) else {}

    def _route_post(self, request, seg: List[str], body: Dict[str, Any]):
        c = self.component
        if len(seg) < 2 or seg[0] != "api":
            return self._json(request, {"ok": False, "error": "not found"}, 404)

        # 统一指令入口
        if seg[1] == "command":
            return self._json(request, c.control.apply_command(body))

        # POST /api/tasks  → 新增任务（body 即任务 spec）
        if seg[1] == "tasks" and len(seg) == 2:
            spec = body.get("spec") if "spec" in body else body
            if not isinstance(spec, dict) or not spec:
                return self._json(request, {"ok": False, "error": "缺少任务配置"}, 400)
            return self._json(request, c.control.apply_command({"action": "add_task", "spec": spec}))

        # POST /api/tasks/<id>/<op>
        if seg[1] == "tasks" and len(seg) >= 4:
            task_id, op = seg[2], seg[3]
            mapping = {"pause": "pause_task", "resume": "resume_task",
                       "remove": "remove_task", "start": "start_task", "stop": "stop_task"}
            if op not in mapping:
                return self._json(request, {"ok": False, "error": f"未知操作: {op}"}, 400)
            return self._json(request, c.control.apply_command(
                {"action": mapping[op], "task_id": task_id}))

        # POST /api/components/<name>/(enable|disable|snapshot)
        if seg[1] == "components" and len(seg) >= 4:
            name, op = seg[2], seg[3]
            if op in ("enable", "disable"):
                return self._json(request, c.control.apply_command(
                    {"action": f"{op}_component", "component": name}))
            if op == "snapshot":
                return self._json(request, c.control.apply_command({"action": "snapshot"}))
            return self._json(request, {"ok": False, "error": f"未知操作: {op}"}, 400)

        # ---- 回测作业 ----
        if seg[1] == "backtest":
            if len(seg) >= 3 and seg[2] == "run":
                return self._json(request, c.submit_backtest(body))
            if len(seg) >= 3 and seg[2] == "jobs":
                return self._json(request, {"jobs": c.backtests.jobs()})

        # ---- 快照策略 ----
        if seg[1] == "snapshot" and len(seg) >= 3 and seg[2] == "mode":
            return self._json(request, c.set_snapshot_mode(
                str(body.get("mode") or ""), body.get("enabled")
            ))

        # ---- 实盘操作：立即对账 / 撤单 ----
        if seg[1] == "live":
            op = seg[2] if len(seg) >= 3 else ""
            if op == "reconcile":
                return self._json(request, c.reconcile_now())
            if op == "cancel-all":
                return self._json(request, c.cancel_all_orders(
                    str(body.get("task_id") or "")
                ))
            if op == "refresh":
                # 去券商拉一次账户/持仓。这是秒级网络 IO，必须异步。
                return self._async(request, threads.deferToThread(
                    c.refresh_live_account
                ))

        # POST /api/endpoints/check —— 试连接一个接入点（不落单，只查询）
        if seg[1] == "endpoints" and len(seg) >= 3 and seg[2] == "check":
            return self._async(request, threads.deferToThread(
                c.check_endpoint, str(body.get("endpoint") or "")
            ))

        # POST /api/strategies/reload —— 重新扫描用户策略目录（热加载）
        if seg[1] == "strategies" and len(seg) >= 3 and seg[2] == "reload":
            return self._json(request, c.reload_strategies())

        # POST /api/collector/(now|interval|add|remove|reload)
        if seg[1] == "collector" and len(seg) >= 3:
            op = seg[2]
            if op == "now":
                # 采集是秒级网络 IO，必须异步：同步返回会把 reactor 连同
                # 行情、策略、撮合、面板一起堵住。这里挂 Deferred 延迟写响应。
                return self._async(request, c.control.collector_now_async(
                    str(body.get("symbol") or "")
                ))
            simple = {
                "interval": "collector_interval",
                "add": "collector_add",
                "remove": "collector_remove",
                "reload": "collector_reload",
            }
            if op in simple:
                payload = dict(body)
                payload["action"] = simple[op]
                if op == "add" and "symbol" not in payload:
                    return self._json(request, {"ok": False, "error": "缺少 symbol"}, 400)
                return self._json(request, c.control.apply_command(payload))
            return self._json(request, {"ok": False, "error": f"未知操作: {op}"}, 400)

        # POST /api/engine/stop
        if seg[1] == "engine" and len(seg) >= 3:
            if seg[2] == "stop":
                graceful = bool(body.get("graceful", True))
                c.control.shutdown(graceful=graceful, reason="panel")
                return self._json(request, {"ok": True})
            if seg[2] == "snapshot":
                return self._json(request, c.control.apply_command({"action": "snapshot"}))

        return self._json(request, {"ok": False, "error": f"未知接口: /{'/'.join(seg)}"}, 404)

    def _async(self, request, d):
        """把 Deferred 的结果写回响应。

        NOT_DONE_YET 期间 reactor 是自由的：这段时间可以同时处理行情、
        撮合和其他 API 请求，不会像同步阻塞那样让整台服务停摆。
        """
        from twisted.web.server import NOT_DONE_YET

        def _ok(result):
            try:
                request.write(self._json(request, result))
            except Exception:
                pass
            if not request.finished:
                request.finish()

        def _fail(failure_obj):
            msg = str(failure_obj.getErrorMessage()) if hasattr(failure_obj, "getErrorMessage") else str(failure_obj)
            try:
                request.write(self._json(request, {"ok": False, "error": msg}, 500))
            except Exception:
                pass
            if not request.finished:
                request.finish()

        d.addCallbacks(_ok, _fail)
        return NOT_DONE_YET


class MonitorWebComponent(IBaseComponent):
    """把监控面板与快照落盘做成一个标准引擎组件。

    - 面板挂在引擎自己的 reactor 上 → 读的是**内存里的真状态**，不是二手副本
    - 控制指令直接调用 ControlCenter → 与 CLI/hub 共用同一套语义
    - 快照采集在 reactor 线程完成、落盘丢给线程池 → 既不阻塞交易，也不会读到半截文件
    """

    name = "monitor"

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

    def build_overview(self) -> Dict[str, Any]:
        self.stats["api_calls"] += 1
        control = self.control
        engine_snap: Dict[str, Any] = {}
        if self.bound_engine is not None:
            try:
                engine_snap = self.bound_engine.snapshot().get("engine", {})
            except Exception as exc:
                engine_snap = {"error": str(exc)}

        tasks = control.list_tasks() if control is not None else []
        symbols = set()
        for t in tasks:
            if t.get("symbol"):
                symbols.add(t["symbol"])

        return {
            "engine": engine_snap,
            "components": control.component_states() if control is not None else [],
            "tasks": tasks,
            "task_count": len(tasks),
            "symbol_count": len(symbols),
            "orders": len(self.store.recent_orders(10_000)) if self.store else 0,
            "monitor": self.stats_dict(),
            "audit": list(self.audit),
        }

    def order_feed(self, limit: int = 100) -> List[Dict[str, Any]]:
        if self.store is None:
            return []
        rows = self.store.order_history(limit)
        if rows:
            return rows
        return self.store.recent_orders(limit)

    # ============================================================
    # 回测
    # ============================================================
    def submit_backtest(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """提交一个回测作业。

        在线程池里跑 —— 回测是 CPU 密集且可能几十秒的操作，
        跑在 reactor 线程上会让行情、策略、撮合、面板一起停摆。
        """
        params = body.get("params") if isinstance(body.get("params"), dict) else body
        if not params.get("symbol"):
            return {"ok": False, "error": "缺少 symbol"}
        job = self.backtests.submit(params, executor=self._run_backtest_in_thread)
        return {"ok": True, "job": job.to_dict()}

    def _run_backtest_in_thread(self, job_id: str) -> None:
        from twisted.internet import reactor

        d = threads.deferToThread(self.backtests.run_job, job_id)
        d.addErrback(lambda f: logger.error(f"回测作业异常: {f.getErrorMessage()}"))
        return d

    # ============================================================
    # 实盘
    # ============================================================
    def build_live_overview(self) -> Dict[str, Any]:
        """实盘总览：网关状态、对账结论、未结订单。"""
        gw = None
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
            except Exception:
                gw = None
        if gw is None:
            return {
                "available": False,
                "reason": "本引擎未启用实盘网关（运行模式非 LIVE）",
                "mode": (self.bound_engine.snapshot().get("engine", {}).get("mode")
                         if self.bound_engine else ""),
            }
        out = {"available": True}
        out.update(json_safe(gw.snapshot()))
        # 未结订单按任务汇总：实盘最需要盯的就是"挂在外面还没成交的单"
        pending = []
        try:
            for o in TradingRepository.recent_orders(limit=500):
                if o.get("status") in ("PENDING", "SUBMITTED", "PARTIAL"):
                    pending.append(o)
        except Exception as exc:
            logger.debug(f"查询未结订单失败: {exc}")
        out["pending_orders"] = pending[:200]
        out["pending_count"] = len(pending)
        return out

    def reconcile_now(self) -> Dict[str, Any]:
        gw = None
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
            except Exception:
                gw = None
        if gw is None:
            return {"ok": False, "error": "本引擎未启用实盘网关"}
        try:
            return {"ok": True, **gw.reconcile_now()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def refresh_live_account(self) -> Dict[str, Any]:
        """让运行中的实盘网关立刻去券商拉一次账户/持仓。"""
        gw = None
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
            except Exception:
                gw = None
        if gw is None:
            return {"ok": False, "error": "本引擎未启用实盘网关"}
        try:
            data = gw.refresh_account()
            self.audit.append({
                "at": datetime.now().isoformat(), "action": "refresh_account",
                "detail": {"positions": len(data.get("positions") or {})},
            })
            return {"ok": True, **data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ============================================================
    # 券商接入点
    # ============================================================
    def endpoints_view(self) -> Dict[str, Any]:
        """接入点配置视图（凭据已由 endpoints 层脱敏）。"""
        from app.core.execution.endpoints import describe_endpoints
        from app.core.execution.gateway import gateway_signature, list_gateways

        try:
            data = describe_endpoints()
        except Exception as exc:
            return {"error": str(exc), "endpoints": []}
        # 标注"当前引擎连的是哪个" —— 光看配置列表判断不出正在用的是谁
        if self.bound_engine is not None:
            try:
                gw = self.bound_engine.get_component("live_gateway")
                data["in_use"] = getattr(gw, "endpoint_id", "") if gw else ""
            except Exception:
                data["in_use"] = ""
        data["gateways"] = [
            {"name": g, "params": gateway_signature(g)} for g in list_gateways()
        ]
        return data

    def check_endpoint(self, endpoint_id: str = "") -> Dict[str, Any]:
        """试连接一个接入点：**只查询，不下单**。

        工作线程里跑（调用方用 deferToThread 包起来），因为它会做网络 IO。
        用途很具体：改完 brokers.json 先验一下能不能连上、账号对不对，
        别等到开了实盘才发现连错账户。
        """
        from app.core.execution.endpoints import load_endpoints

        try:
            cfg = load_endpoints()
        except Exception as exc:
            return {"ok": False, "error": f"读取接入点配置失败: {exc}"}
        try:
            endpoint = cfg.resolve(endpoint_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if endpoint is None:
            return {
                "ok": False,
                "error": f"未找到接入点（配置 {cfg.path} 里没有 default_endpoint）",
                "available": [e.id for e in cfg.list()],
            }
        if not endpoint.enabled:
            return {"ok": False, "error": f"接入点 {endpoint.id} 已禁用"}

        from app.core.execution.gateway import create_gateway, has_gateway, list_gateways

        if not has_gateway(endpoint.gateway):
            return {
                "ok": False,
                "error": f"网关 {endpoint.gateway!r} 未注册",
                "available_gateways": list_gateways(),
            }
        gw = None
        try:
            gw = create_gateway(endpoint.gateway, **endpoint.gateway_kwargs())
            gw.connect()
            acct = gw.query_account()
            poss = gw.query_positions()
            return {
                "ok": True,
                "endpoint": endpoint.to_dict(),
                "gateway": getattr(gw, "name", endpoint.gateway),
                "account": acct.to_dict() if hasattr(acct, "to_dict") else {},
                "positions": {
                    sym: {"size": int(p.size), "avg_price": float(p.avg_price),
                          "sellable": int(getattr(p, "sellable", p.size))}
                    for sym, p in (poss or {}).items()
                },
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if gw is not None:
                try:
                    gw.disconnect()
                except Exception:
                    pass

    def cancel_all_orders(self, task_id: str = "") -> Dict[str, Any]:
        """撤销未结订单。实盘出事时最常用的一个动作。"""
        sm = None
        if self.bound_engine is not None:
            try:
                sm = self.bound_engine.get_component("strategy_manager")
            except Exception:
                sm = None
        runtime = getattr(sm, "runtime", None) if sm is not None else None
        if runtime is None:
            return {"ok": False, "error": "策略中枢不可用"}
        n = 0
        for t in list(runtime):
            if task_id and getattr(t, "task_id", "") != task_id:
                continue
            try:
                n += t.cancel_open_orders()
            except Exception as exc:
                logger.debug(f"任务 {getattr(t, 'task_id', '?')} 撤单失败: {exc}")
        self.audit.append({
            "at": datetime.now().isoformat(), "action": "cancel_all_orders",
            "detail": {"task_id": task_id or "*", "count": n},
        })
        logger.warning(f"面板下发撤单指令 | 任务={task_id or '*'} | 已请求 {n} 笔")
        return {"ok": True, "cancelled": n}

    # ============================================================
    # 配置视图
    # ============================================================
    def build_capabilities(self) -> Dict[str, Any]:
        """能力清单。面板的下拉框从这里取 —— 硬编码一份清单迟早和代码不一致，
        用户会看到一个"填了却报未注册"的策略名。

        也把**接入点**与**回测默认参数**一并返回：面板要能显示"我现在连的是哪个账户"，
        以及"表单里没填的字段默认是什么"。
        """
        from app.backtest.defaults import BacktestDefaults
        from app.backtest.registry import (
            list_strategies as bt_strategies,
            user_strategy_dir,
            user_strategy_names,
        )
        from app.core.execution.endpoints import describe_endpoints
        from app.core.execution.gateway import gateway_signature, list_gateways
        from app.core.factor import list_factors
        from app.core.indicator.registry import describe_indicator, list_indicators
        from app.core.rule import RULE_TYPES
        from app.core.risk import list_risks
        from app.core.strategy import list_strategies as live_strategies

        def _safe(fn, default):
            try:
                return fn()
            except Exception as exc:
                logger.debug(f"能力清单读取失败: {exc}")
                return default

        indicators = _safe(list_indicators, [])
        gateways = _safe(list_gateways, [])
        return {
            "backtest_strategies": _safe(bt_strategies, []),
            "user_strategies": _safe(user_strategy_names, []),
            "user_strategy_dir": _safe(user_strategy_dir, ""),
            "live_strategies": _safe(live_strategies, []),
            "indicators": [
                {"name": n, "params": _safe(lambda n=n: describe_indicator(n), {})}
                for n in indicators
            ],
            "factors": _safe(list_factors, []),
            "risk_rules": _safe(list_risks, []),
            "rule_types": list(_safe(lambda: RULE_TYPES, ())),
            "gateways": [
                {"name": g, "params": _safe(lambda g=g: gateway_signature(g), {})}
                for g in gateways
            ],
            "endpoints": _safe(describe_endpoints, {}),
            "backtest_defaults": _safe(lambda: BacktestDefaults.load().to_dict(), {}),
            "snapshot_modes": list(self.SNAPSHOT_MODES),
        }

    # ============================================================
    # 自定义策略热加载
    # ============================================================
    def reload_strategies(self) -> Dict[str, Any]:
        """重新扫描用户策略目录。

        改完策略代码不想重启服务时用。**只重扫目录、不动已有任务** ——
        已经建好的任务持有的仍是旧策略实例（换策略要重建任务，
        因为策略内部可能已经积累了状态）。
        """
        out: Dict[str, Any] = {"ok": True}
        try:
            from app.backtest.registry import load_user_strategies as bt_load

            out["backtest"] = sorted(bt_load(force=True))
        except Exception as exc:
            out["backtest_error"] = str(exc)
        try:
            from app.core.strategy.registry import load_user_strategies as live_load

            out["engine"] = sorted(live_load(force=True))
        except Exception as exc:
            out["engine_error"] = str(exc)
        self.audit.append({
            "at": datetime.now().isoformat(), "action": "reload_strategies",
            "detail": {"backtest": out.get("backtest", []),
                       "engine": out.get("engine", [])},
        })
        logger.info(f"用户策略已重新扫描 | 回测 {out.get('backtest')} | 引擎 {out.get('engine')}")
        return out

    def build_config_view(self) -> Dict[str, Any]:
        """当前生效的运行时配置。面板据此显示"我现在是什么档位"。"""
        from app.utils.logger import is_debug, throttle_stats

        return {
            "log": {
                "level": settings.LOG_LEVEL,
                "file_level": settings.LOG_FILE_LEVEL or settings.LOG_LEVEL,
                "debug_mode": is_debug(),
                "retention": settings.LOG_RETENTION,
                "rotation": settings.LOG_ROTATION,
                "throttle_window": settings.LOG_THROTTLE_WINDOW,
                "throttled": throttle_stats(),
                "dir": str(settings.LOG_DIR),
            },
            "snapshot": {
                "enabled": self.snapshot_enabled,
                "mode": self.snapshot_mode,
                "check_interval": self.snapshot_interval,
                "min_gap": self.snapshot_min_gap,
                "debounce": self.snapshot_debounce,
                "trigger_events": list(self.TRIGGER_EVENTS),
                "writes": self.stats["writes"],
                "triggers": self.stats["triggers"],
                "last_trigger": self.stats["last_trigger"],
                "skipped_unchanged": self.stats["skipped_unchanged"],
                "skipped_too_soon": self.stats["skipped_too_soon"],
                "pending": bool(self._dirty),
            },
            "persist": get_repository().health(),
            "engine": (self.bound_engine.snapshot().get("engine", {})
                       if self.bound_engine else {}),
        }

    def log_tail(self, n: int = 200):
        today = Path(settings.LOG_DIR) / f"alphaquant_{date.today().strftime('%Y-%m-%d')}.log"
        if today.exists():
            return tail_lines(today, n), today.name
        daemon = Path(settings.DAEMON_LOG)
        if daemon.exists():
            return tail_lines(daemon, n), daemon.name
        cands = sorted(Path(settings.LOG_DIR).glob("alphaquant_*.log"))
        if cands:
            return tail_lines(cands[-1], n), cands[-1].name
        return [], "未找到日志文件"

    def stats_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "snapshot_interval": self.snapshot_interval,
            "snapshot_enabled": self.snapshot_enabled,
            "snapshot_mode": self.snapshot_mode,
            "snapshot_min_gap": self.snapshot_min_gap,
            "snapshot_debounce": self.snapshot_debounce,
            "writes": self.stats["writes"],
            "triggers": self.stats["triggers"],
            "last_trigger": self.stats["last_trigger"],
            "skipped_unchanged": self.stats["skipped_unchanged"],
            "skipped_too_soon": self.stats["skipped_too_soon"],
            "snapshot_pending": bool(self._dirty),
            "orders": self.stats["orders"],
            "rejections": self.stats["rejections"],
            "last_snapshot_at": self.stats["last_snapshot_at"],
            "run_id": self.run_id,
            "namespace": self.namespace,
            "run_dir": str(self.store.root) if self.store else None,
            "listen_ok": self._listening is not None,
            "listen_error": self.stats.get("listen_error", ""),
            "api_calls": self.stats["api_calls"],
        }

    def snapshot(self) -> Dict[str, Any]:
        return self.stats_dict()


__all__ = ["MonitorWebComponent", "MonitorResource"]
