# 事件 · 任务 · 执行器

> AlphaQuant 的调度架构：**引擎发事件 → 调度器建任务 → 执行池干活**。
> 本文是这套机制的完整说明与扩展手册。

---

## 一、为什么需要三层

重构前，服务之间是**直接调用**的：

```
采集服务 → 因子模块 → 策略 → 风控 → 下单
```

一条调用链串到底，看起来很直接，但它有三个致命缺陷：

| 缺陷 | 具体表现 |
|---|---|
| **失败无处可去** | 因子算挂了，整条链断在中间。没人知道卡在第几步，也没有任何一条记录说明"本该发生但没发生" |
| **重试无从谈起** | 链上每一环都得自己写重试，写法各不相同，退避策略更是各写各的 |
| **无法插队** | 回测和撤单抢同一批线程，撤单排在三年回测后面 |

事件 + 任务把这条链拆成**可追踪、可重试、可控制**的工作流：

```
Engine ──发布──> EventBus ──投递──> Scheduler
                                        │ 订阅 → 匹配规则 → 建 EngineTask
                                        ▼
                                  CapabilityRouter（选队列/执行池）
                                        ▼
        market / research / strategy / trading / backtest / maintenance 池
                                        ▼
                        HandlerRegistry → 真实业务服务 → 发布下一个事件
```

每一步都是一个**有 ID、有状态、有幂等键**的任务。
卡在哪一步、重试了几次、为什么失败，全部可查。

### 事件 ≠ 任务 ≠ 命令

这三个概念混在一起是这类架构最常见的翻车方式：

| | 事件（EngineEvent） | 任务（EngineTask） |
|---|---|---|
| 语义 | **已经发生的事实** | **需要完成的工作** |
| 可执行 | 否 | 是 |
| 被忽略 | 正常（没人订阅就没事发生） | 不可接受（必须有终态） |
| 重复投递 | 没关系（去重即可） | 会出事（重复下单、重复记账） |
| 处理耗时 | 必须极短（同步广播） | 可以很长（异步执行） |

所以事件总线与任务队列**逻辑上必须分开**——即使它们底层都用同一个存储。

---

## 二、目录与职责

```
app/runtime/
├── events/
│   ├── types.py     事件/任务/队列/执行池的**名字常量**（六类事件、19 种任务、8 个池）
│   ├── schema.py    EngineEvent —— 总线上流动的唯一消息形态
│   └── bus.py       EventBus —— 同步广播、去重、通配、异常隔离
│
├── tasking/
│   ├── models.py    EngineTask + TaskState 状态机 + 优先级 + 退避
│   └── store.py     TaskStore —— 按队列存、按优先级出队、幂等、死信
│
├── scheduling/
│   ├── rules.py     Rule / RuleRegistry —— 事件 → 任务的规则表
│   ├── router.py    CapabilityRouter —— 任务 → 队列 → 执行池
│   └── defaults.py  内置规则（系统里全部自动流转的唯一清单）
│
├── workers/
│   ├── pools.py     WorkerPool / PoolManager —— 各自独立的线程池
│   ├── registry.py  HandlerRegistry —— 任务类型 → 干活的函数
│   └── handlers.py  默认处理器（接到真实业务服务上）
│
├── scheduler.py     SchedulerComponent：订阅事件 → 建任务 → 分发给执行池
├── executor.py      旧式工作单元（有槽位限制的一次性作业执行器）
└── tasking/task.py  QuantTask：量化任务（"标的+策略+风控+账本"运行实例）
```

> **注意区分两个"任务"**：
> `EngineTask` 是引擎任务（调度器产物，可重试），
> `QuantTask` 是量化任务（策略运行实例）。
> 事件命名空间也严格分开：前者用 `task.*`，后者用 `job.*`。

---

## 三、事件契约

### 3.1 六大分类

不为每个函数、每根 K 线定义事件，只围绕**生命周期、数据变化、计算流程、交易决策、任务执行、系统异常**来设计。
判断标准只有一条：**有没有人会因为它而改变行为**。

| 分类 | 事件 |
|---|---|
| **生命周期** | `engine.started` `engine.ready` `engine.stopping` `engine.stopped` `engine.recovery.requested` |
| **数据与质量** | `data.collection.requested` `market.bar.received` `market.tick.received` `market.kline.updated` `market.kline.gap_detected` `market.data.validated` `news.article.collected` `news.sentiment.updated` `fundamental.data.updated` |
| **因子与策略** | `factor.calculation.requested` `factor.snapshot.updated` `strategy.run.requested` `strategy.run.completed` `strategy.signal.generated` `universe.updated` |
| **交易与风控** | `risk.check.requested` `risk.check.approved` `risk.check.rejected` `trade.order.requested` `trade.order.created` `trade.order.submitted` `trade.order.filled` `trade.order.failed` `trade.order.cancelled` `trade.position.updated` `trade.account.updated` `risk.limit.breached` |
| **任务与执行** | `task.created` `task.queued` `task.started` `task.completed` `task.failed` `task.retry.scheduled` `task.dead_lettered` `executor.registered` `executor.heartbeat.missed` `executor.capacity.changed` |
| **异常与运维** | `system.dependency.unavailable` `system.engine.error` `system.database.degraded` `system.event.publish.failed` `system.market.session.changed` `system.data.staleness.detected` `system.maintenance.due` |

命名约定 `域.对象.动作`。域前缀同时是通配订阅前缀，`market.*` 一次订阅整个行情域。

### 3.2 EngineEvent 结构

| 字段 | 用途 |
|---|---|
| `event_id` | 事件唯一 ID，**消费去重**的依据 |
| `event_type` | 事件类型，取 `EventType` 常量 |
| `occurred_at` | 发生时刻（项目统一用本地朴素时间，混用 aware/naive 会在比较时炸） |
| `source` | 谁发出的（组件名 / 服务名 / `engine`） |
| `correlation_id` | 关联一条完整业务链：K线 → 因子 → 策略 → 下单 共享同一个 ID，事后能一路追回去 |
| `causation_id` | **直接导致**本事件的那个事件 ID |
| `schema_version` | 结构版本，未来改字段靠它兼容读取 |
| `data` | 业务载荷 |

