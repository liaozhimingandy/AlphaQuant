# AlphaQuant 目录结构（在哪里改东西）

> 这份文档回答一个问题：**改某个东西，我应该去哪个文件。**
>
> 只想快速定位 → 直接看文末「六、按需求查位置」。

配套文档：

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 运行时数据流（一根 K 线怎么变成一笔成交）
- [ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) —— 引擎的四类组件与能力注入
- [RUNTIME.md](RUNTIME.md) —— 启动流程、线程模型
- **[EVENT_TASK.md](EVENT_TASK.md) —— 事件 / 任务 / 执行池的完整契约（改 runtime 前必读）**
- [EXTENDING.md](EXTENDING.md) —— 扩展点与已知坑

---

## 一、五层边界

整个系统由五个边界组成。边界一旦稳定，从单实例到多账户、再到多 Engine，
基本都是部署层面的扩展，而不是推倒重做。

```text
        ┌──────────────────────────────────────────────┐
        │  Control Plane    app/control, app/cli        │
        │  谁在指挥引擎：任务增删、组件启停、采集遥控   │
        └───────────────────┬──────────────────────────┘
                            │ 驱动
        ┌───────────────────▼──────────────────────────┐
        │  Engine           app/engine                  │
        │  纯容器：生命周期 / 装配 / 依赖 / Capability  │
        └───────────────────┬──────────────────────────┘
                            │ 装配
        ┌───────────────────▼──────────────────────────┐
        │  Runtime          app/runtime                 │
        │  怎么跑：EventBus → Scheduler → Worker Pools   │
        │  只管"什么时候、用哪个线程、跑什么活"           │
        │  业务实现留在 Modules，这里只有调用它的处理器   │
        └───────────────────┬──────────────────────────┘
                            │ 执行
        ┌───────────────────▼──────────────────────────┐
        │  Modules          app/modules                 │
        │  跑什么：market → collection → universe        │
        │          → factor → strategy → risk            │
        │          → portfolio → trading                 │
        └──────┬────────────────────────────┬──────────┘
               │ 读写                        │ 调用外部
    ┌──────────▼───────────┐   ┌────────────▼────────────┐
    │  Data    app/data     │   │  Providers app/providers │
    │  仓储 / SQLite / 查询 │   │  行情源 / 券商网关        │
    └──────────────────────┘   └─────────────────────────┘
```

**贯穿所有层的共享内核**：`app/domain/`（Bar / Order / Position / Signal 等领域模型）、
`app/common/`（配置、日志、JSON）、`app/account/`（资金仓位账本与 sizing）。

---

## 二、依赖规则（新增代码务必遵守）

```text
app/cli ──► app/control ──► app/engine ──► app/modules ──► app/providers
                                │              │                │
                                ▼              ▼                ▼
                          app/runtime    app/data ◄───── app/providers
                                │              │
                                └──────► app/domain ◄── app/common
```

| 包 | 允许 import | **禁止** import |
|---|---|---|
| `app/domain/` | `app/common` 及以下 | 任何上层（data / providers / engine） |
| `app/providers/` | `app/common`、`app/domain`、`app/data.schema` | `app/modules/`、`app/engine/` |
| `app/data/` | `app/common`、`app/domain` | `app/providers/`、`app/engine/` |
| `app/runtime/` | 上面全部 + `app/engine.{component,capability,context}` | `app/engine.engine`（引擎本体）；`app/modules/` ⚠️见下方说明 |
| `app/modules/` | 上面全部 + `app/engine.{component,capability,context}` | `app/engine.engine`（引擎本体） |
| `app/engine/` | 全部（它是装配方） | —— |
| `app/control/` | 全部 | —— |

> **为什么禁止 module import `app.engine.engine`（引擎本体）？**
> 组件的依赖应当在构造时由 `ServiceRegistry` 注入。反向 `get_component()`
> 会让"谁依赖谁"从显式声明退化成运行期字符串查找，编译期查不出来，
> 改名时静默失效。现有代码里还有 6 处历史遗留（见文末待清理项）。

