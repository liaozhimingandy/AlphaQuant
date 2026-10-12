#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : preflight.py
# @Description : 启动预检 —— 引擎"起飞前绕机一圈"
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
为什么要在启动前先体检
======================

引擎现在最长的路径是：启动 → 采集 → 因子 → 策略 → 风控 → 下单。
这条链上任何一环缺东西，都不会在启动时报错，而是在**几分钟后**
以别的形式冒出来：

* 配置文件 JSON 少个逗号 → 维护计划静默退化成"永不执行"；
* 数据库文件被锁 / 目录只读 → 采集跑完了才发现一条也写不进去；
* 股票池是空的 → 引擎"成功启动"，然后一整天什么都没干；
* 实盘没配券商网关 → 第一笔信号出来时才发现下不出去。

这些都不是崩溃，是**空转**。空转比崩溃难查得多，因为日志里全是
"正常"，唯一的线索是"怎么今天没成交"。

所以启动前先绕机一圈：把"缺什么"在启动那一刻就说清楚，
并且分清楚**哪一档能忍、哪一档不能飞**。

三档结论
--------

``info``
    一切正常，记录下来即可。
``warn``（降级可飞）
    有问题但这台引擎仍能干一部分活。例如股票池空 ——
    行情还能采、面板还能看，只是不会有信号。降级运行并明确报出来，
    好过一刀切拒绝启动把整台引擎废掉。
``error``（不能飞）
    继续跑下去只会产生错误结果或丢数据。例如数据库不可写 ——
    采集会"成功"地把数据丢掉。这类必须拦住。

预检与 :meth:`BaseQuantEngine._readiness` 的分工
-----------------------------------------------

``_readiness`` 问的是"组件起来了没有"（运行期状态），
预检问的是"环境齐不齐"（启动前的静态事实）。
两者都要有：组件全绿但股票池是空的，照样一整天白跑。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.common.config import settings
from app.common.logging import logger

#: 三档结论
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

_LEVEL_ORDER = {LEVEL_INFO: 0, LEVEL_WARN: 1, LEVEL_ERROR: 2}


@dataclass
class CheckResult:
    """一项检查的结论。"""

    name: str
    level: str
    detail: str
    #: 出问题时的处置建议。**每条 warn/error 都必须带** ——
    #: 只说"数据库不可写"而不说"检查 data/ 目录权限"的报告，
    #: 等于把排障工作又推回去。
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.level != LEVEL_ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "ok": self.ok,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass
class PreflightReport:
    """预检总报告。"""

    checks: List[CheckResult] = field(default_factory=list)
    started_at: str = ""
    mode: str = ""

    def add(
        self, name: str, level: str, detail: str, hint: str = ""
    ) -> CheckResult:
        item = CheckResult(name=name, level=level, detail=detail, hint=hint)
        self.checks.append(item)
        return item

    # ---------------- 汇总 ----------------
    @property
    def errors(self) -> List[CheckResult]:
        return [c for c in self.checks if c.level == LEVEL_ERROR]

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if c.level == LEVEL_WARN]

    @property
    def ok(self) -> bool:
        """能不能起飞（没有 error 档问题）。"""
        return not self.errors

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)

    def by_name(self, name: str) -> Optional[CheckResult]:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "degraded": self.degraded,
            "mode": self.mode,
            "started_at": self.started_at,
            "counts": {
                "error": len(self.errors),
                "warn": len(self.warnings),
                "info": sum(1 for c in self.checks if c.level == LEVEL_INFO),
            },
            "checks": [c.to_dict() for c in self.checks],
        }

    def summary(self) -> str:
        return (
            f"预检 {'通过' if self.ok else '未通过'} | "
            f"错误 {len(self.errors)} · 警告 {len(self.warnings)}"
        )


# ============================================================
# 各项检查
# ============================================================
def _db_path() -> str:
    url = str(getattr(settings, "DATABASE_URL", "") or "")
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return ""


def check_config_files(report: PreflightReport) -> None:
    """配置能不能读。

    重点不是"文件在不在"，而是**能不能解析**。缺文件会退化成默认值
    （设计如此，可接受），但一个写坏的 JSON 会让整块配置静默消失 ——
    表现形式是"我明明配了 03:10 清日志，它从来没清过"。
    """
    names = [
        ("maintenance.json", getattr(settings, "CONFIG_DIR", None)),
        ("providers.json", getattr(settings, "CONFIG_DIR", None)),
        ("collector.json", getattr(settings, "CONFIG_DIR", None)),
        ("pool.json", getattr(settings, "CONFIG_DIR", None)),
    ]
    broken: List[str] = []
    missing: List[str] = []
    for name, base in names:
        if base is None:
            continue
        path = os.path.join(str(base), name)
        if not os.path.exists(path):
            missing.append(name)
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:
            broken.append(f"{name}({exc})")

    if broken:
        report.add(
            "配置文件", LEVEL_ERROR,
            f"以下配置文件无法解析: {', '.join(broken)}",
            "修正 JSON 语法（常见是末尾多一个逗号）。坏配置会被静默忽略，"
            "表现为'配了但不生效'。",
        )
    elif missing:
        report.add(
            "配置文件", LEVEL_WARN,
            f"可选配置缺失（将使用内置默认）: {', '.join(missing)}",
            "如需自定义行为，可从 config/ 下的同名示例生成。",
        )
    else:
        report.add("配置文件", LEVEL_INFO, "全部可解析")