```python
from app.runtime.events import EventBus, EventType
from app.runtime.events.schema import EngineEvent

bus = EventBus()
bus.subscribe("market.*", lambda **kw: print(kw["event"].event_type))

evt = EngineEvent.create(
    EventType.MARKET_KLINE_UPDATED,
    {"symbols": ["000001"], "day": "2026-10-12", "rows": 240},
    source="collection_service",
)
bus.publish_event(evt)

# 派生子事件：因果关系被显式固化
nxt = evt.child(EventType.FACTOR_SNAPSHOT_UPDATED, {"symbols": ["000001"]})
```

### 3.3 总线的四道保险

| 机制 | 防的是什么 |
|---|---|
| **事件 ID 去重** | 消息重投导致下游把同一件事做两遍 |
| **通配订阅** | 想"监听整个域"要写十几个 subscribe，漏一个就静默 |
| **异常隔离 + 计数** | 一个订阅者抛异常，后面的订阅者收不到事件 |
| **新旧名同时投递** | 事件名迁移期间，发布方与订阅方版本不一致会静默断链 |

> 旧事件名（`bar_received`）与新名（`market.bar.received`）由 `LEGACY_EVENT_ALIASES` 映射，
> 按旧名发布时总线**同时**投递给新旧两批订阅者，所以迁移可以逐个文件进行。
> 这个映射**必须是单射** —— 曾经把 `control_command` 与 `task_added` 都映射到
> `task.created`，结果调度器一发任务事件，控制面板的审计流就收到一条 `action=""`
> 的幽灵指令。现在 `types.py` 在导入时强制检查这条不变式。

---

## 四、任务契约

```python
EngineTask(
    task_type="factor.calculate",   # 决定路由到哪个池、哪个处理器
    params={"symbols": [...], "day": "2026-10-12"},
    queue="research",               # 不填则按 TASK_QUEUE 默认表推导
    priority=5,                     # 1 最高（下单），9 最低（清理）
    dedup_key="factor.calculate:000001:2026-10-12",
    max_retries=2,                  # "最多额外执行几次"，0 = 不重试
    timeout_seconds=600,
    source_event_id=..., correlation_id=..., causation_id=...,
)
```

### 状态机

```
PENDING → QUEUED → RUNNING → SUCCESS
                          └→ FAILED → (RETRYING → QUEUED)
                                   └→ DEAD_LETTER
PENDING → CANCELLED
```

独立的 `QUEUED` 档不是多余：`PENDING` 是"已创建、还没进队列"，
`QUEUED` 是"已在队列里等着被领"。少了它，"任务建出来了但没人领"和
"任务还没建"在监控上长得一样，而前者通常是路由配错了。

### 三条硬规则

1. **`trade.execute` 永远不自动重试**（`NON_RETRIABLE_TASK_TYPES` 强制归零）。
   网络超时后我们只知道"没收到回执"，不知道"券商收没收到"。
   盲目重试是最经典的重复下单来源。
2. **幂等键在任务终结后才释放**。于是"同一批 K 线第二天再来"仍能触发新任务，
   而"同一批 K 线重复投递"只会建一次任务。
3. **重试带指数退避 + 抖动**。没有抖动时，一批同时失败的任务会同时重试，
   形成周期性尖峰，把刚恢复的数据源再次打垮。

---

## 五、执行池

每个池 = **一个独立线程池 + 一个并发上限 + 一份能力声明**。

| 池 | 槽位 | 负责任务 | 为什么是这个数 |
|---|---|---|---|
| `system` | 2 | 恢复、快照 | 偶发但希望并行 |
| `market` | 3 | K 线采集、补采、体检 | 要并发，但数据源有限流，再多就是自杀 |
| `research` | 2 | 新闻、舆情、因子 | 吃 CPU，给多会把 reactor 拖慢 |
| `strategy` | 1 | 策略运行、选股 | **必须串行**：两个策略同时改账本，净值曲线不再确定 |
| `trading` | 1 | 风控、下单 | **必须串行**：并发下单 + 网络超时 = 重复报单 |
| `backtest` | 1 | 回测、参数实验 | 长任务本来就该排队 |
| `maintenance` | 1 | 日志清理、归档 | 慢活，不该抢任何人的资源 |
| `default` | 2 | 无业务归属的通用作业 | 兜底，不占业务池槽位 |

### 为什么不用一个公共线程池

用公共池 + 总并发上限，看起来更省资源，实际会出这种事：

> 运维点了一次"三年全市场参数寻优"，回测把公共池的 10 个线程全占满并跑 20 分钟；
> 期间行情采集排在队尾，K 线迟迟不来；策略因此不动，本该触发的止损一直没触发。

分池之后，回测再忙也只能用完 **backtest 池自己的 1 个槽位**，行情池、交易池完全不受影响。
这是"防止回测挤占实盘资源"的物理保证，**不是靠优先级调度能替代的**。

### 关于超时

Python 没有安全的线程强杀。这里的超时是**记账层面**的：
超时后释放槽位、判任务失败（进而重试或进死信），让系统继续运转；
那个孤儿线程真跑完时结果会被丢弃。
这比"一个任务卡死，整个池永久少一个槽位"好得多。

---

## 六、内置规则表

`app/runtime/scheduling/defaults.py` 里的这张表，是系统里**全部自动流转**的唯一清单。
想回答"K 线到了之后系统会自动做什么"，读这张表就够了。

