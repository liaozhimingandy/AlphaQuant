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

        # ---- 股票池：推荐列表 ----
        if head == "pool":
            if len(rest) >= 2 and rest[1] == "score":
                sym = rest[2] if len(rest) >= 3 else ""
                if not sym:
                    return self._json(request, {"error": "缺少 symbol"}, 400)
                return self._json(request, c.stock_score(sym))
            return self._json(request, {
                "results": c.pool_top(int(self._q(request, "limit", 30) or 30)),
                "dates": c.pool_dates(),
                "config": c.pool_config_view(),
            })

        # ---- 交易日历 / 市场状态 ----
        if head == "calendar":
            return self._json(request, c.calendar_view())

        # ---- 因子实验室 ----
        if head == "factors":
            return self._json(request, c.factors_view())

        # ---- 采集时间窗口 ----
        if head == "schedule":
            return self._json(request, c.schedule_view())

        # ---- 策略库（界面自定义策略）----
        #      单独一个路径：/api/strategies 已被回测页当"能力清单"在用，
        #      改它的返回会连带影响回测表单的下拉框。
        if head == "strategy-library":
            return self._json(request, c.strategies_view())

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

        # ---- 标的即时采集（全类别）----
        if seg[1] == "collect" and len(seg) >= 3 and seg[2] == "now":
            # 要跑十几个接口，必须异步（否则把 reactor 连同面板一起堵死）
            return self._async(request, threads.deferToThread(
                c.collect_now,
                body.get("symbols") or None,
                body.get("categories") or None,
            ))

        # ---- 采集时间窗口设置 ----
        if seg[1] == "schedule" and len(seg) >= 3 and seg[2] == "save":
            return self._json(request, c.schedule_save(body))

        # ---- 用户策略库（界面自定义策略）----
        if seg[1] == "strategies" and len(seg) >= 3:
            op = seg[2]
            if op == "save":
                return self._json(request, c.strategy_save(body))
            if op == "remove":
                return self._json(request,
                                  c.strategy_remove(str(body.get("name") or "")))
            if op == "deploy":
                # 上线为实盘任务 —— 会真的开始下单，所以必须走审计
                return self._json(request, c.strategy_deploy(body))
            return self._json(request, {"ok": False, "error": f"未知操作: {op}"}, 400)

        # ---- 因子实验室 ----
        if seg[1] == "factors" and len(seg) >= 3:
            op = seg[2]
            if op == "add":
                return self._json(request, c.factor_add(body))
            if op == "remove":
                return self._json(request, c.factor_remove(
                    str(body.get("name") or "")))
            if op == "validate":
                # 纯静态校验，毫秒级，同步返回 —— 用户边写边看到错误
                return self._json(request, c.factor_validate(
                    str(body.get("expr") or "")))
            if op == "check":
                # 要跑全历史，必须异步（否则把 reactor 连同面板一起堵死）
                return self._async(request, threads.deferToThread(
                    c.factor_check,
                    str(body.get("expr") or ""),
                    body.get("symbols") or None,
                    int(body.get("horizon") or 5),
                ))
            if op == "mine":
                return self._async(request, threads.deferToThread(
                    c.factor_mine,
                    body.get("symbols") or None,
                    int(body.get("horizon") or 5),
                    int(body.get("generations") or 10),
                    int(body.get("population") or 50),
                    float(body.get("seconds") or 60),
                    float(body.get("min_score") or 45),
                    bool(body.get("save")),
                ))
            return self._json(request, {"ok": False, "error": f"未知操作: {op}"}, 400)

        # POST /api/pool/refresh —— 刷新股票池。
        # 联网 + 打分，秒级到分钟级，必须异步（否则把 reactor 连同面板一起堵死）
        if seg[1] == "pool" and len(seg) >= 3 and seg[2] == "refresh":
            return self._async(request, threads.deferToThread(
                c.refresh_pool,
                str(body.get("source") or ""),
                body.get("limit"),
                body.get("sync"),
                body.get("symbols") or None,
            ))

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

    # ============================================================
    # 股票池 / 综合评分
    # ============================================================
    def pool_config_view(self) -> Dict[str, Any]:
        try:
            from app.core.pool import PoolConfig, SOURCES
            from app.core.score.engine import ScoreConfig

            return {
                **PoolConfig.load().to_dict(),
                "sources": list(SOURCES),
                "weights": ScoreConfig.load().weights,
            }
        except Exception as exc:
            logger.debug(f"股票池配置读取失败: {exc}")
            return {}

    def pool_top(self, limit: int = 30) -> List[Dict[str, Any]]:
        try:
            from app.core.pool import StockPool

            return StockPool.top(limit=int(limit))
        except Exception as exc:
            logger.debug(f"读取股票池失败: {exc}")
            return []

    def pool_dates(self) -> List[str]:
        try:
            from app.core.pool import StockPool

            return StockPool.dates()
        except Exception:
            return []

    # ============================================================
    # 因子实验室
    # ============================================================
    def factors_view(self) -> Dict[str, Any]:
        """因子总目录（按类别分组）+ 算子/变量速查。

        **必须是全量因子**，不只是因子库里的表达式因子 ——
        只显示 8 条会给用户一个错觉：以为系统只有这几个因子可用，
        于是题材/基本面/资金/舆情/宏观那几十个从来没被用过。
        """
        try:
            from app.core.factor.catalog import CATEGORY_DOCS, factor_catalog
            from app.core.factor.expr import func_catalog, variable_catalog
            from app.core.factor.library import get_library

            lib = get_library()
            cat = factor_catalog()
            return {
                "factors": [s.to_dict() for s in lib.list()],
                "groups": cat["categories"],
                "group_docs": CATEGORY_DOCS,
                "total": cat["total"],
                "categories": lib.categories(),
                "ops": func_catalog(),
                "vars": variable_catalog(),
                "path": str(lib.path),
            }
        except Exception as exc:
            logger.debug(f"因子库读取失败: {exc}")
            return {"factors": [], "groups": [], "ops": [], "vars": []}

    def factor_validate(self, expr: str) -> Dict[str, Any]:
        try:
            from app.core.factor.expr import validate

            return validate(expr)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def factor_add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.core.factor.library import get_library

            ok, msg, spec = get_library().add(
                str(body.get("name") or ""),
                str(body.get("expr") or ""),
                description=str(body.get("description") or ""),
                category=str(body.get("category") or "自定义"),
                overwrite=bool(body.get("overwrite")),
            )
            out: Dict[str, Any] = {"ok": ok, "message": msg}
            if spec is not None:
                out["factor"] = spec.to_dict()
            return out
        except Exception as exc:
            logger.error(f"新增因子失败: {exc}", exc_info=True)
            return {"ok": False, "message": str(exc)}

    # ============================================================
    # 数据采集（标的即时采集 + 时间窗口设置）
    # ============================================================
    def collect_now(self, symbols: Any = None,
                    categories: Any = None) -> Dict[str, Any]:
        """立即采集一批标的的**全类别**数据（工作线程执行）。

        用户选了一只新票，最想的是"马上看看它的因子长什么样"，
        而不是等下一个采集窗口。所以这里把所有类别都跑一遍。
        """
        try:
            from app.core.pool import sync_category

            syms = self._factor_symbols(symbols)
            if not syms:
                return {"ok": False, "error": "没有指定标的"}
            cats = [str(c) for c in (categories or [])] or [
                "lhb", "margin", "forecast", "holders", "rating", "news",
            ]
            report: Dict[str, Any] = {"ok": True, "symbols": syms, "detail": {}}

            # 先采量价类（有行情才能算技术/表达式因子）
            try:
                from app.data.service import MarketDataService

                rows = 0
                for s in syms:
                    info = MarketDataService.collect_incremental(
                        s, lookback_days=365, adjust="qfq")
                    rows += int(info.get("rows") or 0)
                report["detail"]["daily"] = {"ok": len(syms), "rows": rows}
            except Exception as exc:
                report["detail"]["daily"] = {"ok": 0, "error": str(exc)[:120]}

            # 再采日频截面
            from app.core.pool import sync_chip, sync_fund_flow, sync_fundamentals

            for label, fn in (("fundflow", lambda: sync_fund_flow(syms)),
                              ("chip", lambda: sync_chip(syms)),
                              ("fundamental", lambda: sync_fundamentals(syms))):
                try:
                    r = fn()
                    report["detail"][label] = {
                        "ok": r.get("ok", 0),
                        "failed": (r.get("failed") or [])[:5],
                    }
                except Exception as exc:
                    report["detail"][label] = {"ok": 0, "error": str(exc)[:120]}

            for cat in cats:
                try:
                    r = sync_category(cat, syms)
                    report["detail"][cat] = {
                        "ok": r.get("ok", 0),
                        "failed": (r.get("failed") or [])[:5],
                    }
                except Exception as exc:
                    report["detail"][cat] = {"ok": 0, "error": str(exc)[:120]}

            # 市场级
            for cat in ("macro", "breadth"):
                try:
                    r = sync_category(cat)
                    report["detail"][cat] = {"ok": r.get("ok", 0)}
                except Exception as exc:
                    report["detail"][cat] = {"ok": 0, "error": str(exc)[:120]}

            # 采完必须失效特征缓存，否则界面上看到的还是旧值
            try:
                from app.core.data.features import get_provider

                get_provider().invalidate()
            except Exception:
                pass
            return report
        except Exception as exc:
            logger.error(f"即时采集失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def schedule_view(self) -> Dict[str, Any]:
        """采集时间窗口 + 当前处在哪个窗口。"""
        try:
            from app.core.collect.schedule import get_schedule
            from app.core.market.calendar import get_calendar

            sch = get_schedule()
            slot = sch.slot_of()
            return {
                "schedule": sch.to_dict(),
                "current_slot": slot,
                "slot_names": {"pre": "盘前", "intraday": "盘中",
                               "post": "盘后定稿"},
                "market": get_calendar().to_dict(),
            }
        except Exception as exc:
            logger.debug(f"读取采集时间窗口失败: {exc}")
            return {}

    def schedule_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.core.collect.schedule import (
                CollectionSchedule,
                get_schedule,
                set_schedule,
            )

            base = get_schedule()
            fields = {f for f in CollectionSchedule.__dataclass_fields__}
            raw = {k: v for k, v in body.items() if k in fields}
            if not raw:
                return {"ok": False, "message": "没有可识别的字段"}
            merged = CollectionSchedule.from_dict({**base.to_dict(), **raw})
            set_schedule(merged, persist=True)
            # 审计：改采集时间会影响"什么时候有数据"，属于值得留痕的操作
            audit = getattr(self, "audit", None)
            if audit is not None:
                audit.append({
                    "at": datetime.now().isoformat(), "action": "save_schedule",
                    "detail": {k: getattr(merged, k) for k in raw},
                })
            return {"ok": True, "message": "已保存", "schedule": merged.to_dict()}
        except Exception as exc:
            logger.error(f"保存采集时间窗口失败: {exc}", exc_info=True)
            return {"ok": False, "message": str(exc)}

    def factor_remove(self, name: str) -> Dict[str, Any]:
        try:
            from app.core.factor.library import get_library

            ok, msg = get_library().remove(name)
            return {"ok": ok, "message": msg}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def factor_check(self, expr: str, symbols: Any = None,
                     horizon: int = 5) -> Dict[str, Any]:
        """检验一个因子（工作线程执行）。"""
        try:
            from app.core.factor.evaluate import evaluate_factor

            rep = evaluate_factor(expr, self._factor_symbols(symbols),
                                  horizon=int(horizon))
            return {"ok": True, "report": rep.to_dict()}
        except Exception as exc:
            logger.error(f"因子检验失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def factor_mine(self, symbols: Any = None, horizon: int = 5,
                    generations: int = 10, population: int = 50,
                    seconds: float = 60, min_score: float = 45,
                    save: bool = False) -> Dict[str, Any]:
        """遗传算法挖掘（工作线程执行）。"""
        try:
            from app.core.factor.evaluate import load_panel
            from app.core.factor.library import SOURCE_MINED, get_library
            from app.core.factor.mining import FactorMiner

            syms = self._factor_symbols(symbols)
            panel = load_panel(syms)
            if len(panel) < 3:
                return {"ok": False, "error":
                        f"可用标的只有 {len(panel)} 个，截面检验至少需要 3 个"}
            miner = FactorMiner(panel, horizon=int(horizon), seed=None)
            found = miner.run(generations=int(generations),
                              population=int(population), top=10,
                              max_seconds=float(seconds),
                              min_score=float(min_score))
            saved = 0
            if save and found:
                lib = get_library()
                for f in found:
                    ok, _, _ = lib.add(
                        f"mined_{abs(hash(f.expr)) % 100000:05d}", f.expr,
                        description=f"自动挖掘（{f.score}分）",
                        category="挖掘", source=SOURCE_MINED,
                        metrics={"ic": f.ic, "icir": f.icir, "score": f.score},
                        overwrite=True)
                    saved += bool(ok)
            return {
                "ok": True, "evaluated": miner.evaluated, "saved": saved,
                "factors": [f.to_dict() for f in found],
            }
        except Exception as exc:
            logger.error(f"因子挖掘失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    # ============================================================
    # 用户策略库（界面自定义策略）
    # ============================================================
    def strategies_view(self) -> Dict[str, Any]:
        """已注册策略 + 用户自定义策略 + 可用因子/风控（界面要靠它渲染表单）。"""
        from app.core.strategy.store import KINDS, get_store

        store = get_store()
        out: Dict[str, Any] = {
            "kinds": KINDS,
            "user": [s.to_dict() for s in store.list()],
            "path": str(store.path),
        }
        try:
            from app.core.factor import list_factors
            from app.core.risk import list_risks
            from app.core.strategy import list_strategies

            out["factors"] = list_factors()
            out["risk_rules"] = list_risks()
            out["registered"] = list_strategies()
        except Exception as exc:
            logger.debug(f"策略清单读取失败: {exc}")
        return out

    def strategy_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.core.strategy.store import get_store

            ok, msg, s = get_store().add(
                name=str(body.get("name") or ""),
                kind=str(body.get("kind") or ""),
                spec=dict(body.get("spec") or {}),
                description=str(body.get("description") or ""),
                risk=list(body.get("risk") or []),
                overwrite=bool(body.get("overwrite", True)),
            )
            out: Dict[str, Any] = {"ok": ok, "message": msg}
            if s is not None:
                out["strategy"] = s.to_dict()
            return out
        except Exception as exc:
            logger.error(f"保存策略失败: {exc}", exc_info=True)
            return {"ok": False, "message": str(exc)}

    def strategy_remove(self, name: str) -> Dict[str, Any]:
        try:
            from app.core.strategy.store import get_store

            ok, msg = get_store().remove(name)
            return {"ok": ok, "message": msg}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def strategy_deploy(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """把自定义策略上线为实盘任务。

        这一步会**真的开始下单**，所以：
          1. 必须走控制面（统一指令入口，带审计）；
          2. 默认带一条 max_drawdown 风控兜底 —— 用户可能忘记配。
        """
        try:
            from app.core.strategy.store import get_store

            name = str(body.get("name") or "")
            s = get_store().get(name)
            if s is None:
                return {"ok": False, "error": f"策略不存在: {name}"}
            symbol = str(body.get("symbol") or "").strip()
            if not symbol:
                return {"ok": False, "error": "缺少标的 symbol"}

            risk = [dict(r) for r in (s.risk or [])]
            if not any(str(r.get("type")) == "max_drawdown" for r in risk):
                risk.append({"type": "max_drawdown", "max_drawdown": 0.2})
            spec = {
                "task_id": str(body.get("task_id") or f"{name}-{symbol}"),
                "name": s.description or name,
                "symbol": symbol,
                "enabled": True,
                "initial_cash": float(body.get("initial_cash") or 100000),
                "strategy": s.to_task_strategy(),
                "risk": risk,
            }
            result = self.control.apply_command({"action": "add_task", "spec": spec})
            self.audit.append({
                "at": datetime.now().isoformat(), "action": "deploy_strategy",
                "detail": {"strategy": name, "symbol": symbol,
                           "task_id": spec["task_id"]},
            })
            return {"ok": True, "task_id": spec["task_id"], "result": result}
        except Exception as exc:
            logger.error(f"策略上线失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def _factor_symbols(self, symbols: Any = None) -> List[str]:
        if symbols:
            return [str(s) for s in symbols]
        from app.db.database import SessionLocal
        from app.repository.stock_repository import StockRepository

        try:
            with SessionLocal() as db:
                return [r["symbol"] for r in StockRepository.list_symbols(db)]
        except Exception:
            return []

    def calendar_view(self) -> Dict[str, Any]:
        """交易日历与市场状态。

        面板要能回答"为什么它不采数据" —— 闭市是**正常行为**，
        但如果不显示出来，用户看到"采集 0 次"只会以为坏了。
        """
        try:
            from app.core.market.calendar import STATE_NAMES, get_calendar

            cal = get_calendar()
            info = cal.to_dict()
            info["state_names"] = dict(STATE_NAMES)
            return info
        except Exception as exc:
            logger.debug(f"交易日历读取失败: {exc}")
            return {}

    def refresh_pool(self, source: str = "", limit: Any = None,
                     sync: Any = None, symbols: Any = None) -> Dict[str, Any]:
        """刷新股票池（工作线程执行）。

        面板点一下就可能触发几十只票的联网同步 + 打分，
        同步阻塞会把行情、策略、撮合一起停摆。
        """
        try:
            from app.core.pool import StockPool

            pool = StockPool()
            report = pool.refresh(
                source=source or None,
                limit=int(limit) if limit else None,
                do_sync=None if sync is None else bool(sync),
                symbols=list(symbols) if symbols else None,
            )
            if report.get("error"):
                return {"ok": False, "error": report["error"]}
            self.audit.append({
                "at": datetime.now().isoformat(), "action": "pool_refresh",
                "detail": {"source": report.get("source"),
                           "count": report.get("count")},
            })
            return {"ok": True, **report}
        except Exception as exc:
            logger.error(f"股票池刷新失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def stock_score(self, symbol: str) -> Dict[str, Any]:
        """单只票的多维度评分明细。"""
        try:
            from app.core.data.features import get_provider
            from app.core.score.engine import ScoreEngine
            from app.data.datasource import normalize_symbol

            sym = normalize_symbol(symbol)
            r = ScoreEngine(provider=get_provider(ttl=0)).score(sym)
            return {"ok": True, "symbol": sym, **r.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
