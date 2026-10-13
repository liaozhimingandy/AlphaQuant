# 运行原理：从按下回车到一笔成交

> 本文回答"系统到底是怎么跑起来的"。写法是**跟着代码走一遍**——每个环节都标注
> 了 `文件:行号`，可以直接跳过去看实现。
>
> 想看"为什么这么设计"请读 `ARCHITECTURE.md` 与 `ENGINE_ARCHITECTURE.md`；
> 想看"事件 / 任务 / 执行池的完整契约"请读 `EVENT_TASK.md`；
> 想看"怎么加东西"请读 `EXTENDING.md`。

---

## 0. 三句话总览

1. **一个进程 = 一台引擎**：进程里跑一个 Twisted reactor，上面挂若干"组件"，
   组件之间靠**事件总线**通信、靠**服务注册表**拿依赖。
2. **一次运行 = 一个 run_id**：引擎启动那一刻生成，贯穿日志、快照、订单、成交，
   事后复盘全靠它串起来。
3. **策略不知道数据从哪来**：它只订阅 `BAR_RECEIVED` 事件；行情是回放还是实时
   轮询，由 `MarketService` 的 `mode` 决定，策略代码一行都不用改。
4. **引擎发事件，调度器转任务，执行池干活**：三者职责不重叠——
   事件是"发生了什么"（可忽略、可重投），任务是"要做什么"（必须有终态、必须幂等），
   中间只有调度器一个转换点。完整契约见 [EVENT_TASK.md](EVENT_TASK.md)。

---

## 1. 启动链路：`python main.py start` 之后发生了什么

> **启动前先预检**：`engine.start()` 第一件事是 `_run_preflight()`
> （`app/engine/preflight.py`）：配置能否解析、目录可不可写、数据库可读写、
> 行情是否为空、磁盘余量、遗留任务、实盘网关。有 error 档问题时默认
> **拒绝启动**（`PREFLIGHT_STRICT=0` 可强制继续）。
> 它拦的不是崩溃，是**空转** —— 详见 [EVENT_TASK.md §8.6](EVENT_TASK.md)。

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

`service_runner.py:218` 调 `engine.start()` → `engine.py::start` 里的 `reactor.run()` 阻塞。
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

#### 账户账本：`AccountService`（`app/modules/account/service.py`）

上面第 ⑥ 步"入账"过去只发生在**内存里的任务级 `Portfolio`** 上 ——
进程一重启，账户余额就跟着没了。`AccountService` 把这一侧落到
`broker_account` / `account_position` 两张表，并内置一个虚拟账户
（`virtual-default`，初始 10 万），让"能下单"和"有钱可下"分成两件事。

```
AccountService.on_initialize
  ├─ ensure_virtual_account()             # 幂等建/读虚拟账户
  ├─ subscribe(trade.order.filled)  → _on_filled   # 按 side/price/size 增量记账
  └─ subscribe(system.market.session.changed)
        └─ _refresh_session()  → trade.session.opened / closed
```

三条记账纪律（都在 `app/account/store.py` 文件头）：

1. `initial_cash` **只在新建时写一次**。运行中被改成"当前现金"，
   历史收益率就全成了谎言 —— 分母变了。
2. 改经费走**加法** `apply_fill(...)`，不要 `set_cash(...)`。
   两笔成交几乎同时回来时，赋值写法会让后到的那笔覆盖前一笔。
3. 读失败**宁可抛错**，也不要默默按 0 处理。余额显示成 0 会让人以为
   "没钱了所以没下单"，而真相是"读不到"。

**已知边界（本轮有意未打通）**：任务级 `Portfolio`（`strategy` 池内每个任务
一本账）与 `broker_account`（库里这一本）目前**互不约束** ——
下单额度仍由任务链自己管，钱包只是先把"钱在哪、现在多少"这一侧如实记下来。
把两者接成同一个额度来源需要改 `QuantTask._process_signal` 的 quota 环节，
属于下一步，在 `service.py` 文件头已写明。

#### 交易时段风控：`trading_session`

`config/tasks.json` 的全局 `risk` 链默认启用 `trading_session`：
白名单 `open / lunch / post_close`，其余状态（含 `holiday`）一律否决。
日历读不到时**放行** —— 一个"因为日历接口挂了所以全天禁止交易"的风控
比没有风控更糟。离线回放时给该规则加 `allow_outside_hours: true` 即可关掉。

