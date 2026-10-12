#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : runtime.py
# @Description : 多任务运行时：一个引擎里跑 N 个量化任务
#               行情按 symbol 广播，事件按关注列表定向投递，任务之间状态完全隔离
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from app.domain.types import Bar, NewsAnalysis, NewsItem, Order
from app.runtime.tasking.spec import TaskSpec, build_task, parse_task_specs
from app.runtime.tasking.task import QuantTask
from app.common.logging import logger


class TaskRuntime:
    """任务容器 + 事件分发器。

    为什么要有这一层，而不是让引擎直接持有任务：
      - **订阅关系**：一个标的的行情要广播给所有关注它的任务；
        一条新闻要投递给所有关注它的标的的任务。这个映射关系不该散落在引擎里
      - **故障隔离**：单个任务抛异常不能带走整个引擎，这里统一兜底
      - **可观测**：统一快照入口，服务状态查询不需要遍历引擎内部
    """

    def __init__(self) -> None:
        self.tasks: Dict[str, QuantTask] = {}
        self._by_symbol: Dict[str, List[str]] = {}
        self._order_sink: Optional[Callable[[Order], None]] = None
        #: 实盘共享网关的**延迟取值**provider。
        #: 用 lambda 而不是直接塞对象：任务是在组件 initialize 阶段才建的，
        #: 那时网关可能刚连上（甚至还没连），先取对象会拿到 None。
        self.gateway_provider: Optional[Callable[[], Any]] = None
        #: **全局**风控闸门的延迟取值 provider（由策略组件从 RiskService 注入）。
        #: 任务级风控链管"这个策略该不该开这么大"，
        #: 全局闸门管"整个账户还能不能开"——两者串联，缺一不可。
        #: 用 provider 而非直接塞对象，理由同网关：任务建得比服务早。
        self.risk_gate_provider: Optional[Callable[[], Any]] = None
        #: 跨任务额度预留器的延迟取值 provider（由策略组件从 PortfolioService 注入）。
        #: 风控在单任务账本上判断，看不见别的任务刚放行了一单；额度预留
        #: 补的正是这个跨任务的洞。用 provider 的理由同上。
        self.quota_provider: Optional[Callable[[], Any]] = None
        #: 引擎级运行模式。任务 spec 没写 run_mode 时跟随它 ——
        #: 否则 LIVE 起引擎而任务跑模拟撮合，是最危险的静默错配。
        self.run_mode: Any = None

    # ============================================================
    # 实盘共享网关
    # ============================================================
    def current_gateway(self) -> Any:
        if self.gateway_provider is None:
            return None
        try:
            return self.gateway_provider()
        except Exception:
            return None

    def current_risk_gate(self) -> Any:
        """取全局风控闸门。没有注入时返回 None（任务退化为只有自己的链）。"""
        if self.risk_gate_provider is None:
            return None
        try:
            return self.risk_gate_provider()
        except Exception:
            return None

    def current_quota(self) -> Any:
        """取额度预留器。没有注入时返回 None（不做跨任务额度控制）。"""
        if self.quota_provider is None:
            return None
        try:
            return self.quota_provider()
        except Exception:
            return None

    # ============================================================
    # 任务管理
    # ============================================================
    def add(self, task: QuantTask) -> QuantTask:
        if task.task_id in self.tasks:
            raise ValueError(f"任务 ID 重复: {task.task_id}")
        self.tasks[task.task_id] = task
        # 全局风控闸门在这里下发，于是**任何**进运行时的任务
        # （配置装载的、运行时新增的、手工构造的）都会被它覆盖到。
        # 靠 build_task 传参是漏的：运行时新增的任务不走那条路。
        if getattr(task, "global_gate", None) is None:
            gate = self.current_risk_gate()
            if gate is not None:
                task.global_gate = gate
        # 额度预留器同理在这里下发：运行时新增的任务（面板/CLI 下发的）
        # 不走 build_task 那条路，只在构造时传参必然漏掉它们。
        if getattr(task, "quota", None) is None:
            quota = self.current_quota()
            if quota is not None:
                task.quota = quota
        # 把订单出口交给任务。实盘的成交回报是**异步**到来的，
        # 那时已经不在 on_bar/on_news 的调用栈里，返回值机制覆盖不到；
        # 只有让任务自己能往外发订单事件，回报驱动的状态变化才进得了事件总线。
        task.set_order_sink(self._emit)
        for sym in self._watch_symbols(task):
            self._by_symbol.setdefault(sym, []).append(task.task_id)
        logger.info(f"注册任务 {task.task_id} | 标的 {task.symbol} | 关注 {self._watch_symbols(task)}")
        return task

    def add_from_spec(self, spec: TaskSpec | Mapping[str, Any]) -> QuantTask:
        return self.add(
            build_task(
                spec,
                gateway=self.current_gateway(),
                run_mode=self.run_mode,
            )
        )

    def add_from_config(self, raw: Any) -> List[QuantTask]:
        return [self.add_from_spec(s) for s in parse_task_specs(raw)]

    def remove(self, task_id: str) -> bool:
        task = self.tasks.pop(task_id, None)
        if task is None:
            return False
        task.stop()
        # 任务没了，它占着的额度必须还回去，否则删几次任务额度就见底了
        quota = self.current_quota()
        if quota is not None:
            try:
                quota.release_task(task_id, note="task_removed")
                quota.mark_dirty()
            except Exception:
                pass
        for sym, ids in list(self._by_symbol.items()):
            if task_id in ids:
                ids.remove(task_id)
                if not ids:
                    self._by_symbol.pop(sym, None)
        return True

    def get(self, task_id: str) -> Optional[QuantTask]:
        return self.tasks.get(task_id)

    def tasks_for(self, symbol: str) -> List[QuantTask]:
        ids = self._by_symbol.get(symbol, [])
        return [self.tasks[i] for i in ids if i in self.tasks]

    def start_all(self) -> None:
        for t in self.tasks.values():
            try:
                t.start()
            except Exception as exc:
                logger.error(f"任务 {t.task_id} 启动失败: {exc}", exc_info=True)
                t.status = type(t.status).ERROR

    def stop_all(self) -> None:
        for t in self.tasks.values():
            try:
                t.stop()
            except Exception as exc:
                logger.error(f"任务 {t.task_id} 停止异常: {exc}")

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self):
        return iter(self.tasks.values())

    # ============================================================
    # 分发：行情通道
    # ============================================================
    def on_bar(self, bar: Bar) -> List[Order]:
        """把一根K线广播给关注该标的的所有任务。"""
        orders: List[Order] = []
        for task in self.tasks_for(bar.symbol):
            try:
                order = task.on_bar(bar)
            except Exception as exc:
                logger.error(f"任务 {task.task_id} 处理K线异常: {exc}", exc_info=True)
                task.errors.append(str(exc))
                continue
            task.advance()
            if order is not None:
                orders.append(order)
                self._emit(order)
        return orders

    # ============================================================
    # 分发：事件通道
    # ============================================================
    def on_news(
        self, news: NewsItem, analysis: Optional[NewsAnalysis] = None
    ) -> List[Order]:
        """把一条新闻投递给所有相关任务。

        匹配规则：新闻自带的 symbols + 任务自身的 watch_symbols + 任务标的。
        三者取并集——宁可多投（由任务内部的风控/置信度闸门拦掉），
        也不要漏掉一次真正的事件驱动机会。
        """
        targets = set(news.symbols or [])
        if analysis:
            targets.update(analysis.symbols or [])
        if not targets:
            # 没有标注标的的新闻（宏观/政策类）广播给所有任务
            targets = set(self.tasks[t].symbol for t in self.tasks)

        orders: List[Order] = []
        for task in self.tasks.values():
            watch = set(self._watch_symbols(task))
            if not (targets & watch):
                continue
            try:
                order = task.on_news(news, analysis)
            except Exception as exc:
                logger.error(f"任务 {task.task_id} 处理新闻异常: {exc}", exc_info=True)
                task.errors.append(str(exc))
                continue
            if order is not None:
                orders.append(order)
                self._emit(order)
        return orders

    # ============================================================
    # 回测：批量重放
    # ============================================================
    def replay(self, bars: List[Bar]) -> List[Order]:
        """按时间顺序重放一批K线（回测/复盘用）。"""
        all_orders: List[Order] = []
        for bar in sorted(bars, key=lambda b: b.dt):
            all_orders.extend(self.on_bar(bar))
        return all_orders

    # ============================================================
    # 可观测性
    # ============================================================
    def set_order_sink(self, sink: Optional[Callable[[Order], None]]) -> None:
        """注册订单回调（用于接事件总线 / 落库 / 推送）。"""
        self._order_sink = sink

    def _emit(self, order: Order) -> None:
        if self._order_sink is not None:
            try:
                self._order_sink(order)
            except Exception as exc:
                logger.error(f"订单回调异常: {exc}")

    def snapshot(self) -> Dict[str, Any]:
        return {
            "task_count": len(self.tasks),
            "symbol_count": len(self._by_symbol),
            "tasks": [t.snapshot() for t in self.tasks.values()],
        }

    def summary(self) -> str:
        lines = [f"任务总数: {len(self.tasks)}"]
        for t in self.tasks.values():
            s = t.snapshot()
            lines.append(
                f"  [{s['task_id']}] {s['symbol']} {s['strategy']:<14} "
                f"{s['status']:<8} 权益 {s['equity']:>12.2f} "
                f"持仓 {s['position_size']:>6} 交易 {s['trade_count']:>3} "
                f"回撤 {s['drawdown']:.2%}"
            )
        return "\n".join(lines)

    # ============================================================
    @staticmethod
    def _watch_symbols(task: QuantTask) -> List[str]:
        syms = {task.symbol}
        syms.update(task.meta.get("watch_symbols") or [])
        return sorted(syms)


__all__ = ["TaskRuntime"]
