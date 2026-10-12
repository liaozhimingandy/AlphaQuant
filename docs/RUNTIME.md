# 运行原理：从按下回车到一笔成交

> 本文回答"系统到底是怎么跑起来的"。写法是**跟着代码走一遍**——每个环节都标注
> 了 `文件:行号`，可以直接跳过去看实现。
>
> 想看"为什么这么设计"请读 `ARCHITECTURE.md` 与 `ENGINE_ARCHITECTURE.md`；
> 想看"怎么加东西"请读 `EXTENDING.md`。

---

## 0. 三句话总览

1. **一个进程 = 一台引擎**：进程里跑一个 Twisted reactor，上面挂若干"组件"，
   组件之间靠**事件总线**通信、靠**服务注册表**拿依赖。
2. **一次运行 = 一个 run_id**：引擎启动那一刻生成，贯穿日志、快照、订单、成交，
   事后复盘全靠它串起来。
3. **策略不知道数据从哪来**：它只订阅 `BAR_RECEIVED` 事件；行情是回放还是实时
   轮询，由 `MarketService` 的 `mode` 决定，策略代码一行都不用改。

---

## 1. 启动链路：`python main.py start` 之后发生了什么

```
main.py:20  cli()
  └─ app/cli/base.py:47  @click.group  命令组根
       └─ app/cli/__init__.py:32-41  导入 9 个 *_cmds 模块（导入即注册）
            └─ app/cli/engine_cmds.py:291  start_cmd()
                 ├─ :304  daemon.service_cmd(...)  拼命令行
                 └─ :328  daemon.start(cmd)
```

### 1.1 前台 vs 后台

| 命令 | 入口 | 行为 |
|---|---|---|
| `engine` | `engine_cmds.py:33` | 前台跑，回测模式跑完自动停 |
| `serve` | `engine_cmds.py:130` | 前台常驻，`:150` 装配、`:247` 启动 |
| `start` | `engine_cmds.py:291` | 拉起**独立进程**常驻 |

`start` 走的是守护进程路径（`daemon.py:171-270`）：

1. 清掉残留的停止标志文件（避免上次没清干净导致新进程秒退）
2. `Popen` 拉子进程（`:215`，Windows 参数见 `_popen_kwargs` `:95-123`）
3. 写 PID（`:247`）
4. `sleep(0.8)` 探测"秒退"——**以子进程自报的 PID 为准**（`:253-261`），
   因为 Windows 下 launcher 的 PID 和真正的 python 进程不是同一个
   （`service_runner.py:134-150` 用 `os.getpid()` 覆盖回来）

> **停止机制**：Windows 没有 SIGTERM，所以停止靠**写标志文件**
> （`daemon.py:306`）。子进程里 `service_runner.py:63-93` 用 `LoopingCall`
> 每 0.5s 查一次这个标志，命中就调 `engine.stop(graceful=True)`。
> 这个 watcher 必须在 `engine.start()` **之前**装好（`:214`）——
> 因为 `start()` 内部会 `reactor.run()` 阻塞到进程结束。

### 1.2 装配：`app/engine/builder.py`

`build_engine()`（`builder.py:137-432`）做两件事：读 `tasks.json`、把组件注册进引擎。

| 行 | 组件 | 构造参数来源 |
|---|---|---|
| `:219` | `SchedulerComponent()` | 无参 |
| `:226` | `RiskService` | `tasks.json` 的 `risk` 段 |
| `:237` | `PortfolioService` | `settings.PORTFOLIO_BUDGET` / `PORTFOLIO_RESERVE_TTL` |
| `:299` | `CollectionService` | 采集三单元合一（见 §2.2），`:298` `set_engine(engine)` |
| `:314` | `MarketService` | `syms` / `MARKET_MODE`(默认 replay) / `MARKET_INTERVAL` |
| `:327` | `TradingService` | `gateway_enabled=(run_mode==LIVE)`、`:326` 注入 engine |
| `:339` | `NewsService` | `NEWS_ENABLED` |
| `:354` | `StrategyComponent` | `cfg["tasks"]`，`:353` 注入 `gateway_provider` |
| `:363` | `WorkerComponent` | `WORKER_ENABLED` / `WORKER_CAPACITY` |
| `:381` | `MonitorWebComponent` | `MONITOR_HOST/PORT` |
| `:392` | `control.load_runtime_tasks()` | 重放上次运行期新增的任务 |

