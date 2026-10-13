#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集编排视图：立即补采/计划查看与保存/交易日历。"""
from __future__ import annotations


from datetime import date, datetime
from typing import Any, Deque, Dict, List, Optional

from app.common.logging import logger


class CollectViewsMixin:
    # ============================================================
    # 数据采集（标的即时采集 + 时间窗口设置）
    # ============================================================
    def collect_now(self, symbols: Any = None,
                    categories: Any = None) -> Dict[str, Any]:
        """立即采集一批标的的**全类别**数据（工作线程执行）。

        用户选了一只新票，最想的是"马上看看它的因子长什么样"，
        而不是等下一个采集窗口。所以这里把所有类别都跑一遍。

        实际逻辑抽到 ``collection.service.run_full_collection``，
        面板任务处理器 ``handle_collect_now`` 也走同一份，避免两份实现漂移。
        """
        try:
            from app.modules.collection.service import run_full_collection

            syms = self._factor_symbols(symbols)
            if not syms:
                return {"ok": False, "error": "没有指定标的"}
            cats = [str(c) for c in (categories or [])] or [
                "lhb", "margin", "forecast", "holders", "rating", "news",
            ]
            return run_full_collection(syms, cats)
        except Exception as exc:
            logger.error(f"即时采集失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def schedule_view(self) -> Dict[str, Any]:
        """采集时间窗口 + 当前处在哪个窗口。"""
        try:
            from app.modules.collection.schedule import get_schedule
            from app.domain.calendar import get_calendar

            sch = get_schedule()
            slot = sch.slot_of()
            return {
                "schedule": sch.to_dict(),
                "current_slot": slot,
                "slot_names": {"pre": "盘前", "intraday": "盘中",
                               "post": "盘后定稿"},
                "market": get_calendar().to_dict(),
            }
        except Exception as exc:
            logger.debug(f"读取采集时间窗口失败: {exc}")
            return {}

    def schedule_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.modules.collection.schedule import (
                CollectionSchedule,
                get_schedule,
                set_schedule,
            )

            base = get_schedule()
            fields = {f for f in CollectionSchedule.__dataclass_fields__}
            raw = {k: v for k, v in body.items() if k in fields}
            if not raw:
                return {"ok": False, "message": "没有可识别的字段"}
            merged = CollectionSchedule.from_dict({**base.to_dict(), **raw})
            set_schedule(merged, persist=True)
            # 审计：改采集时间会影响"什么时候有数据"，属于值得留痕的操作
            audit = getattr(self, "audit", None)
            if audit is not None:
                audit.append({
                    "at": datetime.now().isoformat(), "action": "save_schedule",
                    "detail": {k: getattr(merged, k) for k in raw},
                })
            return {"ok": True, "message": "已保存", "schedule": merged.to_dict()}
        except Exception as exc:
            logger.error(f"保存采集时间窗口失败: {exc}", exc_info=True)
            return {"ok": False, "message": str(exc)}

    def calendar_view(self) -> Dict[str, Any]:
        """交易日历与市场状态。

        面板要能回答"为什么它不采数据" —— 闭市是**正常行为**，
        但如果不显示出来，用户看到"采集 0 次"只会以为坏了。
        """
        try:
            from app.domain.calendar import STATE_NAMES, get_calendar

            cal = get_calendar()
            info = cal.to_dict()
            info["state_names"] = dict(STATE_NAMES)
            return info
        except Exception as exc:
            logger.debug(f"交易日历读取失败: {exc}")
            return {}

    # ============================================================
    # 关注标的（自选股 —— 按各自频率定时自动采集）
    # ============================================================
    # 这里的清单一律**不依赖采集服务是否已装配**：清单在库里，
    # 服务没起来时用户也该看得见自己关注了什么。
    # 只有"立即采集"需要活的服务（它要用服务当前生效的配置）。

    def _collection_component(self) -> Any:
        """取采集服务实例；没装配返回 ``None``（而不是抛）。"""
        try:
            control = getattr(self, "control", None)
            getter = getattr(control, "get_component", None)
            if callable(getter):
                return getter("collection_service")
        except Exception:
            pass
        return None

    def _watchlist_default_interval(self) -> str:
        """新增关注标的时用的默认频率。"""
        try:
            from app.modules.collection.watchlist import get_watchlist_config

            return str(get_watchlist_config().interval or "1h")
        except Exception:
            return "1h"

    def _watchlist_audit(self, action: str, detail: Dict[str, Any]) -> None:
        """增删改都值得留痕 —— 它们改变的是"系统接下来会自动去做什么"。"""
        audit = getattr(self, "audit", None)
        if audit is None:
            return
        audit.append({
            "at": datetime.now().isoformat(),
            "action": f"watchlist_{action}",
            "detail": dict(detail),
        })

    def watchlist_view(self, enabled_only: bool = False) -> Dict[str, Any]:
        """关注标的全景：清单 + 默认配置 + 服务运行状态。"""
        try:
            from app.modules.collection.watchlist import watchlist_view as _view

            data = _view(enabled_only=enabled_only)
        except Exception as exc:
            logger.error(f"读取关注标的失败: {exc}", exc_info=True)
            return {}
        svc = self._collection_component()
        data["service"] = svc.watchlist_status() if svc is not None else {}
        return data

    def watchlist_add(self, symbol: Any, freq: Any = "",
                      categories: Any = None, name: str = "") -> Dict[str, Any]:
        """加入关注。重复添加 = 更新配置（不报错、不产生第二条）。"""
        try:
            from app.data.repo.watchlist_repo import add_watch

            sym = str(symbol or "").strip()
            if not sym:
                return {"ok": False, "error": "缺少 symbol"}
            # 没给频率就用配置里的默认值 —— 让"最常见的操作"不需要任何思考
            item = add_watch(sym, freq=freq or self._watchlist_default_interval(),
                             categories=categories, name=str(name or ""))
        except Exception as exc:
            logger.error(f"添加关注标的失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}
        self._watchlist_audit("add", {"symbol": item.get("symbol"),
                                      "freq": item.get("freq")})
        return {"ok": True, "item": item}

    def watchlist_remove(self, symbol: Any) -> Dict[str, Any]:
        try:
            from app.data.repo.watchlist_repo import remove_watch

            got = remove_watch(str(symbol or "").strip())
        except Exception as exc:
            logger.error(f"取消关注失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}
        if not got:
            return {"ok": False, "error": "该标的本来就不在关注列表里"}
        self._watchlist_audit("remove", {"symbol": str(symbol)})
        return {"ok": True, "message": "已取消关注"}

    def watchlist_set_freq(self, symbol: Any, freq: Any) -> Dict[str, Any]:
        """改单只标的的采集频率。**存库**，不被配置文件覆盖。"""
        try:
            from app.data.repo.watchlist_repo import set_watch_freq

            item = set_watch_freq(str(symbol or "").strip(), freq)
        except KeyError as exc:
            # ``str(KeyError)`` 会把消息再包一层引号（"'标的 X 不在关注列表里'"），
            # 直接透传会让面板上显示成一道带引号的错误。取它的原始消息。
            return {"ok": False,
                    "error": str(exc.args[0]) if exc.args else str(exc)}
        except Exception as exc:
            logger.error(f"修改关注标的频率失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}
        self._watchlist_audit("freq", {"symbol": item.get("symbol"),
                                       "freq": item.get("freq")})
        return {"ok": True, "item": item}

    def watchlist_set_categories(self, symbol: Any,
                                 categories: Any) -> Dict[str, Any]:
        try:
            from app.data.repo.watchlist_repo import set_watch_categories

            item = set_watch_categories(str(symbol or "").strip(), categories)
        except KeyError as exc:
            # ``str(KeyError)`` 会把消息再包一层引号（"'标的 X 不在关注列表里'"），
            # 直接透传会让面板上显示成一道带引号的错误。取它的原始消息。
            return {"ok": False,
                    "error": str(exc.args[0]) if exc.args else str(exc)}
        except Exception as exc:
            logger.error(f"修改关注标的类别失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}
        self._watchlist_audit("categories", {"symbol": item.get("symbol"),
                                             "categories": item.get("categories")})
        return {"ok": True, "item": item}

    def watchlist_set_enabled(self, symbol: Any,
                              enabled: bool = True) -> Dict[str, Any]:
        """停用/启用单只标的（保留记录与已配频率，随时能开回来）。"""
        try:
            from app.data.repo.watchlist_repo import set_watch_enabled

            item = set_watch_enabled(str(symbol or "").strip(), bool(enabled))
        except KeyError as exc:
            # ``str(KeyError)`` 会把消息再包一层引号（"'标的 X 不在关注列表里'"），
            # 直接透传会让面板上显示成一道带引号的错误。取它的原始消息。
            return {"ok": False,
                    "error": str(exc.args[0]) if exc.args else str(exc)}
        except Exception as exc:
            logger.error(f"切换关注标的开关失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}
        self._watchlist_audit("enabled", {"symbol": item.get("symbol"),
                                          "enabled": item.get("enabled")})
        return {"ok": True, "item": item}

    def watchlist_set_default_freq(self, freq: Any) -> Dict[str, Any]:
        """改**新增**关注标的时的默认频率，并落盘到 config/collector.json。

        只改内存的话，重启就退回去了；而面板不会提示，
        用户会以为自己改成功了 —— 这类"静默失效"最伤信任。
        """
        try:
            from app.modules.collection.watchlist import (
                get_watchlist_config, set_watchlist_config,
            )

            cfg = get_watchlist_config()
            cfg.interval = str(freq or "").strip() or cfg.interval
            set_watchlist_config(cfg, persist=True)
        except Exception as exc:
            logger.error(f"保存关注标的默认频率失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}
        self._watchlist_audit("default_freq", {"interval": cfg.interval})
        return {"ok": True, "message": "已保存", "config": cfg.to_dict()}

    def watchlist_collect(self, symbols: Any = None) -> Dict[str, Any]:
        """**不管到没到点**，立刻采一轮（联网，秒级，由路由层异步执行）。

        为什么这里不去走 ``dispatch_task("collect.now")``：
        那个任务只管取数，不会更新关注标的的 ``last_collected_at``。
        用户点了"立即采集"，如果记账没更新，下一轮扫描会立刻再采一遍 ——
        等于点一次按钮触发两轮全类别请求。
        """
        svc = self._collection_component()
        if svc is None:
            # 服务没装配时也别假成功：给一个明确的替代路径或原因
            return {"ok": False,
                    "error": "采集服务未装配，无法执行关注标的采集"}
        try:
            return svc.collect_watchlist_now(symbols=symbols)
        except Exception as exc:
            logger.error(f"关注标的立即采集失败: {exc}", exc_info=True)
            return {"ok": False, "error": str(exc)}
