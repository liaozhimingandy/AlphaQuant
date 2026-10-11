#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段三：为每个包重建 __init__.py —— 声明它属于哪一层、职责边界、依赖方向。

刻意**只写文档、不写导出**：真实的 re-export 会让所有子模块在 ``import app.xxx``
时被一次性拉起，本项目的 historical 包之间存在多处延迟 import 掩盖的循环依赖，
一上来就补全会立刻死锁。先让包结构生效，导出留到依赖理顺之后逐个收口。

用法：python make_inits.py --apply
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

HDR = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"

#: 包路径 -> (一句话职责, 依赖方向说明)
PACKAGES: dict[str, tuple[str, str]] = {
    "app": (
        "AlphaQuant 应用主包。",
        "分层自底向上单向依赖：common → domain → datasource → storage → "
        "feature → alpha → strategy → risk/portfolio → execution → engine → monitoring/cli。\n"
        "**禁止反向依赖**（storage 不许 import feature，datasource 不许 import alpha）。",
    ),
    "app/common": (
        "L0 公共基础设施：配置、日志、JSON 序列化。",
        "零业务依赖，可被任意层引用；自身不 import 任何 app 子包。",
    ),
    "app/domain": (
        "L1 领域模型：证券代码、K 线、Tick、信号、订单、持仓、账户、交易日历。",
        "纯数据模型与领域规则，只依赖 common；不认识任何数据源、不认识任何策略。",
    ),
    "app/datasource": (
        "L2 数据源层：DataSource → Provider → Capability → Adapter → Router → DataService。",
        "对外唯一出口是 service.DataService；上层禁止直接 import adapters/*。",
    ),
    "app/datasource/model": (
        "L2 Canonical Model：系统内部统一数据模型（Kline/RankRow/FundFlowRow + Request）。",
        "外部数据源的原始格式只在 adapters 里转成这里的结构，**只此一次**。",
    ),
    "app/datasource/provider": (
        "L2 Provider 抽象：DataCapability 能力枚举、ProviderError 错误分类、"
        "能力级熔断与健康评分、ProviderRegistry。",
        "Provider 按「能力」声明自己能干什么，而不是按「数据源」堆方法。",
    ),
    "app/datasource/adapters": (
        "L2 Adapter：把各外部数据源的原始格式转成 Canonical Model。",
        "新增数据源只需在这里加一个适配器并在 config/providers.json 注册，"
        "其余层零改动。",
    ),
    "app/datasource/routing": (
        "L2 Router：能力匹配 → 排序 → 熔断 → 执行 + 质量校验 → 换下一个。",
        "以及 Provider 的 YAML/JSON 配置装载。",
    ),
    "app/datasource/service": (
        "L2 DataService：对上层提供的统一数据 API。",
        "``get_kline / get_hot_rank / get_flow_rank / get_fund_flow`` —— "
        "调用方不需要知道数据来自哪个源。",
    ),
    "app/datasource/transport": (
        "L2 统一网络传输层：UA/Referer、代理→直连回退、域名健康、僵尸代理摘除。",
        "所有走 HTTP 的适配器都应经由这里，便于统一埋点与故障降级。",
    ),
    "app/datasource/raw": (
        "L2 Raw(Bronze) 层：原始响应留档。",
        "第三方接口改字段/失效时可回溯与重放，标准模型的血缘 raw_id 指向这里。",
    ),
    "app/datasource/compat": (
        "L2 兼容期保留的旧注册表实现（旧的 REGISTRY + 各 *_source 适配器）。",
        "新写入的取数路径请一律走 datasource.provider/router；"
        "本包会随迁移完成逐步下线，请勿新增依赖。",
    ),
    "app/storage": (
        "L3 存储层：ORM schema、会话管理、Repository。",
        "面向存储的实现细节关在这里；业务层只见 Repository，不见 Session。",
    ),
    "app/storage/repo": (
        "L3 Repository：按聚合根组织的持久化访问。",
        "kline_repo(日线)/minute_repo(分钟线)/stock_repo(股票与榜单)/trade_repo(交易记录)。",
    ),
    "app/feature": (
        "L4 特征层：向量化技术指标 → 标量因子 → 因子表达式 / 挖掘。",
        "indicator 输入等长序列输出数组；factor 输入上下文输出标量；"
        " mining 负责机器学习驱动的因子挖掘。",
    ),
    "app/feature/indicator": (
        "L4 向量化技术指标：输入 BarSeries 输出等长 ndarray，前导 NaN。",
        "被 feature.base 的因子通过 FactorContext 降维消费。",
    ),
    "app/feature/mining": (
        "L4 因子挖掘：候选因子生成、评估与机器学习模型。",
        "依赖 numpy/pandas，不引入额外框架。",
    ),
    "app/alpha": (
        "L5 Alpha 层：多维度加权打分引擎与候选股票池。",
        "把 feature 层的原始因子值合成为可比较的综合分，产出交易候选。",
    ),
    "app/alpha/pool": (
        "L5 候选池：题材/概念/基本面/资金/舆情/榜单等维度的候选股同步与管理。",
        "依赖 storage 与 datasource，产出给 strategy 消费。",
    ),
    "app/strategy": (
        "L6 策略层：策略基类、规则 DSL、内置策略、用户策略发现与存储。",
        "策略只消费 feature/alpha 的输出，产出 Signal；"
        "**不得直接 import 任何数据源**（那是 datasource 层的事）。",
    ),
    "app/strategy/rule": (
        "L6 规则：把因子值判定为布尔条件，可嵌套组合（All/Any）。",
        "规则是纯只读的判定，**不允许在规则内下单**。",
    ),
    "app/risk": (
        "L6 风控闸门链：下单前后逐级拦截。",
        "依赖 domain 与 strategy.state（DecisionState），不依赖执行细节。",
    ),
    "app/portfolio": (
        "L6 账户与持仓：仓位计算、资金分配、成交记录。",
        "纯计算，不含 IO；成交由 execution 回填。",
    ),
    "app/execution": (
        "L7 交易执行：模拟撮合网关、HTTP 网关、实盘券商接入。",
        "依赖 risk 与 portfolio；engine 通过组件装配它。",
    ),
    "app/engine": (
        "L8 运行时：事件驱动引擎、组件、任务容器、采集调度、跨进程编排。",
        "依赖最广的一层，但也因此**不得被 domain/datasource/storage/feature 反向依赖**。",
    ),
    "app/engine/component": (
        "L8 引擎组件：采集/行情/新闻/策略/数据校验/实盘网关/交易落库等。",
        "组件通过统一生命周期（start/stop/快照/进度）挂载到引擎。",
    ),
    "app/engine/tasking": (
        "L8 任务容器：单进程内管理 N 个标的的运行任务。",
        "与 engine 是容器/运行时关系，不是并行实现。",
    ),
    "app/engine/collect": (
        "L8 采集调度：决定采什么、多久采一次、当前时点是否可采。",
        "纯声明式，被 engine/component/collector 消费。",
    ),
    "app/engine/bus": (
        "L8 跨进程文件总线（IPC）。",
        "用于进程间解耦通信，进程隔离是刻意设计。",
    ),
    "app/engine/hub": (
        "L8 多进程编排：拉起并监管 N 个 engine 进程 + 面板。",
        "复用 engine.daemon，不重复实现进程管理。",
    ),
    "app/news": (
        "舆情：新闻采集 → 去重 → 分析 → 事件。",
        "只依赖 domain 与 common。",
    ),
    "app/research": (
        "回测研究：backtrader 跑批、结果分析、报表。",
        "**策略本体来自 app/strategy**（回测与实盘共用同一份策略代码），"
        "本包只提供回测驱动与评估。",
    ),
    "app/monitoring": (
        "监控：运行时快照、HTTP 服务与面板静态资源。",
        "面板前端在 static/dashboard.html，Python 侧只负责路由与数据接口。",
    ),
    "app/monitoring/api": (
        "监控 HTTP 接口：按业务域划分的资源实现。",
        "只读/控制分开，避免在同一个类里堆 40+ 方法。",
    ),
    "app/tests": (
        "测试套件。",
        "约定：凡触碰 熔断/健康分/权重/域名健康 这类全局状态的用例，"
        "必须在 setUp 里一并 reset，否则用例之间会互相污染。",
    ),
}


def _render(pkg: str, desc: str, dep: str) -> str:
    return (
        HDR
        + '"""{}'.format(desc)
        + "\n\n"
        + "依赖方向：" + dep + "\n"
        + '"""\n\n'
        + "__all__: list[str] = []\n"
    )


def main() -> int:
    apply = "--apply" in sys.argv
    created = 0
    for pkg in sorted(PACKAGES):
        if pkg == "app":
            continue
        path = os.path.join(ROOT, pkg.replace("/", os.sep), "__init__.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        desc, dep = PACKAGES[pkg]
        content = _render(pkg, desc, dep)
        if os.path.exists(path):
            print(f"  [skip] 已存在 {pkg}/__init__.py")
            continue
        created += 1
        if apply:
            open(path, "w", encoding="utf-8", newline="\n").write(content)
        else:
            print(f"  [new ] {pkg}/__init__.py")
    # app/tests 不需要 __init__
    print(f"\n新建 {created} 个 __init__.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