熔断（`RiskService.halt/resume`）现在会发 `risk.halted` / `risk.resumed`：
熔断是**全场状态变化**，只打日志的话别的组件只能靠轮询去发现它。

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

### 2.3 调度链路：事件 → 任务 → 执行池（`SchedulerComponent`）

调度器是**唯一**把"发生了什么"翻译成"要做什么"的地方。两半各跑各的节拍：

**上半段（同步，跑在事件总线的投递里）**——收到事件就建任务：

```
EventBus.publish(event)                         events/bus.py:199
  └─ SchedulerComponent 的订阅回调                scheduler.py:302 _make_listener
       └─ handle_event                           scheduler.py:326-361
            ├─ 回声事件（task.*）直接返回          :335   ← 防 任务→事件→任务 自激
            ├─ rules.match(event_type)            :339   按规则表找
            ├─ rule.should_run(event, self)       :346   条件不满足就不跑
            ├─ rule.build_task(event, self)       :348   造 EngineTask（含幂等键）
            └─ store.enqueue(task)                :349   按优先级入对应队列
                                                        被幂等键拦下就跳过（:359）
```

> `handle_event` **不做任何耗时的事**，所以敢放在总线的同步投递路径上。
> 这是"事件同步广播"能成立的前提——订阅者必须很快返回。

**下半段（异步，由 `callLater` 自递归驱动）**——有空槽就领活：

```
_dispatch_loop                                  scheduler.py:366-377
  └─ _dispatch_once                             scheduler.py:379-399
       └─ for 每个池: while pool.has_slot():      :382-389
            └─ store.pop(queue)                  :390
                 └─ _start_task                  :401-447
                      ├─ 无处理器     → 死信       :404-413
                      ├─ 无池能领     → 死信       :415-422
                      ├─ 池与队列不符 → 重新入队   :423-427
                      ├─ 抢不到槽位   → 重新入队   :434-439（正常竞态，不计失败）
                      └─ pools.dispatch(...)      :433
                           └─ WorkerPool.try_submit  workers/pools.py:105
  └─ _sweep_timeouts                            scheduler.py:494  超时巡检
```

**定时任务**是这条链路的一个特例——定时器不发命令，只发**事件**：

```
start_timer(interval, event_name, timer_id)     scheduler.py:661
  └─ 同 id 重复注册直接忽略（防节拍翻倍）
     └─ LoopingCall(_publish_timer_event).start(max(0.01, interval))
          └─ event_bus.publish(event_name, ...)   → 走上面同一条链路
```

> 定时器只负责**发事件**，发完就完事；事件变成什么任务由规则表决定。
> 所以改"某个定时该干什么"请改 `scheduling/defaults.py` 的规则，不要改定时器。

#### 执行池 worker 数怎么调（`config/runtime.json`）

七个池各自一个线程池，**槽位 = 该池线程池的 worker 数**，写在：

```jsonc
// config/runtime.json
{
  "worker_pools": {
    "capacities": {"market": 3, "research": 2, "strategy": 1, "trading": 1},
    "enabled": ["system","market","research","strategy","trading","backtest","maintenance"]
  }
}
```

| 写法 | 含义 |
|---|---|
| 不写某个池 | 用 `Pool.DEFAULT_CAPACITY` 里的内置值 |
| `capacities` 里给了值 | 覆盖内置值（最小 1，写 0 / 负数会被拉回 1 并告警） |
| `enabled` 省略或 `[]` | 全建（`[]` 按"没配"处理 —— 配错就把系统打死太危险了） |
| 池名写错 / 值不是整数 | 忽略该项 + 告警，**不让坏配置挡住启动** |

读配置的入口是 `workers/pools.py:load_worker_pool_config`，由 `SchedulerComponent`
在**构造时**调一次。所以：

> **改完必须重启引擎。** `ThreadPoolExecutor` 的 `max_workers` 在建池那一刻就固定，
> 运行中改数字只会得到"面板显示 8、实际并发还是 3"—— 这种"改了但没真的改"
> 比不支持热更新难排查得多，所以这里刻意不提供热更新。

面板「运行时 → 执行池」会显示 **worker 数来源**（文件路径 / 内置默认 /
代码显式传参）和配置告警，用来确认这次到底读的是哪份配置。