> **⚠️ `app/runtime/` import `app/modules/` —— 现有 3 个文件越界，性质各不相同**
>
> 执行层最终总得有人去调业务，问题是这个"有人"放哪儿。用 AST 统计现状：
>
> | 文件 | 模块级 import | 函数内 import | 判定 |
> |---|---|---|---|
> | `runtime/workers/handlers.py` | 0 | 3 | ✅ 本轮新增，**刻意**延迟导入 |
> | `runtime/tasking/task.py`（`QuantTask`） | 4 | 2 | ❌ 历史遗留 |
> | `runtime/tasking/spec.py` | 2 | 0 | ❌ 历史遗留 |
>
> **`handlers.py` 为什么刻意全部延迟导入**：它是执行层与业务层之间唯一的
> 适配器，写 `from app.modules.x import y` 在模块顶部会立刻造成导入环——
> 执行层要调业务，业务要发事件回执行层，Python 在 import 期就炸。
> 放进函数体则只在真正调用那一刻才解析，环自然断开。
> 它是**纯转发**（取服务 → 调一个方法 → 发后续事件），不写业务规则；
> 业务规则改动永远落在 `app/modules/`，不会出现在执行层。
> 所以**新增处理器只改这一个文件**就够，不要往别处散。
>
> **`tasking/` 那两个是不同的问题**：`QuantTask` 本身就是
> 「标的 + 策略 + 风控 + 账本」的**业务运行实例**，天然要直接 import
> 策略与风控基类，放在 `runtime/` 是历史归位不彻底。
> 属于待清理项（见文末 §七），**新增代码不要照抄这个模式**。

---

## 三、职责表

| 层 | 包 | 核心职责 | 关键入口 |
|---|---|---|---|
| **控制面** | `app/control/` | 任务增删启停、组件启停、采集遥控 | `control_plane.py` → `ControlCenter` |
| | `app/cli/` | 命令行入口（11 个命令组） | |
| **容器** | `app/engine/` | 生命周期、装配、依赖解析、**启动预检** | `engine.py` `builder.py` `registry.py` `capability.py` `preflight.py` |
| | `app/engine/hub/` | 多进程协作：一个 hub 拉起 N 个引擎进程 | `supervisor.py` `worker.py` |
| **运行时** | `app/runtime/scheduler.py` | 调度中枢：订阅事件 → 建任务 → 投队列 → 领活 → 启动恢复 | `SchedulerComponent` |
| | `app/runtime/maintenance.py` | 维护任务的时间源（六种周期 → `maintenance.due`） | `MaintenancePlanner` |
| | `app/runtime/cron.py` | cron 表达式解析与"下一次执行时刻"（不引第三方包） | `CronExpr` |
| | `app/runtime/events/` | **事件契约层**：类型常量、`EngineEvent` 信封、总线、**事件流水账（Outbox）** | `types.py` `schema.py` `bus.py` `store.py` |
| | `app/runtime/scheduling/` | **调度层**：规则表（21 条）+ 能力路由 | `rules.py` `router.py` `defaults.py` |
| | `app/runtime/tasking/` | **任务层**：`EngineTask` 状态机、队列、死信、**流水账（崩溃恢复）** | `models.py` `store.py` `journal.py` |
| | `app/runtime/workers/` | **执行层**：分池线程池（8 个）+ 处理器注册表 | `pools.py` `registry.py` `handlers.py` |
| | `app/runtime/executor.py` | 旧作业执行器（`WorkerComponent`） | |
| | `app/runtime/bus/` | 跨进程 append-only 事件总线 | `filebus.py` → `FileBus` |
| **业务** | `app/modules/market/` | 实时行情 → 标准 Bar | `service.py` → `MarketService` |
| | `app/modules/collection/` | 采集 / 清洗 / 落库 / 完整性核验 | `service.py` `spec.py` `integrity.py` |
| | `app/modules/universe/` | 全市场 → 股票池 → 候选股 + 打分 | `pool/pool.py` `score.py` |
| | `app/modules/factor/` | 因子 / 技术指标 / 因子挖掘 | `features.py` `mining/` |
| | `app/modules/strategy/` | 策略定义、注册、实例 | `registry.py` `builtin.py` `component.py` |
| | `app/modules/risk/` | 风控闸门链 | `registry.py` `builtin.py` `service.py` |
| | `app/modules/portfolio/` | 跨任务额度预留与组合视图 | `service.py` → `PortfolioService` |
| | `app/modules/trading/` | 订单生命周期 → 网关 → 成交 | `service.py` `live_broker.py` |
| | `app/modules/news/` | 新闻抓取、分析、发事件 | `center.py` `analyzer.py` `service.py` |
| | `app/modules/backtest/` | 回测引擎与报告 | `runner.py` `report.py` |
| **数据** | `app/data/` | SQLite 引擎、ORM Schema、仓储 | `engine.py` `schema.py` `repo/` |
| **外部** | `app/providers/market/` | 行情源：AkShare / BaoStock / 东财 / 雪球… | `adapters/` `routing/` `service/facade.py` |
| | `app/providers/broker/` | 券商网关与端点配置 | `gateway.py` `http_gateway.py` `endpoints.py` |
| **账户** | `app/account/` | 资金 / 持仓账本 / 仓位 sizing | `portfolio.py` `sizer.py` |
| **共享** | `app/domain/` | 领域模型（不依赖任何上层） | `types.py` `calendar.py` `series.py` |
| | `app/common/` | 配置、日志、JSON 工具 | `config.py` `logging.py` |
| **监控** | `app/monitoring/` | 面板 / API / 快照 | `server.py` `api/` |

