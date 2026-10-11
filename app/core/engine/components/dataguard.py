#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : dataguard.py
# @Description : 数据校验服务组件：引擎/组件启动后体检一次，之后定期复查 + 自动补全
#
#                三个必须在设计上讲清楚的点：
#
#                1. **不能阻塞启动**。体检要查库、可能还要联网补全，
#                   直接放在 `on_start()` 里同步跑，等于把"引擎起来"这件事
#                   押在数据源的脸色上 —— 网络一慢，面板半小时打不开。
#                   所以首次体检延后 20 秒、丢进线程池跑；
#                   这 20 秒也是留给采集服务的：它先把今天的数据喂进库，
#                   校验才不会把"还没来得及采"误判成"缺数据"。
#
#                2. **上一轮没跑完就跳过本轮**。体检会联网补全，
#                   堆积的补全请求是最容易被数据源限流的；
#                   漏检一轮无所谓（下一轮还会看到同一个缺口），
#                   被封 IP 才是真麻烦。
#
#                3. **回测模式默认不启用**。回测读的是历史库，
#                   "今天缺不缺"跟它没关系；而且回测跑完就停，
#                   体检还没开始就被杀了，只会留一条没头没尾的日志。
#
#                体检不等于修复：``auto_repair=False`` 时只出报告不动数据源，
#                先看清问题再决定要不要补（面板/CLI 都能单独触发一次）。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from twisted.internet import defer, task, threads

from app.core.config import settings
from app.core.data.integrity import (
    DataIntegrityChecker,
    IntegrityReport,
    summarize,
)
from app.core.engine.components.ibase import IBaseComponent
from app.utils.logger import logger


