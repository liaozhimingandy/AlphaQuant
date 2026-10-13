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
from app.runtime.tasking.store import parse_ledger_dt
from app.common.jsonio import json_safe, read_json, write_json_atomic
from app.common.logging import logger

if TYPE_CHECKING:  # pragma: no cover
    from app.engine.engine import BaseQuantEngine


#: 周期定时器的中文名。定时器 ID 是代码里起的技术名字，
#: 直接摆在面板上等于让用户猜 ``maintenance_tick`` 是干嘛的。
TIMER_LABELS: Dict[str, str] = {
    "maintenance_tick": "维护计划扫描",
}

#: 每个定时器到底负责什么。"30 秒一拍"只是它的节拍，
#: 真正的答复是它在替谁干活 —— 这两件事不是一回事。
TIMER_NOTES: Dict[str, str] = {
    "maintenance_tick": (
        "每 30 秒扫一次维护计划，看有没有任务到点；"
        "到点的才发事件转任务，所以它是「检查者」不是「执行者」"
    ),
}


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
    # 定向采集：指定标的 × 指定类别
    # ============================================================
    def cross_collect(self, symbols: Any = None, categories: Any = None,
                      wait: bool = False) -> Dict[str, Any]:
        """采**指定标的的指定类别**（基本面 / 资金流 / 龙虎榜…），立刻执行。

        与 :meth:`collector_now` 的分工：后者补 K 线，这里补日频截面。
        "把 600519 和 000858 的基本面现在采一遍"走这条路径 ——
        既不用改 services.json 重启服务，也不用等盘后定稿窗口。

        :param symbols: 标的列表或逗号分隔字符串；空 = 跟随本服务配置的截面标的
        :param categories: 类别列表；空 = 只采 ``fundamental``（基本面）
        :param wait: True=同步等待结果（CLI），False=丢后台（面板/hub 指令）
        """
        comp = self._collector()
        if comp is None:
            return {"ok": False, "error": "本引擎未装配采集服务"}
        fn = getattr(comp, "collect_categories", None)
        if not callable(fn):
            return {"ok": False, "error": "当前采集服务不支持定向采集"}

        syms = _as_symbols(symbols)
        cats = _as_symbols(categories)
        audit_detail = {"symbols": syms or "*", "categories": cats or ["fundamental"]}

        def _capture_err(f: Any) -> Dict[str, Any]:
            msg = str(f.getErrorMessage() if hasattr(f, "getErrorMessage") else f)
            logger.error(f"定向采集失败: {msg}")
            return {"error": msg}

        if wait:
            box: Dict[str, Any] = {}
            failure: Dict[str, Any] = {}
            d = fn(syms, cats)
            d.addCallback(lambda r: box.update(r))
            d.addErrback(lambda f: failure.update(_capture_err(f)))
            # CLI 是同步上下文，只能等。但必须挂 errback：
            # 否则采挂了只能干等到超时，真实错误反而被吞掉。
            deadline = time.time() + 300.0
            while not box and not failure and time.time() < deadline:
                time.sleep(0.05)
            if failure:
                return {"ok": False, **failure}
            if not box:
                return {"ok": False, "error": "定向采集超时（300s）"}
            self._audit("cross_collect", audit_detail)
            return {"ok": True, **box}

        def _done(result: Any) -> Any:
            self._audit("cross_collect", audit_detail)
            return result

        d = fn(syms, cats)
        d.addCallback(_done)
        d.addErrback(_capture_err)
        return {
            "ok": True, "accepted": True,
            "symbols": syms or "跟随服务配置",
            "categories": cats or ["fundamental"],
            "note": "已在后台采集，结果见 /api/collector 的 cross.last_report",
        }

    def cross_collect_async(self, symbols: Any = None, categories: Any = None):
        """异步版定向采集，返回 Deferred —— 面板/CLI 路由用它等真实结果。

        必须走这一条而不是 :meth:`cross_collect` 的同步等待：
        ``/api/command`` 是在 reactor 线程上执行的，在那儿 time.sleep
        会把行情、策略、撮合、面板一起堵住。
        """
        from twisted.internet import defer

        comp = self._collector()
        if comp is None:
            return defer.succeed({"ok": False, "error": "本引擎未装配采集服务"})
        fn = getattr(comp, "collect_categories", None)
        if not callable(fn):
            return defer.succeed({"ok": False, "error": "当前采集服务不支持定向采集"})

        syms = _as_symbols(symbols)
        cats = _as_symbols(categories)

        def _ok(result: Any) -> Dict[str, Any]:
            self._audit(
                "cross_collect",
                {"symbols": syms or "*", "categories": cats or ["fundamental"]},
            )
            detail = (result or {}).get("detail") or {}
            # 只要有一项真的失败就报失败：面板要能一眼看出"没采全"，
            # 而不是给出一片绿的"成功 0 条"。
            bad = [
                k for k, v in detail.items()
                if v.get("error") or v.get("failed")
            ]
            return {"ok": not bad, "failed_items": bad, **(result or {})}

        d = fn(syms, cats)
        d.addCallback(_ok)
        d.addErrback(lambda f: {"ok": False, "error": str(
            f.getErrorMessage() if hasattr(f, "getErrorMessage") else f)})
        return d

    # ============================================================
    # 任务系统：事件 → 任务 → 执行池
    #
    # 这一组回答的是事件驱动架构里最难回答的问题：
    #   "现在系统在做什么？哪一步卡住了？"
    # 服务互相直接调用时这个问题无解（调用栈一闪而过，不留痕迹）；
    # 有了任务队列与状态机之后，它是"读一张表"就能回答的。
    # ============================================================
    def _event_scheduler(self) -> Any:
        """取事件驱动版调度器。

        用 ``hasattr(sch, "store")`` 而不是 isinstance 判断：
        调度器可能被替换/包装，而"有没有任务存储"才是这里真正依赖的能力。
        """
        sch = self.engine.get_component("scheduler")
        if sch is None or not hasattr(sch, "store"):
            return None
        return sch

    def runtime_system(self) -> Dict[str, Any]:
        """任务系统全景：队列积压 / 执行池占用 / 规则 / 处理器 / 死信 / 维护计划。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        return {
            "ok": True,
            "stats": dict(sch.stats),
            "store": sch.store.snapshot(),
            "queues": sch.store.queues(),
            "pending": sch.store.pending_count(),
            "running": sch.store.running_count(),
            "running_tasks": sch.store.running(30),
            "pools": sch.pools.snapshot(),
            "router": sch.router.snapshot(),
            "rules": sch.rules.snapshot(),
            "handlers": sch.handlers.snapshot(),
            "dead_letters": sch.store.dead_letters(30),
            "dead_letter_count": sch.store.dead_letter_count(),
            "maintenance": sch.maintenance.snapshot(),
            "recovery": dict(getattr(sch, "recovery_report", None) or {}),
        }

    # ============================================================
    # 错误队列：人工干预
    #
    # 死信不是"失败日志"，是**待办清单**。清单的价值在于有人处理它，
    # 所以这里必须提供"继续"和"算了"两个动作 —— 只有清空的话，
    # 真正该重跑的任务会被一起清掉，而系统无从判断。
    # ============================================================
    def dead_retry(self, task_id: str) -> Dict[str, Any]:
        """把一条死信重新投回队列（人工点"继续"）。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        if not task_id:
            return {"ok": False, "error": "缺少 task_id"}
        try:
            ok, reason = sch.dead_retry(task_id)
        except Exception as exc:
            return {"ok": False, "error": f"重投失败: {exc}"}
        self._audit("dead_retry", {"task_id": task_id, "ok": ok, "reason": reason})
        return {"ok": ok, "reason": reason, "task_id": task_id}

    def dead_discard(self, task_id: str) -> Dict[str, Any]:
        """认掉一条死信：不重跑，从待办清单里划掉。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        if not task_id:
            return {"ok": False, "error": "缺少 task_id"}
        try:
            ok, reason = sch.dead_discard(task_id)
        except Exception as exc:
            return {"ok": False, "error": f"处置失败: {exc}"}
        self._audit("dead_discard", {"task_id": task_id, "ok": ok})
        return {"ok": ok, "reason": reason, "task_id": task_id}

    def dead_retry_all(self, task_type: str = "") -> Dict[str, Any]:
        """批量重投。不可重投的类型（下单）会被跳过而不是硬来。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        try:
            done, skipped = sch.dead_retry_all(task_type)
        except Exception as exc:
            return {"ok": False, "error": f"批量重投失败: {exc}"}
        self._audit("dead_retry_all", {"task_type": task_type or "*",
                                       "done": done, "skipped": skipped})
        return {"ok": True, "done": done, "skipped": skipped}

    def journal_view(self) -> Dict[str, Any]:
        """任务流水账状态 + 死信处置历史。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        store = sch.store
        journal = getattr(store, "journal", None)
        if journal is None:
            return {"ok": True, "enabled": False, "reason": "未启用持久化"}
        try:
            counts = journal.counts()
            history = journal.dead_history(50)
        except Exception as exc:
            return {"ok": False, "error": f"读取流水账失败: {exc}"}
        return {
            "ok": True,
            "enabled": True,
            "counts": counts,
            "dead_history": history,
            "recovered": {
                k: v for k, v in (getattr(sch, "recovery_report", None) or {}).items()
                if k != "items"
            },
        }

    # ============================================================
    # 任务明细 / 事件指标（面板核对数据用）
    #
    # 这两个视图回答的是同一类问题："系统说它做了，我怎么确认它真做了"。
    # 只有汇总数字时这个问题答不了 —— "成功 1200 条"和
    # "这 1200 条分别是哪些、参数是什么、耗时多久"是两种可信度。
    # ============================================================
    def task_ledger(
        self,
        status: str = "",
        task_type: str = "",
        queue: str = "",
        keyword: str = "",
        limit: int = 50,
        since: str = "",
        until: str = "",
        order: str = "desc",
        sort: str = "time",
    ) -> Dict[str, Any]:
        """已建任务 + 终态历史，合并成**一份**排好序的明细。

        ``rows`` 是唯一该被渲染的那份数据；``tasks`` / ``history`` 保留给
        既要"现在还活着的任务"又要"已淘汰的记录"的调用方。
        两者分开看容易算重（同一条任务两边都有），所以列表一律用 ``rows``。

        :param status: 精确状态或状态组名。``failed`` 会把"失败后正在重试"
                       的也算进来 —— ``failed`` 在存储里从不停留，
                       精确匹配永远是空
        :param since/until: 创建时间的上下界，``YYYY-MM-DD`` 或完整 ISO
        :param sort: ``time``（创建时间）/ ``elapsed``（耗时）/ ``attempts``
        :param order: ``desc``（默认，最新在前）/ ``asc``
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        store = sch.store
        limit = max(1, min(int(limit or 50), 500))
        since_dt = parse_ledger_dt(since)
        until_dt = parse_ledger_dt(until, end=True)
        ledger = store.ledger_rows(
            status=status, task_type=task_type, queue=queue, keyword=keyword,
            limit=limit, since=since_dt, until=until_dt, order=order, sort=sort,
        )
        return {
            "ok": True,
            "rows": ledger["rows"],
            # 计数与明细同源：按钮上写 N，点下去就是 N 条（超出 limit 除外）
            "status_counts": ledger["status_counts"],
            "total": ledger["total"],
            "scoped": ledger["scoped"],
            "matched": ledger["matched"],
            "filters": {
                "status": status, "task_type": task_type, "queue": queue,
                "keyword": keyword, "limit": limit,
                "since": since, "until": until, "sort": sort, "order": order,
            },
            "tasks": store.tasks(status=status, task_type=task_type,
                                 queue=queue, keyword=keyword, limit=limit,
                                 since=since_dt, until=until_dt),
            "history": store.history(status=status, task_type=task_type,
                                     limit=limit, since=since_dt, until=until_dt),
            "history_summary": store.history_summary(),
            "summary": {
                "success": store.stats.get("success", 0),
                "failed": store.stats.get("failed", 0),
                "dead_letter": store.stats.get("dead_letter", 0),
                "cancelled": store.stats.get("cancelled", 0),
                "created": store.stats.get("created", 0),
            },
            "task_types": store.task_types_seen(),
            "tracked": len(store.tasks(limit=0)),
            "history_size": len(store.history(limit=0)),
        }

    def engine_task_detail(self, task_id: str) -> Dict[str, Any]:
        """单条引擎任务的完整明细（区别于旧式 QuantTask 的 ``task_detail``）。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        detail = sch.store.task_detail(str(task_id or ""))
        if detail is None:
            return {"ok": False, "error": f"没有这条任务: {task_id}（可能已被清理）"}
        return {"ok": True, "task": detail}

    def event_metrics(self, limit: int = 0, event_type: str = "") -> Dict[str, Any]:
        """事件指标：每种事件发布了几条、有没有人听、处理失败多少。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        bus = getattr(sch, "bus", None)
        if bus is None:
            return {"ok": False, "error": "事件总线不可用"}
        try:
            types = bus.event_metrics(limit)
            if event_type:
                detail = bus.event_detail(event_type)
                samples = bus.recent(20, event_type=event_type)
            else:
                detail = None
                samples = bus.recent(20)
        except Exception as exc:
            return {"ok": False, "error": f"读取事件指标失败: {exc}"}
        return {
            "ok": True,
            "stats": dict(getattr(bus, "stats", {}) or {}),
            "types": types,
            "detail": detail,
            "recent": samples,
            "known_types": bus.known_event_types(),
            "store": (bus.store.snapshot()
                      if getattr(bus, "store", None) is not None
                      else {"enabled": False}),
        }

    # ============================================================
    # 链路追踪 / 事件重放
    # ============================================================
    def trace_view(
        self,
        trace_id: str = "",
        event_id: str = "",
        limit: int = 30,
    ) -> Dict[str, Any]:
        """一条链路的全景：事件链 + 链上产生的任务。

        事件和任务记在**两本账**里（events.db / tasks.db），
        靠 trace_id 对齐。分开记的理由是写入频率差一个量级、
        清理策略也不同；代价是不能 SQL JOIN，只能在 Python 里拼 ——
        对每天几千条的量级，这个代价远小于引入额外依赖。
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        store = getattr(sch, "event_store", None)
        if store is None or not getattr(store, "enabled", False):
            return {"ok": False, "error": "事件流水账未启用（事件只在内存广播）"}

        tid = str(trace_id or "")
        if not tid and event_id:
            row = store.get(str(event_id))
            if not row:
                return {"ok": False, "error": f"没有这条事件: {event_id}"}
            tid = row.get("trace_id") or ""

        events = store.trace(tid, limit=max(1, int(limit)) * 4) if tid else []
        if not events and tid:
            return {"ok": False, "error": f"链路 {tid} 上没有事件（可能已被清理）"}

        # 链上任务：任务表按 correlation_id 找，取不到就退化为空列表
        tasks: List[Dict[str, Any]] = []
        try:
            tasks = sch.store.tasks(keyword=tid, limit=50) if tid else []
        except Exception:
            tasks = []

        return {
            "ok": True,
            "trace_id": tid,
            "events": events,
            "tasks": tasks,
            "counts": {
                "events": len(events),
                "tasks": len(tasks),
            },
            "timeline": self._merge_timeline(events, tasks),
            "traces": store.traces(limit=max(1, int(limit))),
            "store": store.snapshot(),
        }

    @staticmethod
    def _merge_timeline(
        events: List[Dict[str, Any]], tasks: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """把事件与任务拼成一条时间线。

        排序键用 ``published_at`` / ``created_at`` 的**字符串**比较：
        两本账都用 ``isoformat(timespec="seconds")``，字典序等于时间序，
        混用 datetime 与字符串反而会抛类型错误。
        """
        out: List[Dict[str, Any]] = []
        for e in events:
            out.append({
                "kind": "event",
                "at": e.get("published_at") or e.get("occurred_at") or "",
                "name": e.get("event_type") or "",
                "id": e.get("event_id") or "",
                "source": e.get("source") or "",
                "subscribers": e.get("subscribers") or 0,
                "replay_of": e.get("replay_of") or "",
                "data": e.get("data") or {},
            })
        for t in tasks:
            out.append({
                "kind": "task",
                "at": t.get("created_at") or "",
                "name": t.get("task_type") or "",
                "id": t.get("task_id") or "",
                "status": t.get("status") or "",
                "attempts": t.get("attempts") or 0,
                "error": t.get("error") or "",
            })
        out.sort(key=lambda x: str(x.get("at") or ""))
        return out

    def replay_event(self, event_id: str, by: str = "panel") -> Dict[str, Any]:
        """重放一条历史事件。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        bus = getattr(sch, "bus", None)
        if bus is None or getattr(bus, "store", None) is None:
            return {"ok": False, "error": "事件流水账未启用，没有可重放的历史"}
        try:
            evt = bus.replay(str(event_id or ""), by=str(by or "panel"))
        except Exception as exc:
            return {"ok": False, "error": f"重放失败: {exc}"}
        if evt is None:
            return {"ok": False, "error": f"没有这条事件: {event_id}"}
        return {
            "ok": True,
            "event_id": evt.event_id,
            "origin_id": str(event_id),
            "event_type": evt.event_type,
            "trace_id": evt.trace_id,
        }

    # ============================================================
    # 启动预检
    # ============================================================
    def preflight_view(self) -> Dict[str, Any]:
        """最近一次启动预检的结论。

        引擎还没跑过预检时返回 ``ran=False`` —— 与"跑过且通过"
        是两回事，面板不该把它们显示成同一个样子。
        """
        report = getattr(self.engine, "preflight", None)
        if report is None:
            return {"ok": True, "ran": False, "reason": "本次运行尚未执行启动预检"}
        return {"ok": True, "ran": True, **report.to_dict()}

    def preflight_run(self) -> Dict[str, Any]:
        """当场再跑一遍预检（改完配置想立刻验证时用）。"""
        try:
            from app.engine.preflight import run_preflight
        except Exception as exc:
            return {"ok": False, "error": f"预检模块不可用: {exc}"}
        sch = self._event_scheduler()
        journal = getattr(getattr(sch, "store", None), "journal", None)
        mode = str(getattr(getattr(self.engine, "run_mode", None), "value", "") or "")
        try:
            report = run_preflight(mode=mode, journal=journal)
        except Exception as exc:
            return {"ok": False, "error": f"预检执行失败: {exc}"}
        self.engine.preflight = report
        self._audit("preflight_run", {"ok": report.ok,
                                      "errors": len(report.errors),
                                      "warnings": len(report.warnings)})
        return {"ok": True, **report.to_dict()}

    def clear_dead_letters(self) -> Dict[str, Any]:
        """清空死信队列（运维处理完这批之后用）。

        死信是待办清单不是账本 —— 处理完还挂着，健康判定会一直报
        "有死信"，告警就失去意义了。累计计数保留在 ``stats`` 里，
        清掉的只是"还有几条没处理"。
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        cleared = sch.store.clear_dead_letters()
        self._audit("clear_dead_letters", {"cleared": cleared})
        return {"ok": True, "cleared": cleared}

    # ============================================================
    # 维护计划（日志清理 / 归档 / 校验）
    # ============================================================
    def maintenance_view(self) -> Dict[str, Any]:
        """维护计划与它的运行态（下次执行时间、上次结果）。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        return {"ok": True, **sch.maintenance.snapshot()}

    def maintenance_save(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """保存维护计划。改完立刻生效并落盘（重启仍在）。

        安全约束在这里**再拦一道**：清理/归档的 ``apply`` 不会因为
        手滑配错就变成真删 —— 见 :meth:`_guard_destructive`。
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        guard = self._guard_destructive(payload)
        if guard is not None:
            return guard
        try:
            snap = sch.maintenance.update(payload or {})
        except Exception as exc:
            return {"ok": False, "error": f"保存失败: {exc}"}
        self._audit("maintenance_save", {"jobs": sorted(
            (payload or {}).get("jobs", payload or {}).keys())})
        return {"ok": True, **snap}

    @staticmethod
    def _guard_destructive(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """拦截危险的维护配置。

        :return: None = 放行；否则是要直接返回给调用方的错误响应。

        两条规定都是真实事故的形状：
          1. 保留天数/归档天数 < 1 → 一拍清空整个目录；
          2. 清理周期短于 1 小时 → 每天扫几十遍，日志被自己的清理日志刷满。
        """
        raw = (payload or {}).get("jobs")
        if not isinstance(raw, dict):
            raw = dict(payload or {})
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            params = cfg.get("params") or {}
            for key in ("retention_days", "before_days"):
                if key in params:
                    try:
                        if int(params[key]) < 1:
                            return {
                                "ok": False,
                                "error": f"{name}.{key} 必须 >= 1 天（收到 {params[key]}）",
                            }
                    except (TypeError, ValueError):
                        return {"ok": False, "error": f"{name}.{key} 不是整数"}
            sched = str(cfg.get("schedule") or "").lower()
            if sched == "interval":
                # **只在旧字段 interval_sec 真的写了才校验**。
                # 新配置用 interval_minutes，写 `cfg.get("interval_sec") or 0`
                # 会拿到 0，于是 0 < 300 恒成立 —— 任何周期任务都会被
                # 这条护栏误拦（包括"十分钟刷一次股票池"这种正当配置）。
                # 新字段的下限已由 MaintenanceJob.interval_seconds_value()
                # 按任务类别兜底（维护类 1 小时、业务类 1 分钟），这里不重复设限。
                raw_sec = cfg.get("interval_sec")
                if raw_sec is None or str(raw_sec).strip() == "":
                    continue
                try:
                    if int(raw_sec) < 300:
                        return {
                            "ok": False,
                            "error": f"{name} 的周期必须 >= 300 秒（太快会把日志刷满）",
                        }
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{name}.interval_sec 不是整数"}
        return None

    def maintenance_run(self, name: str) -> Dict[str, Any]:
        """立刻跑一次维护任务（不看周期与开关 —— 手动就是要立刻跑）。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        result = sch.maintenance.run_now(str(name or ""))
        if result.get("ok"):
            self._audit("maintenance_run", {"job": name})
        return result

    def maintenance_add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """新增一条定时任务（周期性或一次性）。

        这是"调度可配置"的入口：以前任务清单写死在 ``DEFAULT_JOBS`` 里，
        要加一类周期跑的活只能改代码；现在往配置里写一条就能生效。

        危险参数仍走 :meth:`_guard_destructive` —— 新增通道不该成为绕过口。
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "缺少任务名（name）"}
        cfg = payload.get("cfg")
        if not isinstance(cfg, dict):
            cfg = {k: v for k, v in payload.items() if k != "name"}
        cfg = dict(cfg)
        cfg.setdefault("task_type", payload.get("task_type") or "")
        guard = self._guard_destructive({name: cfg})
        if guard is not None:
            return guard
        try:
            result = sch.maintenance.add_job(name, cfg)
        except Exception as exc:
            return {"ok": False, "error": f"新增失败: {exc}"}
        if result.get("ok"):
            self._audit("maintenance_add", {"job": name,
                                            "task_type": cfg.get("task_type")})
        return result

    def maintenance_remove(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """删除一条自定义定时任务（内置任务只能停用）。"""
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "缺少任务名（name）"}
        try:
            result = sch.maintenance.remove_job(name)
        except Exception as exc:
            return {"ok": False, "error": f"删除失败: {exc}"}
        if result.get("ok"):
            self._audit("maintenance_remove", {"job": name})
        return result

    # ============================================================
    # 内置定时任务总清单
    # ============================================================
    def builtin_schedules(self) -> Dict[str, Any]:
        """系统里**所有会自动执行的事**的一份清单。

        为什么要单独有这一屏：定时任务散在四个地方 ——

          1. ``MaintenancePlanner`` 的任务（``config/maintenance.json`` 驱动）
          2. 调度器注册的周期定时器（代码里 ``start_timer`` 出来的节拍源）
          3. 采集服务内部的四个单元（各自一个 ``LoopingCall``）

        它们的周期、数据来源、能不能改各不相同，但用户关心的只有一个问题：
        **"这台机器自己会做哪些事，多久做一次"**。以前答这个问题要翻三处代码，
        现在是读一条接口。

        ``source`` 区分 "@可以改" 与 "只能改代码"：面板上标错这两者的代价比
        标漏更严重 —— 用户会以为改了配置就有用，然后等一个永远不会来的执行。
        """
        items: List[Dict[str, Any]] = []
        sch = self._event_scheduler()
        if sch is not None:
            self._collect_maintenance_schedules(sch, items)
            self._collect_timer_schedules(sch, items)
        # 采集单元挂在引擎组件上，与调度器是否存在无关，单独取
        self._collect_collection_schedules(items)
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "enabled": sum(1 for i in items if i.get("enabled")),
            "configurable": sum(1 for i in items if i.get("editable")),
        }

    def _collect_maintenance_schedules(self, sch: Any, items: List[Dict[str, Any]]) -> None:
        try:
            snap = sch.maintenance.snapshot()
        except Exception as exc:  # pragma: no cover - 计划器不可用不算致命
            logger.warning(f"读取维护计划失败（已跳过）: {exc}")
            return
        for job in snap.get("jobs") or []:
            cadence = self._maintenance_cadence(job)
            items.append({
                "name": str(job.get("name") or ""),
                "label": str(job.get("label") or job.get("name") or ""),
                "group": "维护计划",
                "source": "配置",
                "editable": True,
                "task_type": str(job.get("task_type") or ""),
                "enabled": bool(job.get("enabled")),
                "kind": str(job.get("schedule") or ""),
                "kind_label": str(job.get("schedule_label") or ""),
                "cadence": cadence,
                "interval_text": cadence,
                "interval_seconds": float(job.get("interval_seconds") or 0.0),
                "trading_hours_only": bool(job.get("trading_hours_only")),
                "waiting_market": bool(job.get("waiting_market")),
                "next_run_text": str(job.get("next_run_text") or "—"),
                "last_run_text": str(job.get("last_run_iso") or "从未执行"),
                "runs": int(job.get("runs") or 0),
                "errors": int(job.get("errors") or 0),
                "config_error": str(job.get("config_error") or ""),
                "where": "config/maintenance.json",
                "note": str(job.get("description") or ""),
            })

    @staticmethod
    def _maintenance_cadence(job: Dict[str, Any]) -> str:
        """把一种周期翻译成人话。七种周期各有自己的字段，落到一行里要说清。"""
        kind = str(job.get("schedule") or "")
        minutes = int(job.get("interval_minutes") or 0)
        if kind == "interval":
            if minutes > 0:
                return f"每 {minutes} 分钟"
            hours = job.get("interval_hours") or 0
            return f"每 {hours} 小时"
        if kind == "daily":
            return f"每天 {job.get('at') or '?'}"
        if kind == "weekly":
            return f"每{job.get('weekday_label') or '周?'} {job.get('at') or '?'}"
        if kind == "monthly":
            return f"每月 {job.get('day_of_month')} 号 {job.get('at') or '?'}"
        if kind == "cron":
            return str(job.get("cron_text") or job.get("cron") or "cron 未填")
        if kind == "once":
            return f"一次性（{job.get('once_at') or '时刻未填'}）"
        if kind == "startup":
            return "引擎每次启动一次"
        return kind or "未知周期"

    def _collect_timer_schedules(self, sch: Any, items: List[Dict[str, Any]]) -> None:
        try:
            timers = sch.timers() or []
        except Exception as exc:  # pragma: no cover
            logger.debug(f"读取周期定时器失败（已跳过）: {exc}")
            return
        for timer in timers:
            tid = str(timer.get("timer_id") or "")
            seconds = float(timer.get("interval") or 0.0)
            running = bool(timer.get("running"))
            items.append({
                "name": tid,
                "label": TIMER_LABELS.get(tid, tid),
                "group": "节拍定时器",
                "source": "代码",
                "editable": False,
                "task_type": "",
                "enabled": running,
                "kind": "timer",
                "kind_label": "周期发事件",
                "cadence": self._humanize_seconds(seconds),
                "interval_text": self._humanize_seconds(seconds),
                "interval_seconds": seconds,
                "trading_hours_only": False,
                "waiting_market": False,
                "next_run_text": "持续运行中" if running else "未运行",
                "last_run_text": "—",
                "runs": 0,
                "errors": 0,
                "config_error": "",
                "where": "代码注册",
                "note": TIMER_NOTES.get(tid, "周期性发出节拍事件，由订阅它的规则转成具体任务"),
            })

    @staticmethod
    def _humanize_seconds(seconds: float, bare: bool = False) -> str:
        """秒数 → 「每 N 分钟」（``bare=True`` 时省掉「每」，用于嵌进句子里）。"""
        seconds = float(seconds or 0.0)
        if seconds <= 0:
            return "—" if bare else "—"
        if seconds < 90:
            text = f"{seconds:g} 秒"
        elif seconds < 5400:
            text = f"{seconds / 60.0:g} 分钟"
        elif seconds < 172800:
            text = f"{seconds / 3600.0:g} 小时"
        else:
            text = f"{seconds / 86400.0:g} 天"
        return text if bare else f"每 {text}"

    def _collect_collection_schedules(self, items: List[Dict[str, Any]]) -> None:
        """采集服务四个单元的节拍。

        它们不走调度器也不用任务队列，而是各自一个 ``LoopingCall`` ——
        这是上一轮合并时保留的设计（节拍不同，合并后仍是四个单元）。
        代价是不进维护计划面板，所以必须在这里手工列出来，
        否则"系统会定时做什么"这份清单是缺的。
        """
        comp = None
        try:
            comp = self.engine.get_component("collection_service")
        except Exception as exc:
            logger.debug(f"取不到采集服务（未装配？）: {exc}")
            return
        if comp is None:
            return
        try:
            snap = comp.snapshot() or {}
        except Exception as exc:  # pragma: no cover
            logger.warning(f"读取采集服务快照失败（已跳过）: {exc}")
            return

        def _add(name: str, label: str, unit: Dict[str, Any], cadence: str,
                 seconds: float, note: str, interval_key: str = "") -> None:
            stats = unit.get("stats") or {}
            items.append({
                "name": name,
                "label": label,
                "group": "采集单元",
                "source": "配置",
                "editable": True,
                "task_type": "",
                "enabled": bool(unit.get("enabled")),
                "kind": "loop",
                "kind_label": "常驻循环",
                "cadence": cadence,
                "interval_text": cadence,
                "interval_seconds": float(seconds or 0.0),
                "trading_hours_only": bool(unit.get("trading_hours_only")),
                "waiting_market": False,
                "next_run_text": "运行中" if unit.get("running") else "未运行",
                "last_run_text": "—",
                "runs": int(stats.get("runs") or 0),
                "errors": int(stats.get("errors") or 0),
                "config_error": str(unit.get("disabled_reason") or ""),
                "where": "config/collector.json",
                "note": note,
            })

        kline = snap.get("kline") or {}
        if kline:
            buckets = sorted(kline.get("buckets") or [])
            jobs = kline.get("jobs") or []
            # 多个频率就是多个 LoopingCall，说"每 60 秒"会漏掉慢的那批 ——
            # 明说区间，人才不会对得上了一半觉得错
            if not buckets:
                cadence = "按任务各自频率"
            elif len(buckets) == 1:
                cadence = self._humanize_seconds(buckets[0])
            else:
                cadence = (self._humanize_seconds(buckets[0])
                           + " ~ " + self._humanize_seconds(buckets[-1]))
            _add(
                "collection.kline", "K 线采集", kline, cadence,
                buckets[0] if buckets else 0.0,
                f"{len(jobs)} 个采集任务，分 {len(buckets)} 种频率各跑一个循环",
            )
        cross = snap.get("cross") or {}
        if cross:
            _add(
                "collection.cross", "日频截面采集", cross,
                "盘中每 " + self._humanize_seconds(cross.get("interval"), bare=True)
                + "，盘后再补一次",
                float(cross.get("interval") or 0.0),
                "资金流 / 筹码 / 基本面等日频数据，"
                + (f"下一次窗口 {cross.get('next_slot') or '—'}"
                   if cross.get("next_slot") else "按交易时段自动安排"),
            )
        guard = snap.get("guard") or {}
        if guard:
            _add(
                "collection.guard", "数据完整性与补全", guard,
                self._humanize_seconds(guard.get("interval")),
                float(guard.get("interval") or 0.0),
                f"启动后延迟 {guard.get('start_delay')} 秒首检，自动补全 {guard.get('auto_repair')}",
            )
        watch = snap.get("watchlist") or {}
        if watch:
            cfg = watch.get("config") or {}
            _add(
                "collection.watchlist", "关注标的采集", watch,
                f"扫描节拍 "
                + self._humanize_seconds(cfg.get("sweep_seconds"), bare=True)
                + f"，标的默认 {cfg.get('interval') or '?'}",
                float(cfg.get("sweep_seconds") or 0.0),
                f"共 {watch.get('total') or 0} 只关注标的，其中 {watch.get('enabled_count') or 0} 只启用"
                + f"，当前到期 {watch.get('due_count') or 0} 只",
            )

    # ============================================================
    # 日志级别（运行中可调，不用重启）
    # ============================================================
    def logging_view(self) -> Dict[str, Any]:
        """当前生效的日志配置。"""
        from app.common.logging import current_levels

        return {"ok": True, **current_levels()}

    def set_log_level(
        self,
        console: Optional[str] = None,
        file: Optional[str] = None,
        throttle_window: Optional[float] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """运行中切换日志级别。

        排查线上问题时最尴尬的是"想看 DEBUG 就得重启，而重启正好把
        要排查的那个状态冲掉"。这里让它热切换，并可选持久化。
        """
        from app.common.logging import _LEVELS, set_level

        if console is not None:
            lv = str(console).strip().upper()
            if lv and lv not in _LEVELS:
                return {"ok": False, "error": f"非法级别: {console}", "levels": list(_LEVELS)}
            console = lv or None
        if file is not None and file != "":
            lv = str(file).strip().upper()
            if lv not in _LEVELS:
                return {"ok": False, "error": f"非法级别: {file}", "levels": list(_LEVELS)}
            file = lv
        try:
            result = set_level(
                console=console, file=file,
                throttle_window=throttle_window, persist=bool(persist),
            )
        except Exception as exc:
            return {"ok": False, "error": f"切换失败: {exc}"}
        self._audit("set_log_level", {
            "console": result.get("console"), "file": result.get("file"),
            "throttle_window": result.get("throttle_window"), "persist": bool(persist),
        })
        return {"ok": True, **result}

    def dispatch_task(
        self,
        task_type: str = "",
        params: Any = None,
        priority: Any = None,
        dedup_key: str = "",
        max_retries: int = 0,
    ) -> Dict[str, Any]:
        """手动投递一个**带类型**的引擎任务（走完整路由与执行池）。

        与 :meth:`add_task` 的区别：``add_task`` 加的是量化任务
        （"标的 + 策略 + 风控 + 账本"的运行实例），这里投的是引擎任务
        （采集 / 因子 / 策略 / 风控 / 运维这类一次性工作）。

        未知任务类型当场报错，不给"投递成功但没人执行"的假成功。
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        task_type = str(task_type or "").strip()
        if not task_type:
            return {"ok": False, "error": "缺少 task_type"}
        if not sch.handlers.has(task_type):
            return {
                "ok": False,
                "error": f"任务类型 {task_type} 没有注册处理器",
                "known": sch.handlers.task_types(),
            }
        pool = sch.router.pool_for(task_type)
        if pool is None:
            return {"ok": False, "error": f"没有执行池能处理 {task_type}"}

        prio = priority
        code = getattr(priority, "code", None)
        if code is not None:
            prio = int(code) * 2 + 1
        ok, reason, task_id = sch.submit_engine_task_detail(
            task_type=task_type,
            params=dict(params or {}),
            priority=int(prio) if prio is not None else 5,
            dedup_key=str(dedup_key or ""),
            max_retries=int(max_retries or 0),
        )
        self._audit(
            "dispatch_task",
            {"task_type": task_type, "pool": pool, "params": dict(params or {}),
             "task_id": task_id},
        )
        return {
            "ok": ok, "reason": reason, "pool": pool,
            "task_type": task_type, "task_id": task_id,
        }

    def set_rule_enabled(self, name: str, enabled: bool = True) -> Dict[str, Any]:
        """启停一条调度规则。

        用途：临时掐断某条自动链路（比如"因子更新就跑策略"在
        补采历史数据时会造成大量无意义的策略运行），
        不必改代码、不必重启引擎。
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        name = str(name or "").strip()
        if not sch.rules.set_enabled(name, bool(enabled)):
            return {
                "ok": False,
                "error": f"没有这条规则: {name}",
                "known": [r.name for r in sch.rules.all_rules()],
            }
        self._audit("set_rule", {"rule": name, "enabled": bool(enabled)})
        return {"ok": True, "rule": name, "enabled": bool(enabled)}

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
            {"action": "collector_now", "symbol": "600519"}
            {"action": "cross_collect", "symbols": ["600519"],
             "categories": ["fundamental"]}   # 定向采基本面，缺省即 fundamental
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
            if action == "cross_collect":
                # 定向采集：{"action":"cross_collect","symbols":["600519"],
                #            "categories":["fundamental"]}
                # 走 hub 命令文件时保持异步（默认），CLI 可传 wait=true。
                return self.cross_collect(
                    command.get("symbols"),
                    command.get("categories"),
                    wait=bool(command.get("wait", False)),
                )
            return {"ok": False, "error": f"未知指令: {action!r}"}
        except Exception as exc:
            logger.error(f"控制指令执行失败 {action}: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}


def _norm_symbol(symbol: str) -> str:
    from app.domain.symbol import normalize_symbol

    return normalize_symbol(symbol)


def _as_symbols(v: Any) -> List[str]:
    """把 ``"600519,000858"`` 或 ``["600519"]`` 统一成标的列表。

    逗号分隔的字符串是 CLI 与 hub 指令文件里最常见的写法；
    不接受它，就得让每个调用方各自 split —— 迟早有人忘，
    然后把整串当成一只股票去查，得到"采集成功 0 条"。
    """
    if v is None:
        return []
    if isinstance(v, str):
        raw: List[Any] = v.split(",")
    elif isinstance(v, (list, tuple, set)):
        raw = list(v)
    else:
        raw = [v]
    return [str(x).strip() for x in raw if str(x).strip()]


def runtime_watch(task: Any) -> set:
    """一个任务实际关注的全部标的（自身标的 + watch_symbols）。"""
    syms = {getattr(task, "symbol", "")}
    meta = getattr(task, "meta", {}) or {}
    syms.update(meta.get("watch_symbols") or [])
    syms.discard("")
    return syms


__all__ = ["ControlCenter", "runtime_watch"]