---

## 四、2026-10 目录重构：旧 → 新对照

这次重构把 `app/engine` 从「42 个文件 13441 行什么都有」
收窄成「16 个文件 4532 行的纯容器」，并让业务包按五层边界归位。

<!-- check_doc_paths:allow-stale-paths -->
| 旧路径 | 新路径 | 理由 |
|---|---|---|
| `app/engine/base.py` | `app/engine/engine.py` | `base` 表达不出它是引擎本体 |
| `app/engine/settings.py` | `app/engine/context.py` | 内容是 `EngineContext`，不是设置项 |
| `app/engine/component/base.py` | `app/engine/component.py` | 组件契约属于容器层 |
| `app/engine/component/kinds.py` | `app/engine/capability.py` | 装的是 `Cap` 能力常量 |
| `app/engine/component/registry.py` | `app/engine/registry.py` | 依赖解析是容器职责 |
| `app/engine/component/scheduler.py` | `app/runtime/scheduler.py` | 调度不属于业务 |
| `app/engine/component/worker.py` | `app/runtime/executor.py` | 作业执行器，归 Runtime |
| `app/engine/component/strategy.py` | `app/modules/strategy/component.py` | 策略是业务模块 |
| `app/engine/component/service/market.py` | `app/modules/market/service.py` | 业务服务进 Modules |
| `app/engine/component/service/collection.py` | `app/modules/collection/service.py` | 同上 |
| `app/engine/component/service/trading.py` | `app/modules/trading/service.py` | 同上 |
| `app/engine/component/service/risk.py` | `app/modules/risk/service.py` | 同上 |
| `app/engine/component/service/portfolio.py` | `app/modules/portfolio/service.py` | 同上 |
| `app/engine/component/service/news.py` | `app/modules/news/service.py` | 同上 |
| `app/engine/event.py` | `app/runtime/event_bus.py` | 事件总线归 Runtime |
| `app/engine/task.py` | `app/runtime/task.py` | 作业单元归 Runtime |
| `app/engine/tasking/` | `app/runtime/tasking/` | 任务容器归 Runtime |
| `app/engine/bus/` | `app/runtime/bus/` | 跨进程总线归 Runtime |
| `app/engine/collect/` | `app/modules/collection/` | 采集规格是业务配置 |
| `app/engine/integrity.py` | `app/modules/collection/integrity.py` | 完整性是采集的一环 |
| `app/engine/control.py` | `app/control/control_plane.py` | 控制面独立于引擎 |
| `app/datasource/` | `app/providers/market/` | 外部行情数据源 |
| `app/execution/{gateway,http_gateway,broker,endpoints}.py` | `app/providers/broker/` | 券商网关是外部系统 |
| `app/execution/live_broker.py` | `app/modules/trading/live_broker.py` | 订单生命周期是业务 |
| `app/storage/` | `app/data/` | 持久化归位 |
| `app/portfolio/` | `app/account/` | 资金 / 仓位账本 |
| `app/alpha/` | `app/modules/universe/` | 股票池 + 打分 |
| `app/feature/` | `app/modules/factor/` | 因子层 |
| `app/strategy/` | `app/modules/strategy/` | 策略层 |
| `app/research/` | `app/modules/backtest/` | 回测 |
| `app/risk/` | `app/modules/risk/` | 风控规则与闸门链 |
| `app/news/` | `app/modules/news/` | 新闻与舆情 |

