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
| `recovery_requested` | `engine.recovery.requested` | `system.recover` | system | 1 | 1 |

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

## 七、最小闭环

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

## 八、运维入口

### CLI

```bash
python main.py ctl runtime status      # 队列积压与执行池占用
python main.py ctl runtime pools       # 各池能力声明与占用
python main.py ctl runtime rules       # 规则表（什么事件会生成什么任务）
python main.py ctl runtime dead        # 死信列表

# 手动投递一个引擎任务
python main.py ctl runtime dispatch \
    --task-type factor.calculate \
    --params '{"symbols":["600519","000858"],"day":"2026-10-12"}' \
    --priority 3

# 临时掐断一条自动链路（不用改代码、不用重启）
python main.py ctl runtime rule off factor_snapshot_strategy
```

### HTTP

```
GET  /api/runtime           任务系统全景（队列/池/规则/处理器/死信）
POST /api/runtime/dispatch  {"task_type": "...", "params": {...}}
POST /api/runtime/rule      {"name": "...", "enabled": false}
```

### 引擎快照

`SchedulerComponent.snapshot()` 把队列、执行池、路由、规则、处理器、统计全部带出来，
面板与 `/api/runtime` 共用这一份数据，不会出现"两边说的不一样"。

`health_check()` 在死信里含交易类任务时返回**不健康**：
那意味着一笔该下的单没下出去，或者存在一笔状态不确定的单。

---

## 九、如何扩展

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

---

## 十、可靠性现状

| 机制 | 状态 | 说明 |
|---|---|---|
| 事件 ID 与消费去重 | ✅ | `EventBus._seen`，有界 LRU |
| 任务唯一键与幂等 | ✅ | `dedup_key`，终结后释放 |
| 任务状态机 | ✅ | `TaskState` 八态，含死信 |
| 超时、重试、死信 | ✅ | 指数退避 + 抖动；超时为记账层面 |
| 任务优先级 | ✅ | 1（下单）… 9（清理） |
| 资源隔离 | ✅ | 六池独立线程池，互不挤占 |
| 处理器缺失可见 | ✅ | 直接进死信，不假成功 |
| **Outbox / 可靠投递** | ❌ | 事件目前只在内存广播，进程重启即丢 |
| **任务持久化** | ❌ | 队列与在途任务全是内存态，重启后需靠 `system.recover` 重建 |
| **执行器心跳与租约** | ❌ | 单进程内不需要；跨进程后必须补 |
| **订单幂等与状态核对** | ⚠️ | 有额度预留 + TTL 兜底，但没有持久化的订单状态机 |

**下一步优先级**：任务持久化 + Outbox。
现在的 `system.recover` 只能恢复"内存里还记着的"任务，
进程被 `kill -9` 时这些记录也一起没了 ——
这是从"能跑通"到"敢实盘"之间最大的一道缺口。

---

## 十一、测试

`app/tests/test_event_task_runtime.py`（45 项）钉住的是**架构不变式**：

- 旧事件名映射必须是单射（回归：控制面板曾收到幽灵指令）；
- 一个旧事件不能被投递给另一个旧事件的订阅者；
- 任务系统自己发出的事件不能再生成任务（回声防护）；
- 执行池之间互不影响（回测跑满不挤死实盘）；
- 下单类任务永远不允许自动重试；
- 没有处理器的任务必须进死信；
- 完整闭环：K 线更新 → 因子任务 → 因子快照 → 策略任务。

任何一条破了，系统都能"看起来在跑"而实际什么都没做 ——
那正是这类架构最危险的失败模式。
