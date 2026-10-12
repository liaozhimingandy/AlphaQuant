# 引擎架构：四类组件与服务化

> 本文回答三个问题：**引擎是怎么组织的**、**为什么这么分**、**我要加东西该放哪**。
> 面向的是要改引擎代码的人，不是要用它跑策略的人（后者看 `ARCHITECTURE.md` 第七节）。

---

## 一、重构前的样子，以及它为什么难维护

重构前引擎里有 **11 个平级组件**，全部塞在同一个字典里：

```
engine._components = {
    "data_collector":   K 线采集,
    "ext_data":         日频截面采集,
    "data_guard":       完整性与补全,
    "market_center":    行情,
    "live_gateway":     券商网关,
    "trading_store":    交易落库,
    "news_center":      资讯,
    "task_scheduler":   任务队列,
    "timerV2":          周期定时器,
    "strategy_manager": 策略中枢,
    "monitor":          网页面板,
}
```

"平级"是全部问题的根源。它带来三个没法在代码里表达、只能靠人记住的约束：

| 隐性约束 | 存在哪里 | 失效方式 |
|---|---|---|
| 谁提供能力、谁消费能力 | 只存在于人的脑子里 | 新组件直接 `get_component()` 拿任何东西，循环依赖无人发现 |
| 谁先启动、谁后停止 | `builder.py` 里的**注册顺序** | 调整一行注册代码就悄悄改了启停顺序 |
| 组件之间怎么互相找 | 约 **30 处** `engine.get_component("market_center")` 字符串 | 重命名即崩，且崩在运行时而非编译期 |

这三条有一个共同点：**约束是隐式的**。隐式约束不会在重构时报错，
只会在某个深夜以"奇怪的行为"出现——比如面板一切正常但一笔单都没发出去。

---

## 二、重构后的四类组件

```
IBaseComponent                      ← 生命周期（initialize / start / stop）
│
├── ServiceComponent                提供能力、被别人依赖
│      ├── MarketService            market.bar_feed / market.quote / market.watchlist
│      ├── CollectionService        collect.kline / collect.cross / collect.integrity
│      ├── TradingService           trade.route / trade.account / trade.reconcile / trade.persist
│      ├── RiskService              risk.gate
│      └── NewsService              news.feed
│
├── StrategyComponent               消费能力、产出订单
│
├── WorkerComponent                 消费能力、执行作业（有并发上限）
│
├── SchedulerComponent              决定"什么时候跑"
│
└── （PLAIN）MonitorWebComponent    观测者，不参与上面四种角色
```

四个基类都在 `app/engine/capability.py`。**它们不新增生命周期**——
`initialize / start / stop` 的语义完全沿用 `IBaseComponent`，
只是在该接口之上加"角色语义"。这是刻意的：引擎主循环不需要为服务单独写一套启停逻辑。

### 2.1 四种角色的定义

| 种类 | 干什么 | 判定标准 |
|---|---|---|
| `SERVICE` | 常驻，对外提供能力，被别人依赖 | **它消费别人的输出吗？** 不消费、只提供 → 服务 |
| `STRATEGY` | 被行情/新闻驱动，产出订单 | 要消费行情 + 风控 |
| `WORKER` | 被调用触发，干完继续等 | 没有自己的节拍，节拍来自调度或外部 |
| `SCHEDULER` | 决定执行节拍 | 它不干活，它决定别人什么时候干 |

判定标准只有一条，而且很硬：**问它消费不消费别人的输出**。

以前采集、行情、交易、策略全平铺在一个目录里，就是因为没人问过这个问题。
问了就清楚了：行情服务不消费任何东西（它读库、它推 bar），
策略组件消费行情与风控（它是终点），于是它们天然分居两层。

### 2.2 分类带来的三个硬收益

1. **依赖显式化**：服务声明 `provides`（我提供什么）与 `depends_on`（我需要什么），
   引擎据此**拓扑排序**启停，不再依赖注册顺序。
2. **注入替代查找**：`StrategyComponent` / `WorkerComponent` 通过
   `required_services` 拿到服务实例，组件内部不再出现魔法字符串。
3. **可观测分组**：面板按 `kind` 分组，一眼看出"服务层 5 个全绿、策略层 1 个在跑"。

---

## 三、能力（Capability）

服务不再用"名字"互相找，而是用**能力**。能力名是常量，不是裸字符串：

```python
from app.engine.capability import Cap

Cap.BAR_FEED          # "market.bar_feed"     推送/回放 K 线
Cap.SNAPSHOT_QUOTE    # "market.quote"        最新快照行情
Cap.WATCHLIST         # "market.watchlist"    动态增删订阅标的
Cap.COLLECT_KLINE     # "collect.kline"       抓 K 线入库
Cap.COLLECT_CROSS     # "collect.cross"       日频截面（资金流/筹码/涨停池）
Cap.DATA_INTEGRITY    # "collect.integrity"   完整性与缺口补全
Cap.ORDER_ROUTE       # "trade.route"         订单路由到券商
Cap.ACCOUNT_QUERY     # "trade.account"       查资金/持仓
Cap.RECONCILE         # "trade.reconcile"     对账
Cap.TRADE_PERSIST     # "trade.persist"       订单/成交/权益落库
Cap.RISK_GATE         # "risk.gate"           下单前拦截/放行/改量
Cap.NEWS_FEED         # "news.feed"           新闻采集与分析
```