#### 运维节拍：为什么是 tick + due 两个事件

维护任务（日志清理 / 快照归档 / 定期校验）曾有一个安静的缺陷：
规则表里有三条 `system.maintenance.due` 规则，但**没有任何东西发这个事件** ——
三条规则是死配置，日志清理从来没跑过。它不报错、不告警，只是"该做的没做"。

现在补上了时间源（`app/runtime/maintenance.py`）：

```
start_timer(30s, "system.maintenance.tick")    scheduler.py:on_start
  └─ MaintenancePlanner.tick()                 maintenance.py
       └─ 哪个 job 到点了？（周期来自 config/maintenance.json）
            └─ publish system.maintenance.due {job, params}
                 └─ 规则表按 job 名过滤 → 建任务 → maintenance 池
```

拆成两个事件是为了让"节拍在跑"和"该干活了"在事件流里能分开看：
**有 tick 没 due** = 没到点或计划被停用；**有 due 没任务** = 规则被关了。
一个事件也能做，但那样排障时只能看到"到点了"，看不出"到的是哪个点"。

#### 七种周期

| `schedule` | 含义 | 用到的字段 | 最小粒度/约束 |
|---|---|---|---|
| `startup` | 引擎**每次启动**跑一次（不是周期） | 无 | 不参与 `tick`，由 `on_start` 显式触发 |
| `interval` | 每 N **小时** | `interval_hours` | ≥ 1 小时 |
| `daily` | 每天 `HH:MM` | `at` | 错过 5 分钟以上不补跑 |
| `weekly` | 每周某天 `HH:MM` | `weekday`(1=周一) + `at` | 同上 |
| `monthly` | 每月某日 `HH:MM` | `day_of_month` + `at` | 1–28 号（29–31 小月不存在，封顶） |
| `cron` | cron 表达式 | `cron` | 5 段，自己实现（`app/runtime/cron.py`，零依赖） |
| `once` | 指定时刻跑一次 | `once_at`（`YYYY-MM-DD HH:MM`） | 跑完自动停用 |

两点是特意定的：

- **间隔的最小粒度是小时，不是秒**。维护任务是"扫一遍日志目录"这种活，
  做成分钟级只会让引擎一直在做无用功；而秒级输入框会让人填出
  "每 300 秒清一次日志"这种配置。旧配置的 `interval_sec` 读入时按
  **向上取整**换算成小时（向下取整会把 1.5 小时悄悄缩短成 1 小时）。
- **`startup` 存在的理由**：像"运行校验"这种活，它的价值就是
  "这次启动的环境是好的"，每小时再校验一遍既不增加信息量，
  又会在日志里堆一堆"正常"。它由 `scheduler.on_start` 的
  `_run_startup_maintenance()` 触发，**必须**在 `tick` 之外 ——
  `tick` 每 30 秒一拍，让 startup 参与判定就会变成每 30 秒跑一次。

cron 支持 `*`、`*/n`、`a-b`、`a,b`、`a-b/n`。**「日」与「周」是 OR 语义**：
`0 3 1 * 1` 是"每月 1 号**或**每周一"，不是"1 号且是周一"。
表达式写错时任务**不会执行**（`next_run_at` 返回 None）并在面板上显示原因，
而不是悄悄按某个默认值跑。

配置在 `config/maintenance.json`，面板「运行时 → 维护计划」改完即生效。
切换周期时面板只显示该周期用得到的输入框 —— 七种周期共用一套字段，
选错了框就等于配上了一个永远不生效的参数。

**任务清单本身是可配的**。内置任务写在 `maintenance.py` 的 `DEFAULT_JOBS`，
但配置里（或面板新增表单里）写的其它条目同样会被加载，无需改动代码。
`interval` 的下限按任务类别区分：`maintenance.*` 仍 ≥ 1 小时，
其它业务任务可以到分钟级（例如 `pool.refresh` 每 10 分钟一次）；
周期任务建议勾 `trading_hours_only` 以免休市空跑。
详见 [EVENT_TASK.md](EVENT_TASK.md) §七。
保留期参数支持**小时/天切换**（`retention_hours` / `before_hours` 优先，
`*_days` 兼容），详见 [EVENT_TASK.md](EVENT_TASK.md) §七。

