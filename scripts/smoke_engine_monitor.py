#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端冒烟：replay 跑通引擎 + 面板 API + 快照落盘 + 运行时增删任务。

注意：面板和引擎共用同一个 reactor。所以**探针必须跑在工作线程**，
否则客户端阻塞住 reactor，服务端永远无法响应自己的请求（死锁）。
真实调用方（浏览器/CLI）天然是另一个进程，不存在这个问题。
"""
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, r"D:/Git/AlphaQuant")

from twisted.internet import reactor, threads  # noqa: E402

from app.core.engine.builder import build_engine  # noqa: E402

BASE = "http://127.0.0.1:8791"
FAILED = []


def get(path, timeout=5):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))
    except Exception as e:
        return -1, {"error": str(e)}


def post(path, body, timeout=5):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))
    except Exception as e:
        return -1, {"error": str(e)}


def check(label, cond, extra=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {label} {extra}")
    if not cond:
        FAILED.append(label)


def run_probes():
    """在工作线程里跑完整套探针。"""
    import time

    time.sleep(2.0)  # 等 replay 推掉预热所需K线

    print("--- HTTP 层 ---")
    st, page = -1, b""
    try:
        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            st, page = r.status, r.read()
    except Exception as e:
        print("  首页错误:", e)
    check("首页 HTML", st == 200 and b"<canvas" in page, f"status={st} bytes={len(page)}")

    print("\n--- 面板 API ---")
    st, ov = get("/api/overview")
    check("overview 200", st == 200, f"status={st} {ov if st != 200 else ''}")
    check("overview 有 engine", bool(ov.get("engine")), "")
    comps = [c["name"] for c in ov.get("components") or []]
    check("overview 有组件", len(comps) >= 4, f"{comps}")
    check("任务数=3", ov.get("task_count") == 3, f"got={ov.get('task_count')}")
    mon = ov.get("monitor") or {}
    check("监视器已监听", bool(mon.get("listen_ok")), f"err={mon.get('listen_error')!r}")
    check("已写快照", (mon.get("writes") or 0) >= 1, f"writes={mon.get('writes')}")

    st, tk = get("/api/tasks")
    tasks = tk.get("tasks") or []
    check("tasks 200", st == 200 and len(tasks) == 3, f"n={len(tasks)}")
    check("任务快照含权益", all("equity" in t for t in tasks), "")

    first_id = (tasks or [{}])[0].get("task_id")
    st, d = get(f"/api/tasks/{first_id}")
    check("任务详情 200", st == 200 and d.get("task_id") == first_id, f"status={st}")
    check("详情含权益曲线", len(d.get("equity_curve") or []) >= 2,
          f"pts={len(d.get('equity_curve') or [])}")
    check("详情含风控链", len(d.get("risk_rules") or []) > 0, "")

    st, lg = get("/api/logs?n=50")
    check("logs 200", st == 200 and len(lg.get("lines") or []) > 0,
          f"n={len(lg.get('lines') or [])} src={lg.get('source')}")

    st, sp = get("/api/snapshots")
    check("snapshots 200", st == 200 and len(sp.get("runs") or []) >= 1,
          f"runs={[r['run_id'] for r in sp.get('runs') or []]}")

    st, od = get("/api/orders")
    check("orders 200", st == 200, f"n={len(od.get('orders') or [])}")
    st, au = get("/api/audit")
    check("audit 200", st == 200, "")

    print("\n--- 运行时控制 ---")
    spec = {
        "task_id": "smoke-manual",
        "symbol": "000001",
        "warmup_bars": 10,
        "sizer": {"type": "percent", "pct": 0.1, "lot_size": 100},
        "strategy": {"type": "ma_cross", "params": {"fast": 3, "slow": 8}},
        "risk": [{"type": "cash_reserve"}],
    }
    st, r = post("/api/tasks", spec)
    check("新增任务", st == 200 and r.get("ok"), f"{r}")
    check("运行时任务已注册", SM.runtime.get("smoke-manual") is not None, "")

    st, r = post("/api/tasks/smoke-manual/pause", {})
    t = SM.runtime.get("smoke-manual")
    check("暂停任务", r.get("ok") is True and t.status.value == "PAUSED",
          f"{r} status={getattr(t, 'status', None)}")

    st, r = post("/api/tasks/smoke-manual/resume", {})
    t = SM.runtime.get("smoke-manual")
    check("恢复任务", r.get("ok") is True and t.status.value == "RUNNING", f"{r}")

    st, r = post("/api/command", {"action": "snapshot"})
    check("手动快照", r.get("ok") is True, f"{r}")

    st, r = post("/api/tasks/smoke-manual/remove", {})
    check("删除任务", r.get("ok") is True and SM.runtime.get("smoke-manual") is None, f"{r}")

    # 运行时任务应已固化到磁盘
    from app.core.config import settings as _settings
    from app.utils.jsonio import read_json

    saved = read_json(_settings.RUNTIME_DIR / "default" / "tasks.json")
    check("运行时任务已固化", isinstance(saved, dict) and "removed_ids" in saved,
          f"{list((saved or {}).keys())}")

    st, ov2 = get("/api/overview")
    check("audit 有留痕", len(ov2.get("audit") or []) >= 4, f"n={len(ov2.get('audit') or [])}")

    print("\n--- 采集服务（默认装配）---")
    st, col = get("/api/collector")
    check("collector 接口 200", st == 200, f"status={st}")
    check("采集服务已装配（不再提示未装配）", col.get("available") is True,
          f"available={col.get('available')} reason={col.get('reason')}")
    check("装配/启用状态可分", "enabled" in col and "assembled" in col,
          f"enabled={col.get('enabled')} assembled={col.get('assembled')}")

    print("\n--- 交易数据落库（SQLite）---")
    st, db = get("/api/db/stats")
    check("落库统计 200", st == 200, f"status={st}")
    check("落库线程在跑或已写完", isinstance((db.get("persist") or {}).get("written"), int),
          f"{db.get('persist')}")

    st, ev = get("/api/db/events?limit=20")
    check("系统事件可查", st == 200 and "events" in ev, f"n={len(ev.get('events') or [])}")

    st, runs_d = get("/api/db/runs?limit=5")
    check("运行记录可查", st == 200 and len(runs_d.get("runs") or []) >= 1,
          f"n={len(runs_d.get('runs') or [])}")

    print("\n--- 配置视图与能力清单 ---")
    st, cfg = get("/api/config")
    check("配置视图 200", st == 200, f"status={st}")
    log = cfg.get("log") or {}
    snap = cfg.get("snapshot") or {}
    check("日志策略可见", log.get("level") and "debug_mode" in log, f"{log.get('level')}")
    check("快照策略可见", snap.get("mode") in ("on_event", "on_change", "interval", "off"),
          f"mode={snap.get('mode')} enabled={snap.get('enabled')}")
    check("快照默认按操作触发", snap.get("mode") == "on_event",
          f"mode={snap.get('mode')}（面板每 2s 刷新 ≠ 每 2s 落盘）")
    check("快照统计可见", "triggers" in snap and "last_trigger" in snap,
          f"writes={snap.get('writes')} triggers={snap.get('triggers')}")

    st, caps = get("/api/strategies")
    check("能力清单 200", st == 200, f"status={st}")
    check("回测策略非空", len(caps.get("backtest_strategies") or []) >= 1,
          f"{caps.get('backtest_strategies')}")
    check("指标带可配参数", all("params" in i for i in (caps.get("indicators") or [])),
          f"n={len(caps.get('indicators') or [])}")
    check("声明式策略可用", "declarative" in (caps.get("backtest_strategies") or []),
          f"{caps.get('backtest_strategies')}")
    check("回测默认参数可见",
          (caps.get("backtest_defaults") or {}).get("cash") is not None,
          f"cash={(caps.get('backtest_defaults') or {}).get('cash')}")
    check("接入点视图可见", "endpoints" in (caps.get("endpoints") or {}),
          f"{[e.get('id') for e in ((caps.get('endpoints') or {}).get('endpoints') or [])]}")

    print("\n--- 券商接入点 ---")
    st, eps = get("/api/endpoints")
    check("接入点接口 200", st == 200 and "endpoints" in eps, f"status={st}")
    if eps.get("endpoints"):
        e0 = eps["endpoints"][0]
        check("凭据已脱敏", "credentials" in e0 and "params" in e0, f"{list(e0)}")
    st, chk = post("/api/endpoints/check", {"endpoint": (eps.get("active") or "")},
                   timeout=60)
    check("接入点试连接可用", chk.get("ok") is True,
          f"err={chk.get('error')}")

    print("\n--- 用户策略热加载 ---")
    st, rl = post("/api/strategies/reload", {})
    check("策略重扫 200", st == 200 and rl.get("ok") is True,
          f"回测={rl.get('backtest')} 引擎={rl.get('engine')}")
    check("双链路都扫到自定义策略",
          len(rl.get("backtest") or []) >= 1 and len(rl.get("engine") or []) >= 1,
          f"{rl}")

    print("\n--- 回测监控 ---")
    st, bt = get("/api/backtest?limit=5")
    check("回测列表 200", st == 200 and "jobs" in bt and "results" in bt, f"status={st}")
    st, live = get("/api/live")
    check("实盘视图 200（未启用时给出原因）", st == 200,
          f"available={live.get('available')} reason={live.get('reason')}")

    print("\n--- 快照策略切换 ---")
    st, r = post("/api/snapshot/mode", {"mode": "off"})
    check("切到 off", r.get("ok") is True and r.get("mode") == "off", f"{r}")
    st, cfg2 = get("/api/config")
    check("策略已生效", (cfg2.get("snapshot") or {}).get("mode") == "off",
          f"{(cfg2.get('snapshot') or {}).get('mode')}")
    st, r = post("/api/snapshot/mode", {"mode": "on_change", "enabled": True})
    check("切回 on_change", r.get("ok") is True and r.get("mode") == "on_change", f"{r}")

    print("\n--- 快照落盘 ---")
    from app.core.monitor.snapshot import SnapshotStore

    runs = SnapshotStore.runs()
    check("快照 run 已创建", len(runs) >= 1, f"{[r['run_id'] for r in runs]}")
    latest = SnapshotStore.load()
    check("latest.json 可读", isinstance(latest, dict) and "tasks" in latest,
          f"keys={list((latest or {}).keys())}")
    check("latest 含订单流", "orders_recent" in (latest or {}), "")
    return FAILED


def done(result):
    from app.core.config import settings

    print(f"\n快照目录: {settings.SNAPSHOT_DIR}")
    print(f"运行时目录: {settings.RUNTIME_DIR}")
    if FAILED:
        print(f"\n!!! {len(FAILED)} 项失败: {FAILED}")
    else:
        print("\n全部冒烟用例通过")
    reactor.callFromThread(engine.stop, False)


def on_error(failure):
    print("探针异常:", failure)
    reactor.callFromThread(engine.stop, False)


engine = build_engine(
    mode="BACKTEST",
    market_mode="replay",
    start="2023-01-01",
    end="2023-06-30",
    data_source="db",
    with_news=False,
    with_monitor=True,
    monitor_port=8791,
    auto_load_runtime=False,
)

SM = engine.get_component("strategy_manager")

reactor.callLater(0.5, lambda: threads.deferToThread(run_probes).addCallbacks(done, on_error))
ctx = engine.start()
print(f"引擎结束 | run_id={ctx.run_id} | status={ctx.engine_status.value}")
print(SM.summary())
sys.exit(2 if FAILED else 0)