**为什么用常量而不是字符串**：`Cap.BAR_FEED` 写错时 IDE 与静态检查立刻发现；
`"market.bar"` 拼成 `"market.bars"` 只会在运行时静默查不到——
而"静默查不到"在交易系统里意味着"这个检查从来没生效过"。
另外改名只影响 `Cap` 一处，不用全局替换。

---

## 四、服务注册表与依赖注入

`app/engine/registry.py` 的 `ServiceRegistry` 是这一层的核心。
引擎在 `BaseQuantEngine.__init__` 里持有一份：`engine.services`。

### 4.1 对外接口

| 方法 | 作用 |
|---|---|
| `register(service, alias=())` | 登记服务与其别名（重名抛错） |
| `get(name)` / `require(name)` | 按名取（`require` 取不到就抛） |
| `by_capability(cap)` | 按能力取**全部**提供者 |
| `first_by_capability(cap)` | 按能力取第一个 |
| `resolve_dependencies()` | 拓扑排序，**成环抛 `ServiceRegistryError`** |
| `build_injections(target)` | 按目标的 `depends_on` / `required_services` 打包依赖 |
| `snapshot()` | `{服务名: 条目}`，每条含能力/依赖/状态/健康与 `missing_dependencies` |
| `missing_capabilities(required)` | 缺哪些能力 |

### 4.2 注入发生在初始化**之前**

`BaseQuantEngine.initialize()` 里的顺序是：

```
1. ordered_component_names()            ← 先算启停顺序
2. component.bind_services(engine.services.build_injections(component))
3. component.initialize(...)            ← 后初始化
```

顺序不能反。组件的 `on_initialize` 里就要用这些服务
（策略组件在 `on_initialize` 里就把风控闸门交给任务运行时），
先初始化再注入，那一瞬间拿到的是空字典。

### 4.3 服务 vs 策略：依赖缺失的处理是**相反**的

| 组件种类 | 依赖缺失时 | 理由 |
|---|---|---|
| `ServiceComponent` | 记 warning，**降级继续** | 服务是基础设施，少一个可选依赖不该让引擎起不来 |
| `StrategyComponent` | **直接抛错** | 策略没有行情就是瞎子、没有风控就是裸奔，降级运行毫无意义 |

这个不对称是刻意的。实盘系统最贵的一类 bug 是"面板一切正常，但一笔单都没发出去"，
策略层的静默降级正是它的温床。

### 4.4 Provider 的延迟取值

策略组件拿网关与风控闸门用的是 **lambda 而不是对象**：

```python
sm.runtime.gateway_provider = lambda: trade.gateway
runtime.risk_gate_provider  = lambda: risk.gate
```

原因：任务建得比服务早。装配时 `trade.gateway` 可能还是 `None`
（网关在交易服务的 `on_initialize` 里才创建）。早取会拿到 `None`，
而 `None` 会一路传到下单路径，最后报出"'NoneType' object has no attribute submit"
这种完全看不出根因的错。

---

## 五、启停顺序

### 5.1 顺序由依赖算出来，不是写出来的

`ordered_component_names()` 的排序键有两个：

1. **硬约束**：`depends_on` 声明的真实依赖（DFS + 三色标记，成环报错）；
2. **软约束**：`KIND_LAYER` 种类层级（数字小的先启动、后停止）。

```python
KIND_LAYER = {
    SCHEDULER: 10,   # 调度先起
    SERVICE:   20,   # 服务紧接着
    STRATEGY:  30,   # 策略与工作单元最后（它们要消费服务）
    WORKER:    40,
    PLAIN:     50,   # 监控面板最后起、最先停
}
```

软约束存在的理由：光靠 `depends_on` 排序，**输出顺序不稳定**
（没有依赖关系的组件之间顺序任意），日志与面板每次都在跳。
加上层级之后顺序既正确又稳定。

`PLAIN` 排最后启动、最先停止，于是面板永远看不到"引擎正在散架"的中间态。

### 5.2 实测顺序

`build_engine(mode="SIMULATE")` 的七个组件：

```
 1. scheduler            调度
 2. collection_service   服务
 3. market_service       服务
 4. risk_service         服务
 5. trading_service      服务
 6. strategy             策略
 7. worker               工作单元
```

注意 `scheduler` 排在最前，尽管它在 `builder.py` 里是第一个注册的——
**顺序来自层级而非注册顺序**。现在改 `builder.py` 的注册次序只影响可读性，不再影响行为。

> `component_names()` 也走同一套排序。以前它返回注册顺序，
> 于是"面板上组件的顺序"会随一行注册代码改变，而看面板的人会以为那个顺序有意义。

---

## 六、目录与文件

> **2026-10 目录重构后**，组件按「容器 / 运行时 / 模块」三层归位。
> 引擎本体被收窄成纯容器，业务实现全部搬去了 `app/modules/`，
> 调度与 жертв作业搬去了 `app/runtime/`。详见 [STRUCTURE.md](STRUCTURE.md)。

```
app/engine/                     ← 容器层：只管生命周期、装配、依赖
├── engine.py       BaseQuantEngine / IQuantEngine（原 base.py）
├── builder.py      build_engine：配置 → 组件的唯一装配入口
├── context.py      EngineContext / EngineStatus（原 settings.py）
├── component.py    IBaseComponent：生命周期 + 进度 + 事件订阅（原 component/base.py）
├── capability.py   ★ 四种角色基类 + ComponentKind + KIND_LAYER + Cap（原 component/kinds.py）
├── registry.py     ★ ServiceRegistry：别名 / 拓扑排序 / 依赖注入（原 component/registry.py）
├── service.py      Twisted Service 包装
├── service_runner.py  常驻进程入口
├── daemon.py       PID / 进程管控
└── hub/            多进程协作（一个 hub 拉起 N 个独立引擎进程）
```