**这些 `register_component` 的书写顺序不影响实际启动顺序**（文件头注释
`builder.py:20-31` 专门解释了这一点）。真正的顺序由引擎算出来，见 §3。

### 1.3 进入主循环

`service_runner.py:218` 调 `engine.start()` → `base.py:265` `reactor.run()` 阻塞。
此后所有事情都发生在 reactor 的回调里。

---

## 2. 三条链路

这是理解系统的核心。三条链路互不干扰，各自独立运转。

### 2.1 实时自动交易链路（行情 → 信号 → 订单 → 账本）

```
① 行情产生
   MarketService.on_start            market.py:211-221
     └─ LoopingCall(self._tick).start(interval)      # 默认 0.05s
   MarketService._tick               market.py:230-239
     ├─ mode=replay → _tick_replay   market.py:241-254   # 从内存队列逐根 pop
     └─ mode=poll   → _tick_poll     market.py:256-276
          ├─ 非交易时段直接跳过                 market.py:264
          └─ yield deferToThread(_fetch_latest) market.py:270   # DB/网络 IO 走线程池

② 推到总线
   MarketService._emit               market.py:302-319
     └─ event_bus.publish(BAR_RECEIVED, bar=bar)
        （dedupe=True 按 _last_bar_date 去重，避免同一根 bar 反复驱动策略）

③ 策略接住（订阅关系在初始化时就建好）
   StrategyComponent.on_initialize   strategy.py:101
     └─ subscribe_event(BAR_RECEIVED, self._on_bar)
   StrategyComponent._on_bar         strategy.py:175-182
     └─ runtime.on_bar(bar)

④ 广播给关心这只票的任务
   TaskRuntime.on_bar                tasking/runtime.py:171-185
     └─ 按 bar.symbol 查 _by_symbol 索引 → 逐个 task.advance()
   QuantTask.on_bar                  tasking/task.py:273-305
     ├─ _roll_day / series.append / broker.set_price
     ├─ 预热期检查、持股超时强平
     └─ signal = strategy.on_bar(ctx, state) → _process_signal

⑤ 信号变订单（**四道闸门，全部在下单之前**）
   QuantTask._process_signal         tasking/task.py:406-544
     ├─ ① 全局闸门 global_gate      :417-435   （RiskService 注入，异常时放行）
     ├─ ② 任务级风控链 risk_chain   :438-442
     ├─    scale 取两者更小值        :444
     ├─ ③ 已有未结订单则跳过         :456-460   （防每根 K 线重复报单）
     ├─ ④ 跨任务额度预留 quota      :483-498   （失败则 quota_exhausted 否决）
     └─ broker.create_order → broker.submit

⑥ 下单与回报
   模拟：execution/broker.py:94-124    即时成交，apply_fill 入账
   实盘：execution/live_broker.py:120-180  本地前置校验 → 网关 → 返回 SUBMITTED
         （网关通信失败**不重试**，因为可能已经报到柜台了）

⑦ 回报回来
   TradingService._tick_poll         trading.py:426-432
     └─ deferToThread(self._poll_once)
   TradingService._poll_once         trading.py:479-530
     ├─ gateway.poll_fills()  → 按本地单号找 broker → broker.on_fill
     └─ gateway.poll_order_updates() → broker.apply_order_update
   QuantTask._on_broker_update       tasking/task.py:213-232
     └─ _count_fill（按累计量差值防重复计数）→ _release_quota → _book_order
```

**关键点**：被风控否决的信号**不会消失**——`_reject`（`task.py:387-404`）仍然
造一条 `REJECTED` 订单并发出事件，所以事后能查到"当时为什么没成交"。

### 2.2 历史采集链路（`CollectionService`，三单元合一）

服务入口 `collection.py:284-287` 初始化三个单元，`:290-296` 按
`kline → cross → guard` 启动（K 线先供数据，体检最后跑），`:299-303` 逆序停止。