def check_directories(report: PreflightReport) -> None:
    """关键目录在不在、可不可写。"""
    unwritable: List[str] = []
    for label, path in (
        ("数据", getattr(settings, "DATA_DIR", None)),
        ("输出", getattr(settings, "OUTPUT_DIR", None)),
        ("日志", getattr(settings, "LOG_DIR", None)),
    ):
        if path is None:
            continue
        try:
            os.makedirs(str(path), exist_ok=True)
            probe = os.path.join(str(path), ".preflight_probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except Exception as exc:
            unwritable.append(f"{label}({path}): {exc}")

    if unwritable:
        report.add(
            "目录权限", LEVEL_ERROR,
            f"以下目录不可写: {'; '.join(unwritable)}",
            "检查目录权限与磁盘占用。不可写会让采集'成功执行但一条都没存下'。",
        )
    else:
        report.add("目录权限", LEVEL_INFO, "数据/输出/日志目录均可写")


def check_database(report: PreflightReport) -> None:
    """数据库能不能读写 —— 这一项失败必须拦住启动。"""
    path = _db_path()
    if not path:
        report.add(
            "数据库", LEVEL_WARN,
            f"数据库 URL 不是 SQLite（{settings.DATABASE_URL}），跳过文件级检查",
            "",
        )
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _preflight_probe (k TEXT PRIMARY KEY)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO _preflight_probe VALUES ('ok')"
            )
            conn.commit()
            cur = conn.execute("SELECT k FROM _preflight_probe LIMIT 1")
            cur.fetchone()
            conn.execute("DROP TABLE _preflight_probe")
            conn.commit()
        finally:
            conn.close()
        report.add("数据库", LEVEL_INFO, f"可读写 | {path}")
    except Exception as exc:
        report.add(
            "数据库", LEVEL_ERROR,
            f"数据库不可读写: {exc}",
            "检查 data/ 目录权限、磁盘空间，或文件是否被其它进程独占锁定。",
        )


def check_market_data(report: PreflightReport) -> None:
    """库里有没有行情。空库不是错误，但意味着今天不会有信号。"""
    path = _db_path()
    if not path or not os.path.exists(path):
        report.add(
            "行情数据", LEVEL_WARN,
            "数据库文件尚不存在，未做数据检查（首次启动属正常）",
            "首次启动请先跑一次采集。",
        )
        return
    try:
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM stock_daily")
            rows = int(cur.fetchone()[0] or 0)
            cur2 = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM stock_daily"
            )
            symbols = int(cur2.fetchone()[0] or 0)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        report.add(
            "行情数据", LEVEL_WARN,
            f"无法读取日线表: {exc}",
            "首次启动尚未建表属正常；若长期如此，检查采集服务是否启用。",
        )
        return
    except Exception as exc:
        report.add("行情数据", LEVEL_WARN, f"数据检查失败: {exc}", "")
        return

    if rows == 0:
        report.add(
            "行情数据", LEVEL_WARN,
            "日线表为空 —— 引擎可以启动，但不会产生任何策略信号",
            "先跑一次采集（`python main.py collect`）或启用采集服务。",
        )
    else:
        report.add(
            "行情数据", LEVEL_INFO,
            f"日线 {rows} 行 / {symbols} 只标的",
        )


def check_disk_space(report: PreflightReport) -> None:
    """磁盘余量。写满会导致日志与数据库同时失效。"""
    try:
        target = str(getattr(settings, "OUTPUT_DIR", "") or ".")
        total, used, free = _disk_usage(target)
    except Exception as exc:
        report.add("磁盘空间", LEVEL_INFO, f"无法获取磁盘信息: {exc}")
        return
    free_gb = free / (1024 ** 3)
    if free_gb < 0.5:
        report.add(
            "磁盘空间", LEVEL_ERROR,
            f"剩余空间不足 0.5GB（{free_gb:.2f}GB）",
            "清理 logs/ 与 output/snapshots/。磁盘写满会让日志与数据库同时失效。",
        )
    elif free_gb < 2.0:
        report.add(
            "磁盘空间", LEVEL_WARN,
            f"剩余空间偏低（{free_gb:.2f}GB）",
            "建议启用运维计划里的日志清理与快照归档。",
        )
    else:
        report.add("磁盘空间", LEVEL_INFO, f"剩余 {free_gb:.1f}GB")


