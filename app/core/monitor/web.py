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
from app.core.monitor.dashboard import DASHBOARD_HTML
from app.core.monitor.snapshot import SnapshotStore
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
        return self._json(request, {"error": f"未知接口: /{'/'.join(seg)}"}, 404)

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
        snapshot_enabled: bool = True,
        snapshot_dir: Optional[str] = None,
        keep: Optional[int] = None,
        namespace: str = "default",
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.bound_engine = engine
        self.host = str(host or settings.MONITOR_HOST)
        self.port = int(port if port is not None else settings.MONITOR_PORT)
        self.snapshot_interval = float(
            snapshot_interval if snapshot_interval is not None else settings.MONITOR_SNAPSHOT_INTERVAL
        )
        self.snapshot_enabled = bool(snapshot_enabled)
        self.snapshot_base = str(snapshot_dir) if snapshot_dir else None
        self.keep = keep
        self.namespace = namespace or "default"

        self.store: Optional[SnapshotStore] = None
        self.resource: Optional[MonitorResource] = None
        self.audit: Deque[Dict[str, Any]] = deque(maxlen=100)

        self._site: Optional[Site] = None
        self._listening: Any = None
        self._loop: Optional[task.LoopingCall] = None
        self._writing = False
        self.stats: Dict[str, Any] = {
            "writes": 0,
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

        logger.info(
            f"监控组件就绪 | 面板 {self.host}:{self.port} | "
            f"快照目录 {self.store.root} | 间隔 {self.snapshot_interval}s"
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

        if self.snapshot_enabled:
            self._loop = task.LoopingCall(self._tick_snapshot)
            self._loop.start(self.snapshot_interval, now=True)
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
    def _tick_snapshot(self):
        if self.store is None or self.bound_engine is None:
            return defer.succeed(None)
        if self._writing:
            # 上一份还没落完（磁盘慢），跳过本轮，避免堆积
            logger.debug("上一份快照尚未落盘，跳过本轮")
            return defer.succeed(None)

        self._writing = True
        try:
            # 采集必须在 reactor 线程：此时遍历任务字典才是安全的
            snap = self.store.capture(self.bound_engine, extra=self._extra())
            snap["engine"]["snapshot_seq"] = self.store.bump_seq()
            self.stats["writes"] += 1
            self.stats["last_snapshot_at"] = snap.get("captured_at")
            d = threads.deferToThread(self.store.persist, snap)
            d.addBoth(self._after_write)
            return d
        except Exception as exc:
            self._writing = False
            logger.error(f"快照采集失败: {exc}", exc_info=True)
            return defer.succeed(None)

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

    def flush(self):
        """同步写一份快照（引擎停止时调用，保证最后一次状态不丢）。"""
        if self.store is None or self.bound_engine is None:
            return None
        try:
            snap = self.store.capture(self.bound_engine, extra=self._extra())
            snap["engine"]["snapshot_seq"] = self.store.bump_seq()
            self.stats["writes"] += 1
            self.stats["last_snapshot_at"] = snap.get("captured_at")
            return self.store.persist(snap)
        except Exception as exc:
            logger.error(f"最终快照落盘失败: {exc}", exc_info=True)
            return None

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

    def log_tail(self, n: int = 200):
        """取运行日志尾部。优先今天的 loguru 文件，其次守护进程输出。"""
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
            "writes": self.stats["writes"],
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