> **注意一处名实不符**：`on_start` 注册的 `TIMER_3MIN` 事件传的 `interval=5`（秒），
> 当前**没有任何规则订阅它**，只是保留了默认节拍。接真实消费者时记得把
> 间隔改成 180，别让它继续每 5 秒空转一次。

---

## 3. 顺序是怎么算出来的

**重构前**：注册顺序 == 启动顺序。于是 `builder.py` 里挤满了"采集必须在行情之前"
这类约束——**只写在注释里**，改代码的人一不小心就打乱，而乱序的后果
不是报错，是"挂载静默失败 / 体检把没采到的数据误判成缺口"。

**重构后**由引擎自己算（`engine.py::ordered_component_names`），排序键是三元组：

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

停止时严格 `reversed(...)`（`engine.py::_graceful_shutdown`），所以是
`monitor → worker → strategy → trading_service → ... → scheduler`。

> **一处"已就绪但未使用"**：六个服务的 `depends_on` **全是空元组**
> （默认 `()`，`kinds.py:177`），所以服务之间的拓扑排序目前退化成**服务名升序**。
> 真正的排序靠 `KIND_LAYER` 分层。服务间依赖机制是通的，只是还没人声明。

---

## 4. 依赖注入：为什么在初始化之前

```
engine.py::initialize   bind_services(...)      ← 先注入
engine.py::initialize  component.initialize()  ← 后初始化
```

原因写在 `engine.py::initialize` 的注释里：策略的 `on_initialize()` 里**就要用**注入进来的服务
（比如把任务挂到网关上）。顺序反了就会拿到 `None`。

**服务缺失 vs 策略缺失，处理是相反的**（`engine.py` 的组件注册与 `get_component`）：

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

- `engine.py::start` 的 `reactor.run()`；`engine.py::_graceful_shutdown` 关停协程
- `engine.py::_start_idle_watchdog` 空闲看门狗
- `market.py:230-254` 行情节拍与 `_emit`
- `event.py:76-83` `EventBus.publish` —— **同步**调用所有订阅者
- **整条交易链路**：`runtime.on_bar` → `task.on_bar` → 策略计算 →
  `_process_signal` → 风控 → `broker.submit`

> 最后这条值得留意：策略 `on_bar` 是在 reactor 线程里**同步**跑的。
> 如果某个策略里写了很重的循环（比如全市场遍历），会直接卡住整个引擎。
> 重活应该做成任务类型，交给对应的 Worker Pool（见下）。

### Worker Pools（8 个独立线程池）—— 按职能隔离

`app/runtime/workers/pools.py`。**每个池一个 `ThreadPoolExecutor`**，
槽位满了就 `try_submit()` 返回 `None`（这是正常调度状态，不是异常，
任务放回队列下次再领）。

| 池 | 默认容量 | 负责 | 为什么必须独立 |
|---|---|---|---|
| `market` | 3 | 行情采集、K 线处理、缺口补采 | 盘中延迟最敏感，不能被别人挤 |
| `research` | 2 | 新闻、舆情、因子计算 | 一次因子计算可能扫全市场 |
| `strategy` | 1 | 策略运行 | 同一策略实例不该并发跑 |
| `trading` | 1 | 报单执行 | **必须串行**：并发下单会超额度 |
| `backtest` | 1 | 历史回测、参数实验 | 打满也只能吃掉自己这一个线程 |
| `maintenance` | 1 | 日志清理、归档、定期校验 | 慢是它的本分，别影响别人 |
| `system` | 2 | 快照持久化、状态恢复 | |
| `default` | 2 | 兜底（含旧式 `submit_task` 作业） | |

> **这张表存在的理由**：所有活共用一个线程池时，一次批量回测会把线程占满，
> 行情采集排在后面排队——表现为"盘中 K 线延迟"，但根因在几百行外的回测代码里，
> 光看行情日志永远查不出来。分池之后这个故障模式被物理消除了。

### `deferToThread`（线程池）—— 所有阻塞 IO 都在这里