```text
业务实现已归位到这里（不再是 engine 的子目录）
─────────────────────────────────────────────
app/runtime/        调度与执行，不含业务语义
├── scheduler.py    SchedulerComponent（原 component/scheduler.py）
├── executor.py     WorkerComponent（原 component/worker.py）
├── event_bus.py    EventBus / StandardEvents（原 engine/event.py）
├── task.py         ITask / TaskStatus（原 engine/task.py）
├── tasking/        TaskRuntime / TaskSpec / QuantTask（原 engine/tasking/）
└── bus/            FileBus 跨进程事件总线（原 engine/bus/）

app/modules/        业务能力层
├── market/         MarketService
├── collection/     CollectionService（K线 + 日频截面 + 完整性 三合一）
│                   spec.py / schedule.py / integrity.py（原 engine/collect/、integrity.py）
├── trading/        TradingService + live_broker.py
├── risk/           RiskService + 闸门链
├── portfolio/      PortfolioService + Reservation
├── news/           NewsService + NewsCenter
├── strategy/       StrategyComponent + 策略注册表
├── factor/         因子 / 指标 / 挖掘（原 feature/）
├── universe/       股票池 + 打分排序（原 alpha/）
└── backtest/       回测引擎（原 research/）
```

重构前后同一个类的路径对照：

<!-- check_doc_paths:allow-stale-paths -->
| 类 | 旧路径 | 新路径 |
|---|---|---|
| `MarketService` | `app.engine.component.service.market` | `app.modules.market.service` |
| `CollectionService` | `app.engine.component.service.collection` | `app.modules.collection.service` |
| `TradingService` | `app.engine.component.service.trading` | `app.modules.trading.service` |
| `RiskService` | `app.engine.component.service.risk` | `app.modules.risk.service` |
| `PortfolioService` | `app.engine.component.service.portfolio` | `app.modules.portfolio.service` |
| `NewsService` | `app.engine.component.service.news` | `app.modules.news.service` |
| `StrategyComponent` | `app.engine.component.strategy` | `app.modules.strategy.component` |
| `SchedulerComponent` | `app.engine.component.scheduler` | `app.runtime.scheduler` |
| `WorkerComponent` | `app.engine.component.worker` | `app.runtime.executor` |
| `IBaseComponent` | `app.engine.component.base` | `app.engine.component` |

---

## 七、合并：从 11 个组件到 7 个

### 7.1 三个采集组件 → 一个 `CollectionService`

| 原组件 | 变成 | 方法后缀 | 属性前缀 |
|---|---|---|---|
| `DataCollectorComponent` | K 线单元 | `_kline` | `kline_*` |
| `ExtDataComponent` | 日频截面单元 | `_cross` | `cross_*` |
| `DataGuardComponent` | 完整性单元 | `_guard` | `guard_*` |

**为什么要合并**：它们本来就是一件事的三个面——**让库里的数据变完整**。
分成三个平级组件时，用户的心智模型只有一个"采集服务"，
实际却要在三个地方分别开关，漏一个就还在联网。
合并后对外是一个服务、一个状态、一份快照。

**为什么方法名要加后缀**：三个源类里 `_tick` / `_release` / `snapshot` /
`health_check` / `is_busy` 各重复 2~3 次。合并进同一个类必须区分，
否则后面的定义会**静默覆盖**前面的——这类覆盖不报错，
只会让"资金流采集"悄悄变成"什么都没做"。

**没被合并掉的**：节拍。K 线按任务频率（分钟级到日级）、
截面按盘前盘后各一次、完整性启动后延后首检——
节拍不同是它们当初被写成三个组件的唯一正当理由，所以完整保留。
`on_start` 按 `K线 → 截面 → 完整性` 起，`on_stop` 严格逆序。

### 7.2 两个交易组件 → 一个 `TradingService`

| 原组件 | 变成 | 方法后缀 |
|---|---|---|
| `LiveGatewayComponent` | 网关单元 | `_gateway` |
| `TradingStoreComponent` | 落库单元 | `_store` |

券商网关与落库是"发出去的订单"与"记下来的账"，本来就是同一笔交易的两面。

合并后出现一个需要说清的区别：

| 开关 | 含义 | 回测/模拟下 |
|---|---|---|
| `service.enabled` | 服务是否装配 | 仍然装配 |
| `gateway_enabled` | 是否**真的连券商** | 关（非 LIVE 一律关） |
| `store_enabled` | 是否落库 | 开（只读档位也要留审计记录） |

只读档位不下单，但**也要能落库**——否则"这轮跑了哪些任务"永远查不到。

### 7.3 合并是怎么保证不出错的

`scripts/merge_services.py` 是**一次性迁移工具**，用 AST 做机械搬运：

1. 定位源类，按源码切片取出每个方法（含装饰器与上方注释块）；
2. 按重命名表改 `self.<属性>` 与方法名（正则带 `\b`，保证幂等）；
3. 拼进新类，**方法体逐字节原样保留**；
4. 校验：语法可编译、每个源方法都以新名落地、搬来的方法体无残留旧名、
   **重名方法直接报错**（合并类最凶险的一类错误）。

