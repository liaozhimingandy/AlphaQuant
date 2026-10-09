#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情采集服务的集成冒烟：真实引擎 + 真实 reactor + 真实面板 API。

与 app/test/test_collector.py 的分工：
  - 单元测试用假的采集动作，验证调度/统计/频率解析等纯逻辑
  - 这个脚本验证**整条链路真的通**：组件注册、API 路由、异步立即采集、
    频率热改、增删标的、事件发布

探针必须跑在工作线程 —— 面板和引擎共用一个 reactor，
在 reactor 线程里发 HTTP 请求会等自己的响应，永久死锁。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from twisted.internet import reactor, threads  # noqa: E402

BASE = "http://127.0.0.1:8793"
PORT = 8793
FAILED: list = []

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, timeout=10):
    try:
        with _opener.open(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")
    except Exception as e:
        return -1, {"error": str(e)}


def post(path, body, timeout=10):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")
    except Exception as e:
        return -1, {"error": str(e)}


def check(label, cond, extra=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {label} {extra}")
    if not cond:
        FAILED.append(label)


def run_probes():
    print("\n" + "=" * 62)
    print("1. 采集服务是否真的挂上引擎")
    print("=" * 62)

    st, data = get("/api/collector")
    check("GET /api/collector 200", st == 200, f"-> {st}")
    check("available=true", bool(data.get("available")), str(data)[:120])
    jobs = data.get("jobs") or []
    check("任务数 ≥ 1", len(jobs) >= 1, f"实际 {len(jobs)}")
    if not jobs:
        return FAILED
    print("   任务:", [(j["symbol"], j["period"], j["interval_human"]) for j in jobs])
    check("min_interval 存在", isinstance(data.get("min_interval"), (int, float)),
          f"= {data.get('min_interval')}")

    print("\n" + "=" * 62)
    print("2. 频率热改（面板里改了就生效，不用重启）")
    print("=" * 62)

    sym, per = jobs[0]["symbol"], jobs[0]["period"]
    st, data = post("/api/collector/interval",
                    {"symbol": sym, "period": per, "interval": "17m"})
    check("POST /api/collector/interval 200", st == 200, f"-> {st}")
    check("影响 1 个任务", data.get("affected") == 1, str(data)[:120])

    st, data = get("/api/collector")
    after = [j for j in data.get("jobs") or []
             if j["symbol"] == sym and j["period"] == per]
    check("频率真的变成 17m", after and after[0]["interval"] == 1020.0,
          f"= {after[0]['interval_human'] if after else '?'} ({after[0]['interval'] if after else '?'}s)")

    st, data = post("/api/collector/interval",
                    {"symbol": sym, "period": per, "interval": "1s"})
    check("非法频率被夹紧到下限", data.get("ok") is True)
    st, data = get("/api/collector")
    after = [j for j in data.get("jobs") or []
             if j["symbol"] == sym and j["period"] == per]
    check("1s 未突破 min_interval", after and after[0]["interval"] >= 15.0,
          f"= {after[0]['interval'] if after else '?'}s")

    print("\n" + "=" * 62)
    print("3. 运行时增删标的")
    print("=" * 62)

    st, data = post("/api/collector/add",
                    {"symbol": "sh600519", "period": "1d", "interval": "23m"})
    check("新增采集 200", st == 200, f"-> {st}")
    check("symbol 被归一化", (data.get("job") or {}).get("symbol") == "600519",
          f"= {(data.get('job') or {}).get('symbol')}")

    st, data = post("/api/collector/remove", {"symbol": "600519", "period": "1d"})
    check("移除采集 200", st == 200, f"-> {st}")
    check("影响 1 个任务", data.get("affected") == 1)

    st, data = post("/api/collector/remove", {"symbol": "600519", "period": "1d"})
    check("重复移除返回失败而非报错", data.get("ok") is False, str(data)[:80])

    print("\n" + "=" * 62)
    print("4. 异步立即采集（关键：不能把 reactor 堵死）")
    print("=" * 62)

    # 这个请求如果实现成了同步阻塞，下面这行会超时
    import time

    t0 = time.time()
    st, data = post("/api/collector/now", {"symbol": sym}, timeout=180)
    dt = time.time() - t0
    check("POST /api/collector/now 200", st == 200, f"-> {st} 耗时 {dt:.1f}s")
    has_row_detail = "rows" in data
    check("返回带 rows 字段", has_row_detail, str(data)[:160])
    if data.get("ok"):
        check("确有任务被执行", int(data.get("tasks") or 0) >= 1,
              f"tasks={data.get('tasks')} rows={data.get('rows')}")
    print("   注：若网络不可达，rows=0 且 errors 非空也算链路通（失败隔离生效）")

    # 引擎还活着吗？reactor 若被堵死，overview 会超时
    st, data = get("/api/overview")
    check("采集后引擎仍响应（reactor 未被阻塞）", st == 200, f"-> {st}")

    print("\n" + "=" * 62)
    print("5. 采集状态被正确记录")
    print("=" * 62)

    st, data = get("/api/collector")
    stt = data.get("state") or {}
    print("   state keys:", list(stt.keys()))
    key = f"{sym}:{per}"
    entry = stt.get(key) or {}
    check("该任务有运行记录", int(entry.get("runs") or 0) >= 1,
          f"runs={entry.get('runs')} rows={entry.get('rows')}")
    if entry.get("last_error"):
        print(f"   ⚠ 最近错误（网络不通时属预期）: {entry['last_error'][:100]}")

    print("\n" + "=" * 62)
    print("6. 采集组件出现在 health/组件列表里")
    print("=" * 62)

    st, data = get("/api/overview")
    comps = {c["name"]: c for c in (data.get("components") or [])}
    check("data_collector 已注册", "data_collector" in comps, f"组件: {list(comps)}")
    if "data_collector" in comps:
        c = comps["data_collector"]
        check("健康判定有结论", isinstance(c.get("healthy"), bool),
              f"healthy={c.get('healthy')} detail={c.get('health_detail')}")

    st, data = get("/api/audit")
    audits = [a["action"] for a in (data.get("audit") or [])]
    print("   留痕:", audits[-8:])
    check("采集操作有留痕", any(a.startswith("collector_") for a in audits))

    return FAILED


def main():
    global engine, TMP
    from app.core.engine.builder import build_engine

    cfg = {
        "interval": "30m",          # 冒烟期间别真去高频请求数据源
        "period": "1d",
        "min_interval": 15,
        "symbols": ["000001"],
    }
    tmp = Path(tempfile.mkdtemp(prefix="collector-smoke-"))
    TMP = tmp
    cfg_path = tmp / "collector.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    engine = build_engine(
        mode="SIMULATE",
        symbols=["000001"],
        market_mode="poll",
        market_interval=2.0,
        with_news=False,
        with_monitor=True,
        monitor_port=PORT,
        monitor_interval=999.0,      # 冒烟期间别频繁落盘快照
        namespace="collector-smoke",
        auto_load_runtime=False,
        with_collector=True,
        collector_config=str(cfg_path),
    )

    col = engine.get_component("data_collector")
    if col is None:
        print("❌ 采集组件未注册，冒烟中止")
        sys.exit(1)
    print("=" * 62)
    print(f"引擎已装配 | 采集任务 {len(col.spec.jobs)} 个")
    for j in col.spec.jobs:
        print(f"  {j.key}  每 {j.interval}s")
    print("=" * 62)

    # 探针必须在工作线程跑：面板与引擎共用一个 reactor，
    # 在 reactor 线程发 HTTP 请求会等自己的响应，直接死锁。
    reactor.callLater(
        0.5, lambda: threads.deferToThread(run_probes).addCallbacks(on_done, on_error)
    )
    ctx = engine.start()
    print(f"引擎结束 | run_id={ctx.run_id} | status={ctx.engine_status.value}")
    sys.exit(2 if FAILED else 0)


engine = None
TMP = None


def _finish(failed, tmp):
    print("\n" + "=" * 62)
    if failed:
        print(f"❌ {len(failed)} 项未通过: {failed}")
    else:
        print("✅ 采集服务集成冒烟全部通过")
    print("=" * 62)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    reactor.callFromThread(engine.stop, False)


def on_done(result):
    _finish(result, TMP)


def on_error(failure):
    print("探针异常:", failure.getErrorMessage())
    _finish(["异常"], TMP)


if __name__ == "__main__":
    main()