| 单元 | 触发 | 频率 | 幂等保证 |
|---|---|---|---|
| **kline** | `:553-567` 按频率**分档**起定时器 | 间隔即任务频率（默认 5m），`:577-578` 明确不套下限 | `_busy_kline[interval]` 标志，上一轮没跑完就跳过（`:609-615`） |
| **cross**（日频截面） | `:942-951` | `max(30, cross_interval)`（默认 600s）；市场级 `max(300, ...)` | `_cross_slots[today]` 集合，**每个采集窗口每天只做一次**（`:1018-1021`）；日频另用 `_cross_last_daily == date.today()` |
| **guard**（数据体检） | `:1273-1293` | `guard_interval`（默认 3600s） | `_busy_guard` 标志；**首检延后 20s**（`:1289`），留给 K 线先入库 |

**为什么体检要延后**：否则它会把"还没采到的数据"误判成"数据缺口"，然后去补一轮——白费请求额度。

**数据落库的两步**（`storage/service.py`）：
```
DataSourceFactory.get_stock_data(...)        # :50-56  取数（可能联网）
  → StockRepository.save_df(session, df, commit=False)
      └─ session_scope 统一提交，异常整体回滚
```
日线有 **coverage 窗口**优化（`collect_incremental` `:65-111`）：先查库内最后日期，
只补 `最后日期 - lookback_days` 之后的；库里完全没数据才退回全量 3 年。

### 2.3 定时任务链路（`SchedulerComponent`）

```
start_timer(interval, event_name, timer_id)      scheduler.py:315-339
  └─ tid = timer_id or f"{event_name}@{interval}"
     └─ 同 id 重复注册**直接忽略**（:331-332，防节拍翻倍）
        └─ LoopingCall(_publish_timer_event).start(max(0.01, interval))

_publish_timer_event                            scheduler.py:352-359
  └─ event_bus.publish(event_name, context=data)

submit_task(...)                                scheduler.py:181-214
  └─ Task(priority, create_time, name, handler, ...) → _task_queue.put()
     （queue.PriorityQueue：优先级 → 创建时间 → 任务名，保证同优先级 FIFO）

_schedule_loop                                  scheduler.py:219-245
  ├─ BACKTEST：严格串行（_active_tasks == 0 才取下一个）
  │            → 同样的输入必然得到同样的结果顺序
  └─ 其它模式：while _active_tasks < max_concurrent 并发取
  └─ reactor.callLater(0.01, _schedule_loop)  自递归，避免 CPU 空转

_execute_task                                   scheduler.py:250-302
  └─ deferToThread(_run_task) + addErrback(_on_task_crashed)
```

> **注意一处名实不符**：`:104-108` 注册的是 `TIMER_3MIN` 事件，但传的
> `interval=5`（秒）。当前**没有任何订阅者**，`on_start` 里只是保留了默认节拍。
> 接真实消费者时记得把间隔改成 180，别让它继续每 5 秒空转一次。

---

## 3. 顺序是怎么算出来的

**重构前**：注册顺序 == 启动顺序。于是 `builder.py` 里挤满了"采集必须在行情之前"
这类约束——**只写在注释里**，改代码的人一不小心就打乱，而乱序的后果
不是报错，是"挂载静默失败 / 体检把没采到的数据误判成缺口"。

**重构后**由引擎自己算（`base.py:456-465`），排序键是三元组：

```
(KIND_LAYER, 0 或 1, 服务位次或注册序号)
```

`KIND_LAYER`（`kinds.py:82-88`）：

| 层 | 值 | 组件 |
|---|---|---|
| 调度 | 10 | `scheduler` |
| 服务 | 20 | `collection / market / news / portfolio / risk / trading` |
| 策略 | 30 | `strategy` |
| 工作单元 | 40 | `worker` |
| 其它 | 50 | `monitor` 等未声明 kind 的 |

**实测启动顺序**（默认全组件 + SIMULATE）：

```
scheduler → collection_service → market_service → news_service
→ portfolio_service → risk_service → trading_service
→ strategy → worker → monitor
```