它**不改写任何方法体内部的逻辑**，改的只有名字。
复合生命周期（`on_initialize` / `on_start` / `on_stop` / `snapshot` / `health_check`）
是手写模板里的语义决策，不适合机器推导。

> 源组件合并后已删除，脚本无法再跑（会明确报"源组件已不在"而不是崩溃）。
> 需要复核映射关系时用 `git show <合并前的提交>:app/engine/component/collector.py` 取回。<!-- check_doc_paths:allow-stale-paths -->
> 服务文件现在由人工维护，**不要再 `--apply` 覆盖回去**。

### 7.4 合并带出来的一个真问题：进度条

三个组件合并后，基类那一 `self._progress` 成了**共享变量**。
K 线单元（每分钟一轮）会把日频截面单元（一天两轮）的进度刷成 "1/68"，
面板上就再也看不到截面跑到哪了——而"看不到进度"恰好是进度功能存在的理由
（看不到就以为卡死，以为卡死就重启，重启又丢一轮数据）。

处理办法：三个单元各发一份 `Progress`（`_progress_kline` / `_progress_cross` /
`_progress_guard`），`start_progress` / `advance_progress` / `finish_progress`
按**调用方方法名里的单元后缀**路由。

为什么不改搬来的方法体：那样改了生成产物，下一次 `--apply` 会被覆盖回去。
后缀路由认不出归属时退回服务级进度——行为与合并前一致，不会更差。
有 `test_progress_routing` 守着。

---

## 八、新增的 `RiskService`

### 8.1 它解决什么

重构前风控只有**任务级**的 `RiskChain`（每个策略自己配的规则）。
缺的是"整个账户"这一层：今天已经亏了 3%、刚才连着 5 笔被拒、
行情源明显不对——这些都不是某个策略的问题，而是**整个账户该不该继续开仓**。

### 8.2 两级风控的关系

| 层级 | 管什么 | 在哪 |
|---|---|---|
| 任务级链 | "这个策略该开多大" | `task.risk_chain` |
| 全局闸门 | "整个账户还能不能开" | `RiskService.check` |

两者**串联**：`_process_signal` 里先过全局闸门、再过任务级链，
`scale` 取两者的**更小值**。

### 8.3 关键 API

```python
risk = engine.require_service("risk_service")

risk.gate            # 一个可调用对象（不是整个服务）
risk.check(signal, state, ctx=..., account=...)   # → RiskVerdict
risk.halt("手动熔断")   # 一键熔断：只挡新信号，不砍已有持仓
risk.resume()
risk.is_halted         # 属性，不是方法
```

平仓信号同样会被熔断挡住（本服务不区分方向）——这是刻意的：
熔断是"我还没想清楚"，不是"自动清仓"。要清仓请显式操作，
别让一个按钮同时具备"停手"和"砍仓"两种语义。

`gate` 返回的是 `self.check` 的绑定方法而**不是整个服务**——
一个策略不该有停掉全场的权力。

### 8.4 三个刻意的取舍

1. **熔断状态纯内存，不落库。**
   持久化的典型故障是"上次熔断忘恢复，下次起来永远不开仓"——
   这种故障查起来极其昂贵，因为它看起来像"市场没有信号"。

2. **风控永不抛异常。**
   风控崩溃导致"完全不交易"比"这一笔没拦住"严重得多。
   所以：熔断直接否决，规则自身异常则**放行并留痕**。

3. **熔断时 `health_check()` 仍然报健康。**
   熔断是人为且预期的状态。标成不健康会让监控一直告警，
   最后的结果是没有人再看那个告警。

### 8.5 闸门怎么下发到任务

闸门在 `TaskRuntime.add()` 里下发，而不是靠 `build_task` 传参：

```python
if getattr(task, "global_gate", None) is None:
    gate = self.current_risk_gate()
    if gate is not None:
        task.global_gate = gate
```

**运行时新增的任务不走 `build_task` 那条路**，只在那条路上挂闸门会漏掉它们。

被否决的订单仍然会造一个 `REJECTED` 状态的订单对象（`_reject()`）。
否决是**应当被审计的事实**——只打日志，则"今天拦了几笔、因为什么拦"永远为空。

---

## 九、新增的 `WorkerComponent`

一次性作业（面板的"立即采集""立即体检"）以前直接 `threads.deferToThread`，
**没有并发上限**。连点几下"立即体检"就会把数据源打到限流，
而限流之后的失败又会被记成"数据源不靠谱"，把权重降下去。

`WorkerComponent` 用**有上限的槽位**替代它：

```python
worker = engine.get_component("worker")
d = worker.submit("立即体检", fn, *args)     # 返回 Deferred
d.addErrback(...)                            # 槽位满时这里会收到错误
```

槽位满时返回的是**已失败的 Deferred**——显式的"现在忙"，
而不是把请求悄悄排队、让用户以为它在跑。

`app/control/control_plane.py` 的 `_submit_job()` 统一走这里，
没有工作单元时才退回线程池。

---

## 十、新增的 `PortfolioService`

服务名 `portfolio_service`，提供 `portfolio.view`（账户聚合）与
`portfolio.quota`（额度预留）两个能力。

### 10.1 它补的是两个洞

**洞一：系统里没有"账户"这个概念。**

