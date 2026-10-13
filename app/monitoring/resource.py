#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控面板的 HTTP 传输层。

只负责「请求进来 → 找到处理函数 → 结果序列化」：
URL 分段、读 body、JSON/HTML 响应、把 Deferred 接到 Twisted 的 request 上。
**不含任何业务判断** —— 业务全在 MonitorWebComponent 及其视图 mixin 里。

依赖方向：本模块 -> app.monitoring.api.* / app.monitoring.server（仅类型注解）"""
from __future__ import annotations


from typing import Any, Deque, Dict, List, Optional

from twisted.internet import defer, reactor, task, threads
from twisted.web.resource import Resource

from app.common.jsonio import dumps, tail_lines
from app.common.logging import logger
from app.monitoring.dashboard import dashboard_html
from app.monitoring.snapshot import SnapshotStore, content_fingerprint
from app.data.repo.trade_repo import TradingRepository, get_repository


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
            return self._text(request, dashboard_html(), b"text/html; charset=utf-8")
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
                "/api/collector", "/api/dataguard", "/api/providers",
                "/api/runtime", "/api/maintenance", "/api/logging",
                "/api/journal", "/api/preflight",
                "/api/ledger", "/api/task?id=", "/api/events",
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
        if head == "runtime":
            # 任务系统全景：队列积压 / 执行池占用 / 规则 / 死信。
            # 事件驱动架构里"现在系统在做什么"只靠这一屏回答。
            return self._json(request, c.control.runtime_system())
        if head == "maintenance":
            # 维护计划：开关 / 周期 / 参数 / 下次执行时间
            return self._json(request, c.control.maintenance_view())
        if head == "ledger":
            # 任务明细：已建任务 + 终态历史（成功/失败/死信/取消）
            # 参数：status / task_type / queue / keyword / limit
            return self._json(request, c.control.task_ledger(
                status=self._q(request, "status", ""),
                task_type=self._q(request, "task_type", ""),
                queue=self._q(request, "queue", ""),
                keyword=self._q(request, "keyword", ""),
                limit=int(self._q(request, "limit", 50) or 50),
            ))
        if head == "task":
            # 单条引擎任务明细：?id=xxx
            task_id = self._q(request, "id", "") or (
                rest[1] if len(rest) >= 2 else ""
            )
            return self._json(request, c.control.engine_task_detail(task_id))
        if head == "events":
            # 事件指标：每种事件发布数 / 订阅者 / 处理错误 / 最近时间
            # ?type=xxx 时额外返回该类事件的详情与样例
            return self._json(request, c.control.event_metrics(
                limit=int(self._q(request, "limit", 0) or 0),
                event_type=self._q(request, "type", ""),
            ))
        if head == "trace":
            # 链路追踪：?trace_id=xxx 或 ?event_id=xxx（没有 trace 时按事件反查）
            return self._json(request, c.control.trace_view(
                trace_id=self._q(request, "trace_id", ""),
                event_id=self._q(request, "event_id", ""),
                limit=int(self._q(request, "limit", 30) or 30),
            ))
        if head == "logging":
            # 当前生效的日志级别（面板据此显示"我现在是什么档位"）
            return self._json(request, c.control.logging_view())
        if head == "journal":
            # 任务流水账：落盘状态 + 死信处置历史（内存里清空后仍能追溯）
            return self._json(request, c.control.journal_view())
        if head == "preflight":
            # 启动预检结论；?rerun=1 表示当场再跑一遍
            if self._q(request, "rerun", ""):
                return self._json(request, c.control.preflight_run())
            return self._json(request, c.control.preflight_view())
        if head == "collector":
            return self._json(request, c.control.collector_status())
        if head == "dataguard":
            # 数据校验报告：缺了哪几天、补回多少、用的哪个源
            return self._json(request, c.control.data_guard_status())
        if head == "providers":
            # 新采集层：能力 / 路由 / 健康分 / 熔断（"谁在、能干什么、现在健康吗"）
            import app.providers.market.service.facade  # noqa: F401  触发数据源装配

            from app.providers.market.provider import PROVIDERS

            return self._json(request, {
                "routes": PROVIDERS.snapshot(),
                "capabilities": PROVIDERS.capabilities(),
                "config": PROVIDERS.config.to_dict(),
                "used": PROVIDERS.health_snapshot(),
            })
        if head == "sources":
            # 数据源权重与健康度（排障"为什么先试这个源"时看这里）
            import app.providers.market.service.facade  # noqa: F401  确保内置源已注册

            from app.providers.market.compat.registry import REGISTRY

            # hosts 是"网络层实测的域名健康度"：多个源常共用同一域名
            # （push2 一挂，三个人气榜源一起挂），只看源列表会误判成源不行。
            # 旧路径 ``app.data`` 已随数据层重构迁走，网络层现在在 transport 下
            from app.providers.market.transport import net

            return self._json(request, {
                "health": REGISTRY.health(),
                "weights": REGISTRY.weights(),
                "used": REGISTRY.used_sources(),
                "events": REGISTRY.recent_events(30),
                "hosts": net.host_state(),
                "proxy": {
                    "configured": net.proxy_configured(),
                    "reachable": net.proxy_reachable(),
                },
            })

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
            dates = c.pool_dates()
            # 数据日 = 库里最新的评分日；未开盘且数据早于今天 → 标记为历史数据，
            # 让面板提示"显示为上一个交易日的数据"。
            data_day = dates[0] if dates else None
            is_historical = False
            if data_day:
                from datetime import date as _d, datetime as _dt

                from app.domain.calendar import get_calendar

                cal = get_calendar()
                today_str = _d.today().isoformat()
                is_historical = (data_day != today_str) and (not cal.is_open(_dt.now()))
            return self._json(request, {
                "results": c.pool_top(int(self._q(request, "limit", 30) or 30),
                                      source=self._q(request, "source", "") or ""),
                "dates": dates,
                "config": c.pool_config_view(),
                "data_day": data_day,
                "is_historical": is_historical,
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
        import app.monitoring.server as _self  # noqa: F401 - 保持符号可 monkeypatch

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
            # 统一走调度器分配任务：进任务明细、失败可进错误队列重放。
            # 无调度器（serve/CLI 模式）时退回直连执行，保持旧行为。
            syms = body.get("symbols") or None
            cats = body.get("categories") or None
            disp = c.control.dispatch_task(
                "collect.now", {"symbols": syms, "categories": cats})
            if disp.get("ok"):
                return self._json(request, disp)
            return self._async(request, threads.deferToThread(
                c.collect_now, syms, cats))

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
                body.get("exclusions") or None,
            ))

        # POST /api/collector/(now|interval|add|remove|reload)
        if seg[1] == "collector" and len(seg) >= 3:
            op = seg[2]
            if op == "now":
                # 统一走调度器分配任务（进任务明细、失败可进错误队列重放）。
                # 无调度器时退回 collector_now_async（自带工作单元限流）。
                disp = c.control.dispatch_task(
                    "collect.kline", {"symbol": str(body.get("symbol") or "")})
                if disp.get("ok"):
                    return self._json(request, disp)
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

        # POST /api/cross/collect —— 定向采集：指定标的 × 指定类别
        #   body: {"symbols": ["600519"], "categories": ["fundamental"]}
        # 与 /api/collector/now 的区别：后者补 K 线，这里补日频截面
        # （基本面/资金流/筹码/龙虎榜…），且标的与类别由调用方指定。
        # 统一走调度器分配任务（进任务明细、失败可进错误队列重放）。
        if seg[1] == "cross" and len(seg) >= 3 and seg[2] == "collect":
            disp = c.control.dispatch_task(
                "collect.cross",
                {"symbols": body.get("symbols"),
                 "categories": body.get("categories")},
            )
            if disp.get("ok"):
                return self._json(request, disp)
            return self._async(request, c.control.cross_collect_async(
                body.get("symbols"), body.get("categories"),
            ))

        # POST /api/runtime/dispatch —— 手动投递一个引擎任务
        #   body: {"task_type": "factor.calculate", "params": {"symbols": ["600519"]}}
        # 与 /api/tasks（新增量化任务）的区别：这里投的是一次性工作，
        # 会走"队列 → 能力路由 → 执行池 → 处理器"的完整链路。
        if seg[1] == "runtime" and len(seg) >= 3 and seg[2] == "dispatch":
            return self._json(request, c.control.dispatch_task(
                str(body.get("task_type") or ""),
                body.get("params"),
                priority=body.get("priority"),
                dedup_key=str(body.get("dedup_key") or ""),
                max_retries=int(body.get("max_retries") or 0),
            ))

        # POST /api/runtime/rule —— 启停一条调度规则（临时掐断某条自动链路）
        if seg[1] == "runtime" and len(seg) >= 3 and seg[2] == "rule":
            return self._json(request, c.control.set_rule_enabled(
                str(body.get("name") or ""), bool(body.get("enabled", True))))

        # POST /api/runtime/dead/clear —— 清空死信（运维处理完这批之后用）
        if seg[1] == "runtime" and len(seg) >= 4 and seg[2] == "dead" and seg[3] == "clear":
            return self._json(request, c.control.clear_dead_letters())

        # POST /api/runtime/dead/retry —— 人工重投一条死信（点"继续"）
        #   body: {"task_id": "..."}  省略 task_id 且带 all=true 时批量重投
        if seg[1] == "runtime" and len(seg) >= 4 and seg[2] == "dead" and seg[3] == "retry":
            if body.get("all"):
                return self._json(request, c.control.dead_retry_all(
                    str(body.get("task_type") or "")))
            return self._json(request, c.control.dead_retry(
                str(body.get("task_id") or "")))

        # POST /api/runtime/dead/discard —— 认掉一条死信（不重跑，划掉）
        if seg[1] == "runtime" and len(seg) >= 4 and seg[2] == "dead" and seg[3] == "discard":
            return self._json(request, c.control.dead_discard(
                str(body.get("task_id") or "")))

        # POST /api/events/replay —— 重放一条历史事件
        #   body: {"event_id": "..."}
        if seg[1] == "events" and len(seg) >= 3 and seg[2] == "replay":
            return self._json(request, c.control.replay_event(
                str(body.get("event_id") or ""), by="panel"))

        # POST /api/preflight/run —— 当场再跑一遍启动预检
        if seg[1] == "preflight" and len(seg) >= 3 and seg[2] == "run":
            return self._json(request, c.control.preflight_run())

        # POST /api/maintenance/save —— 保存维护计划（开关/周期/参数）
        if seg[1] == "maintenance" and len(seg) >= 3 and seg[2] == "save":
            return self._json(request, c.control.maintenance_save(body))

        # POST /api/maintenance/run —— 立刻跑一次某个维护任务
        if seg[1] == "maintenance" and len(seg) >= 3 and seg[2] == "run":
            return self._json(request, c.control.maintenance_run(
                str(body.get("name") or body.get("job") or "")))

        # POST /api/logging/level —— 运行中切换日志级别（不用重启）
        if seg[1] == "logging" and len(seg) >= 3 and seg[2] == "level":
            raw_window = body.get("throttle_window")
            return self._json(request, c.control.set_log_level(
                console=body.get("console") or body.get("level"),
                file=body.get("file") if "file" in body else None,
                throttle_window=None if raw_window is None else float(raw_window),
                persist=bool(body.get("persist", True)),
            ))

        # POST /api/dataguard/check —— 立刻体检（可选自动补全）
        if seg[1] == "dataguard" and len(seg) >= 3 and seg[2] == "check":
            return self._async(request, c.control.data_guard_check_async(
                str(body.get("symbol") or ""),
                auto_repair=body.get("auto_repair"),
            ))

        # POST /api/sources/reset —— 权重与熔断恢复默认（修完环境问题用）
        if seg[1] == "sources" and len(seg) >= 3 and seg[2] == "reset":
            from app.providers.market.compat.registry import REGISTRY

            REGISTRY.reset()
            # 新链路的健康分/熔断一起清 —— 修完代理端口要能让所有源重新公平竞争
            try:
                from app.providers.market.provider import PROVIDERS

                PROVIDERS.reset()
            except Exception:
                pass
            return self._json(request, {"ok": True, "weights": REGISTRY.weights()})

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