| 规则 | 触发事件 | 生成任务 | 队列 | 优先级 | 重试 |
|---|---|---|---|---|---|
| `ready_recover` | `engine.ready` | `system.recover` | system | 1 | 1 |
| `ready_collect_kline` | `engine.ready` | `market.kline.collect` | market | 3 | 2 |
| `ready_integrity_check` | `engine.ready` | `data.integrity.check` | market | 7 | 1 |
| `ready_maintenance_validate` | `engine.ready` | `maintenance.validate` | maintenance | 9 | 0 |
| `kline_gap_backfill` | `market.kline.gap_detected` | `market.kline.backfill` | market | 3 | 2 |
| `kline_updated_factor` | `market.kline.updated` | `factor.calculate` | research | 5 | 2 |
| `news_collected_sentiment` | `news.article.collected` | `sentiment.analyze` | research | 5 | 2 |
| `sentiment_updated_factor` | `news.sentiment.updated` | `factor.calculate` | research | 5 | 1 |
| `fundamental_updated_factor` | `fundamental.data.updated` | `factor.calculate` | research | 5 | 1 |
| `factor_snapshot_strategy` | `factor.snapshot.updated` | `strategy.run` | strategy | 3 | 1 |
| `strategy_run_requested` | `strategy.run.requested` | `strategy.run` | strategy | 3 | 0 |
| `universe_updated_factor` | `universe.updated` | `factor.calculate` | research | 7 | 1 |
| `signal_generated_risk` | `strategy.signal.generated` | `risk.evaluate` | trading | 1 | 0 |
| `risk_check_requested` | `risk.check.requested` | `risk.evaluate` | trading | 1 | 0 |
| `risk_approved_execute` | `risk.check.approved` | `trade.execute` | trading | 1 | **0** |
| `collection_requested_kline` | `data.collection.requested` | `market.kline.collect` | market | 3 | 2 |
| `collection_requested_fundamental` | `data.collection.requested` | `fundamental.collect` | research | 5 | 2 |
| `collection_requested_news` | `data.collection.requested` | `news.collect` | research | 5 | 2 |
| `maintenance_due_cleanup` | `system.maintenance.due` | `maintenance.log_cleanup` | maintenance | 9 | 0 |
| `maintenance_due_archive` | `system.maintenance.due` | `maintenance.archive` | maintenance | 9 | 0 |
| `maintenance_due_validate` | `system.maintenance.due` | `maintenance.validate` | maintenance | 9 | 0 |
| `recovery_requested` | `engine.recovery.requested` | `system.recover` | system | 1 | 1 |

#### 运维规则的 job 分流

三条运维规则共用同一个事件，靠事件的 `job` 字段分流：

```
system.maintenance.tick（每 30s，调度器定时器）
      └─> MaintenancePlanner.tick()  哪个计划到点了？
            └─> system.maintenance.due {job:"log_cleanup", params:{...}}
                  └─> _job_is("log_cleanup") 命中 → 只建清理任务
```

**拆成 tick 和 due 两个事件**是为了让"节拍在跑"和"该干活了"在事件流里能分开看：
有 tick 没 due = 没到点或没启用；有 due 没任务 = 规则被关了。

没带 `job` 名的裸事件视为**全部命中** —— 手工发一个裸的 `maintenance.due`
是想"把运维活都跑一遍"，这符合直觉，不该静默什么都不做。

### 编排原则（每条都对应一类真实事故）

1. **只有落库/修正才触发因子**。盘中的实时 bar（`market.bar.received`）直接驱动策略，
   不触发因子重算 —— 否则每根 tick 都重算一次，缓存永不命中，CPU 全烧在重复计算上。
2. **下单链路不允许自动重试**，见上一节。
3. **长链路每一步都有幂等键**，缺了它事件重投会变成级联放大。
4. **条件守在规则里，不写在处理器里**。采集服务没启用时，规则直接不建任务，
   而不是建一个注定失败的任务去污染死信列表。
5. **回声事件不建任务**。`task.started` 这类事件是任务系统自己发出的，
   一旦有规则订阅它，`task.started → 任务 → task.started → …` 会在一秒内堆满队列。
   守卫是 `TASK_ECHO_EVENTS`，调度器先查再匹配。

### 降级仍然发布 `engine.ready`

依赖检查没通过时，引擎**仍然**发 `engine.ready`，只是带上 `degraded=true`，
同时另外发一条 `system.dependency.unavailable`。

这是刻意的取舍：完全不发 ready 的后果是"行情服务没连上 → 引擎永远不 ready →
连 K 线都不采"，故障被放大成全面停摆。而发 ready + degraded 标记，
则每条规则仍可用自己的条件决定要不要建任务 —— **判断权留在离业务最近的地方**。

---

## 七、维护计划与日志级别

### 维护计划：周期运维的时间源

`system.maintenance.due` 有三条规则，但它需要一个**时间源**才不会是死配置 ——
`app/runtime/maintenance.py` 就是这个时间源：

```
scheduler 定时器 ──每 30s──> system.maintenance.tick
                                    │
                        MaintenancePlanner.tick()
                                    │  哪个计划到点了？
                                    └─> system.maintenance.due {job, params}
                                              │
                                    规则表（按 job 名过滤）──> 任务 ──> maintenance 池
```

**计划器不发任务，只发"该干活了"这个事实**。建任务仍然归规则表管，
所以维护任务同样享有队列、幂等、重试、死信那一整套，和采集/因子任务一致。

配置在 `config/maintenance.json`，面板「运行时 → 维护计划」改完即生效并落盘：

| 任务 | 默认 | 周期 | 参数 |
|---|---|---|---|
| `log_cleanup` 日志清理 | 启用 | 每天 03:10 | `retention_days=7`、`apply=false` |
| `archive` 快照归档 | **停用** | 每天 03:40 | `before_days=30`、`apply=false` |
| `validate` 运行校验 | 启用 | **引擎启动时** | — |

#### 六种周期

| `schedule` | 含义 | 字段 | 约束 |
|---|---|---|---|
| `startup` | 引擎每次启动跑一次 | — | 不参与 `tick`；`run_startup()` 在 `scheduler.on_start` 触发 |
| `interval` | 每 N 小时 | `interval_hours` | ≥ 1 小时 |
| `daily` | 每天 HH:MM | `at` | 5 分钟错过宽限 |
| `weekly` | 每周某天 HH:MM | `weekday`(1=周一) + `at` | 同上 |
| `cron` | cron 表达式 | `cron` | 5 段；写错则不执行并报原因 |
| `once` | 指定时刻一次 | `once_at` | 跑完自动停用 |