`Portfolio` 是 **per-task** 的——每个任务一本账。于是不存在任何地方能回答
"整个账户现在有多少钱、总回撤多少"：面板只能一个任务一个任务地看，
全账户回撤没人算，风控拿到的 `account` 也只是单个任务的账本。

这里有个**不能走捷径**的点：**全账户回撤无法从各任务的回撤推出来**。
任务 A 亏 10%、任务 B 赚 10%，两个任务的 `drawdown` 都是 10%，账户却可能
压根没回撤；反过来各任务都没回撤，账户也可能在回撤（钱在任务之间腾挪的
时点错配）。所以服务**自己维护聚合权益的峰值**，不做任何"取最大值/平均值"
的近似。`test_each_task_down_but_account_not` 专门钉这条。

**洞二：并发下单会超额度。**

风控是在单个任务的账本上做判断的，它看不见别的任务刚放行了一单。实盘所有
任务共享一个券商账户：任务 A 放行 8 万 → 任务 B 放行 8 万 → 账户一共 10 万，
两笔都发出去就是 16 万。`task._has_open_order` 只挡"同一任务同一方向的
重复单"，挡不住跨任务。

### 10.2 额度预留

在风控放行之后、真正报单之前，把预计占用从全账户额度里扣住；订单落到终态
（成交/撤单/拒单）再释放。这样第二个任务再过风控时，看到的"可用额度"
已经把第一个任务的在途单算进去了。

```python
pf = engine.get_component("portfolio_service")

pf.reserve("t1", "000001", 50_000.0, order_id="o1")   # 成功返回 "r1"，额度不够返回 None
pf.bind_order("r1", "o1")                             # 订单号后置绑定
pf.release_order("o1")                                # 按订单释放（幂等）
pf.release_task("t1") / pf.release_all()
pf.aggregate()["drawdown"]                            # 全账户回撤
```

三个不做就会出事的取舍：

| 取舍 | 不做会怎样 |
|---|---|
| **预留必须有 TTL**（默认 180s） | 一笔丢失的回报会永久占住额度，症状是"跑着跑着就不下单了"，而**日志里没有任何错误** |
| **释放必须幂等** | 成交回报 + 主动查询各来一次，重复释放会把额度还多，预留机制形同虚设 |
| **只预留买入** | 现金是跨任务共享的；持仓是 per-task 独立账本，任务 A 卖不掉任务 B 的仓位，不存在跨任务卖超 |

`PARTIAL` 刻意**不释放**：还有一部分挂在市场上，那部分资金仍在途。
`SUBMITTED` 同理。终态只有 `FILLED` / `CANCELLED` / `REJECTED`。

额度上限没配时（默认）自动取**聚合账户的可用现金**，于是"下单占用的钱
不能超过账户里有的钱"这条底线一行配置都不写也成立。聚合不到任何任务时
返回"不限制"——看不见账户的时候不该瞎拦。

### 10.3 与 `RiskService` 的分工

| | `RiskService` | `PortfolioService` |
|---|---|---|
| 回答的问题 | 这一笔该不该开、开多大 | 整个账户还能不能开 |
| 视角 | 单任务的账本 + 全局熔断 | **跨任务**的在途资金 |
| 时机 | 信号 → 订单之前 | 风控放行 → 报单之前 |

两者串联，缺一不可：风控放行不代表账户还有位置，账户有位置也不代表
这一笔该开。

---

## 十一、新旧对照表

### 11.1 组件

| 旧注册名 | 旧文件 | 现在 |
|---|---|---|
| `data_collector` | `component/collector.py` | `collection_service` 的 K 线单元 |
| `ext_data` | `component/extdata.py` | `collection_service` 的截面单元 |
| `data_guard` | `component/dataguard.py` | `collection_service` 的完整性单元 |
| `market_center` | `component/market.py` | `market_service` |
| `live_gateway` | `component/live_gateway.py` | `trading_service` 的网关单元 |
| `trading_store` | `component/trading_store.py` | `trading_service` 的落库单元 |
| `news_center` | `component/news.py` | `news_service` |
| — | — | **`risk_service`（新增）** |
| `task_scheduler` + `timerV2` | `component/timer.py` | `scheduler` |
| `strategy_manager` | `component/strategy.py` | `strategy` |
| — | — | **`worker`（新增）** |
| `monitor` | `monitoring/` | `monitor`（PLAIN，不变） |

### 11.2 已删除的文件

<!-- check_doc_paths:allow-stale-paths -->
```
app/engine/component/collector.py
app/engine/component/extdata.py
app/engine/component/dataguard.py
app/engine/component/market.py
app/engine/component/live_gateway.py
app/engine/component/trading_store.py
app/engine/component/news.py
app/engine/component/timer.py
```

### 11.3 旧名兼容层

约 30 处 `engine.get_component("market_center")` 不可能一次改完，
所以留了一层**别名**：

```python
# registry.py
LEGACY_ALIASES = {
    "market_center": "market_service",  "ext_data": "collection_service",
    "data_collector": "collection_service", "data_guard": "collection_service",
    "live_gateway": "trading_service",  "trading_store": "trading_service",
    "news_center": "news_service",      "strategy_manager": "strategy",
    "task_scheduler": "scheduler",      "timerV2": "scheduler",
}
```

服务侧的别名在 `ServiceRegistry` 里生效；非服务组件（策略/调度）的别名
由 `BaseQuantEngine._name_aliases` 兜住。

**这是兼容层，不是推荐写法。新代码一律用新服务名**，
别名会在后续版本里逐步清掉。