| 位置 | 阻塞内容 |
|---|---|
| `market.py:270` | DB / 数据源读行情 |
| `workers/pools.py:105` | 任务体（`WorkerPool.try_submit`，受槽位限制） |
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
| 运维节拍 | `scheduler.py:on_start`（`maintenance_tick`） | 30s |
| 定时器事件源 | `scheduler.py:661` `start_timer` | 各自定义（默认 `TIMER_3MIN`@5s） |
| 调度分发循环 | `scheduler.py:366` `_dispatch_loop` | `tick_interval`（callLater 自递归） |

---

## 6. 事件总线

总线在 2026-10 这轮**从空壳变成了中枢**：此前有定义但服务之间仍是直接调用。
契约与实现现在分三个文件：

| 文件 | 职责 |
|---|---|
| `app/runtime/events/types.py` | **唯一真源**：`EventType` / `TaskType` / `Queue` / `Pool` 全部常量 + 旧名映射 |
| `app/runtime/events/schema.py` | `EngineEvent` 信封（`event_id` / `correlation_id` / `causation_id` / `occurred_at`） |
| `app/runtime/events/bus.py` | `EventBus`：同步投递、去重、通配订阅、异常隔离 |

`app/runtime/event_bus.py` 现在是**薄转发层**——存量代码
`from app.runtime.event_bus import StandardEvents` 零改动继续可用。

几个容易踩的行为，都在这里定：

- `publish()` **同步**遍历回调，**逐接收者 try/except 隔离**
  （`bus.py:272` `_invoke`）——一个订阅者抛错不影响其他订阅者，也不影响发布方。
  代价是**订阅者必须很快返回**，耗时的事请发任务。
- **事件 ID 去重**（`bus.py:329`）：同一个 `event_id` 只投一次，有界 LRU。
- **通配订阅**：`subscribe_pattern("market")` 收全部 `market.*`。
- **新旧名同时投递**（`bus.py:302` `_compute_keys`）：发新名时，
  订阅了旧名的老代码也会收到。旧 → 新映射是**单射**，
  `types.py` 在导入期就断言这一点（曾经因为两个旧名映射到同一新名，
  让控制面板收到 `action=""` 的幽灵指令）。
- **自动退订**：`engine/component.py` 的 `_clear_subscriptions` 在 `stop()` 里调用，
  杜绝僵尸回调。

### 主要事件：谁发 → 谁收

**存量业务事件**（发布方是业务服务，订阅方多为策略与交易落库）：

| 事件 | 发布方 | 订阅方 |
|---|---|---|
| `BAR_RECEIVED` | `MarketService`（`bus/components.py` 转发） | `StrategyComponent` → `on_bar` |
| `NEWS_ANALYZED` | `NewsService` | `StrategyComponent` → `on_news` |
| `TASK_ADDED` | `ControlCenter`（运行期新增） | `StrategyComponent` → 挂网关 |
| `TASK_SUBMIT` | 各组件 | `SchedulerComponent._on_task_submit` |
| `ORDER_CREATED` / `FILLED` / `CANCELLED` | `QuantTask` / `TradingService` | `TradingService` → 落库 |
| `SIGNAL_REJECTED` | `QuantTask` | `TradingService` → 落库 |
| `DATA_COLLECTED` | `CollectionService` | `TradingService` → 落库 |
| `COMPONENT_ERROR` | `engine.py` 初始化 / 启动流程 | `TradingService` → 落库 |
| `TIMER_3MIN` | `SchedulerComponent.on_start` | 暂无订阅者 |
| `system.maintenance.tick` | `SchedulerComponent.on_start` | `SchedulerComponent._on_maintenance_tick` → `MaintenancePlanner.tick()` |
| `system.maintenance.due` | `MaintenancePlanner` | 规则表：内置任务按 `job` 名分流成清理/归档/校验；其余走通用规则 `maintenance_due_generic`，按事件里的 `task_type` 建任务 |

**新增的引擎事件**（事件驱动改造引入，驱动任务调度）：
`engine.started` / `engine.ready` / `engine.stopping` / `engine.stopped` /
`market.kline.updated` / `market.bar.received` / `factor.snapshot.updated` /
`strategy.signal.generated` / `risk.check.*` / `task.*` / `system.*`。

> **完整的事件清单、任务清单、21 条调度规则、8 个池的容量与路由表，
> 以及可靠性现状矩阵，见 [EVENT_TASK.md](EVENT_TASK.md)。**
> 那份文档是这块设计的唯一真源，本文只讲它怎么跑起来。