class DataGuardComponent(IBaseComponent):
    """数据校验服务：定期体检数据库，缺的日线自动补。

    它补的不只是"历史空洞"，更常见的是**今天/昨天那几根没采到**的情况：
    服务昨晚没开、采集中途报错、数据源临时抽风 —— 这些都不会有人盯，
    但策略第二天开盘就要用到。
    """

    name = "data_guard"

    def __init__(
        self,
        symbols: Optional[Sequence[str]] = None,
        interval: Optional[float] = None,
        start_delay: Optional[float] = None,
        lookback_days: Optional[int] = None,
        auto_repair: Optional[bool] = None,
        max_symbols: Optional[int] = None,
        max_repair: Optional[int] = None,
        enabled: Optional[bool] = None,
        disabled_reason: str = "",
        **kwargs: Any,
    ) -> None:
        # 注意别把构造参数命名为 start / stop / initialize / health_check：
        # 那会把方法遮蔽成标量（基类有专门检测，报错信息很难定位）。
        super().__init__()
        self.symbols: List[str] = list(symbols or [])
        self.interval = float(
            interval if interval is not None else settings.DATA_GUARD_INTERVAL
        )
        self.start_delay = float(
            start_delay if start_delay is not None
            else settings.DATA_GUARD_START_DELAY
        )
        self.lookback_days = int(
            lookback_days if lookback_days is not None
            else settings.DATA_GUARD_LOOKBACK_DAYS
        )
        self.auto_repair = (
            settings.DATA_GUARD_AUTO_REPAIR if auto_repair is None
            else bool(auto_repair)
        )
        self.max_symbols = int(
            max_symbols if max_symbols is not None
            else settings.DATA_GUARD_MAX_SYMBOLS
        )
        self.max_repair = int(
            max_repair if max_repair is not None
            else settings.DATA_GUARD_MAX_REPAIR
        )
        if enabled is not None:
            self.enabled = bool(enabled)
        self.disabled_reason = str(disabled_reason or "")

        self._loop: Optional[task.LoopingCall] = None
        self._busy = False
        self._report: Optional[IntegrityReport] = None
        self.stats: Dict[str, int] = {
            "runs": 0, "missing": 0, "repaired": 0, "anomalies": 0, "errors": 0,
        }

    # ---------------- 生命周期 ----------------
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        guard_cfg = cfg.get("data_guard") or cfg
        if not self.symbols:
            self.symbols = list(guard_cfg.get("symbols") or [])
        if guard_cfg.get("interval") is not None and self._loop is None:
            try:
                self.interval = float(guard_cfg["interval"])
            except (TypeError, ValueError):
                pass
        if not self.enabled:
            logger.info(
                "数据校验服务已停用"
                + (f" | 原因: {self.disabled_reason}" if self.disabled_reason else "")
            )
            return
        logger.info(
            f"数据校验服务就绪 | 启动后 {self.start_delay:.0f}s 首检 | "
            f"之后每 {self.interval:.0f}s 一次 | 回溯 {self.lookback_days} 天 | "
            f"自动补全: {'开' if self.auto_repair else '关（只体检）'}"
        )

    def on_start(self) -> defer.Deferred:
        if not self.enabled:
            return defer.succeed(None)
        if self.interval <= 0:
            logger.warning("数据校验周期非法（<=0），只跑启动那一次")
        else:
            self._loop = task.LoopingCall(self._tick)
            self._loop.start(self.interval, now=False)
            logger.info(f"数据校验定时器启动 | 每 {self.interval:.0f}s")

        # 首次体检：延后 + 线程池。理由见文件头第 1 点。
        if self.start_delay <= 0:
            self._tick()
        else:
            from twisted.internet import reactor

            reactor.callLater(self.start_delay, self._tick)
            logger.debug(f"数据校验首检已安排在 {self.start_delay:.0f}s 后")
        return defer.succeed(None)

    def on_stop(self, graceful: bool = True) -> defer.Deferred:
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None
        return defer.succeed(None)

    # ---------------- 执行 ----------------
    def _on_step(self, done: int, total: int, bad: bool) -> None:
        """体检每检完一只回调一次，驱动面板上的"已完成 / 全部"。

        第一次回调时才开一轮：总数只有扫描器开始跑之后才知道
        （没指定标的时要从库里列举），没法提前 start。
        """
        if self._progress.round == 0 or not self._progress.running:
            self.start_progress(total, f"数据体检 {total} 只")
        self.advance_progress(1, failed=bad)
        if done >= total:
            self.finish_progress()

    def _tick(self) -> None:
        if self._busy:
            logger.warning("数据校验上一轮尚未结束，跳过本轮")
            return
        if not self.enabled:
            return
        self._busy = True
        d = threads.deferToThread(self._run)
        d.addErrback(self._on_error)
        d.addBoth(self._release)

    def _on_error(self, failure_obj) -> None:
        self.stats["errors"] += 1
        msg = (failure_obj.getErrorMessage()
               if hasattr(failure_obj, "getErrorMessage") else str(failure_obj))
        logger.error(f"数据校验批次异常: {msg}")

    def _release(self, result: Any) -> Any:
        self._busy = False
        return result

    def _run(self) -> IntegrityReport:
        checker = DataIntegrityChecker(
            lookback_days=self.lookback_days,
            auto_repair=self.auto_repair,
            max_symbols=self.max_symbols,
            max_repair=self.max_repair,
        )
        report = checker.scan(self.symbols or None, on_progress=self._on_step)
        if not report.checked:
            # 一个标的都没有：进度条会一直停在上一轮，这里明确置空
            self.start_progress(0, "无可体检标的")
            self.finish_progress()
        self._report = report
        self.stats["runs"] += 1
        self.stats["missing"] += report.missing_total
        self.stats["repaired"] += report.repaired_total
        self.stats["anomalies"] += report.anomaly_total
        if report.failed:
            self.stats["errors"] += len(report.failed)

        line = summarize(report)
        if report.ok:
            logger.info(line)
        else:
            # 缺数据不会让程序崩，但**必须**留下痕迹 ——
            # 否则"策略最近怎么不准了"永远查不到根因。
            logger.warning(line)
            for item in report.failed[:5]:
                logger.warning(f"  校验未通过: {item}")
        return report

    # ---------------- 手动触发 ----------------
    def check_now(self, symbols: Optional[Sequence[str]] = None,
                  auto_repair: Optional[bool] = None) -> defer.Deferred:
        """立刻体检一次（不等定时器）。返回 Deferred[dict]。"""
        want = list(symbols or self.symbols or [])
        repair = self.auto_repair if auto_repair is None else bool(auto_repair)

        def _job() -> Dict[str, Any]:
            checker = DataIntegrityChecker(
                lookback_days=self.lookback_days,
                auto_repair=repair,
                max_symbols=self.max_symbols,
                max_repair=self.max_repair,
            )
            report = checker.scan(want or None, on_progress=self._on_step)
            if not report.checked:
                self.start_progress(0, "无可体检标的")
                self.finish_progress()
            self._report = report
            self.stats["runs"] += 1
            self.stats["missing"] += report.missing_total
            self.stats["repaired"] += report.repaired_total
            self.stats["anomalies"] += report.anomaly_total
            return {"summary": summarize(report), **report.to_dict()}

        return threads.deferToThread(_job)

    # ---------------- 引擎协作 ----------------
    def is_busy(self) -> bool:
        return self._busy

    def health_check(self):
        if not self.enabled:
            return True, "disabled"
        if self._report is None:
            return True, "尚未体检"
        if self._report.anomaly_total:
            return False, f"发现 {self._report.anomaly_total} 条异常行情"
        if self._report.missing_total:
            return False, f"仍有 {self._report.missing_total} 天日线缺口未补上"
        return True, "数据完整"

    def snapshot(self) -> Dict[str, Any]:
        report = self._report.to_dict() if self._report else None
        return {
            "enabled": self.enabled,
            "assembled": True,
            "disabled_reason": self.disabled_reason,
            "running": self._loop is not None and bool(self._loop.running),
            "interval": self.interval,
            "start_delay": self.start_delay,
            "lookback_days": self.lookback_days,
            "auto_repair": self.auto_repair,
            "max_symbols": self.max_symbols,
            "max_repair": self.max_repair,
            "symbols": list(self.symbols),
            "busy": self._busy,
            "stats": dict(self.stats),
            "report": report,
            "last_check_at": (
                self._report.started_at if self._report else None
            ),
        }


__all__ = ["DataGuardComponent"]