### 11.4 引擎新增的 API

```python
engine.services                          # ServiceRegistry
engine.get_service(name)                 # 按名取服务
engine.require_service(name)             # 取不到就抛
engine.service_by_capability(Cap.RISK_GATE)
engine.ordered_component_names()         # 依赖算出来的启停顺序
engine.snapshot()["services"]            # 服务注册表快照（含缺失依赖）
engine.snapshot()["components"][n]["kind"]        # "service" / "strategy" / …
engine.snapshot()["components"][n]["kind_label"]  # "服务" / "策略" / …
```

---

## 十二、我要加东西，放哪

**加一个服务**（比如"因子服务"）：

```
1. 在 app/modules/<新模块>/service.py 下新建，继承 ServiceComponent
   （ServiceComponent 本身在 app/engine/capability.py）
2. 声明 name / provides（用 Cap 常量）/ depends_on / restart_policy
3. 需要别人的能力时用 self.need("xxx")（取不到即抛）或 self.optional("xxx")
4. 在 builder.py 的服务层注册（注册位置不再影响启停顺序）
```

> 判定放哪：`engine/` 只放**容器与契约**，`runtime/` 只放**怎么跑**，
> 剩下的业务一律进 `modules/`。如果一个东西既不提供业务能力、
> 也不决定执行节拍，它多半属于 `app/common/` 或 `app/domain/`。

**加一个策略**：继承 `StrategyComponent`（在 `app/modules/strategy/component.py`），
填 `required_services`（缺一个就起不来，这是刻意的），实现 `on_bar()`。

**加一个定时任务**：不要新建组件。用
`engine.get_component("scheduler").start_timer(interval, event_name)`。
同一个 `(事件名, 间隔)` 重复注册会被忽略——避免节拍翻倍把数据源打到限流。

**加一次性作业**：交给 `worker.submit(...)`，别直接 `deferToThread`。

---

## 十三、约定与红线

1. **服务层不消费别人的输出。** 要消费 → 那是策略或工作单元。
2. **服务之间互相找人用能力，不用名字。** `first_by_capability(Cap.ORDER_ROUTE)`，
   不写 `get_component("trading_service")`。
3. **启停顺序不许靠注释约定。** 加依赖就写进 `depends_on`，让引擎去算。
4. **策略层依赖缺失必须抛错，不许降级。**
5. **风控不许抛异常**，异常一律按放行处理并留痕。
6. **被否决的订单必须留痕**（REJECTED 订单对象），不能只打日志。
7. **合并产物的方法体不要手改。** 要改就改 `scripts/merge_services.py`
   里的重命名表与头部模板，再重新生成。
8. **跨任务共享的资源（券商账户的现金）必须有预留。**
   只在单任务账本上做判断，等于默认"别的任务不存在"。

---

## 十四、附录：目标架构建议 → 本项目现状

这一节回答"外面那套通用量化引擎架构建议，我们做到了哪一步"。
**✅ = 已落地，⚠️ = 部分落地，❌ = 未做。**

### 14.1 容器与调度

| 建议 | 现状 | |
|---|---|---|
| `EngineApp` 顶层容器：装配/注册/生命周期/优雅关闭 | `BaseQuantEngine` + `builder.build_engine()` | ✅ |
| `EngineScheduler` 调度中枢：定时 + 事件触发 | `SchedulerComponent`（定时器 + 优先级队列合并为一个） | ✅ |
| `registry.py` 只是简单字典 | `ServiceRegistry`：能力登记 + 别名 + 拓扑排序 + 注入。**比建议更进一步** | ✅ |
| Scheduler 管调度，EventBus 管分发，两者不混 | `app/runtime/event_bus.py` 的 `EventBus` 与调度器完全独立 | ✅ |

### 14.2 Component 层

| 建议的组件 | 本项目 | |
|---|---|---|
| `MarketComponent` | `MarketService`（`market.bar_feed` / `market.quote` / `market.watchlist`） | ✅ |
| `CollectionComponent` | `CollectionService`（K线 + 日频截面 + 完整性三单元） | ✅ |
| `StrategyComponent` | `StrategyComponent`（持有 `TaskRuntime`） | ✅ |
| `RiskComponent` | `RiskService`（`risk.gate` 全局闸门 + 一键熔断） | ✅ |
| `TradingComponent` | `TradingService`（券商网关 + 落库二单元） | ✅ |
| `PortfolioComponent` | `PortfolioService`（账户聚合 + 额度预留） | ✅ |
| `BacktestComponent` | `app/modules/backtest/`：由面板/CLI 直接调函数，**不是引擎组件** | ❌ |
| 共享基础设施：EventBus | `app/runtime/event_bus.py` | ✅ |
| TaskQueue / Worker（并发与积压控制） | `WorkerComponent`（有槽位上限） | ✅ |
| Provider / Broker Registry | `app/providers/market` 数据源注册表 + `app/providers/broker` 网关注册表 | ✅ |
| Observability（日志/指标/健康检查/告警） | `app/monitoring`（面板 + 快照 + API） | ✅ |
| Persistence（行情库 / Raw 原始 / 交易记录） | `app/data`（行情 + 交易审计 + 原始归档） | ✅ |

### 14.3 三条核心业务链路

**实时自动交易链路** —— 建议里强调的四点，逐条对照：

