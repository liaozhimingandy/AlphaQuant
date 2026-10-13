#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集任务统一调度 + 落库兼容性的回归测试。

覆盖三件事：
1. ``_upsert`` 按 SQLite 变量上限分块，根治 "too many SQL variables"；
2. ``_flush`` 在**系统性存储故障**（批量 + 逐条都写不进）时上浮
   :class:`StorageError`，让任务进错误队列可重放；部分坏行仍走 failed；
3. 看板三类采集（collect.now / collect.cross / collect.kline）已注册为
   引擎任务类型，经调度器路由到行情池、由对应处理器执行。
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest
from sqlalchemy import Column, Float, String, create_engine
from sqlalchemy.orm import Session, declarative_base
from twisted.internet import defer

from app.common.errors import StorageError
from app.data.repo import kline_repo as R
from app.modules.collection import service as COLSVC
from app.modules.universe.pool import sync as SYNC
from app.runtime.events.types import TASK_QUEUE, TaskType
from app.runtime.tasking.models import EngineTask
from app.runtime.workers import handlers as H


# ---------------------------------------------------------------------------
# 1. _upsert 分块
# ---------------------------------------------------------------------------
_Base = declarative_base()


class _Row(_Base):
    __tablename__ = "t_upsert_chunk"
    symbol = Column(String, primary_key=True)
    date = Column(String, primary_key=True)
    c00 = Column(Float); c01 = Column(Float); c02 = Column(Float)
    c03 = Column(Float); c04 = Column(Float); c05 = Column(Float)
    c06 = Column(Float); c07 = Column(Float); c08 = Column(Float)
    c09 = Column(Float); c10 = Column(Float); c11 = Column(Float)
    c12 = Column(Float)


def _make_rows(n: int):
    cols = [f"c{i:02d}" for i in range(13)]
    out = []
    for i in range(n):
        row = {"symbol": f"S{i:04d}", "date": "2024-01-01"}
        for c in cols:
            row[c] = float(i)
        out.append(row)
    return out


def test_upsert_chunks_within_sqlite_var_limit():
    """一次性插 200 行 × 13 列 = 2600 占位符，远超默认 999，
    分块后不应报 too many SQL variables。"""
    eng = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(eng)
    rows = _make_rows(200)
    with Session(eng) as s:
        n = R._upsert(s, _Row, rows, ["symbol", "date"])
        s.commit()
        assert n == 200
        assert s.query(_Row).count() == 200


# ---------------------------------------------------------------------------
# 2. _flush 错误上浮
# ---------------------------------------------------------------------------
def _fake_frame(symbol: str = "600000"):
    return pd.DataFrame([{
        "symbol": symbol, "date": "2024-01-01", "close": 1.0,
        "pct_change": 0.0, "turnover": 1.0, "amount": 1.0,
        "main_net": 0.0, "main_net_pct": 0.0, "super_large_net": 0.0,
        "large_net": 0.0, "medium_net": 0.0, "small_net": 0.0,
        "updated_at": "2024-01-01 09:30:00",
    }])


def test_flush_raises_storage_error_when_all_fail():
    """批量 + 逐条都写不进 → 存储后端故障，必须上浮 StorageError。"""
    frames = [_fake_frame(f"60000{i}") for i in range(3)]

    def boom(_df):
        raise sqlite3.OperationalError("database is locked")

    failed = []
    with pytest.raises(StorageError):
        SYNC._flush("fund_flow", frames, boom, failed)


def test_flush_partial_success_records_failed_not_raises():
    """批量挂、逐条部分成功 → 返回已存行数，坏行进 failed，不上浮。"""
    frames = [_fake_frame(f"60000{i}") for i in range(3)]

    def save(df):
        if len(df) > 1:
            raise RuntimeError("batch boom")  # 批量失败触发退回逐条
        if df.iloc[0]["symbol"] == "600002":
            raise RuntimeError("row boom")     # 这一行数据坏
        return 1

    failed = []
    n = SYNC._flush("fund_flow", frames, save, failed)
    assert n == 2
    assert len(failed) == 1
    assert failed[0][0] == "600002"


# ---------------------------------------------------------------------------
# 3. 采集任务注册 / 路由 / 处理器
# ---------------------------------------------------------------------------
def test_collect_task_types_routed_to_market_queue():
    assert TASK_QUEUE[TaskType.COLLECT_NOW] == "market"
    assert TASK_QUEUE[TaskType.COLLECT_CROSS] == "market"
    assert TASK_QUEUE[TaskType.COLLECT_KLINE] == "market"


def test_collect_handlers_registered():
    assert "collect.now" in H.DEFAULT_HANDLERS
    assert "collect.cross" in H.DEFAULT_HANDLERS
    assert "collect.kline" in H.DEFAULT_HANDLERS


def _fake_ctx_with_collection_service(svc):
    class _Engine:
        def get_component(self, name):
            return svc if name == "collection_service" else None
    # _require(ctx, name) 走 ctx.engine.get_component(name)
    class _Ctx:
        engine = _Engine()
    return _Ctx()


def test_handle_collect_now_delegates_to_run_full_collection(monkeypatch):
    captured = {}

    def fake_run(symbols, categories):
        captured["symbols"] = symbols
        captured["categories"] = categories
        return {"ok": True, "symbols": list(symbols or []), "detail": {}}

    monkeypatch.setattr(COLSVC, "run_full_collection", fake_run)
    task = EngineTask(task_type="collect.now",
                      params={"symbols": ["600519"], "categories": ["fundamental"]})
    result = H.handle_collect_now(task, None)
    assert result["ok"] is True
    assert captured["symbols"] == ["600519"]
    assert captured["categories"] == ["fundamental"]


def test_handle_collect_cross_returns_deferred(monkeypatch):
    svc = type("S", (), {})()
    svc.collect_categories = staticmethod(
        lambda syms, cats: defer.succeed({"symbols": syms, "detail": {}}))
    task = EngineTask(task_type="collect.cross",
                      params={"symbols": ["600519"], "categories": ["fundamental"]})
    d = H.handle_collect_cross(task, _fake_ctx_with_collection_service(svc))
    assert isinstance(d, defer.Deferred)
    out = {}
    d.addCallback(lambda r: out.update(r))
    # 同步驱动：Deferred 已在主线程 resolve
    assert out == {"symbols": ["600519"], "detail": {}}


def test_handle_collect_kline_raises_when_no_jobs(monkeypatch):
    svc = type("S", (), {})()
    svc.kline_spec = type("K", (), {"jobs": []})()
    task = EngineTask(task_type="collect.kline", params={"symbol": "600519"})
    with pytest.raises(RuntimeError):
        H.handle_collect_kline(task, _fake_ctx_with_collection_service(svc))