除了进程内总线，还有一条**进程间总线**（`app/runtime/bus/filebus.py` +
`bus/components.py`）用于多服务协作，靠 `origin.startswith("bus:")`
判断防止互相转发死循环。

---

## 7. 关停：优雅退出做了什么

`engine.py::_graceful_shutdown`：

1. **等空闲**（`:597-609`）：`while not _is_idle() and 未超时: yield async_sleep(0.1)`；
   预算 `SHUTDOWN_TASK_WAIT`（默认 30s），超时**告警后强制继续**——宁可丢一点尾巴，
   也不能卡在关停上。
2. **逆序停组件**（`:612-620`）：每个组件 `maybeDeferred(stop)`，
   并 `addTimeout(5, reactor)`——单个组件最多停 5s。
3. **落状态**（`:623`）：刷最终快照 + 保存运行期任务 + **强制刷任务流水账**。
4. 置 `STOPPED` → `reactor.stop()`。

> **为什么第 3 步要额外刷一次流水账**：调度器 `on_stop` 已经写过一次
> （那时它还在），这里再兜一次是为了覆盖"调度组件压根没起来"或
> "停止顺序异常"的情形 —— 落盘宁可多做一次，不能漏一次。

**空闲判定**（`engine.py::_is_idle`）：先看所有组件 `is_busy()`，任一忙即非空闲；
否则问 `scheduler.is_idle`（队列空且无在跑任务）。

> **一个已知别扭**：`MarketService.is_busy()`（`market.py:328`）在 poll 模式下
> **恒为 True**（常驻轮询 = 一直"有行情要推"）。所以轮询模式下每次关停都会
> 空等满 30s，日志还会留一条"任务等待超时"——其实没有任务在跑，只是常驻组件
> 被当成了"干活中"。语义上应该区分"常驻"和"有活"，改法见 `EXTENDING.md` 的收尾清单。

**调度器怎么停**（`scheduler.py:238-285`）——顺序有讲究：

1. 收到 `engine.stopping` 事件 → `_accept_new = False`（`:634`）。
   **这一步在组件逆序停止之前就发生**，否则会出现"一边在建新任务一边在停"的窗口。
2. 停所有 `LoopingCall` 定时器；取消尚未触发的重试 `callLater`
   （`:260-262`）——不然停止后还会往队列里投任务。
3. 取消分发循环 `callLater`。
4. 等 `store.is_idle()`，带 deadline（`_shutdown_wait()`），超时告警后放弃。
   这里**必须异步让出**（`yield async_sleep(0.1)`），用 `time.sleep` 会冻住
   reactor，而任务正在等回调 → 永远等不到空闲 → **停止即挂死**（历史踩过）。
5. **`_persist_on_stop()`**：把还在跑的任务标成 `interrupted` 写进流水账，
   再强制 `flush()`。不做这件事，它们在盘上仍是 `running`，下次启动
   分不清是"被中断"还是"正在别处跑"—— 而这两者处置完全不同。
6. 最后 `pools.shutdown(wait=False)`。

> 注意第 4 步等的是 `store.is_idle()`（队列空 + 无在跑任务），
> 不是"所有池都空转"。常驻的行情轮询不阻塞这一步。

---

## 7.5 任务落盘与崩溃恢复

**落盘**（`app/runtime/tasking/journal.py`，独立 SQLite `output/runtime/tasks.db`）：

| 时机 | 策略 |
|---|---|
| 通用状态变更 | 标脏，后台线程每 1s 合并成一个事务 |
| 创建 / 进死信 / 终态 | **立即写**（不等后台） |
| 引擎停止 | 强制 `flush()` + 在跑任务标中断 |

**恢复**（`SchedulerComponent.on_start` → `store.recover()`，在订阅事件**之前**）：

| 上次状态 | 动作 |
|---|---|
| `running` / 已标 `interrupted` | 重新入队，`attempts` 不回滚 |
| `queued` / `retrying` | 重新入队 |
| 超龄（>72h） | 转死信 |
| 重试额度用尽 | 转死信 |
| `dead_letter` | 读回内存但**不入队**（错误队列跨重启存在） |

恢复完发 `system.tasks.recovered` 事件（带各分类计数），面板顶部显示。

完整设计见 [`EVENT_TASK.md` §八](EVENT_TASK.md)。