| 建议 | 现状 |
|---|---|
| 行情**事件**触发策略，而非 Scheduler 高频轮询 | `MarketService` 发 `BAR_RECEIVED` → `StrategyComponent.on_bar` 订阅 | ✅ |
| 风控必须在订单提交**前**执行 | 全局闸门先于任务级链，`scale` 取更小值 | ✅ |
| 涉及账户/持仓/资金的检查要**避免并发下单导致超限** | `PortfolioService` 的额度预留（放行即占位，终态释放 + TTL 兜底） | ✅ |
| 订单提交成功 ≠ 成交，状态由券商回报或主动查询更新 | `OrderStatus` 六态 + `live_broker.apply_order_update` + 定期对账 | ✅ |
| 成交/拒单/撤单都要形成事件 | `ORDER_CREATED/FILLED/CANCELLED` + `SIGNAL_REJECTED` 四事件，`TradingService` 订阅落库 | ✅ |
| 订单挂久了要处理（超时撤单/告警） | 有 `cancel_open_orders`，但**没有超时自动处理** | ❌ |

**历史数据采集链路**（Scheduler → 增量计划 → 数据源路由 → 抓 → 存 Raw →
标准化 → 质量检查 → 入库 → 更新任务状态）：

`CollectionService` 三单元已覆盖"抓 + 质量检查 + 入库"，增量计划与数据源
路由分别在 `app/modules/collection` 与 `app/providers/market`。
**任务由调度器启动**这一点是 ✅，但下面这条不是：

| 建议 | 现状 |
|---|---|
| 用交易日历驱动，非交易日不跑，任务带幂等键 | `app/domain/calendar.py` 存在，但 `SchedulerComponent` **只用 `TIMER_3MIN`，完全没接日历** | ❌ |

**定时任务链路**（开盘前准备 / 盘中监控 / 收盘后采集 / 每日结算四类节拍）：

| 建议 | 现状 |
|---|---|
| 四类节拍按交易日历触发 | 只有统一的 3 分钟定时器，没有按交易时段的节拍分类 | ❌ |

### 14.4 "常驻 / 任务驱动 / 事件驱动"的归类

| 类别 | 建议的例子 | 本项目 |
|---|---|---|
| 常驻 | EventBus、实时行情连接、回报监听 | `EventBus`、`MarketService`、`TradingService` 的回报轮询、`CollectionService` 的定时器 |
| 任务驱动 | 历史K线采集、每日结算、定期财务更新、回测 | `CollectionService` 的 K线/截面单元、回测作业（`app/modules/backtest`，非组件） |
| 事件驱动 | 行情→策略、信号→风控、通过→交易、成交→持仓 | `BAR_RECEIVED` → `on_bar` → 风控 → 预留 → 报单 → `ORDER_FILLED` → 落库 |

三条链路都已经按这个分类跑着，只是"任务驱动"那一类还没接交易日历。

### 14.5 目录结构：建议 vs 现状

建议是**按域分目录**：

```
components/market/component.py + service.py + providers/ + streams/
components/collection/component.py + service.py + collectors/ + planners/
...
```

本项目是**按层分目录**（2026-10 目录重构后，见 [STRUCTURE.md](STRUCTURE.md)）：

```
app/
├── engine/      容器层：装配 / 注册 / 生命周期，不含业务
├── runtime/     运行时层：事件总线 / 调度器 / 执行池，不含业务
│   ├── events/      EngineEvent 契约与 EventBus
│   ├── scheduling/  规则表 + 能力路由
│   ├── tasking/     EngineTask 状态机与队列
│   └── workers/     分池线程池 + 处理器注册表
├── modules/     业务模块层：market / collection / universe / factor /
│               strategy / risk / portfolio / trading / news / backtest
├── providers/   外部系统：行情源适配器 / 券商网关
└── data/        持久化
```

**为什么没有按建议重排**：两者语义等价（一个 `component/service/market.py`
与一对 `market/{component,service}.py` 表达的是同一件事）。按**层**分目录
比按域分目录多一层保障：**"这段代码有没有越界"这个问题由目录本身回答**——
`runtime/` 里出现 `import app.modules.*` 就是分层被破坏，一眼看得出；
按域分目录则要用工具去查。

**目录重构之后**：`app/engine` 从「42 个文件 13441 行什么都有」
收窄成「16 个文件 4532 行的纯容器」，业务归到 `app/modules/`。
旧 → 新逐条对照见 [STRUCTURE.md §四](STRUCTURE.md)。

### 14.6 实施顺序（建议的四阶段，我们在哪）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 一 | 引擎骨架：`EngineApp`、`IBaseComponent`、注册、生命周期、简单 Scheduler | ✅ |
| 二 | 数据闭环：采集组件、数据源适配器、质量检查、持久化 | ✅ |
| 三 | 回测闭环：接入 Backtrader 作**回测执行器** | ✅（`app/modules/backtest` 用 `bt.Cerebro` 当执行器，策略本身是自研 `IBaseStrategy` 桥接进去的，正是建议里说的用法） |
| 四 | 实盘能力：实时行情、风控、交易、账户同步、故障恢复、告警 | ✅（账户级聚合与额度预留即本次补齐的部分） |

### 14.7 仍未做的三件事（按优先级）

1. **调度层接交易日历** —— 非交易日不跑、"每天 9:30"不等于每天都执行、
   四类节拍（开盘前/盘中/收盘后/结算）。对 A 股是必需项，不是优化项。
   > **部分缓解**：行情侧已在 `market.py:264` 加了交易日/时段闸门，
   > 非交易时段不再轮询、不再用旧 bar 驱动策略。
   > 但 `SchedulerComponent` 自身的定时器节拍**仍未接日历**（见 §14.3、
   > `RUNTIME.md` §2.3 末尾的 `TIMER_3MIN` 说明）。
