#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票池刷新经引擎调度器分配任务的链路校验。

回归点
======
修复前：``/api/pool/refresh`` 直接 ``StockPool().refresh()`` 在线程里跑，
只写 control audit 一条内存记录，既不进任务明细、也不进死信、崩溃不可恢复。
修复后：提交为 ``pool.refresh`` 引擎任务，受能力路由约束、可在「任务明细」跟踪。

测试只验证「注册 / 路由 / 处理器调用」三件，不真实联网、不建 SQLite，
避免触发本机安全软件对新建库文件的扫描拖慢。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from app.runtime.scheduling.router import CapabilityRouter
from app.runtime.workers.handlers import (
    handle_pool_refresh,
    register_default_handlers,
)
from app.runtime.workers.registry import HandlerRegistry


def test_pool_refresh_handler_registered():
    reg = HandlerRegistry()
    register_default_handlers(reg)
    assert reg.has("pool.refresh")
    assert "刷新股票池" in (reg.snapshot()["handlers"] and
                            [h["description"] for h in reg.snapshot()["handlers"]
                             if h["task_type"] == "pool.refresh"][0])


def test_pool_refresh_routed_to_research_pool():
    router = CapabilityRouter()
    assert router.pool_for("pool.refresh") == "research"
    assert router.can_handle("research", "pool.refresh")


def test_handle_pool_refresh_calls_stockpool_and_returns_report():
    fake_report = {
        "source": "flow", "count": 11, "data_day": "20261012",
        "is_recent_day": True, "data_sources": {"flow_rank": "akshare_flow"},
    }

    class FakeStockPool:
        def __init__(self, *a, **k):
            pass

        def refresh(self, **kw):
            # 确认参数确实透传进刷新逻辑
            assert kw.get("source") == "flow"
            return dict(fake_report)

    task = SimpleNamespace(params={
        "source": "flow", "limit": 30, "sync": True, "force": False,
        "symbols": None, "exclusions": None,
    })
    with mock.patch("app.modules.universe.pool.StockPool", FakeStockPool):
        result = handle_pool_refresh(task, ctx=None)
    assert result["ok"] is True
    assert result["report"]["count"] == 11
    assert result["report"]["source"] == "flow"


def test_handle_pool_refresh_empty_candidate_is_success_not_dead_letter():
    """候选池为空属于「跑完了但没结果」，返回结果而非抛异常（不进死信）。"""
    class FakeStockPool:
        def __init__(self, *a, **k):
            pass

        def refresh(self, **kw):
            return {"error": "候选池为空（先同步数据？）"}

    task = SimpleNamespace(params={"source": "flow", "limit": 30})
    with mock.patch("app.modules.universe.pool.StockPool", FakeStockPool):
        result = handle_pool_refresh(task, ctx=None)
    assert result["ok"] is False
    assert "error" in result


def test_handle_pool_refresh_network_error_raises_for_dead_letter():
    """真实采集异常应抛出，让任务失败并进入错误队列。"""
    class FakeStockPool:
        def __init__(self, *a, **k):
            pass

        def refresh(self, **kw):
            raise RuntimeError("数据源全挂")

    task = SimpleNamespace(params={"source": "flow", "limit": 30})
    with mock.patch("app.modules.universe.pool.StockPool", FakeStockPool):
        try:
            handle_pool_refresh(task, ctx=None)
            raised = False
        except RuntimeError as exc:
            raised = "数据源全挂" in str(exc)
    assert raised