def _disk_usage(path: str):
    import shutil

    return shutil.disk_usage(path)


def check_data_sources(report: PreflightReport) -> None:
    """至少有一个数据源可用，否则采集注定全失败。"""
    priority = getattr(settings, "DATA_SOURCE_PRIORITY", "") or ""
    sources = [s.strip() for s in priority.split(",") if s.strip()]
    if not sources:
        report.add(
            "数据源", LEVEL_WARN,
            "DATA_SOURCE_PRIORITY 为空，采集将无源可用",
            "设置环境变量或 config/providers.json 里启用至少一个数据源。",
        )
        return
    report.add("数据源", LEVEL_INFO, f"优先级链: {' → '.join(sources)}")


def check_pending_tasks(report: PreflightReport, journal: Any = None) -> None:
    """上次是不是有活没干完。这是恢复流程的预告。"""
    if journal is None:
        return
    try:
        counts = journal.counts()
    except Exception as exc:
        report.add("遗留任务", LEVEL_INFO, f"流水账检查失败: {exc}")
        return
    if not counts.get("enabled"):
        report.add("遗留任务", LEVEL_INFO, "任务持久化未启用，重启将丢失在途任务")
        return
    open_n = int(counts.get("open") or 0)
    dead_n = int(counts.get("dead") or 0)
    if open_n:
        report.add(
            "遗留任务", LEVEL_WARN,
            f"流水账里有 {open_n} 条未完成任务，启动后将被恢复重跑",
            "可在面板「运行时 → 错误队列」查看；确认不需要时先清空再启动。",
        )
    else:
        report.add("遗留任务", LEVEL_INFO, "无未完成任务")
    if dead_n:
        report.add(
            "错误队列", LEVEL_WARN,
            f"上次运行留下 {dead_n} 条死信待人工处理",
            "面板「运行时 → 错误队列」可逐条重投或丢弃。",
        )


def check_live_guard(report: PreflightReport, mode: str = "") -> None:
    """实盘模式的前置闸门。

    这一项刻意严格：实盘缺网关时继续跑，会在第一笔信号处
    静默降级成模拟撮合 —— 那比直接拒绝启动危险得多，
    因为账面看起来是"成交了"。
    """
    if str(mode).upper() != "LIVE":
        return
    gateway = str(getattr(settings, "BROKER_GATEWAY", "") or "")
    if not gateway:
        report.add(
            "实盘闸门", LEVEL_ERROR,
            "实盘模式未配置券商网关（BROKER_GATEWAY）",
            "配置 BROKER_GATEWAY 与 brokers.json 接入点后再以 LIVE 启动；"
            "不要指望它自动降级成模拟撮合。",
        )
    else:
        report.add("实盘闸门", LEVEL_INFO, f"网关: {gateway}")


# ============================================================
# 入口
# ============================================================
def run_preflight(
    mode: str = "",
    journal: Any = None,
    extra_checks: bool = True,
) -> PreflightReport:
    """跑一遍启动预检。

    :param mode: 运行模式（``LIVE`` 会额外做实盘闸门检查）
    :param journal: 任务流水账（有则一并报告遗留任务）
    :param extra_checks: 是否做联网无关但耗时的数据检查
    """
    report = PreflightReport(
        started_at=datetime.now().isoformat(timespec="seconds"), mode=str(mode or "")
    )
    check_config_files(report)
    check_directories(report)
    check_database(report)
    if extra_checks:
        check_market_data(report)
        check_disk_space(report)
    check_data_sources(report)
    check_pending_tasks(report, journal)
    check_live_guard(report, mode)
    return report


def log_report(report: PreflightReport) -> None:
    """把报告写进日志。warn/error 一定打，info 只在调试级别打。"""
    logger.info(f"=== 启动预检 | {report.summary()} ===")
    for c in report.checks:
        if c.level == LEVEL_ERROR:
            logger.error(f"  [错误] {c.name}: {c.detail}" + (f" —— {c.hint}" if c.hint else ""))
        elif c.level == LEVEL_WARN:
            logger.warning(f"  [警告] {c.name}: {c.detail}" + (f" —— {c.hint}" if c.hint else ""))
        else:
            logger.debug(f"  [正常] {c.name}: {c.detail}")


__all__ = [
    "PreflightReport", "CheckResult", "run_preflight", "log_report",
    "LEVEL_INFO", "LEVEL_WARN", "LEVEL_ERROR",
]