**为什么间隔的最小粒度是小时**：维护任务是"扫一遍日志目录"这种活，
做成分钟级只会让引擎一直在做无用功。旧配置的 `interval_sec`（秒）读入时
按**向上取整**换算成小时 —— 向下取整会把 1.5 小时悄悄缩短成 1 小时，
维护任务跑太频繁比跑少了更烦人。

**为什么 `validate` 是 `startup`**：它的价值就是"这次启动的环境是好的"，
每小时再校验一遍既不增加信息量，又会在日志里堆一堆"正常"。
但 startup 必须**在 `tick` 之外单独触发** —— `tick` 是 30 秒一拍，
让 startup 参与 `is_due` 判定就变成每 30 秒跑一次了
（`next_run_at` 对 startup 返回 None，判定在 `is_due` 里显式截断）。

cron 由 `app/runtime/cron.py` 自己实现（零依赖，不为 5 段表达式引第三方包），
支持 `*`、`*/n`、`a-b`、`a,b`、`a-b/n`。**「日」与「周」是 OR 语义**：
`0 3 1 * 1` 是"每月 1 号**或**每周一"，不是"1 号且是周一"。

日/周任务有 5 分钟"错过宽限"：到点后 5 分钟内仍会补跑一次
（检查是 30s 一拍，"03:10" 实际在 03:10:xx 被看到），超过就算错过、顺延下个周期 ——
补跑维护任务收益极低，不值得为此在启动时制造一轮文件扫描。
一次性任务同理：时刻过了 5 分钟就不再补跑（补跑"上周三该做的校验"没有意义）。

#### 安全默认与硬拦截

- **清理与归档都默认只报告不删**。`apply=false` 时处理器只列出清单。
  "定时任务自动删文件"是运维事故高发区：配错一天就删掉一周日志。
- 真要删也有上限：清理单次最多 500 个文件，归档 1000 个。
- 控制面 `_guard_destructive` 会**再拦一道**：保留天数 < 1 天、周期 < 300 秒
  直接拒绝保存，不让危险配置有机会落到执行器里。
- 保留天数用 `params.get(k); 7 if v is None else int(v)` 取值 ——
  不能写 `or 7`，否则配错的"保留 0 天"会被悄悄换成保留 7 天。

### 日志级别：运行中可切换

级别原本只能靠环境变量在启动时定，于是排查线上问题就得重启服务，
**而重启恰恰会把你要排查的那个状态冲掉**。

`app/common/logging.py` 现在记住每个 sink 的完整配置，`set_level()` 用
"remove 掉再按原配置 add 回来"的方式热切换（loguru 没有公开的改级别接口）。
配置里带着 rotation/retention/format，重建不会打断轮转。

优先级：**面板/CLI 改过的（`config/logging.json`）> 环境变量 `LOG_LEVEL`**。

```bash
python main.py ctl log show            # 当前档位
python main.py ctl log level DEBUG     # 控制台 DEBUG，文件跟随
python main.py ctl log level DEBUG --file INFO --window 3
python main.py ctl log level INFO --no-persist   # 只本次生效
```

注意 `is_debug()` 现在读的是**当前生效**级别而不是启动时的环境变量：
否则面板把级别调到 DEBUG 之后，日志节流还在按启动时的判断继续压日志。

---

## 八、任务持久化与崩溃恢复

### 8.1 为什么任务必须落盘

内存态队列有一个致命性质：**进程死了，队列跟着死，而且死得特别安静** ——
没有报错、没有告警，那些任务只是"从来没被执行过"。

对采集任务来说这不算大事（下次定时会再来一遍）。但对下单就不是了：
一笔该下的单因为进程重启而消失，系统不会认为它有错，它只是"没发生过"。
要说清这笔钱为什么没投出去，只能去翻日志猜。

所以每个**未终结**的任务都会写进 SQLite（`output/runtime/tasks.db`），
启动时再捞回来。

### 8.2 为什么用独立的 SQLite 文件

| 维度 | 业务库（`/data/alphaquant.db`） | 任务流水账（`output/runtime/tasks.db`） |
|---|---|---|
| 性质 | 资产，长期保留、要备份 | 工作台，恢复完就该归档清理 |
| 写入形态 | 批量追加 + 长期查询 | 高频小写入 + 随时 truncate |
| 故障影响 | 库挂了 = 数据没了 | 库挂了 = 记不住任务（降级为纯内存） |

混在一个库里，"清理任务表"会变成一个不敢执行的操作。分开之后，
业务库出问题时至少流水账还能写，"数据库挂了"这件事本身能被记下来。

用标准库 `sqlite3` 而不是项目里的 SQLAlchemy 封装，是为了让 runtime 层
不依赖数据访问层 —— 改一次 ORM 模型不该让调度器跟着动。

### 8.3 写盘策略：普通变更合并，三件事立即写

| 时机 | 策略 | 理由 |
|---|---|---|
| 状态变更（通用） | 标脏，后台线程每 1s 合并成一个事务 | 调度热路径不能被 IO 拖慢 |
| **任务创建** | **立即写** | 建出来就没了是最坏的情况 |
| **进死信** | **立即写** | 那是等人工处理的，丢了没人对账 |
| **终态** | **立即写** | 用来把这条流水账划掉 |
| 引擎停止 | 强制 `flush()` + 在跑任务标中断 | 见下 |

代价是最多一次毫秒级写，换来的是"任何时刻 `kill -9`，未完成的活都还在盘上"。
`RUNNING` 也落盘，因为它的下一次更新可能永远不会来。

### 8.4 恢复规则：什么该重跑，什么不该

`SchedulerComponent.on_start` 先恢复、再订阅（顺序反了会漏掉恢复期间的事件）。

