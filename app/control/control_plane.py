#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : control.py
# @Description : 运行时控制中心：不重启引擎就能增删任务、启停组件、优雅停止
#               面板 / CLI / hub supervisor 都通过它下达指令，是唯一控制入口
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional

from app.common.config import settings
from app.runtime.event_bus import StandardEvents
from app.domain.types import TaskState
from app.runtime.tasking.spec import build_task, parse_task_spec
from app.common.jsonio import json_safe, read_json, write_json_atomic
from app.common.logging import logger

if TYPE_CHECKING:  # pragma: no cover
    from app.engine.engine import BaseQuantEngine


class ControlCenter:
    """引擎控制面。

    为什么独立成一层，而不是把方法堆在 Engine 上：
      - Engine 的职责是"生命周期与组件调度"，不该知道"任务"这种业务概念
      - 控制指令需要统一的**留痕与持久化**（谁改了什么、重启后还在不在）
      - 面板、CLI、supervisor 三条入口共用同一套语义，行为不会漂移
    """

    def __init__(self, engine: "BaseQuantEngine", namespace: str = "default") -> None:
        self.engine = engine
        self.namespace = namespace or "default"
        #: 运行时新增的任务 spec（用于持久化，重启后自动重放）
        self._extra_specs: Dict[str, Dict[str, Any]] = {}
        self._removed_ids: set[str] = set()
        #: 任务新增后的外部回调（装配器把落库组件的 register_task 挂在这里）。
        #: 用回调而不是让控制面直接 import 落库组件：控制面不该知道存储细节，
        #: 而且落库可能是关的（回调为 None 时这条路自然不生效）。
        self.on_task_added: Optional[Callable[[Any], None]] = None

    # ============================================================
    # 内部存取
    # ============================================================
    @property
    def runtime_tasks_path(self) -> Path:
        return Path(settings.RUNTIME_DIR) / self.namespace / "tasks.json"

    def _sm(self):
        return self.engine.get_component("strategy")

    def _runtime(self):
        sm = self._sm()
        return getattr(sm, "runtime", None) if sm is not None else None

    def _submit_job(self, name: str, fn: Any, *args: Any, **kwargs: Any):
        """把一次性作业交给工作单元（有并发上限），没有工作单元时退回线程池。

        为什么不再直接 ``deferToThread``：面板上"立即采集/立即体检"这类按钮
        是可以连点的。无上限地丢进线程池，五连点就是五轮联网请求，
        数据源直接限流，连定时采集一起被拖下水。

        工作单元槽位占满时会返回**已失败**的 Deferred，
        于是调用方拿到的是明确的"现在忙"，而不是"不知道跑哪去了"。
        """
        worker = self.engine.get_component("worker")
        if worker is not None and hasattr(worker, "submit"):
            return worker.submit(name, fn, *args, **kwargs)

        from twisted.internet import threads

        return threads.deferToThread(fn, *args, **kwargs)

    def _publish(self, event: str, **kwargs: Any) -> None:
        try:
            self.engine.get_event_bus().publish(event, **kwargs)
        except Exception as exc:  # 事件总线故障不能阻断控制指令
            logger.debug(f"控制事件 {event} 发布失败: {exc}")

    def _audit(self, action: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        logger.info(f"[control] {action} | {dict(detail or {})}")
        self._publish(StandardEvents.CONTROL_COMMAND, action=action, detail=dict(detail or {}))

    # ============================================================
    # 任务：查询
    # ============================================================
    def list_tasks(self) -> List[Dict[str, Any]]:
        runtime = self._runtime()
        if runtime is None:
            return []
        out: List[Dict[str, Any]] = []
        for task in runtime:
            try:
                out.append(json_safe(task.snapshot()))
            except Exception as exc:
                logger.debug(f"任务 {getattr(task, 'task_id', '?')} 快照失败: {exc}")
        return out

    def get_task(self, task_id: str) -> Optional[Any]:
        runtime = self._runtime()
        return runtime.get(task_id) if runtime is not None else None

    def task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        """单个任务的详细信息：快照 + 权益曲线 + 成交明细 + 订单/新闻。

        整段包 try：下面这一串 ``.portfolio`` / ``.orders`` / ``.to_dict()``
        里任意一处缺字段或抛错，就会让面板任务详情整个 500。
        宁可少给几段明细，也要保住页面能开。
        """
        task = self.get_task(task_id)
        if task is None:
            return None
        from app.monitoring.snapshot import _sample_curve

        try:
            detail = json_safe(task.snapshot())
        except Exception as exc:
            logger.warning(f"任务 {task_id} 快照失败: {exc}")
            detail = {"task_id": task_id, "error": f"快照失败: {exc}"}
        for key, fn in (
            ("equity_curve", lambda: _sample_curve(
                list(task.portfolio.equity_curve))),
            ("trades", lambda: [t.to_dict() for t in task.portfolio.trades[-200:]]),
            ("orders", lambda: [o.to_dict() for o in task.orders[-200:]]),
            ("news", lambda: [n.to_dict() for n in task.recent_news[-20:]]),
            ("errors", lambda: list(task.errors[-20:])),
            ("risk_rules", lambda: [
                {"name": r.name, "params": dict(getattr(r, "params", {}))}
                for r in task.risk_chain.rules
            ]),
            ("watch_symbols", lambda: sorted(runtime_watch(task))),
        ):
            try:
                detail[key] = fn()
            except Exception as exc:
                logger.warning(f"任务 {task_id} 取 {key} 失败: {exc}")
                detail[key] = []
        return detail

    # ============================================================
    # 任务：增删与状态切换
    # ============================================================
    def add_task(self, raw: Mapping[str, Any], autostart: Optional[bool] = None) -> str:
        """运行时新增一个任务。返回 task_id。"""
        runtime = self._runtime()
        if runtime is None:
            raise RuntimeError("引擎尚未装配策略中枢，无法添加任务")

        spec = parse_task_spec(dict(raw))
        if spec.task_id and spec.task_id in self._removed_ids:
            # 之前被删掉的 id 再新增，视为重新启用
            self._removed_ids.discard(spec.task_id)

        task = runtime.add_from_spec(spec)
        self._extra_specs[task.task_id] = spec.to_dict()

        # 新任务的标的要纳入行情订阅，否则永远等不到K线
        self._watch_symbols(task)

        if autostart is None:
            autostart = self.engine.get_status().value == "RUNNING"
        if autostart and task.status == TaskState.CREATED:
            try:
                task.start()
            except Exception as exc:
                logger.error(f"任务 {task.task_id} 启动失败: {exc}", exc_info=True)

        # 通知外部（落库）：新任务要登记，否则"这轮跑了哪些任务"查不到
        if self.on_task_added is not None:
            try:
                self.on_task_added(task)
            except Exception as exc:
                logger.debug(f"任务登记回调失败（忽略）: {exc}")

        self._audit("add_task", {"task_id": task.task_id, "symbol": task.symbol})
        self._publish(StandardEvents.TASK_ADDED, task_id=task.task_id, task=task)
        self.save_runtime_tasks()
        return task.task_id

    def remove_task(self, task_id: str) -> bool:
        runtime = self._runtime()
        if runtime is None:
            return False
        if not runtime.remove(task_id):
            return False
        self._extra_specs.pop(task_id, None)
        self._removed_ids.add(task_id)
        self._audit("remove_task", {"task_id": task_id})
        self._publish(StandardEvents.TASK_REMOVED, task_id=task_id)
        self.save_runtime_tasks()
        return True

    def pause_task(self, task_id: str) -> bool:
        """暂停任务。只有真的进入 PAUSED（或本来就在 PAUSED）才算成功。

        为什么返回值要较真：``QuantTask.pause()`` 只在 RUNNING 时生效，早期实现
        无论成没成都返回 True——面板会显示"已暂停"，而任务下一根K线照旧下单。
        控制面谎报成功比报错危险得多，所以这里显式校验状态是否真的变了。
        """
        task = self.get_task(task_id)
        if task is None:
            return False
        before = task.status
        task.pause()
        if task.status != TaskState.PAUSED:
            logger.warning(f"任务 {task_id} 当前状态 {before} 不支持暂停（需先启动）")
            return False
        self._audit("pause_task", {"task_id": task_id})
        self._publish(StandardEvents.TASK_PAUSED, task_id=task_id)
        return True

    def resume_task(self, task_id: str) -> bool:
        """恢复任务。语义同 :meth:`pause_task`：没进入 RUNNING 就不报成功。"""
        task = self.get_task(task_id)
        if task is None:
            return False
        before = task.status
        task.resume()
        if task.status != TaskState.RUNNING:
            logger.warning(f"任务 {task_id} 当前状态 {before} 不支持恢复（未暂停过）")
            return False
        self._audit("resume_task", {"task_id": task_id})
        self._publish(StandardEvents.TASK_RESUMED, task_id=task_id)
        return True

    def stop_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False
        task.stop()
        self._audit("stop_task", {"task_id": task_id})
        self._publish(StandardEvents.TASK_STOPPED, task_id=task_id)
        return True

    def start_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False
        if task.status == TaskState.PAUSED:
            task.resume()
        else:
            task.start()
        self._audit("start_task", {"task_id": task_id})
        self._publish(StandardEvents.TASK_STARTED, task_id=task_id)
        return True

    # ============================================================
    # 组件
    # ============================================================
    def component_states(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for name in self.engine.component_names():
            comp = self.engine.get_component(name)
            if comp is None:
                continue
            entry: Dict[str, Any] = {
                "name": name,
                "enabled": bool(comp.enabled),
                "state": comp.state,
            }
            try:
                healthy, detail = comp.health_check()
                entry["healthy"] = bool(healthy)
                entry["health_detail"] = detail
            except Exception as exc:
                entry["healthy"] = None
                entry["health_detail"] = str(exc)
            prog_fn = getattr(comp, "progress", None)
            if callable(prog_fn):
                try:
                    entry["progress"] = prog_fn()
                except Exception as exc:  # pragma: no cover - 防御
                    entry["progress"] = {"total": 0, "error": str(exc)}
            out.append(entry)
        return out

    def enable_component(self, name: str) -> bool:
        try:
            self.engine.enable_component(name)
        except ValueError:
            return False
        self._audit("enable_component", {"component": name})
        self._publish(StandardEvents.COMPONENT_ENABLED, component=name)
        return True

    def disable_component(self, name: str) -> bool:
        try:
            self.engine.disable_component(name)
        except ValueError:
            return False
        self._audit("disable_component", {"component": name})
        self._publish(StandardEvents.COMPONENT_DISABLED, component=name)
        return True

    # ============================================================
    # 行情订阅联动
    # ============================================================
    def _watch_symbols(self, task: Any) -> None:
        market = self.engine.get_component("market_service")
        watch = getattr(market, "watch", None)
        if not callable(watch):
            return
        for sym in sorted(runtime_watch(task)):
            try:
                watch(sym)
            except Exception as exc:
                logger.debug(f"行情订阅 {sym} 失败: {exc}")

    # ============================================================
    # 行情采集服务
    # ============================================================
    #: 采集服务名。三项采集能力（K线 / 日频截面 / 完整性校验）
    #: 已合并进同一个服务，所以这里只剩一个名字。
    COLLECTOR_COMPONENT = "collection_service"

    def _collector(self):
        return self.engine.get_component(self.COLLECTOR_COMPONENT)

    def collector_status(self) -> Dict[str, Any]:
        """采集服务概览：配置 + 每个任务的运行状态与最近一次结果。

        合并后采集服务的快照里含 K线/截面/校验三块，
        这里**只展开 K 线那块**——面板的"采集"页要的就是这个，
        把另外两块混进来会让页面出现一堆无关字段。
        """
        comp = self._collector()
        if comp is None:
            return {"available": False, "reason": "本引擎未装配采集服务"}
        snap = comp.snapshot()
        out: Dict[str, Any] = {"available": True}
        out.update(json_safe(snap.get("kline") or snap))
        return out

    def collector_set_interval(self, seconds: Any, symbol: str = "",
                               period: str = "") -> Dict[str, Any]:
        """现场改采集频率。写 30s / 5m / 1h 都行，无需重启。"""
        comp = self._collector()
        if comp is None:
            return {"ok": False, "error": "本引擎未装配采集服务"}
        n = comp.set_interval(seconds, symbol=symbol, period=period)
        self._audit("collector_interval", {
            "interval": str(seconds), "symbol": symbol or "*",
            "period": period or "*", "affected": n,
        })
        return {"ok": n > 0, "affected": n}

    def collector_add(self, symbol: str, period: str = "", interval: Any = None,
                      **params: Any) -> Dict[str, Any]:
        comp = self._collector()
        if comp is None:
            return {"ok": False, "error": "本引擎未装配采集服务"}
        job = comp.add_symbol(symbol, period=period, interval=interval, **params)
        self._audit("collector_add", {
            "symbol": symbol, "period": period or "1d", "interval": str(interval or ""),
        })
        return {"ok": job is not None, "job": job.to_dict() if job else None}

    def collector_remove(self, symbol: str, period: str = "") -> Dict[str, Any]:
        comp = self._collector()
        if comp is None:
            return {"ok": False, "error": "本引擎未装配采集服务"}
        n = comp.remove_symbol(symbol, period=period)
        self._audit("collector_remove", {"symbol": symbol, "period": period or "*", "affected": n})
        return {"ok": n > 0, "affected": n}

    def collector_reload(self, path: Optional[str] = None) -> Dict[str, Any]:
        """重新读配置文件。改了 collector.json 之后不用重启就能生效。"""
        comp = self._collector()
        if comp is None:
            return {"ok": False, "error": "本引擎未装配采集服务"}
        try:
            info = comp.reload_config(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._audit("collector_reload", info)
        return {"ok": True, **info}

    def collector_now(self, symbol: str = "", wait: bool = False) -> Dict[str, Any]:
        """立刻采一次。

        默认**不等结果**（把活丢进线程池即刻返回），所以这个方法是安全的，
        可以在任意线程调用。采集是秒级的网络 IO，同步等待会把调用方线程
        堵死——如果调用方恰好是 reactor，行情、策略、撮合、面板会一起停。

        :param wait: True=同步阻塞等待（仅 CLI / 离线脚本用），False=后台执行
        :return: wait=True 时带 rows；否则带 accepted=True，结果去 /api/collector 看
        """
        comp = self._collector()
        if comp is None:
            return {"ok": False, "error": "本引擎未装配采集服务"}
        jobs = [
            j for j in comp.kline_spec.jobs
            if j.enabled and (not symbol or j.symbol == _norm_symbol(symbol))
        ]
        if not jobs:
            return {"ok": False, "error": f"没有匹配的采集任务: {symbol or '*'}"}

        if wait:
            try:
                result = comp._run_kline_forced(jobs)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            self._audit("collector_now", {"symbol": symbol or "*", "rows": result.get("rows", 0)})
            return {"ok": True, **result}

        def _done(result):
            self._audit(
                "collector_now",
                {"symbol": symbol or "*", "rows": result.get("rows", 0)},
            )
            return result

        def _failed(failure_obj):
            msg = str(failure_obj.getErrorMessage()) if hasattr(failure_obj, "getErrorMessage") else str(failure_obj)
            logger.error(f"后台采集失败: {msg}")
            return None

        # 走工作单元而不是直接 deferToThread：并发有上限，
        # 连点"立即采集"不会把自己打到数据源限流。
        d = self._submit_job(
            f"立即采集 {symbol or '全部'}", comp._run_kline_forced, jobs
        )
        d.addCallback(_done)
        d.addErrback(_failed)
        return {
            "ok": True, "accepted": True, "tasks": len(jobs),
            "note": "已在后台执行，结果见 /api/collector",
        }

    def collector_now_async(self, symbol: str = ""):
        """异步版立即采集，返回 Deferred —— 面板路由用它等真实结果。"""
        from twisted.internet import defer

        comp = self._collector()
        if comp is None:
            return defer.succeed({"ok": False, "error": "本引擎未装配采集服务"})
        jobs = [
            j for j in comp.kline_spec.jobs
            if j.enabled and (not symbol or j.symbol == _norm_symbol(symbol))
        ]
        if not jobs:
            return defer.succeed(
                {"ok": False, "error": f"没有匹配的采集任务: {symbol or '*'}"}
            )

        def _ok(result):
            rows = int(result.get("rows") or 0)
            errors = result.get("errors") or []
            self._audit("collector_now", {"symbol": symbol or "*", "rows": rows})
            return {"ok": not errors, **result}

        d = self._submit_job(
            f"立即采集 {symbol or '全部'}", comp._run_kline_forced, jobs
        )
        d.addCallback(_ok)
        return d

    # ============================================================
    # 数据校验服务
    # ============================================================
    #: 数据校验能力所在的**服务名**。它现在是采集服务的一个单元，
    #: 不再是独立组件 —— 所以这里指向的是同一个服务。
    GUARD_COMPONENT = "collection_service"

    def _guard(self):
        return self.engine.get_component(self.GUARD_COMPONENT)

    def data_guard_status(self) -> Dict[str, Any]:
        """数据校验概览：最近一次体检报告 + 累计统计。

        与 :meth:`collector_status` 同理：从采集服务的快照里
        只取"完整性校验"那一块，保持面板的字段契约不变。
        """
        comp = self._guard()
        if comp is None:
            return {"available": False, "reason": "本引擎未装配数据校验服务"}
        snap = comp.snapshot()
        guard = snap.get("guard")
        if not guard:
            # 不能回退成整个采集服务快照：面板会拿到一堆 jobs/symbols，
            # 唯独取不到它要的"缺失数/补全数"，等于报错报了等于没报。
            return {
                "available": True,
                "error": "体检单元未启用（collection_service 未装配 guard 单元）",
            }
        out: Dict[str, Any] = {"available": True}
        out.update(json_safe(guard))
        return out

    def data_guard_check(self, symbol: str = "", wait: bool = False,
                         auto_repair: Optional[bool] = None) -> Dict[str, Any]:
        """立刻体检一次（可自动补全）。

        :param wait: True=同步等待（CLI 用）；False=后台跑，结果去 API 看
        :param auto_repair: 覆盖组件配置。None=跟随组件（默认开）
        """
        comp = self._guard()
        if comp is None:
            return {"ok": False, "error": "本引擎未装配数据校验服务"}
        syms = [s.strip() for s in symbol.split(",") if s.strip()] or None

        def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
            self._audit("data_guard_check", {
                "symbol": symbol or "*",
                "missing": result.get("missing_total", 0),
                "repaired": result.get("repaired_total", 0),
            })
            return {"ok": True, **result}

        if wait:
            d = comp.check_now(syms, auto_repair=auto_repair)
            box: Dict[str, Any] = {}

            def _capture(value):
                box.update(value)
                return value

            failure: Dict[str, Any] = {}

            def _capture_err(f: Any) -> None:
                failure["error"] = str(
                    f.getErrorMessage() if hasattr(f, "getErrorMessage") else f
                )
                return None

            d.addCallback(_capture)
            # 必须挂 errback：否则校验失败时 box 永远为空，只能干等到
            # 300s 超时，报"体检超时"——把真实错误吞掉，还让人白等 5 分钟。
            d.addErrback(_capture_err)
            # CLI 是同步上下文：这里只能等。校验本来就慢，
            # 用户是明确点了"立刻体检"，等是有预期的。
            deadline = time.time() + 300.0
            while not box and not failure and time.time() < deadline:
                time.sleep(0.05)
            if failure:
                return {"ok": False, **failure}
            if not box:
                return {"ok": False, "error": "体检超时（300s）"}
            return _ok(box)

        d = comp.check_now(syms, auto_repair=auto_repair)
        d.addCallback(_ok)
        d.addErrback(lambda f: {"ok": False, "error": str(
            f.getErrorMessage() if hasattr(f, "getErrorMessage") else f)})
        return {
            "ok": True, "accepted": True,
            "note": "已在后台体检，结果见 /api/dataguard",
        }

    def data_guard_check_async(self, symbol: str = "",
                               auto_repair: Optional[bool] = None):
        """异步版体检，返回 Deferred —— 面板路由用它等真实结果。"""
        from twisted.internet import defer

        comp = self._guard()
        if comp is None:
            return defer.succeed({"ok": False, "error": "本引擎未装配数据校验服务"})
        syms = [s.strip() for s in symbol.split(",") if s.strip()] or None
        d = comp.check_now(syms, auto_repair=auto_repair)

        def _ok(result):
            self._audit("data_guard_check", {
                "symbol": symbol or "*",
                "missing": result.get("missing_total", 0),
                "repaired": result.get("repaired_total", 0),
            })
            return {"ok": True, **result}

        d.addCallback(_ok)
        return d

    # ============================================================
    # 引擎
    # ============================================================
    def shutdown(self, graceful: bool = True, reason: str = "control") -> None:
        self._audit("shutdown", {"graceful": graceful, "reason": reason})
        self.engine.stop(graceful=graceful)

    # ============================================================
    # 运行时任务持久化（重启自动重放）
    # ============================================================
    def save_runtime_tasks(self) -> Path:
        payload = {
            "namespace": self.namespace,
            "updated_at": datetime.now().isoformat(),
            "tasks": list(self._extra_specs.values()),
            "removed_ids": sorted(self._removed_ids),
        }
        path = write_json_atomic(self.runtime_tasks_path, payload, indent=2)
        return path

    def load_runtime_tasks(self, path: Optional[str | Path] = None) -> List[str]:
        """把上次运行期间新增的任务重新装回运行时。"""
        target = Path(path) if path else self.runtime_tasks_path
        data = read_json(target)
        if not isinstance(data, dict):
            return []
        runtime = self._runtime()
        if runtime is None:
            return []

        ids: List[str] = []
        for item in data.get("tasks") or []:
            try:
                task_id = self.add_task(item, autostart=False)
                ids.append(task_id)
            except Exception as exc:
                logger.warning(f"运行时任务重放失败 {item.get('task_id')}: {exc}")
        self._removed_ids = set(data.get("removed_ids") or [])
        if ids:
            logger.info(f"已重放上次运行新增的任务: {ids}")
        return ids

    # ============================================================
    # 统一指令入口（面板 / hub 命令文件都走这里）
    # ============================================================
    def apply_command(self, command: Mapping[str, Any]) -> Dict[str, Any]:
        """执行一条控制指令。

        支持::

            {"action": "add_task",     "spec": {...}}
            {"action": "remove_task",  "task_id": "..."}
            {"action": "pause_task" | "resume_task" | "stop_task" | "start_task", "task_id": "..."}
            {"action": "enable_component" | "disable_component", "component": "..."}
            {"action": "shutdown",     "graceful": true}
            {"action": "snapshot"}
            {"action": "status"}
        """
        action = str(command.get("action") or "").strip()
        try:
            if action == "add_task":
                spec = command.get("spec") or command.get("task") or {}
                return {"ok": True, "task_id": self.add_task(spec)}
            if action == "remove_task":
                return {"ok": self.remove_task(str(command.get("task_id") or ""))}
            if action in ("pause_task", "resume_task", "stop_task", "start_task"):
                task_id = str(command.get("task_id") or "")
                fn = getattr(self, action)
                if bool(fn(task_id)):
                    return {"ok": True, "task_id": task_id}
                return {
                    "ok": False,
                    "task_id": task_id,
                    "error": f"{action} 未生效：任务不存在，或当前状态不允许该切换",
                }
            if action in ("enable_component", "disable_component"):
                fn = getattr(self, action)
                return {"ok": bool(fn(str(command.get("component") or "")))}
            if action == "shutdown":
                self.shutdown(graceful=bool(command.get("graceful", True)))
                return {"ok": True}
            if action == "snapshot":
                monitor = self.engine.get_component("monitor")
                flush = getattr(monitor, "flush", None)
                if callable(flush):
                    path = flush()
                    return {"ok": True, "path": str(path)}
                return {"ok": False, "error": "监控组件未启用，无法落盘快照"}
            if action == "status":
                return {"ok": True, "engine": self.engine.snapshot()["engine"]}

            # ---- 行情采集服务 ----
            if action == "collector_status":
                return {"ok": True, **self.collector_status()}
            if action == "collector_interval":
                return self.collector_set_interval(
                    command.get("interval") or command.get("seconds"),
                    symbol=str(command.get("symbol") or ""),
                    period=str(command.get("period") or ""),
                )
            if action == "collector_add":
                symbol = str(command.get("symbol") or "")
                if not symbol:
                    return {"ok": False, "error": "collector_add 需要 symbol"}
                return self.collector_add(
                    symbol,
                    period=str(command.get("period") or ""),
                    interval=command.get("interval"),
                    **{
                        k: command[k] for k in ("adjust", "source", "lookback_days", "keep_days")
                        if k in command
                    },
                )
            if action == "collector_remove":
                return self.collector_remove(
                    str(command.get("symbol") or ""),
                    period=str(command.get("period") or ""),
                )
            if action == "collector_reload":
                return self.collector_reload(command.get("path"))
            if action == "collector_now":
                # 注意：这个 action 走 /api/command 时是在 worker 线程里执行的，
                # 同步等待没问题；面板另有一条专用的异步路由。
                return self.collector_now(str(command.get("symbol") or ""))
            return {"ok": False, "error": f"未知指令: {action!r}"}
        except Exception as exc:
            logger.error(f"控制指令执行失败 {action}: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}


def _norm_symbol(symbol: str) -> str:
    from app.domain.symbol import normalize_symbol

    return normalize_symbol(symbol)


def runtime_watch(task: Any) -> set:
    """一个任务实际关注的全部标的（自身标的 + watch_symbols）。"""
    syms = {getattr(task, "symbol", "")}
    meta = getattr(task, "meta", {}) or {}
    syms.update(meta.get("watch_symbols") or [])
    syms.discard("")
    return syms


__all__ = ["ControlCenter", "runtime_watch"]
