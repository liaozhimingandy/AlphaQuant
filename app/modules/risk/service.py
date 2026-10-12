#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : risk.py
# @Description : 风控服务：引擎级的全局闸门与一键熔断
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""

为什么要有"服务级风控"
======================

重构前风控是**任务级**的：每个任务在 ``TaskSpec`` 里配一串规则，
建任务时 ``build_risk_chain`` 成一条链，挂在该任务身上。

这套设计有两个补不上的洞：

1. **没有全局视角**。一条规则要看"整个账户现在亏了多少"，
   而任务只知道自己这一本账。N 个任务各自限 20% 回撤，
   加起来总回撤可以到 60% —— 每个任务都合规，整体爆仓。
2. **没有一键熔断**。发现行情源抽风、网关对不上账、
   或者干脆是"今天不对劲想先停手"时，以前只能逐个改配置再重启，
   而重启意味着放弃所有持仓状态——恰恰是最不该重启的时刻。

``RiskService`` 补的就是这两条：

  * **全局闸门链** —— 与任务级链**串联**执行（先全局后任务），
    任一否决即否决，两边的 ``scale`` 取更小值。任务级规则保留，
    它管"这个策略该不该开这么大"，全局规则管"整个账户还能不能开"。
  * **一键熔断** —— :meth:`halt` 让之后的一切信号立即被拒，
    不重启、不改配置、不丢状态。:meth:`resume` 恢复。

熔断的实现刻意在**内存**里（不落库）：它是"值班的人现在想停手"，
进程重启后自然恢复交易是对的；把它持久化反而会导致
"上次熔断忘了恢复，下次启动永远不开仓"这种查不出来的故障。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.engine.capability import Cap, RestartPolicy, ServiceComponent
from app.domain.types import Account, Signal
from app.modules.risk.base import RiskVerdict
from app.modules.risk.registry import RiskChain, build_risk_chain
from app.modules.strategy.state import DecisionState
from app.common.logging import logger