| 上次的状态 | 恢复动作 | 理由 |
|---|---|---|
| `running` | 标 `interrupted`，重新入队 | 结果未知。尝试次数**不回滚**（那次可能真执行了，比如下单请求已发出） |
| 已标 `interrupted` | 重新入队 | 停机时已处理过，但还没跑完 |
| `queued` / `retrying` | 重新入队 | 排队中/等重试，本来就还没做 |
| 超龄（默认 72h） | **转死信** | 三天前没干完的活，今天再跑通常没意义（行情变了、上下文没了） |
| 重试额度用尽 | **转死信** | 这条路已走过上限次数，再走不会有不同结果 |
| `dead_letter` | 读回内存**但不入队** | 错误队列必须跨重启存在，否则"重启一下死信就没了"等于最佳掩盖手段 |

两点容易踩的坑，都已写进测试：

1. **中断优先于"重试耗尽"**。一个 `max_retries=0` 的任务被中断时
   `attempts` 已经是 1，若先按"额度用尽"判，它会被直接丢进死信 ——
   而它根本没失败过，只是进程没了。
2. **幂等键要重建**。否则恢复出来的任务挡不住重复入队，
   同一批数据会被处理两遍。

### 8.5 错误队列：人工干预

死信不是"失败日志"，是**待办清单**。清单的价值在于有人处理它，
所以必须有两个动作：

| 动作 | 语义 | 实现要点 |
|---|---|---|
| **继续** | 人工确认后重投 | `attempts` **清零**。自动重试的额度是给"暂时性故障"的，用完了说明走不通；人看过原因后给的这次是**新机会**，不该带着上次的欠账上路 |
| **丢弃** | 认了，不重跑 | 不是每条死信都该重跑："三天前的因子"没意义，"昨天的下单"危险 |

`trade.execute` **不给"继续"**（`actionable=false`，按钮置灰并说明原因）：
点了会重复下单。这类任务必须让人先核对订单状态。

每次处置都记进 `dead_letter_log` 表，即使之后清空了内存里的死信，
"谁在什么时候处置了什么"仍然查得到。

### 8.6 启动预检

见 `app/engine/preflight.py`。它拦的不是崩溃，是**空转**：

| 检查项 | 出问题的等级 | 空转表现 |
|---|---|---|
| 配置文件可解析 | error（写坏） / warn（缺失） | 写坏的 JSON 被静默忽略 → "我明明配了 03:10 清日志，它从来没清过" |
| 目录可写 | error | 采集"成功执行"，一条都没存下 |
| 数据库可读写 | error | 同上，且没有任何报错 |
| 行情数据非空 | warn | 引擎"成功启动"，然后一整天没信号 |
| 磁盘余量 | error(<0.5G) / warn(<2G) | 日志与数据库同时失效 |
| 数据源链 | warn | 采集注定全失败 |
| 遗留任务 | warn | 提示"启动后将恢复 N 条" |
| 实盘网关 | error（仅 LIVE） | 第一笔信号才发现下不出去 |

三档结论：`info` 正常、`warn` 降级可飞、`error` 不能飞。
默认 `error` 时**拒绝启动**（`PREFLIGHT_STRICT=0` 可强制继续）。

预检与 `engine.ready` 的 `_readiness()` 分工：后者问"组件起来了没有"
（运行期状态），前者问"环境齐不齐"（启动前的静态事实）。
两者都要有 —— 组件全绿但股票池是空的，照样一整天白跑。

---

## 九、最小闭环

以一次 5 分钟 K 线更新为例：

```
定时/手工            → market.kline.collect 任务
  market 池           → 采集、落库
  collection_service  → 发布 market.kline.updated（只在真的新增/修正后发）
  research 池         → factor.calculate → 失效因子缓存
  处理器              → 发布 factor.snapshot.updated
  strategy 池         → strategy.run（只跑依赖这些因子且已启用的策略）
  信号                → 发布 strategy.signal.generated
  trading 池          → risk.evaluate → 发布 risk.check.approved / rejected
  trading 池          → trade.execute → 执行瞬间**再过一次风控**（TOCTOU 防护）
```

每一步都必须坚持：

- K 线采集成功 ≠ K 线完整，也不等于可以立即交易；
- 因子更新后不一定要跑所有策略，只跑依赖相关因子的；
- 策略产生信号 ≠ 自动下单；
- 补采历史数据可能触发因子重算，但**不能**误触发过期的实盘交易信号；
- 消息重复投递、任务重试、服务重启，都不能造成重复下单。

### 一个关键实现细节：`execute_approved`

事件链路要在**另一个线程、另一个时刻**完成下单，所以 `QuantTask` 暴露了
`execute_approved(signal, approved_size)`。但它**照样走一遍 `_plan_order`**
（全局闸门 + 任务链 + 仓位 + 已有在途单检查）：

```python
def _process_signal(...)     # 策略 on_bar 调用
def execute_approved(...)    # 事件链路调用
        └── 两者共用 _plan_order() + _place_order()
```

**不存在一条绕过风控的下单路径** —— 这是刻意的设计约束。
从"风控说可以"到"真正报单"之间账户可能已经变了（别的任务刚成交、手动熔断、
额度被占），只看之前的裁决就下单，正是并发下超限的经典来源。

---

## 十、运维入口

### CLI