---

## 五、当前目录树

```text
AlphaQuant/
├── app/
│   ├── domain/        领域模型（共享内核，不依赖上层）
│   ├── common/        配置 / 日志 / JSON 工具
│   │
│   ├── engine/        ── 容器层 ──
│   │   ├── engine.py           BaseQuantEngine
│   │   ├── builder.py          build_engine（唯一装配入口）
│   │   ├── context.py          EngineContext / EngineStatus
│   │   ├── component.py        IBaseComponent（组件契约）
│   │   ├── capability.py       Cap / ComponentKind / 四种角色基类
│   │   ├── registry.py         ServiceRegistry（依赖解析与注入）
│   │   ├── service.py          Twisted Service 包装
│   │   ├── service_runner.py   常驻进程入口
│   │   ├── daemon.py           PID / 进程管控
│   │   └── hub/                多进程协作（future: multi-Engine）
│   │
│   ├── runtime/       ── 运行时层（不含业务）──
│   │   │                      改这里之前先读 EVENT_TASK.md
│   │   ├── scheduler.py        调度器组件（订阅事件 → 建任务 → 投队列 → 启动恢复）
│   │   ├── maintenance.py      维护计划（六种周期 → maintenance.due 事件）
│   │   ├── cron.py             cron 表达式解析（5 段，零依赖）
│   │   ├── executor.py         WorkerComponent（旧作业执行器）
│   │   ├── event_bus.py        薄转发兼容层（真身在 events/）
│   │   ├── task.py             ITask / TaskStatus
│   │   ├── events/             ★事件契约层
│   │   │   ├── types.py            EventType / TaskType / Queue / Pool（唯一真源）
│   │   │   ├── schema.py           EngineEvent 信封（create / child / to_dict）
│   │   │   ├── bus.py              EventBus（去重 / 通配 / 异常隔离 / 重放）
│   │   │   └── store.py            EventStore 事件流水账（落盘 / 链路查询 / 重放留痕）
│   │   ├── scheduling/         ★调度层
│   │   │   ├── rules.py            Rule / RuleRegistry（事件 → 任务的规则）
│   │   │   ├── router.py           CapabilityRouter（任务 → 队列 → 池）
│   │   │   └── defaults.py         21 条默认规则（全系统自动流转的唯一清单）
│   │   ├── tasking/            ★任务层 + 作业层
│   │   │   ├── models.py           EngineTask / TaskState / Priority
│   │   │   ├── store.py            按队列的优先级堆 + 幂等键 + 死信 + 人工干预
│   │   │   ├── journal.py          流水账：SQLite 落盘（崩溃恢复 / 错误队列）
│   │   │   ├── task.py             QuantTask（标的+策略+风控+账本 运行实例）
│   │   │   ├── runtime.py          TaskRuntime
│   │   │   └── spec.py             TaskSpec
│   │   ├── workers/            ★执行层
│   │   │   ├── pools.py            WorkerPool / PoolManager（分池线程池）
│   │   │   ├── registry.py         HandlerRegistry（任务类型 → 处理器）
│   │   │   └── handlers.py         15 个默认处理器（适配器，业务导入在函数体内）
│   │   └── bus/                FileBus 跨进程事件总线
│   │
│   ├── modules/       ── 业务模块层 ──
│   │   ├── market/       实时行情 → Bar
│   │   ├── collection/   采集 / 清洗 / 落库 / 完整性
│   │   ├── universe/     股票池 + 打分排序
│   │   ├── factor/       因子 / 指标 / 挖掘
│   │   ├── strategy/     策略定义与实例
│   │   ├── risk/         风控闸门链
│   │   ├── portfolio/    额度预留与组合视图
│   │   ├── trading/      订单 → 网关 → 成交
│   │   ├── news/         新闻抓取与分析
│   │   └── backtest/     回测引擎
│   │
│   ├── account/      资金 / 持仓账本 / sizing
│   ├── data/         SQLite 引擎 / Schema / 仓储
│   ├── providers/
│   │   ├── market/   行情源适配器（AkShare / BaoStock / 东财…）
│   │   └── broker/   券商网关与端点
│   │
│   ├── control/      ControlCenter（控制面）
│   ├── monitoring/   面板 / API / 快照
│   ├── cli/          命令行入口
│   └── tests/        测试
│
├── config/           tasks.json / services.json / providers.json …
├── docs/             本文档与各专题文档
├── scripts/          运维与迁移脚本
└── strategies/       用户自定义策略
```

