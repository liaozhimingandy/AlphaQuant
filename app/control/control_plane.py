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
    ) -> Dict[str, Any]:
        """已建任务 + 终态历史。

        ``tasks`` 是"现在还看得见的任务"（含在途），``history`` 是
        "最近做完的那些"。核对数据时两边一起看：成功数对不上，
        通常是任务被 ``_tasks`` 淘汰了，只有历史里还留着。
        """
        sch = self._event_scheduler()
        if sch is None:
            return {"ok": False, "error": "当前引擎没有事件驱动的调度器"}
        store = sch.store
        limit = max(1, min(int(limit or 50), 500))
        return {
            "ok": True,
            "tasks": store.tasks(status=status, task_type=task_type,
                                 queue=queue, keyword=keyword, limit=limit),
            "history": store.history(status=status, task_type=task_type,
                                     limit=limit),
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
                try:
                    if int(cfg.get("interval_sec") or 0) < 300:
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
        ok, reason = sch.submit_engine_task(
            task_type=task_type,
            params=dict(params or {}),
            priority=int(prio) if prio is not None else 5,
            dedup_key=str(dedup_key or ""),
            max_retries=int(max_retries or 0),
        )
        self._audit(
            "dispatch_task",
            {"task_type": task_type, "pool": pool, "params": dict(params or {})},
        )
        return {"ok": ok, "reason": reason, "pool": pool, "task_type": task_type}

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