class RiskService(ServiceComponent):
    """风控服务。

    **提供的能力**：``risk.gate``

    用法（由策略组件在初始化时把 :meth:`gate` 注入任务运行时）::

        task_runtime.risk_gate_provider = lambda: risk_service.gate

    于是每个任务在下单前会先过一遍全局闸门。
    """

    name = "risk_service"
    provides = frozenset({Cap.RISK_GATE})
    restart_policy = RestartPolicy.NEVER

    def __init__(
        self,
        rules: Optional[Any] = None,
        enabled: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        #: 全局闸门链。``rules`` 为空时是空链 —— 空链全部放行，
        #: 但熔断依然生效。这两件事是独立的：
        #: 没配全局规则不代表不需要一键停手的能力。
        self.chain: RiskChain = (
            rules if isinstance(rules, RiskChain) else build_risk_chain(rules)
        )
        if enabled is not None:
            self.enabled = bool(enabled)

        self._halted = False
        self._halt_reason = ""
        self._halt_at = ""
        self.stats: Dict[str, int] = {
            "checks": 0, "blocked": 0, "scaled": 0, "halts": 0,
        }
        #: 最近若干次否决，面板展示用（有界，长跑不涨内存）
        self.recent_blocks: list = []
        self._RECENT_MAX = 50

    # ============================================================
    # 生命周期
    # ============================================================
    def on_initialize(self) -> None:
        cfg = self.component_config or {}
        if cfg.get("rules"):
            self.chain = build_risk_chain(cfg["rules"])
        if not self.enabled:
            logger.info("风控服务已停用（所有信号直接放行）")
            return
        logger.info(
            f"风控服务就绪 | 全局规则 {len(self.chain)} 条: "
            f"{[r.name for r in self.chain.rules] or '（无，仅熔断生效）'}"
        )

    def on_start(self) -> None:
        # 已熔断时重启服务不该悄悄恢复交易 —— 那是人手动按下的按钮，
        # 只有人能再按回来。启动日志里明确写清楚，避免误解。
        if self._halted:
            logger.warning(
                f"风控服务已启动，但处于熔断状态 | 原因: {self._halt_reason}"
                f" | 需手动 resume() 才会恢复交易"
            )

    # ============================================================
    # 闸门（核心能力）
    # ============================================================
    @property
    def gate(self):
        """交给任务运行时注入的那个可调用对象。

        为什么返回 :meth:`check` 的绑定方法而不是整个服务：
        任务只需要"问一下能不能过"，给整个服务会让任务有能力
        去调 ``halt()`` —— 一个策略不该有停掉全场的权力。
        """
        return self.check

    def check(
        self,
        signal: Signal,
        state: DecisionState,
        ctx: Any = None,
        account: Optional[Account] = None,
    ) -> RiskVerdict:
        """全局闸门。**任何情况下都不抛异常**。

        风控抛异常比风控缺失更危险：异常会顺着下单链路往上冒，
        轻则这笔单丢掉，重则整个任务被标成 ERROR 而停止交易 ——
        于是"风控太严"变成了"完全不交易"，而这正是风控最不该造成的后果。
        所以：熔断直接否决，规则异常放行并留痕。
        """
        if not self.enabled:
            return RiskVerdict(True, rule="risk_service", reason="服务已停用")

        self.stats["checks"] += 1

        if self._halted:
            self.stats["blocked"] += 1
            return RiskVerdict(
                False, rule="manual_halt", reason=self._halt_reason or "已手动熔断"
            )

        try:
            verdict = self.chain.check(
                signal, state, ctx=ctx, account=account
            )
        except Exception as exc:
            # 链本身有 bug：放行并留痕（RiskChain 内部已对单条规则做过兜底，
            # 能走到这里说明是链结构的问题，不是某条规则的问题）
            logger.error(f"全局风控链执行异常，按放行处理: {exc}", exc_info=True)
            return RiskVerdict(True, rule="risk_service", reason="链异常，按放行")

        if not verdict.allowed:
            self.stats["blocked"] += 1
            self._remember(verdict, signal)
        elif verdict.scale < 1.0:
            self.stats["scaled"] += 1
        return verdict

    def _remember(self, verdict: RiskVerdict, signal: Signal) -> None:
        self.recent_blocks.append({
            "at": self._now(),
            "symbol": str(getattr(signal, "symbol", "") or ""),
            "side": str(getattr(getattr(signal, "side", None), "value", "") or ""),
            "rule": verdict.rule,
            "reason": verdict.reason,
        })
        if len(self.recent_blocks) > self._RECENT_MAX:
            del self.recent_blocks[: len(self.recent_blocks) - self._RECENT_MAX]

    # ============================================================
    # 一键熔断
    # ============================================================
    @property
    def is_halted(self) -> bool:
        return self._halted

    def halt(self, reason: str = "手动熔断") -> None:
        """停止一切新开仓。**只挡新信号，不砍已有持仓**。

        平仓信号同样会被挡住（本服务不区分方向）—— 这是刻意的：
        熔断是"我还没想清楚"，不是"自动清仓"。要清仓请显式操作，
        别让一个按钮同时具备"停手"和"砍仓"两种语义。
        """
        self._halted = True
        self._halt_reason = str(reason or "手动熔断")
        self._halt_at = self._now()
        self.stats["halts"] += 1
        logger.warning(f"🛑 风控熔断已触发 | {self._halt_reason}")

    def resume(self, reason: str = "") -> None:
        self._halted = False
        self._halt_reason = ""
        self._halt_at = ""
        logger.warning(f"✅ 风控熔断已解除{(' | ' + reason) if reason else ''}")

    # ============================================================
    # 可观测
    # ============================================================
    @staticmethod
    def _now() -> str:
        from datetime import datetime

        return datetime.now().isoformat(timespec="seconds")

    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base.update({
            "enabled": self.enabled,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "halt_at": self._halt_at or None,
            "rules": self.chain.to_dict(),
            "stats": dict(self.stats),
            "recent_blocks": list(self.recent_blocks[-10:]),
        })
        return base

    def health_check(self):
        if not self.enabled:
            return True, "disabled"
        if self._halted:
            # 熔断是**人为且预期**的状态，不是故障。
            # 标成不健康会让监控一直告警，最后没人看告警。
            return True, f"已熔断: {self._halt_reason}"
        blocked = self.stats["blocked"]
        checks = self.stats["checks"]
        if checks >= 50 and blocked / checks > 0.9:
            return False, f"近 {checks} 次检查中 {blocked} 次被否决（规则可能过严）"
        return True, f"{len(self.chain)} 条全局规则"


__all__ = ["RiskService"]