---

## 六、按需求查位置

| 我要改… | 去这里 |
|---|---|
| 加一个数据源 | `app/providers/market/adapters/` + `config/providers.json` |
| 改取数限流 / 熔断策略 | `app/providers/market/service/facade.py` |
| 代理抓不到数据 | `app/providers/market/transport/net.py`（`DATA_PROXY` 配置） |
| 加一张表 / 改字段 | `app/data/schema.py` + `app/data/repo/` |
| 改落库逻辑 | `app/data/repo/kline_repo.py` 等 |
| 加一个因子 | `app/modules/factor/builtin.py` 或 `config/factors.json` |
| 加一个策略 | `app/modules/strategy/builtin.py` 或 `strategies/` |
| 改风控规则 | `app/modules/risk/builtin.py` |
| 改仓位计算 | `app/account/sizer.py` |
| 改持仓账本逻辑 | `app/account/portfolio.py` |
| 订单超时 / 卡单 | `app/modules/trading/live_broker.py`（`sweep_stale_orders`） |
| 改下单流程 | `app/modules/trading/service.py` |
| 回测成本模型 | `app/modules/backtest/commission.py` |
| 采集时间窗 | `app/modules/collection/schedule.py` |
| 采哪些数据、多久一次 | `app/modules/collection/spec.py` + `config/collector.json` |
| 数据缺口核验 | `app/modules/collection/integrity.py` |
| 股票池怎么筛 | `app/modules/universe/pool/pool.py` + `score.py` |
| 加一个定时任务 | `app/engine/builder.py` → `SchedulerComponent.start_timer` |
| **加一种事件** | `app/runtime/events/types.py` 加常量 → 需要自动流转再在 `scheduling/defaults.py` 加规则 |
| **加一种任务类型** | `types.py` 加 `TaskType` → `router.py` 声明哪个池能领 → `workers/handlers.py` 注册处理器 |
| **改调度规则（什么事件触发什么活）** | `app/runtime/scheduling/defaults.py`（21 条规则的唯一清单） |
| **改线程池容量 / 加一个池** | `types.py` 的 `DEFAULT_CAPACITY` + `DEFAULT_CAPABILITIES` |
| **某类活跑太慢 / 挤住别人** | `scheduling/router.py` 改队列→池映射，或 `set_task_pool` 定向 |
| **任务卡住 / 进死信了** | `ctl runtime status` / `ctl runtime dead`，或 `GET /api/runtime` |
| 事件与任务的整体设计 | [EVENT_TASK.md](EVENT_TASK.md)（含规则全表与可靠性现状矩阵） |
| 组件启停顺序 | `app/engine/registry.py`（`ServiceRegistry`） |
| 组件能提供 / 需要什么能力 | `app/engine/capability.py`（`Cap` 常量） |
| 面板上加一个视图 | `app/monitoring/api/` + `app/monitoring/static/` |
| 加一条 CLI 命令 | `app/cli/` 对应命令组 |
| 任务怎么从配置到实例 | `app/engine/builder.py` + `app/runtime/tasking/spec.py` |
| 多进程 / 多引擎协作 | `app/engine/hub/` |
| 服务健康判定（进程 / 引擎 / 组件三层） | `app/engine/hub/supervisor.py`（`degraded` 档）+ `hub/worker.py` 心跳，见 [RUNTIME.md §9](RUNTIME.md) |
| 采集分工（哪个服务采哪些标的、哪些类别） | `config/services.json` 的 `collector` 段 → `app/engine/hub/worker.py` → `builder.py` |
| 采指定标的的基本面（临时一次） | `app/control/control_plane.py::cross_collect` → `app/modules/collection/service.py::collect_categories` |
| 新闻源配置 / 资讯服务 | `app/modules/news/service.py` + `config/services.json` 的 `news_config` |
| 领域模型（Bar / Order…） | `app/domain/types.py` |