2. **回测组件化** —— 让 `BacktestComponent` 可装配、可观测、走执行池的槽位限流
   （原先连点"跑回测"会打满线程池，把行情采集一起挤死）。
   > **部分缓解**：本轮事件化后新增 `Backtest Worker Pool`（容量 1，
   > 与 Market / Research 池**物理隔离**），回测跑满也吃不掉行情的线程。
   > 但 `backtest.run` / `backtest.sweep` 的处理器**刻意没有注册**——
   > 宁可让任务进死信把"还没接上"暴露出来，也不挂一个假成功的空壳。
   > 等回测组件化时补上，接通成本是注册一个处理器，不需要改架构。
3. **订单超时未成交的处理** —— 挂单久了要撤单或告警。
   > **已修**：`LiveBroker.sweep_stale_orders()` 接管订单生命周期终点，
   > 由 `TradingService._poll_once` 定期驱动（不新增定时器）。
   > 三步走：① 回查券商对齐状态（回报丢失是主因，不撤单）；
   > ② 挂满 `order_timeout × 10` 才请求撤单（限价单挂着等成交是正常的）；
   > ③ 撤单后无回报或回查用尽 → 本地置终态并打 ERROR，
   > 恢复防堆单判定与额度释放。撤单失败另加 3 次退避重试。

### 14.8 本轮架构体检补修的其他项

4. **`RunMode` 双定义**（`engine/settings.py` 与 `domain/types.py` 各一份）——
   `builder` 用前者、`base`/`scheduler` 用后者，跨类 `==` 恒为 False，
   直接导致 `base.py` 的"回测装空闲自动停止看门狗"判断永不成立，
   回测跑完 reactor 永久阻塞。**已合并为单一真源**（domain 版本，
   `settings.py` 保留别名以兼容旧导入）。
5. **限速/熔断非线程安全** —— 采集跑在线程池里，`_throttle` 读改写
   `_LAST_CALL` 无锁，N 个线程同时发车，限速与熔断在并发下失效。
   **已改"预约时刻"模型**（锁内领 slot、锁外睡），熔断计数与惩罚状态加锁。
6. **`_ff_snapshot` 缓存击穿** —— 68 只票同时 miss 会一起翻 100+ 页。
   **已加 in-flight 抢占**，只让一个线程真去取，其余等待结果。
7. **过期额度只在 `reserve` 里清理** —— 最后一笔预留过期后若再无新买单，
   `is_busy()` 永远为真、可用额度被永久低估，引擎再不判定空闲。
   **已改为读路径也清理**（`reserved` / `available` / `is_busy`）。
8. **默认风控链缺 A 股硬约束** —— T+1 / 涨跌停 / 可卖量一条都没配。
   `build_risk_chain(enforce_astock=True)` 会自动补齐（用户显式配过的
   以用户参数为准，不重复挂），实盘/模拟盘启用，回测不动。

### 14.9 事件驱动改造（本轮）

建议里"Engine 统一指挥 → EventBus → Scheduler → Worker"这一条，此前
**只做到了一半**：`EventBus` 有定义，但服务之间仍是直接调用，总线是个空壳。
本轮把它补成真正的中枢，完整设计与规则全表见
[EVENT_TASK.md](EVENT_TASK.md)。这里只记**为什么必须这么切**：

| 概念 | 语义 | 不切的后果 |
|---|---|---|
| **事件** | 已经发生的事实。不可执行、可忽略、可重复投递 | 事件里塞执行逻辑 → 一个订阅者抛异常，发布者跟着炸 |
| **任务** | 待完成的工作。可执行、必须有终态、必须幂等 | 任务当事件发 → 没人领就是静默丢失，日志里什么都没有 |

调度器是两者之间**唯一**的转换点：订阅事件 → 按规则表匹配 → 生成
`EngineTask` → 按能力路由投到某个队列 → 由对应执行池领走。

**为什么执行池要按职能分开**：所有池共享一个线程池时，一次批量回测
（几十个参数组合 × 几千根 K 线）会把线程全部占满，而行情采集排在后面
排队——表现为"盘中 K 线延迟"，但根因在几百行外的回测代码里。
分池之后回测跑满只能吃掉 `Backtest` 池（容量 1），Market 池不受影响。
现有 8 个池：`system / market / research / strategy / trading /
backtest / maintenance / default`。

**两个必须守住的防环机制**（都是踩过的坑）：

1. **回声防护** —— 调度器发 `task.*` 事件，如果规则表里也有"收到
   `task.started` 就建任务"，就会 `任务 → 事件 → 任务` 无限自激。
   `TASK_ECHO_EVENTS` 里的事件一律不建任务。
2. **幂等键** —— 事件重复投递是常态（重试、重放、多订阅者）。
   没有 `dedup_key` 时，一次事件重投会级联放大成 N 个重复任务：
   重复采集烧配额，重复下单是实盘事故。

**`trade.execute` 强制 `max_retries=0`**：这是唯一一个不允许自动重试的
任务类型。网络抖动重试一次采集只是浪费配额，重试一次下单可能买两遍。
`EngineTask.__post_init__` 里硬性覆盖，调用方传什么都无效。