---

## 8. 三种运行模式的关键差别

| | BACKTEST | SIMULATE | LIVE |
|---|---|---|---|
| 行情来源 | 历史回放（内存队列） | 实时轮询 DB | 实时轮询 DB |
| 撮合 | 模拟立即成交 | 模拟立即成交 | 真实券商网关 |
| 订单终态 | 同步返回 FILLED | 同步返回 FILLED | SUBMITTED → 等回报 |
| `因子快照 → 跑策略` 这条规则 | **关闭**（`_mode_not("BACKTEST")`） | 开 | 开 |
| 空闲自动停 | 是（`BACKTEST_AUTO_STOP`） | 否 | 否 |

模式在装配时从 `RunMode` 读（`builder.py`），并下沉到 `runtime.run_mode`
（`builder.py:349`），任务据此决定撮合方式。

> **行为变更（事件化改造带来的）**：旧调度器在 `BACKTEST` 模式下强制
> **全局串行**（`_active_tasks == 0` 才取下一个），以此保证结果顺序可复现。
> 新调度器的并发上界由**池容量**决定，不再看 `run_mode`。
> 实际后果有限——回测任务走 `backtest` 池（容量 1，天然串行），
> 且 21 条规则里唯一带 `_mode_not("BACKTEST")` 的恰好是
> `因子快照 → 跑策略`（`defaults.py:376`），即回测期间**不会再自动触发策略**。
> 其余规则（K 线更新 → 因子计算等）在回测模式下照常生效——
> 它们只写库不算钱，不影响回测结果，但也说明这个闸门是**逐条**判的，
> 不是"BACKTEST 模式整体关链"。
>
> 结论：**如果以后给回测加多任务并行，可复现性要自己保证**，别指望调度器。

**调度器为什么不按模式分叉**：模式是"业务怎么跑"（撮合方式、风控口径），
并发是"资源怎么分"（哪个池有几个槽）。这两件事混在一起，
就会出现"为了复现结果，把行情采集也一起串行了"这种牵连。
分开之后 `backtest` 池容量调成 4 就并行，其他池不受任何影响。

---

## 9. 多服务编排（hub）：三层进程、三层健康

`python main.py hub start` 起的是**三层互相独立的东西**，搞清楚它们的边界，
"面板上一片绿但什么都没干"这种事就不会再困惑：

| 层 | 进程 | 代码 | 职责 |
|---|---|---|---|
| ①hub | hub 进程 ×1 | `app/engine/hub/hub.py` | 读 `config/services.json`、拉起并看护 N 个子进程、聚合面板（默认 8899） |
| ②worker | 每个服务 ×1 进程 | `app/engine/hub/worker.py` | 装配**自己的**引擎容器、写心跳、轮询远程指令 |
| ③engine | 进程内的对象 | `app/engine/engine.py` | 组件生命周期（initialize → start → stop） |

### 9.1 为什么"engine 容器没启动"，hub 上的服务还是健康的

三层各自独立，能同时成立的组合比直觉多：

1. **hub 不看引擎**。它只 spawn 子进程，然后读 `output/services/<id>/state.json`
   这个文件。文件新、PID 在 → 它就认为服务在。
2. **worker 在 `engine.start()` 之前就开始写心跳**（`worker.py:297`）。
   这是有意的：supervisor 要尽快看到新起的服务，不能等装配完（装配可能几十秒）。
3. **引擎对组件异常是逐个隔离的**（`engine.py:228` / `:583`）。
   组件初始化或启动抛异常，只记日志发事件，进程照常活着。

于是"进程活着 + 心跳新鲜"完全可能与"引擎还没起来"或"组件全挂了"同时存在。
**以前 hub 只判第一层**，这三种状态在面板上都显示"健康"——看着一片绿，
实际一笔数据没采、一个任务没跑。

### 9.2 三层健康判定（现在分开判）

`hub/supervisor.py::service_status` 的返回值：

| health | 判定 | 含义 |
|---|---|---|
| `healthy` | 进程活 + 心跳新 + 引擎 `running` + 无异常组件 | 三层都过了 |
| `degraded` | 进程活 + 心跳新，但引擎非 running（超过启动宽限）或存在 unhealthy/ERROR 组件 | **在跑，但没在干活** |
| `stale` | 心跳超过 `heartbeat_timeout`（默认 15s） | 卡死/被挂起 |
| `stopped` | 进程不在 | 没起或已退出 |