---

## 七、已知待清理项

重构只做了**位置归位**，下面这些是扫描出来的结构债，尚未处理：

1. **`app/domain/symbol.py` 里塞了完整取数层**（HTTP、线程锁、单例工厂）。
   它本该只有领域概念，实际却是第二套数据源实现，被 4 个非 domain 包直接调用
   （`app/providers/market/compat/kline_sources.py`、`app/data/service.py` 等）。
   应当把其中 `DataSourceFactory` 那部分迁到 `app/providers/market/`，
   让 `domain` 回归纯净共享内核。
2. **5 组模块级循环依赖**：
   `domain.calendar ↔ providers.market.service.facade`、
   `modules.universe.pool ↔ modules.universe.pool.pool`、
   `modules.risk ↔ modules.risk.registry`、
   `app.engine ↔ app.engine.engine`、
   `monitoring.resource ↔ monitoring.server`。
3. **6 处组件仍持有引擎反向引用**（`engine.get_component("strategy")` 等硬编码字符串），
   分布在 `control_plane.py`、`modules/trading/service.py`、`hub/worker.py` 等。
4. **`app/runtime/task.py`（325 行）几乎无人使用**，与 `runtime/tasking/task.py::QuantTask`
   是两套并行的 Task 语义，需要二选一。
   **同一问题的另一面**：`tasking/task.py` 与 `tasking/spec.py` 在模块级 import 了
   6 处 `app.modules.*`（策略基类 / 风控链 / 因子上下文），让 runtime 层不再纯净——
   它们是业务装配，只是放在了运行时目录。本轮新建的 `events/` `scheduling/`
   `tasking/{models,store}.py` `workers/{pools,registry}.py` 全部零业务导入，
   后续把 `QuantTask` 迁到 `app/modules/` 即可补齐这条边界。
5. **9 个假配置项**被文档描述但无人读取（尤其 `LIVE_MAX_DAILY_TRADES`，
   让人误以为有日下单闸门），见 [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md)。
6. **`CashReserveRisk` 读的 `planned_size` 从未被写入**，恒等于 100 股。

---

## 八、下一步的扩展位（多 Engine）

目标架构里 Engine 代表**一个完整运行环境**，而不是一个账户。当前 `app/engine/hub/`
已经具备"一个 hub 拉起 N 个独立引擎进程"的能力，所以向下列形态演进不需要改目录：

```text
阶段1  单 Node / 单 Engine / 多 Account / 多 Strategy        ← 当前
阶段2  + Universe 自动选股 + Factor + Backtest 完整闭环
阶段3  多 Engine（hub/supervisor.py 已是进程级隔离）
阶段4  Control Plane + Message Bus + 专业化的 Research/Execution 节点
```

阶段 3→4 只需要把 `app/engine/hub/` 的进程间通信从本地 FileBus
换成消息队列，业务层（`modules/`、`runtime/`）无需改动。

要让账户成为真正的隔离边界，还需要引入 `AccountRuntime`
（`account_id` + `portfolio` + `risk_state` + `broker` 四件套），
把现在按 `task_id` 隔离的账本升级为按 `account_id` 隔离。