```bash
python main.py ctl runtime status      # 队列积压与执行池占用
python main.py ctl runtime pools       # 各池能力声明与占用
python main.py ctl runtime rules       # 规则表（什么事件会生成什么任务）
python main.py ctl runtime dead        # 错误队列（死信）列表
python main.py ctl runtime dead-retry <task_id>     # 人工点"继续"
python main.py ctl runtime dead-retry --all         # 批量重投（下单类会跳过）
python main.py ctl runtime dead-discard <task_id>   # 认了，不重跑
python main.py ctl runtime dead-clear  # 清空死信（处理完这批之后用）
python main.py ctl runtime journal     # 流水账：落盘状态 + 死信处置历史
python main.py ctl runtime maint       # 维护计划与下次执行时间
python main.py ctl runtime maint-run validate   # 立刻跑一次（不看周期/开关）
python main.py ctl preflight           # 启动预检结论
python main.py ctl preflight --rerun   # 当场再体检一遍

# 核对数据：任务明细与事件指标
python main.py ctl runtime tasks                      # 已建任务 + 终态历史
python main.py ctl runtime tasks --status failed      # 只看失败
python main.py ctl runtime tasks --type factor.calculate --history
python main.py ctl runtime task <task_id>             # 单条：参数/结果/错误/耗时
python main.py ctl runtime events                     # 每种事件发布数/订阅者/异常
python main.py ctl runtime events --type market.kline.updated

# 手动投递一个引擎任务
python main.py ctl runtime dispatch \
    --task-type factor.calculate \
    --params '{"symbols":["600519","000858"],"day":"2026-10-12"}' \
    --priority 3

# 临时掐断一条自动链路（不用改代码、不用重启）
python main.py ctl runtime rule off factor_snapshot_strategy

# 日志级别（不用重启）
python main.py ctl log show
python main.py ctl log level DEBUG --file INFO
```

### HTTP

```
GET  /api/runtime                任务系统全景（队列/池/规则/处理器/死信/维护计划）
POST /api/runtime/dispatch       {"task_type": "...", "params": {...}}
POST /api/runtime/rule           {"name": "...", "enabled": false}
POST /api/runtime/dead/clear     {}                       清空死信
POST /api/runtime/dead/retry     {"task_id": "..."}       人工重投一条
POST /api/runtime/dead/retry     {"all": true}            批量重投
POST /api/runtime/dead/discard   {"task_id": "..."}       认掉一条（不重跑）
GET  /api/journal                流水账状态 + 死信处置历史
GET  /api/preflight              启动预检结论（ran=false 表示还没跑过）
GET  /api/preflight?rerun=1      当场再跑一遍
POST /api/preflight/run          {}                       同上
GET  /api/maintenance            维护计划与运行态（含可用周期 kinds 与下次执行）
POST /api/maintenance/save       {"jobs": {...}}          保存并生效
POST /api/maintenance/run        {"name": "log_cleanup"}  立即执行
GET  /api/ledger                 任务明细：已建任务 + 终态历史
     ?status=success&task_type=factor.calculate&keyword=000001&limit=100
GET  /api/task?id=<task_id>      单条任务明细（参数 / 结果 / 错误 / 耗时）
GET  /api/events                 事件指标：每种事件发布数/触达/订阅者/异常
GET  /api/events?type=<name>     该类事件的指标 + 最近 20 条样例
GET  /api/logging                当前生效的日志配置
POST /api/logging/level          {"console":"DEBUG","file":"INFO","persist":true}
```

> `/api/tasks/<id>` 是**旧式量化任务**的详情，引擎任务的明细在 `/api/task?id=`。
> 两个路由刻意分开：它们的字段完全不同，合并只会让两边都要判断"这次返回的是哪种"。

### 面板

监控面板新增 **运行时** 页（最后一个 tab），一屏回答"现在系统在做什么"：

- 执行池卡片（占用条 + 完成/失败/超时/满槽拒收计数）
- 队列积压（对应池与容量一起显示，积压 > 10 打标）
- 在跑任务、调度规则（可逐条开关）、能力路由表
- **错误队列**：逐条「继续 / 丢弃」，下单类任务的"继续"按钮置灰并说明原因；
  带"重投 N 次""中断过"标记
- **任务持久化**：流水账路径 / 未完成数 / 本次恢复了多少 / 上次刷盘时间 /
  落盘是否出错 + 死信处置历史（内存清空后仍可追溯）
- **启动预检**：逐项结果与处置建议，可当场重跑
- 手动投递任务（任务类型下拉直接取已注册处理器）
- 维护计划（周期/时刻/参数/立即执行）—— 切换周期时只留下该周期用得到的输入框
- **任务明细**：按状态/类型/关键字查已建任务与终态历史，点「详情」看完整
  参数、结果与错误原文
- **事件指标**：每种事件的发布数、触达订阅者数、订阅者数、处理异常数与最近时间；
  点「样例」只看该类事件的最近 20 条

#### 为什么要有"明细"这一层

汇总数字只能回答"成功 1200 条"，回答不了"这 1200 条分别是哪些、
参数对不对、什么时候跑的"。核对数据时这两件事的可信度完全不同：
前者可以来自任何地方，后者能直接对着日志和数据库验。

两个数的区别也要看得见：**累计**是本次进程启动以来的总数，
**历史里有明细**是还看得见逐条记录的条数（`TaskStore` 的任务表有上限，
满了会淘汰终态任务，但终态历史会再留最近 `history_size` 条）。
两个数差得越多，说明越多旧任务已经滚出明细窗口。

事件指标里 **订阅者 = 0 而发布数在涨** 是最该看的一格：
事件发出去了但没人接，链路在跑起来之后断了。
- 日志等级（控制台/文件/节流窗口，可勾"重启后仍生效"）

### 引擎快照

`SchedulerComponent.snapshot()` 把队列、执行池、路由、规则、处理器、统计、
维护计划全部带出来，面板与 `/api/runtime` 共用这一份数据，
不会出现"两边说的不一样"。

`health_check()` 在死信里含交易类任务时返回**不健康**：
那意味着一笔该下的单没下出去，或者存在一笔状态不确定的单。
非交易类死信只是提示（仍返回健康），且**清掉之后提示必须消失** ——
否则告警一旦响过就永久响下去，运维再也不会看它。

---

## 十一、如何扩展

### 加一种任务类型（三步）