- **启动宽限**：`max(heartbeat_timeout, 30s)`。装配 + 初始化组件可能要几十秒，
  这段时间内引擎没到 `running` 属于正常启动过程，不报警。
- `degraded` **不会触发自动重启**：重启治不了"组件装配失败"，只会变成重启风暴。
  修根因（看 `degraded_reasons`）才是正解。
- 面板上每个服务卡片显示 `引擎 <状态>` 与 `组件 N 个异常`，鼠标悬停健康徽标看原因。
- CLI 同样分三层：`python main.py hub status` 的健康列现在会显示 `degraded`。

### 9.3 采集分工：别让三个服务烧掉三倍配额

每个 hub 服务都装配了同一套采集服务（K 线 + 日频截面 + 完整性校验）。
不分工的话，三个服务会把同一批标的各采一遍——数据源配额是整台机器共享的。
用服务的 `collector` 段声明本服务负责什么：

```json
"collector": {
  "enabled": true,          // K 线单元；省略 = 跟随全局
  "symbols": ["600519"],    // 本服务负责的标的
  "with_fundamental": true, // 基本面
  "with_flow": false,       // 资金流交给别的服务
  "with_chip": false,       // 筹码交给别的服务
  "categories": []          // 龙虎榜/两融/… 都不采
}
```

> **`[]` 不等于"没配"**：`symbols: []` 表示"本服务只采市场级数据（涨停池/题材/宏观）"，
> `categories: []` 表示"这些类别一个都不采"。
> 代码里一律用 `is not None` 判断，不会把空数组静默填成默认集合——
> 真值判断（`or`）在这里会把"我不要"读成"给我全部"，是分工功能最容易踩的坑。

`config/services.json` 末尾有一个默认关闭的 `fundamental` 服务，就是分工示例。

### 9.4 想采"指定标的的基本面"，走哪条路

三条，按"临时 → 长期"排：

| 场景 | 做法 |
|---|---|
| 临时采一次 | `python main.py ctl collector categories --symbols 600519,000858 --categories fundamental --port 8788` |
| 面板 / 程序调用 | `POST /api/cross/collect` `{"symbols":["600519"],"categories":["fundamental"]}` |
| 交给 hub 上的某个服务 | `python main.py hub cmd market cross_collect` + 命令文件（见下） |
| 长期分工 | `config/services.json` 里加一个带 `collector` 段的服务（§9.3） |

hub 命令文件（`output/services/<id>/commands/*.json`）写法：

```json
{"action": "cross_collect", "symbols": ["600519","000858"], "categories": ["fundamental"]}
```

支持的类别（`CollectionService.MANUAL_CATEGORIES`）：
`fund_flow` 资金流 · `chip` 筹码 · `fundamental` 基本面 ·
`lhb` 龙虎榜 · `margin` 两融 · `forecast` 业绩预告 · `holders` 股东户数 ·
`rating` 评级 · `news` 舆情。
**拼错会当场报错**（返回"未知采集类别"），不会给你一个"成功 0 条"的假成功。

链路：`ControlCenter.cross_collect` → `CollectionService.collect_categories`
→ `deferToThread(_run_manual_cross)` → `modules/universe/pool/sync_fundamentals`
→ 写 `stock_fundamental` 表 → 失效特征缓存（否则面板上还是旧值）。

### 9.5 新闻（news）服务怎么开

`config/services.json` 里已有一个 `news` 服务，默认关闭。三步开起来：

1. 填源：`news_config.sources[0].urls`（或环境变量 `NEWS_SOURCES`，逗号分隔）；
2. `"enabled": true`；
3. `python main.py hub start`，或不停 hub 单独起：`python main.py hub cmd news start`。

开起来后它把新闻事件发布到总线（`publish: ["news"]`），
`beta` 这类服务 `subscribe: ["news"]` 就能拿到。

> **源为空时它现在会被标成 `degraded`**（`NewsService.health_check`）。
> 以前是"健康"——服务在跑、轮询在转，但一条新闻都拿不到，
> 这种"在跑但什么都没干"比报错更难发现，所以按 §9.2 的原则一并标出来。