停止时严格 `reversed(...)`（`base.py:612`），所以是
`monitor → worker → strategy → trading_service → ... → scheduler`。

> **一处"已就绪但未使用"**：六个服务的 `depends_on` **全是空元组**
> （默认 `()`，`kinds.py:177`），所以服务之间的拓扑排序目前退化成**服务名升序**。
> 真正的排序靠 `KIND_LAYER` 分层。服务间依赖机制是通的，只是还没人声明。

---

## 4. 依赖注入：为什么在初始化之前

```
base.py:217-219   bind_services(...)      ← 先注入
base.py:229       component.initialize()  ← 后初始化
```

原因写在 `base.py:214-216`：策略的 `on_initialize()` 里**就要用**注入进来的服务
（比如把任务挂到网关上）。顺序反了就会拿到 `None`。

**服务缺失 vs 策略缺失，处理是相反的**（`base.py:151-160`）：

| 声明 | 缺失时 |
|---|---|
| `required_services`（服务/策略依赖） | **直接抛错**——宁可起不来，也不要带着残缺依赖跑 |
| `optional_services` | 静默跳过，只是不注入 |

具体：策略侧 `strategy.py:62` `required_services = ("risk_service",)`，
`:65` `optional_services = ("portfolio_service",)`。

---

## 5. 线程模型：什么能阻塞，什么不能

Twisted 是**单线程事件循环**。reactor 线程里任何一次阻塞，都会让行情推送、
面板响应、撤单回调一起停摆。

### reactor 主线程（绝不能阻塞）

- `base.py:265` `reactor.run()`；`base.py:589-631` 关停协程
- `base.py:285-307` 空闲看门狗
- `market.py:230-254` 行情节拍与 `_emit`
- `event.py:76-83` `EventBus.publish` —— **同步**调用所有订阅者
- **整条交易链路**：`runtime.on_bar` → `task.on_bar` → 策略计算 →
  `_process_signal` → 风控 → `broker.submit`

> 最后这条值得留意：策略 `on_bar` 是在 reactor 线程里**同步**跑的。
> 如果某个策略里写了很重的循环（比如全市场遍历），会直接卡住整个引擎。
> 重活应该丢给 `WorkerComponent`（有 `capacity` 限流）。

### `deferToThread`（线程池）—— 所有阻塞 IO 都在这里

| 位置 | 阻塞内容 |
|---|---|
| `market.py:270` | DB / 数据源读行情 |
| `scheduler.py:300` | 任务体 |
| `worker.py:124,154` | 作业体（受 `capacity` 限制） |
| `collection.py:624` | K 线采集 |
| `collection.py:828` | 手动补采 |
| `collection.py:965,990` | 日频截面 |
| `collection.py:1162` | 市场级截面 |
| `collection.py:1329` | 数据体检 |
| `trading.py:430` | 券商回报轮询（网络） |
| `trading.py:445` | 券商账户查询（网络） |
| `news.py:104` | RSS 抓取（网络） |
| `monitoring/server.py:340` | 快照落盘 |

### `LoopingCall` / `callLater` 一览

| 用途 | 位置 | 周期 |
|---|---|---|
| 行情推送 | `market.py:219` | `MARKET_INTERVAL` 默认 0.05s |
| K 线采集 | `collection.py:583` | 按频率分档，默认 5m |
| 日频截面（个股） | `collection.py:945` | `max(30, 600)` |
| 日频截面（市场级） | `collection.py:947` | `max(300, ...)` |
| 数据体检 | `collection.py:1279` | 默认 3600s |
| 回报轮询 | `trading.py:329` | `max(0.2, 1)` |
| 对账 | `trading.py:332` | `max(10, 300)` |
| 账户查询 | `trading.py:337` | `max(5, 30)` |
| 权益采样 | `trading.py:758` | `max(1, ...)` |
| 停止标志轮询 | `service_runner.py:90` | 0.5s |
| 定时器事件源 | `scheduler.py:334` | 各自定义 |

---

## 6. 事件总线