```python
# 1) 注册常量（app/runtime/events/types.py）
class TaskType:
    MY_TASK = "my.task"
TASK_QUEUE[TaskType.MY_TASK] = Queue.RESEARCH
Pool.DEFAULT_CAPABILITIES[Pool.RESEARCH] |= {TaskType.MY_TASK}

# 2) 写处理器（app/runtime/workers/handlers.py）
def handle_my_task(task, ctx):
    svc = _require(ctx, "my_service", "我的任务")   # 取不到就抛，不假成功
    return svc.do(task.params)

# 3) 接上事件（app/runtime/scheduling/defaults.py）
Rule(name="my_rule",
     event_type=EventType.MARKET_KLINE_UPDATED,
     task_type=TaskType.MY_TASK,
     condition=_has_symbols,                        # 没有标的不跑
     dedup_fn=lambda e, c: f"my:{e.get('day')}",    # 幂等
     priority=Priority.NORMAL)
```

**不要**把处理器注册成空壳：查不到处理器会明确进死信并写明原因，
好过一个"跑成功但什么都没做"的实现。

### 加一个执行池

```python
PoolManager(capacities={"research": 4})     # 改容量
router.register_pool("gpu", ["my.task"], queue="gpu")   # 加池 + 声明能力
router.set_task_pool("my.task", "gpu")                   # 定向到独占资源
```

### 加一条规则

规则是**数据**，所以不必改调度器：

```python
scheduler.rules.register(Rule(
    name="my_rule", event_type="...", task_type="...",
    condition=lambda e, ctx: bool(ctx.engine),
))
```

`condition` 抛异常按**不跑**处理（记日志 + 跳过），
不会让同事件的其它规则跟着失效。

### 加一个维护任务

维护任务**不需要**新规则 —— 在 `app/runtime/maintenance.py` 的 `DEFAULT_JOBS`
里加一项即可，规则表已有 `maintenance_due_*` 三条按 job 名分流。
新任务的 `params` 会原样透传给处理器（`_maintenance_params`）。

前提是它的任务类型已有处理器，否则会进死信（这是刻意的：明确失败好过假成功）。

---

## 十二、可靠性现状

| 机制 | 状态 | 说明 |
|---|---|---|
| 事件 ID 与消费去重 | ✅ | `EventBus._seen`，有界 LRU |
| 任务唯一键与幂等 | ✅ | `dedup_key`，终结后释放 |
| 任务状态机 | ✅ | `TaskState` 八态，含死信 |
| 超时、重试、死信 | ✅ | 指数退避 + 抖动；超时为记账层面 |
| 任务优先级 | ✅ | 1（下单）… 9（清理） |
| 资源隔离 | ✅ | 八池独立线程池，互不挤占 |
| 处理器缺失可见 | ✅ | 直接进死信，不假成功 |
| 维护任务周期可配 | ✅ | `config/maintenance.json`，保存即生效 |
| 危险维护配置拦截 | ✅ | 保留天数 < 1 天 / 周期 < 300 秒 直接拒绝 |
| 日志级别热切换 | ✅ | 不用重启；可持久化 |
| 任务持久化 | ✅ | 未终结任务写 SQLite，`kill -9` 也在盘上 |
| 崩溃恢复 | ✅ | 启动时按状态分流恢复；中断/超龄/耗尽各有处置 |
| 错误队列人工干预 | ✅ | 继续（额度重置）/ 丢弃；下单类不可重投 |
| 启动预检 | ✅ | 配置/目录/数据库/数据/磁盘/网关，三档结论 |
| 回测任务接入 | ✅ | `backtest.run` 注册处理器，走 `backtest` 池（capacity=1），幂等 |
| 任务结果可核对 | ✅ | 成功看 `result`（处理器返回值），失败看 `error` |
| **Outbox / 可靠投递** | ❌ | 事件目前只在内存广播，**事件**本身重启即丢。任务已落盘，但"事件→任务"这一步没有 |
| **执行器心跳与租约** | ❌ | 单进程内不需要；跨进程后必须补 |
| **订单幂等与状态核对** | ⚠️ | 有额度预留 + TTL 兜底，但没有持久化的订单状态机 |
| **死信改参重投** | ❌ | 现在只能"原样继续"或"丢弃"。参数写错导致的失败，改不了参数只能重来 |
| **事件级追踪持久化** | ❌ | `correlation_id` 在内存里跟着事件走，重启后串不起来 |

**下一步优先级**：Outbox。
任务这一侧已经能活过重启了，但**事件**还是内存广播：
`market.kline.updated` 在到达调度器之前进程没了，这条事实就消失了，
连"它曾经发生过"都查不到。把事件也写进同一份流水账
（发布即落盘、消费后标记），"事件 → 任务"这一步才真正闭环。

次一级是**死信改参重投**：现在的错误队列只能"原样再跑一次"或"丢弃"，
而相当一部分死信的原因是参数写错（标的代码打错、日期区间反了）——
这种情况下"继续"必然再失败一次，只能去重新提交一遍。
让死信条目可编辑参数再入队，是把错误队列从"重试按钮"变成"修复入口"的关键一步。

任务持久化关掉的方式：`AQ_TASK_PERSIST=0`（测试环境默认关，
避免测试造出来的任务污染真实输出目录）。

---

## 十三、测试

`app/tests/test_event_task_runtime.py`（45 项）钉住的是**架构不变式**：

- 旧事件名映射必须是单射（回归：控制面板曾收到幽灵指令）；
- 一个旧事件不能被投递给另一个旧事件的订阅者；
- 任务系统自己发出的事件不能再生成任务（回声防护）；
- 执行池之间互不影响（回测跑满不挤死实盘）；
- 下单类任务永远不允许自动重试；
- 没有处理器的任务必须进死信；
- 完整闭环：K 线更新 → 因子任务 → 因子快照 → 策略任务。

`app/tests/test_maintenance_logging.py`（38 项）钉住的是**周期性运维与可观测性**：

