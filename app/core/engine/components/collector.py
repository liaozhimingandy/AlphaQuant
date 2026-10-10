#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : collector.py
# @Description : 行情采集服务组件：按用户设定的频率自动把行情拉进本地库
#               取代"需要数据了再手动跑一次 python main.py collect"的做法——
#               策略要高频数据，就得有一个一直在跑的采集器
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from twisted.internet import defer, task, threads

from app.core.collect.spec import (
    DAILY_PERIOD,
    CollectJob,
    CollectorSpec,
    humanize_frequency,
    is_intraday,
    load_collector_spec,
    normalize_period,
    parse_frequency,
)
from app.core.config import settings
from app.core.engine.components.ibase import IBaseComponent
from app.core.engine.event import StandardEvents
from app.data.datasource import DataFetchError, normalize_symbol
from app.utils.logger import logger

#: 每跑这么多次清理一次分钟线。每天删一次表比每分钟删一次划算得多
_RETENTION_EVERY_N_RUNS = 240


class DataCollectorComponent(IBaseComponent):
    """定时行情采集。

    三个必须在设计上讲清楚的点：

    1. **采集必须走线程池**。``MarketDataService.collect_*`` 里是同步 HTTP，
       直接放在 reactor 线程会把行情、策略、撮合、面板全部一起卡住。
    2. **同一频率的任务合并成一个定时器**。20 个标的各起一个 LoopingCall
       只会让 reactor 把时间浪费在定时器调度上；而同频率的采集天然可以串行做。
    3. **上一轮没跑完就跳过本轮**。网络慢的时候最忌讳请求堆积——
       堆积只会让数据源更慢，最后被限流，比漏采一次严重得多。
    """

    name = "data_collector"

    def __init__(
        self,
        spec: Optional[CollectorSpec] = None,
        symbols: Optional[Sequence[str]] = None,
        interval: Any = None,
        period: Any = None,
        adjust: str = "",
        source: str = "",
        min_interval: Optional[float] = None,
        config_path: Optional[str] = None,
        enabled: Optional[bool] = None,
        disabled_reason: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._pending_spec = spec
        self._symbols = list(symbols or [])
        self._interval = interval
        self._period = period
        self._adjust = adjust
        self._source = source
        self._min_interval = min_interval
        self._config_path = config_path
        self._enabled = enabled
        #: 被停用时的原因。装配但未启用时面板要能说清"为什么没在跑"，
        #: 否则用户只会看到一个莫名其妙的"未启用"而不知道该改哪里。
        self.disabled_reason = str(disabled_reason or "")

        # 构造时就把配置解析出来，而不是等到 on_initialize。
        # 理由：装配器要在启动前打印"采集服务=N 个任务"，CLI/面板也可能在
        # 引擎起来之前就读 snapshot()。等到 initialize 才解析的话，
        # 这些地方看到的永远是 0 个任务，日志会说谎。
        self.spec: CollectorSpec = spec or self._load_spec({})
        self._loops: Dict[float, task.LoopingCall] = {}
        self._busy: Dict[float, bool] = {}
        self._job_state: Dict[str, Dict[str, Any]] = {}
        self._run_counts: Dict[float, int] = {}
        self.stats: Dict[str, int] = {
            "runs": 0, "rows": 0, "errors": 0, "skipped": 0, "out_of_session": 0,
        }
        self._sync_state()

    def _load_spec(self, extra_cfg: Dict[str, Any]) -> CollectorSpec:
        """按「构造参数 → 组件配置 → 配置文件」的优先级解析采集配置。"""
        return load_collector_spec(
            path=self._config_path,
            symbols=(extra_cfg.get("symbols") or self._symbols or None),
            interval=(
                self._interval if self._interval is not None
                else extra_cfg.get("interval")
            ),
            period=(
                self._period if self._period is not None
                else extra_cfg.get("period")
            ),
            adjust=self._adjust,
            source=self._source,
            enabled=self._enabled,
            min_interval=self._min_interval,
        )

    def _sync_state(self) -> None:
        """为每个任务补齐状态槽，并清掉已经不存在的任务的旧状态。"""
        live = {job.key for job in self.spec.jobs}
        for key in list(self._job_state):
            if key not in live:
                self._job_state.pop(key, None)
        for job in self.spec.jobs:
            self._job_state.setdefault(job.key, {
                "runs": 0, "rows": 0, "errors": 0,
                "last_run_at": None, "last_rows": 0, "last_error": None,
            })

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        collector_cfg = cfg.get("collector") or cfg

        if self._pending_spec is None:
            # 只有真的传了组件配置（且非构造期已经覆盖过的）才重解析；
            # 构造时那次解析保证了「未 initialize 时 snapshot() 也是对的」
            self.spec = self._load_spec(collector_cfg)

        self._sync_state()
        self._ensure_tables()

        if not self.spec.jobs:
            logger.warning("采集服务没有配置任何标的，组件空转（不注册定时器）")
            return

        buckets: Dict[float, int] = {}
        for job in self.spec.jobs:
            buckets[job.interval] = buckets.get(job.interval, 0) + 1
        logger.info(
            f"行情采集就绪 | 任务 {len(self.spec.jobs)} 个 | 频率分档: "
            + ", ".join(f"{humanize_frequency(k)}×{v}" for k, v in sorted(buckets.items()))
            + f" | 交易时段限定: {self.spec.trading_hours_only}"
        )

    def _ensure_tables(self) -> None:
        """补建分钟线表。

        用户可能从没跑过 init-db 就直接用采集服务；``create_all`` 是幂等的，
        这里补一次比让整条链路在第一次入库时才炸要好定位得多。
        """
        try:
            from app.db.database import engine
            from app.db.models import Base

            Base.metadata.create_all(bind=engine)
        except Exception as exc:
            logger.warning(f"补建数据表失败（忽略，后续写入仍会尝试）: {exc}")

    def on_start(self) -> defer.Deferred:
        if not self.spec.enabled or not self.spec.jobs:
            logger.info(
                "行情采集服务未启用或无任务"
                + (f" | 原因: {self.disabled_reason}" if self.disabled_reason else "")
                + "（组件仍已装配，可在面板手动补采）"
            )
            return defer.succeed(None)
        for interval in sorted({j.interval for j in self.spec.jobs}):
            self._start_bucket(interval)
        if self.spec.backfill_on_start:
            # 立刻先采一轮：服务刚起来就要有数据，而不是干等第一个周期
            for interval in sorted({j.interval for j in self.spec.jobs}):
                self._tick(interval)
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        for loop in self._loops.values():
            if loop.running:
                loop.stop()
        self._loops.clear()
        self._busy.clear()
        return defer.succeed(None)

    # ---------------- 定时器 ----------------
    def _start_bucket(self, interval: float) -> None:
        # LoopingCall 的间隔就是任务频率本身，不要再套一层 max(0.5, ...)：
        # 那会让配了 sub-second 频率的部署悄悄变成 0.5s，与用户写的配置不符。
        seconds = float(interval)
        if seconds <= 0:
            logger.warning(f"采集频率 {seconds}s 非法，跳过该任务")
            return
        loop = task.LoopingCall(self._tick, seconds)
        loop.start(seconds, now=False)
        self._loops[seconds] = loop
        self._busy[seconds] = False
        logger.info(f"采集定时器启动 | 每 {humanize_frequency(seconds)}")

    def _stop_bucket(self, interval: float) -> None:
        loop = self._loops.pop(interval, None)
        if loop is not None and loop.running:
            loop.stop()
        self._busy.pop(interval, None)

    def _reschedule(self) -> None:
        """按当前 jobs 重算频率分档，增删必要的定时器。

        **组件没在运行时只登记意图、不起定时器**。定时器一动就向 reactor
        注册调度；若在 INITIALIZED 阶段起了定时器，on_start 又会按桶再起一轮，
        同一个频率出现两个定时器 —— 采集频率翻倍，最后被数据源限流。
        """
        wanted = {j.interval for j in self.spec.jobs if j.enabled and self.spec.enabled}
        for interval in list(self._loops):
            if interval not in wanted:
                self._stop_bucket(interval)
        if not self.is_running:
            return
        for interval in sorted(wanted):
            if interval not in self._loops:
                self._start_bucket(interval)

    def _tick(self, interval: float) -> None:
        if self._busy.get(interval):
            self.stats["skipped"] += 1
            logger.warning(
                f"采集({humanize_frequency(interval)}) 上一轮尚未结束，跳过本轮"
            )
            return
        if not self.spec.enabled:
            return

        jobs = [j for j in self.spec.jobs if j.interval == interval and j.enabled]
        if not jobs:
            return

        self._busy[interval] = True
        d = threads.deferToThread(self._run_bucket, jobs)
        d.addErrback(self._on_bucket_error, interval)
        d.addBoth(self._release, interval)

    def _on_bucket_error(self, failure_obj, interval: float):
        """整批采集炸了（线程池里的漏网之鱼）。单个任务的失败在 _run_bucket 里已记过。"""
        logger.error(
            f"采集批次({humanize_frequency(interval)}) 异常: "
            f"{failure_obj.getErrorMessage() if hasattr(failure_obj, 'getErrorMessage') else failure_obj}"
        )
        return None

    def _release(self, result: Any, interval: float) -> Any:
        self._busy[interval] = False
        return result

    # ---------------- 采集执行（工作线程）----------------
    def _run_bucket(self, jobs: List[CollectJob]) -> None:
        if not jobs:
            return
        key = jobs[0].interval
        n = self._run_counts.get(key, 0) + 1
        self._run_counts[key] = n

        for job in jobs:
            if not self.spec.should_run(job):
                self.stats["out_of_session"] += 1
                continue
            self.stats["runs"] += 1
            try:
                rows = self._collect_one(job)
            except DataFetchError as exc:
                self._record_failure(job, exc)
            except Exception as exc:
                logger.error(f"采集 {job.key} 异常: {exc}", exc_info=True)
                self._record_failure(job, exc)
            else:
                self._record_success(job, rows)
                if job.keep_days > 0 and n % _RETENTION_EVERY_N_RUNS == 0:
                    self._evict(job)

    def _collect_one(self, job: CollectJob) -> int:
        from app.data.service import MarketDataService

        source = job.source or None
        if is_intraday(job.period):
            return int(
                MarketDataService.collect_minute(
                    job.symbol,
                    period=job.period,
                    adjust=job.adjust,
                    source=source,
                )
            )
        info = MarketDataService.collect_incremental(
            job.symbol,
            lookback_days=job.lookback_days,
            adjust=job.adjust,
            source=source,
        )
        return int(info.get("rows") or 0)

    def _evict(self, job: CollectJob) -> None:
        """清理过期分钟线。失败不影响主流程——磁盘撑爆是个慢问题。"""
        try:
            from app.repository.minute_repository import MinuteRepository
            from app.repository.stock_repository import session_scope

            cutoff = datetime.now() - timedelta(days=int(job.keep_days))
            with session_scope(None) as session:
                removed = MinuteRepository.delete_before(
                    session, cutoff, period=job.period, symbol=job.symbol, commit=False
                )
            if removed:
                logger.info(f"分钟线回收 | {job.key} 清理 {removed} 条（早于 {cutoff:%F}）")
        except Exception as exc:
            logger.warning(f"分钟线回收失败（忽略）: {exc}")

    def _record_success(self, job: CollectJob, rows: int) -> None:
        st = self._job_state.setdefault(job.key, {})
        st["runs"] = int(st.get("runs", 0)) + 1
        st["rows"] = int(st.get("rows", 0)) + rows
        st["last_rows"] = rows
        st["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        st["last_error"] = None
        self.stats["rows"] += rows
        logger.info(f"采集完成 | {job.key} | {rows} 条")
        if self.event_bus is not None:
            try:
                self.event_bus.publish(
                    StandardEvents.DATA_COLLECTED,
                    symbol=job.symbol,
                    period=job.period,
                    rows=rows,
                )
            except Exception as exc:  # 事件回调故障不该影响采集
                logger.debug(f"DATA_COLLECTED 发布失败: {exc}")

    def _record_failure(self, job: CollectJob, exc: Exception) -> None:
        st = self._job_state.setdefault(job.key, {})
        st["runs"] = int(st.get("runs", 0)) + 1
        st["errors"] = int(st.get("errors", 0)) + 1
        st["last_error"] = str(exc)
        st["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        self.stats["errors"] += 1
        logger.warning(f"采集失败 | {job.key} | {exc}")

    # ---------------- 运行时控制 ----------------
    def set_interval(self, seconds: Any, symbol: str = "", period: str = "") -> int:
        """现场改采集频率，立刻生效——不用重启服务。"""
        target = parse_interval(seconds, self.spec.min_interval)
        want_period = normalize_period(period) if period else ""
        n = 0
        for job in self.spec.jobs:
            if symbol and job.symbol != normalize_symbol(symbol):
                continue
            if want_period and job.period != want_period:
                continue
            job.interval = target
            n += 1
        if n:
            self._reschedule()
            logger.info(f"采集频率已更新为 {humanize_frequency(target)} | 影响 {n} 个任务")
        return n

    def add_symbol(
        self,
        symbol: str,
        period: str = "",
        interval: Any = None,
        **params: Any,
    ) -> Optional[CollectJob]:
        """运行时新增采集标的（面板新增任务/手动补全数据时用）。"""
        sym = normalize_symbol(symbol)
        per = period or DAILY_PERIOD
        for job in self.spec.jobs:
            if job.symbol == sym and job.period == per:
                if interval is not None:
                    self.set_interval(interval, sym, per)
                return job
        job = CollectJob(
            symbol=sym,
            period=per,
            interval=parse_interval(interval, self.spec.min_interval),
            adjust=params.get("adjust") or self.spec.adjust,
            source=params.get("source") or self.spec.source,
            lookback_days=int(params.get("lookback_days") or self.spec.lookback_days),
            keep_days=int(params.get("keep_days") or self.spec.keep_days),
        )
        self.spec.jobs.append(job)
        self._job_state[job.key] = {
            "runs": 0, "rows": 0, "errors": 0,
            "last_run_at": None, "last_rows": 0, "last_error": None,
        }
        self._reschedule()
        logger.info(
            f"采集新增标的 | {job.key} | 频率 {humanize_frequency(job.interval)}"
        )
        return job

    def remove_symbol(self, symbol: str, period: str = "") -> int:
        sym = normalize_symbol(symbol)
        keep, removed = [], 0
        for job in self.spec.jobs:
            if job.symbol == sym and (not period or job.period == period):
                removed += 1
                self._job_state.pop(job.key, None)
            else:
                keep.append(job)
        self.spec.jobs = keep
        if removed:
            self._reschedule()
        return removed

    def reload_config(self, path: Optional[str] = None) -> Dict[str, Any]:
        """重新读一遍采集配置文件，热更新任务列表（保留已跑出来的统计）。"""
        self.spec = load_collector_spec(
            path=path or self._config_path,
            symbols=self._symbols or None,
            interval=self._interval,
            period=self._period,
        )
        for job in self.spec.jobs:
            self._job_state.setdefault(job.key, {
                "runs": 0, "rows": 0, "errors": 0,
                "last_run_at": None, "last_rows": 0, "last_error": None,
            })
        self._reschedule()
        logger.info(f"采集配置已热加载 | 任务 {len(self.spec.jobs)} 个")
        return {"jobs": len(self.spec.jobs)}

    def collect_now(self, symbol: str = "") -> defer.Deferred:
        """立刻采一次（不等定时器）。返回 Deferred，供面板/CLI await。"""
        jobs = [
            j for j in self.spec.jobs
            if j.enabled and (not symbol or j.symbol == normalize_symbol(symbol))
        ]
        if not jobs:
            return defer.succeed({"rows": 0, "detail": "没有匹配的采集任务"})

        d = threads.deferToThread(self._run_forced, jobs)
        return d

    def _run_forced(self, jobs: List[CollectJob]) -> Dict[str, Any]:
        """强制执行一次：忽略交易时段（用户点了"立即采集"就是要立刻看到结果）。"""
        rows, errors = 0, []
        for job in jobs:
            self.stats["runs"] += 1
            try:
                n = self._collect_one(job)
            except Exception as exc:
                self.stats["errors"] += 1
                self._record_failure(job, exc)
                errors.append(f"{job.key}: {exc}")
            else:
                self._record_success(job, n)
                rows += n
        return {"rows": rows, "tasks": len(jobs), "errors": errors}

    # ---------------- 引擎协作 ----------------
    def is_busy(self) -> bool:
        """还有定时器在跑 → 引擎不该被判定为空闲。采集服务本身就是"有事要做"。"""
        return any(loop.running for loop in self._loops.values())

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self.spec.enabled,
            "assembled": True,
            "disabled_reason": self.disabled_reason,
            "running": bool(self._loops),
            "jobs": [j.to_dict() for j in self.spec.jobs],
            "state": {k: dict(v) for k, v in self._job_state.items()},
            "stats": dict(self.stats),
            "buckets": sorted(self._loops.keys()),
            "trading_hours_only": self.spec.trading_hours_only,
            "min_interval": self.spec.min_interval,
            "config": self.spec.config_path,
            "pending": {str(k): v for k, v in self._busy.items() if v},
        }

    def health_check(self):
        if not self.spec.enabled:
            return True, "disabled"
        failing = sum(
            1 for st in self._job_state.values() if st.get("last_error")
        )
        if failing and failing == len(self.spec.jobs):
            return False, f"全部 {failing} 个采集任务最近都失败了"
        return True, f"{len(self.spec.jobs)} 个采集任务"


def parse_interval(value: Any, min_interval: float = 15.0) -> float:
    """把各种写法转成秒并夹紧到下限。

    缺省（None）直接取下限而不是硬编码 300s：下限才是"这套部署允许多快"，
    拿一个写死的默认值当兜底会在配了 min_interval=1s 的环境里出人意料。
    """
    floor = float(min_interval)
    return max(parse_frequency(value, floor), floor)


__all__ = ["DataCollectorComponent", "parse_interval"]