- 定义：`app/engine/event.py:19-59` `StandardEvents`（纯字符串常量）
- 实现：`EventBus`（`event.py:62-91`），`_receivers: Dict[str, List[Callable]]`
- `publish()` **同步**遍历回调，**逐接收者 try/except 隔离**（`:82-83`）——
  一个订阅者抛错不影响其他订阅者，也不影响发布方
- 组件封装：`component/base.py:265-272` `subscribe_event`
- **自动退订**：`component/base.py:274-285` `_clear_subscriptions`，
  在 `stop()` 里调用（`:198`），杜绝僵尸回调

### 主要事件：谁发 → 谁收

| 事件 | 发布方 | 订阅方 |
|---|---|---|
| `BAR_RECEIVED` | `market.py:319`（`bus/components.py:272` 转发） | **`strategy.py:101`** → `_on_bar` |
| `NEWS_ANALYZED` | `news.py`、`bus/components.py:280` | **`strategy.py:102`** → `_on_news` |
| `TASK_ADDED` | `control.py`（运行期新增） | **`strategy.py:105`** → 挂网关 |
| `TASK_SUBMIT` | 各组件 | **`scheduler.py:102`** → `submit_task` |
| `ORDER_CREATED` | `strategy.py:216` | `trading.py:728` → 落库 |
| `ORDER_FILLED` | `strategy.py:220`、`trading.py:535` | `trading.py:729` → 落库 |
| `ORDER_CANCELLED` | `strategy.py:226` | `trading.py:730` → 落库 |
| `SIGNAL_REJECTED` | `strategy.py:230` | `trading.py:731` → 落库 |
| `DATA_COLLECTED` | `collection.py:723` | `trading.py:732` → 落库 |
| `COMPONENT_ERROR` | `base.py:232/587` | `trading.py:733` → 落库 |
| `TIMER_3MIN` | `scheduler.py:104` | 暂无订阅者 |

除了进程内总线，还有一条**进程间总线**（`app/engine/bus/filebus.py` +
`bus/components.py`）用于多服务协作，靠 `origin.startswith("bus:")`
判断防止互相转发死循环。

---

## 7. 关停：优雅退出做了什么

`base.py:589-631` `_graceful_shutdown`：

1. **等空闲**（`:597-609`）：`while not _is_idle() and 未超时: yield async_sleep(0.1)`；
   预算 `SHUTDOWN_TASK_WAIT`（默认 30s），超时**告警后强制继续**——宁可丢一点尾巴，
   也不能卡在关停上。
2. **逆序停组件**（`:612-620`）：每个组件 `maybeDeferred(stop)`，
   并 `addTimeout(5, reactor)`——单个组件最多停 5s。
3. **落状态**（`:623`）：刷最终快照 + 保存运行期任务。
4. 置 `STOPPED` → `reactor.stop()`。

**空闲判定**（`base.py:326-340`）：先看所有组件 `is_busy()`，任一忙即非空闲；
否则问 `scheduler.is_idle`（队列空且无在跑任务）。

> **一个已知别扭**：`MarketService.is_busy()`（`market.py:328`）在 poll 模式下
> **恒为 True**（常驻轮询 = 一直"有行情要推"）。所以轮询模式下每次关停都会
> 空等满 30s，日志还会留一条"任务等待超时"——其实没有任务在跑，只是常驻组件
> 被当成了"干活中"。语义上应该区分"常驻"和"有活"，改法见 `EXTENDING.md` 的收尾清单。

---

## 8. 三种运行模式的关键差别

| | BACKTEST | SIMULATE | LIVE |
|---|---|---|---|
| 行情来源 | 历史回放（内存队列） | 实时轮询 DB | 实时轮询 DB |
| 撮合 | 模拟立即成交 | 模拟立即成交 | 真实券商网关 |
| 订单终态 | 同步返回 FILLED | 同步返回 FILLED | SUBMITTED → 等回报 |
| 调度并发 | **严格串行**（结果可复现） | `max_concurrent` | `max_concurrent` |
| 空闲自动停 | 是（`BACKTEST_AUTO_STOP`） | 否 | 否 |

模式在装配时从 `RunMode` 读（`builder.py`），并下沉到 `runtime.run_mode`
（`builder.py:349`），任务据此决定撮合方式。