- 日任务到点后第一拍必须触发（回归：曾因"时刻过了就顺延明天"永远不触发）；
- 错过太久不补跑，今天跑过不再重复；
- 裸的 `maintenance.due`（无 job）= 三个运维任务都建；带 job 只建对应的那个；
- 默认配置里**不能**有任何"会删东西"的开关；
- 保留天数 0、周期 10 秒必须被拒绝；
- 归档真的移动文件，且归档区不重复处理；
- 日志级别切了之后 `is_debug()` 要跟着变；
- 取消任务必须真的出队（否则"队列积压"把没事可做的任务算进去）；
- 清空死信后健康提示必须消失，但累计计数保留。

`app/tests/test_task_persistence.py`（36 项）钉住的是**崩溃恢复与人工干预**：

- 任务入队那一刻就必须在盘上（不等后台 flush）；
- 跑了一半的活恢复后必须重跑，且带"中断过"标记、尝试次数不回滚；
- **中断优先于"重试耗尽"**（回归：`max_retries=0` 的任务被中断时
  `attempts` 已是 1，曾因此被误判成额度用尽而直接丢进死信）；
- 超龄（>72h）转死信，不再自动重跑；
- 死信必须跨重启存在；恢复后幂等键仍生效；
- 人工重投必须**清零 `attempts`** 并累加 `manual_retries`；
- 下单类死信不给"继续"，且 `actionable=false`；
- 一次性函数型任务不落盘（参数含 callable，恢复后必然失败）；
- 流水账清理时，未终结与死信一条都不许动；
- 预检的每条 warn/error 都必须带处置建议；LIVE 无网关必须判为阻塞。

这三组共同的立场：**宁可明确失败，也不要"看起来在跑"**。
落盘、恢复、人工干预这三件事的失败都不会报错、不会告警，
只是"该做的事从来没发生过"。

---

## 十四、对照 Rhapsody 引擎的队列设计

参考对象：`Rhapsody Engine 7`（医疗数据集成引擎），
`rhapsody/data/queue/` 与 `rhapsody/data/` 下的运行时产物。

选它对照是因为它把"消息不能丢"这件事做到了文件系统层面，
而这一层恰好是我们现在最弱的地方（见 §十二 的 Outbox）。

### 14.1 它怎么做的

```
data/queue/
├── input/<队列号>/        待处理：0000000000.queue3（预分配 4MB）
│                          queue.head / queue.tail / queueMessageMeta.map
├── process/<队列号>/      处理中：取出来先放这，跑完才移走
├── output/<队列号>/       成功
├── errorInput/<队列号>/   失败（**保留原始消息体**，可修正后重投）
└── redirected/            重定向

data/messagestore/         body（16MB 预分配）/ meta（8MB）：正文与索引分离
data/tracking/             routeTracking / communicationPointTracking /
                           messageIdentifier / trackingStatus：全链路追踪独立落盘
data/transaction/          0000000000.txn（12.8MB 预分配）
data/notifications/        events / history / cleanup.tracker（清理也有位点）
```

四个关键选择：

1. **状态用目录表达，不用字段表达。** 一条消息在哪儿，看它在哪个目录。
   崩溃后不需要"查数据库猜状态"，列目录就知道有多少卡在 `process/`。
2. **预分配定长块 + head/tail 指针。** 文件一上来就是 4MB/16MB，
   写满了再开下一块。顺序写、不产生碎片、可以 mmap。
   `queue.head` / `queue.tail` 是两个独立的小文件，只记消费位点。
3. **`process/` 是两阶段提交。** 取消息先移到 `process/`，
   处理完再移到 `output/`。进程死在中间，消息还在 `process/` 里 ——
   重启后知道"这条取出来了但没跑完"，而不是"不知道跑没跑"。
4. **`errorInput` 是修复入口，不是垃圾桶。** 它保留完整的原始消息体，
   所以可以改完内容再放回 `input/` 重放。

### 14.2 哪些不该照搬

Rhapsody 是跨系统集成的消息中间件：每秒上千条消息、多个外部系统、
"一条都不能丢"是硬性合规要求。AlphaQuant 是单进程量化引擎，
任务量级是**每天几千个**，不是每秒上千条。

在这个量级下，预分配块文件 + head/tail 位点带来的是纯粹的复杂度：
位点管理、块回收、归档策略、跨块读取。
而它换来的好处（顺序写、无碎片）在 SQLite WAL 模式下已经拿到了 ——
我们现在缺的不是写入性能。

结论：**存储格式不动，继续用 SQLite。** 要借鉴的是它表达"状态"和
"失败"的方式，不是它存文件的方式。

### 14.3 值得借鉴的三点

| Rhapsody 的做法 | 我们的差距 | 价值 |
|---|---|---|
| 消息先落盘再处理 | **Outbox** ❌：事件仍是内存广播，重启即丢 | 补上"事件→任务"的断点，这是当前最大的缺口 |
| `errorInput` 保留原始载荷、可改后重放 | **死信改参重投** ❌：只能"原样继续"或"丢弃" | 参数写错导致的失败（代码打错、日期写反）占了死信相当一部分，现在只能去重新提交 |
| `tracking/` 独立持久化 | **事件级追踪** ❌：`correlation_id` 只在内存里 | "这条数据从哪来到哪去" 重启后串不起来，排障只能靠时间戳猜 |

`process/` 的两阶段我们已经用另一种形式实现了：停机时把在跑任务标
`interrupted`（见 §8.4），恢复时它优先于"重试耗尽"被判定。
语义是一样的 —— **区分"没跑过"和"跑了一半"**。

### 14.4 一个反面的提醒

Rhapsody 的 `errorInput/5` 和 `input/5` 里都躺着一个 3.9MB 的
`0000000000.queue3`，而 `output/5` 只有 40 字节。
也就是说：**这个队列里堆着的失败消息比成功的多几个数量级，
而且一直没被清走。**

这提醒了一件事：**错误队列本身也会变成垃圾场。**
我们刚加的错误队列同样需要清理策略 —— 否则半年后打开面板是几千条
三年前的死信，真出事的那条淹在里面。
（现状：`journal.prune_terminal()` 保留死信一条不动，
这是对的；但死信的**处置历史** `dead_letter_log` 只增不删，
需要给它一个保留窗口。）
