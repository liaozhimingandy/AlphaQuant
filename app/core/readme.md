# AlphaQuant 核心架构

> 目标：一个后台常驻服务，支持**自定义因子**、**策略自由组合**、**实时新闻事件触发交易**、
> **大模型分析新闻**、**可插拔风控**，并且**一个引擎同时跑多个量化任务**；
> 同时**跑起来以后看得见、管得住**——网页面板、运行快照、运行中增删任务、
> 多进程服务协作。

---

## 一、架构

### 分层（数据 → 决策 → 执行）

```
┌──────────────────────────────────────────────────────────────┐
│  数据层                                                        │
│  MarketData(行情)  ──┐                                         │
│  News(新闻/事件)   ──┤──> 两条独立通道，最终汇入同一条决策链      │
└──────────────────────────────────────────────────────────────┘
                       │
┌──────────────────────────────────────────────────────────────┐
│  计算层（纯函数、可回测、无副作用）                              │
│  Indicator 指标  →  Factor 因子                                │
│      SMA/EMA/RSI/MACD/ATR       price/ma_spread/rsi/news_impact│
└──────────────────────────────────────────────────────────────┘
                       │
┌──────────────────────────────────────────────────────────────┐
│  决策层（只读、可组合）                                          │
│  Rule 规则(All/Any/Not/阈值/穿越)  →  Strategy 策略             │
│                                        →  Signal 交易意图       │
└──────────────────────────────────────────────────────────────┘
                       │
┌──────────────────────────────────────────────────────────────┐
│  风控层（闸门，只否决不修改）                                     │
│  RiskChain: 回撤/仓位/资金/冷却/新闻置信度/单日亏损                │
└──────────────────────────────────────────────────────────────┘
                       │
┌──────────────────────────────────────────────────────────────┐
│  执行层（每个任务独立一份）                                       │
│  Sizer 仓位计算 → Broker 撮合 → Portfolio 记账                   │
└──────────────────────────────────────────────────────────────┘
```

### 引擎与任务（解决"一个引擎跑 N 个任务"）

```
                        ┌──────────────────────┐
                        │   BaseQuantEngine    │  生命周期 / 组件调度 / 优雅退出
                        │   (Twisted Reactor)  │
                        └──────────┬───────────┘
                                   │ 组件注册（启停逆序）
     ┌─────────────────┬───────────┼───────────┬─────────────────┐
     ▼                 ▼           ▼           ▼                 ▼
MarketCenter    TaskScheduler  StrategyManager  NewsCenter   （可扩展）
行情中心          定时调度器      策略中枢          新闻中心
 BAR_RECEIVED                  ┌────────────┐   NEWS_ANALYZED
                               │ TaskRuntime│
                               │  ├ Task A (000001 + 双均线 + 风控A + 账本A)
                               │  ├ Task B (000001 + RSI   + 风控B + 账本B)
                               │  └ Task C (600000 + 新闻驱动 + 风控C + 账本C)
                               └────────────┘
                                      │ ORDER_FILLED / SIGNAL_REJECTED
                                      ▼
                                  EventBus
```

**关键点**：`TaskRuntime` 是任务容器。行情按 symbol 广播，新闻按关注列表定向投递，
每个任务有自己的 `BarSeries / Portfolio / Broker / 策略实例`——**状态完全隔离**。

---

## 二、双通道数据流（新闻触发交易怎么落）

每个 `QuantTask` 有两个入口，但**只有一条出口**：

```
   行情通道                              事件通道
on_bar(Bar)                          on_news(NewsItem, NewsAnalysis)
     │                                        │
     │  append 到 BarSeries                   │ 写入 ctx.features
     │  {news_sentiment, news_confidence,     │ {news_sentiment, news_confidence,
     │   news_impact, news_count}             │  news_impact, news_count}
     │                                        │
     └──────────────► 同一个 FactorContext ◄──┘
                              │
                              ▼
                   Strategy.decide(ctx, state, source)
                              │
                              ▼  Signal（标记 source: bar / news / llm）
                     ┌────────────────┐
                     │  RiskChain     │  任一规则否决 → SIGNAL_REJECTED（留痕）
                     └────────┬───────┘
                              ▼
                     Sizer → Broker → Portfolio
                              │
                              ▼
                        ORDER_FILLED
```

**为什么这样设计**：新闻不产生特殊的"新闻订单"，它只是让同一套因子在**非K线时刻**被求值一次。
因此"新闻触发"和"行情触发"共享全部因子、规则、风控，不需要任何一套并行实现。

---

## 三、可观测与可控（服务跑起来之后的部分）

引擎能跑只是及格线。真实场景里更常问的是这几个问题：
**现在什么状态？刚才发生了什么？能不能不重启就改？一个进程扛不住怎么办？**
下面四块就是回答它们的。

### 3.1 控制中心：唯一的控制面

```
              网页面板           CLI (ctl ...)          hub supervisor
                 │                    │                       │
                 │  HTTP              │  HTTP                 │  命令文件
                 └────────────────────┴───────────┬───────────┘
                                                  ▼
                                         ControlCenter
                                    （增删任务 / 启停组件 / 快照 / 停机）
                                                  │
                           ┌──────────────────────┼──────────────────────┐
                           ▼                      ▼                      ▼
                     TaskRuntime            EventBus              持久化
                   （真正改任务）      （CONTROL_COMMAND 留痕）  （runtime/tasks.json）
```

为什么独立成一层，而不是把方法堆在 `Engine` 上：

- `Engine` 的职责是"生命周期与组件调度"，不该知道"任务"这种业务概念
- 控制指令需要统一的**留痕与持久化**（谁改了什么、重启后还在不在）
- 面板、CLI、hub 三条入口共用同一套语义，**行为不会漂移**

**运行中新增任务**的两件事必须一起做，少一件就是坑：

1. **纳入行情订阅**。新任务的标的如果不在 `MarketCenter` 的订阅列表里，
   它永远等不到 K 线，看起来"任务跑起来了"其实一条都不会成交。
2. **持久化到 `output/runtime/<namespace>/tasks.json`**，重启后自动重放。
   删除过的 id 记在 `removed_ids` 里，避免重放又把删掉的任务装回来。

**控制指令的返回值必须诚实**。`QuantTask.pause()` 只在 RUNNING 时生效，
早期实现无论成没成都返回 `True`——面板显示"已暂停"，任务下一根 K 线照旧下单。
控制面谎报成功比报错危险得多，所以现在只有状态真的切过去了才返回成功，
失败会带上原因（"任务不存在，或当前状态不允许该切换"）。

### 3.2 运行快照：事后复盘靠它

```
output/snapshots/<run_id>/
├── latest.json     最新一份完整快照（原子写：先写临时文件再 os.replace）
├── meta.json       这个 run 的基本信息
├── orders.jsonl    订单流水（append-only，只增不改）
└── history/*.json  历史快照，按 MONITOR_SNAPSHOT_KEEP 滚动保留
```

每份快照：引擎状态 + 所有组件状态与健康度 + 每个任务的
**权益曲线 / 成交 / 订单 / 新闻 / 风控规则 / 错误** + 监控自身统计。
权益曲线会降采样到 400 个点（`_sample_curve`），否则跑一年的 run
光曲线就有几十万个点，面板直接卡死。

**线程边界是这个模块的关键**：

- `capture()` 必须在 **reactor 线程**完成——它要遍历任务字典。
  如果在工作线程里遍历而主线程正在增删任务，
  就是 `RuntimeError: dictionary changed size during iteration`
- `persist()` 是拆出来的另一半，专门丢给线程池——磁盘慢的时候不能卡住交易

读快照则简单得多：`os.replace` 保证 `latest.json` 永远是完整文件，面板随便读，
不会读到半截 JSON。

### 3.3 内嵌网页面板：挂在引擎自己的 reactor 上

```
        ┌──────────────────────── 引擎进程 ────────────────────────┐
        │                                                          │
        │   reactor ──┬── MarketCenter / StrategyManager / ...      │
        │             │                                            │
        │             └── Site ── MonitorResource                  │
        │                          ├── GET  /            → HTML    │
        │                          ├── GET  /api/overview         │
        │                          ├── GET  /api/tasks[/<id>]     │
        │                          ├── POST /api/tasks            │
        │                          └── POST /api/engine/stop       │
        └──────────────────────────────────────────────────────────┘
```

**为什么内嵌而不是另起一个进程读文件**：面板读的是**内存里的真状态**，
不是"每 3 秒刷一次的文件副本"。延迟为零，也就不存在"面板上看到的和实际在跑的不一致"。

面板是自包含的单文件 HTML（内联 CSS/JS，**不引任何 CDN**）——
内网、离线环境必须能打开。权益曲线用 canvas 手绘，涨红跌绿跟随 A 股习惯，
深色模式走 `prefers-color-scheme`。

`MonitorWebComponent.is_busy()` 返回 `False`：监控组件**不参与**"引擎是否空闲"的判定，
否则回测引擎会因为"面板还在工作"而永远不触发自动停止。

端口被占用时面板降级为不可用并打日志，**引擎继续跑**——
监控是"可选增强"，不是"关键路径"。

`run_id` 只有一个来源（引擎上下文）：面板上显示的、快照目录名、事件里的必须永远一致，
否则同一个页面上会出现两个 run_id，运维根本没法判断该去看哪个目录。

> 有个容易踩的坑：面板和引擎共用同一个 reactor，所以**任何探测面板的代码都不能
> 跑在 reactor 线程上**（`urllib` 阻塞等自己的 HTTP 响应会死锁）。
> 冒烟脚本里的探针全部包在 `threads.deferToThread` 里，真实调用方
> （浏览器、CLI）天然是另一个进程，不存在这个问题。

### 3.4 后台运行：一个引擎进程

```
python main.py start    →  Popen(分离会话)  →  service_runner
        │                                             │
        │  写 PID 文件（子进程自己写 os.getpid()）      │  写 PID / 装停止看门狗
        │  父进程 sleep 后回读，修正真实 PID            │  engine.start() → reactor.run()
        ▼                                             ▼
   status / ctl / snapshot                   轮询"停止标记文件" → 优雅退出
```

两个跨平台的坑，都写在这里免得再踩：

1. **停服务用"停止标记文件"而不是信号**。Windows 没有 SIGTERM，
   `taskkill` 是强杀，信号方案在 Windows 上根本不成立。
   服务侧用 `LoopingCall` 轮询标记文件，看到就优雅退出。
2. **Windows 上 venv 的 `python.exe` 是个启动器壳**，它 `spawn` 出真正的解释器后
   自己退出/等待，所以 `Popen.pid` **不是**引擎进程的 PID。
   解法：让子进程把 `os.getpid()` 写进 PID 文件，父进程稍后回读覆盖，
   原来的壳 PID 记在 `launcher_pid` 里备查。

### 3.5 多服务协作：文件消息总线 + supervisor

单进程引擎有天花板：新闻抓取、大模型分析、不同频率的策略混在一起会互相拖累。
所以支持**多进程 supervisor**：一个 hub 进程拉起并看护 N 个独立引擎进程。

```
config/services.json
   ├── market   行情服务：拉数据 → 发布到总线
   ├── alpha    策略服务：订阅总线 → 跑自己的策略（自己**不**连行情源）
   ├── beta     策略服务：订阅行情 + 新闻 → 跑另一套策略
   └── news     新闻服务：抓新闻 → 分析 → 发布到总线
                         ▲
                         │  文件消息总线 output/bus/*.jsonl
                         └─ 零依赖、可审计、进程崩了消息也不丢
```

**为什么不用真正的 MQ**：量化服务天然是"低吞吐、强顺序、要能事后审计"的场景，
文件足够；换来零依赖、跨平台，以及出问题时 `cat output/bus/bar.jsonl` 就能看清一切。

总线的三个关键设计：

| 设计 | 为什么 |
|---|---|
| 进度用 **byte offset** 落盘 | 进程重启从上次位置继续，不重复回放整个积压 |
| 默认**不消费自己发的消息** | 否则两个服务会互相转发，无限循环（自激） |
| 进度里再存一个**头部指纹** | 纯 offset 分不清"文件被追加"和"文件被清空后重写成了差不多长"——后者在文件重新长到 ≥ 原 offset 后看起来完全合法，订阅者会**静默读到错位数据**。指纹一变化就从头重放 |

另外：单次 poll 最多读 8MB（防止落后太久把内存打满）、
只消费**完整的行**（写到一半的行留给下次）、
进度文件**不做 fsync**（丢了只会重放少量消息，消息本身已经在磁盘上）。

hub 还负责三件事：

- **存活看护**：心跳超时就重启。但要注意"回测服务正常跑完"不算挂——
  `state.json` 里有 `stopped_at` 的一律不重启，否则回测服务会被无限复活
- **指令投递**：把控制指令以命令文件写进目标服务的 `commands/` 目录，
  目标服务轮询取走执行、归档到 `done/`。这样 hub 不需要知道服务内部长什么样
- **聚合面板**：把所有服务的状态收敛到一个页面，点卡片能跳到该服务的详细面板

### 3.6 行情采集服务：让数据自己长出来

策略要高频数据，就不能靠"需要了再手动跑一次 `collect`"。
采集服务是一个标准引擎组件（`DataCollectorComponent`，名 `data_collector`），
常驻在数据流转的最上游。

**它为什么是必需的，不是选配**：`market_mode=poll` 每隔一段时间读一次"最新行情"，
但如果没人往库里写新数据，它读到的永远是同一批旧行——
**服务像在跑，行情其实从未更新**。采集服务补的就是这个缺口。

所以装配器里自动开启，不需要用户理解这件事：

| 场景 | 采集是否开启 | 理由 |
| --- | --- | --- |
| `market_mode=poll` | ✅ 自动开 | 不采集就是假在跑 |
| `market_mode=replay` | ❌ 自动关 | 回放的数据本来就是死的 |
| `RUN_MODE=BACKTEST` | ❌ 自动关 | 离线回放读历史快照，再实时采集既没意义也拖慢启动 |

#### 分桶调度

```
CollectJob(symbol × period × interval)
   │
   ├── 按 interval 分桶 ──► 5m 桶 ──► 一个 LoopingCall ──► 线程池串行跑
   ├──                      30m 桶 ──► 一个 LoopingCall ──► 线程池串行跑
   └──                      1m 桶  ──► 一个 LoopingCall ──► 线程池串行跑
```

三个决定都是被现实逼出来的：

| 决定 | 原因 |
| --- | --- |
| **同一频率合并成一个定时器** | 20 个标的各起一个 LoopingCall，reactor 会把时间浪费在定时器调度上；而同频率的采集天然可以串行做 |
| **必须走线程池** | 采集是同步 HTTP。放在 reactor 线程会把行情、策略、撮合、面板**全部一起卡住** |
| **上一轮没跑完就跳过本轮** | 网络慢时最忌讳请求堆积——堆积只会让数据源更慢，最后被限流，比漏采一次严重得多 |

#### 采集 semantics（这一层的正确性最容易错）

- **增量采集，不全量重下**：日线每次只回看 `lookback_days` 天。
  有人会问"已经有的数据为什么要重复采"——这段重复是必要的：
  **前复权价格会因除权除息被整体重算**，停牌数据也会回填。
  只看"最新一天"永远修不好旧账。
- **分钟线限交易时段**：默认 `trading_hours_only=null`（自动）——
  日内数据限交易时段（收盘后留 **30 分钟缓冲**，数据源需要时间结算，
  没有缓冲就会系统性漏掉当天最后一根）；日线一天只变一次，不限时段反而更好
  （周末也能补到收盘价）。
- **频率有下限**：`min_interval` 默认 15 秒，写 `1s` 会被自动夹紧并告警。
  配置写错导致采集失败，比服务起不来（那是真没数据）轻微得多。
- **失败按标的隔离**：一个标的失败（停牌/退市/代理不通）记录进它自己的
  `last_error`，兄弟任务照跑，服务健康判定也不受影响——只有**全部**失败才算不健康。

#### 周线为什么是"逐根因果"的

`WeeklyMAIndicator` 把日线聚成周线再取 N 周滚动均值。这里有个极易出错的点：

站在周三时，"本周"的代表价应该是什么？

- 用**上一周**的值 → 整周滞后。任何周金叉信号都会晚一周才被发现。
- 等到**周五**再算 → 信号滞后到周末。
- 用**本周周五**的收盘（哪怕还没到）→ **未来函数**。回测用它赚到的钱，实盘一分都拿不到。

正确答案是：**本周就用已有最后一根**（周三=周三的收盘价），逐根向前推进。
这样周内就能捕捉到交叉，且站在任意时刻只用当时已知的信息。

`scripts/weekly_ma_check.py` 与 `scripts/weekly_factor_check.py` 各自用一段
独立的 pandas 实现（同样逐根截断重算）交叉验证，偏差量级 1e-14。

---

## 四、扩展点

| 想做的事 | 在哪扩展 | 怎么做 |
|---|---|---|
| 加一个自定义因子 | `app/core/factor/builtin.py` | 继承 `IBaseFactor`，加 `@register_factor`，实现 `compute(ctx)` |
| 加一个自定义指标 | `app/core/indicator/builtin.py` | 继承 `IBaseIndicator`，加 `@register_indicator` |
| 加一条风控 | `app/core/risk/builtin.py` | 继承 `IBaseRiskRule`，加 `@register_risk` |
| 组合出一个新策略 | **不用写代码** | 在 `config/tasks.json` 里用 `all/any/not` + 规则拼装 |
| 加一个新闻源 | `app/core/news/source.py` | 继承 `IBaseNewsSource`，加 `@register_news_source` |
| 接大模型 | `settings.LLM_API_KEY` / `NEWS_ANALYZER=llm` | 未配 key 时自动降级到规则版，服务照常跑 |
| 换券商 | `app/core/execution/broker.py` | 实现 `IBaseBroker`（当前是 `SimulatedBroker`） |

### 策略组合示例（纯配置，零代码）

```json
{
  "type": "combo",
  "entry": { "all": [
      { "factor": "rsi", "op": "lt", "value": 35, "params": { "period": 14 } },
      { "factor": "price_position", "op": "lt", "value": 0.5, "params": { "period": 20 } }
  ]},
  "exit":  { "factor": "rsi", "op": "gt", "value": 65, "params": { "period": 14 } },
  "event_entry": { "all": [
      { "factor": "news_impact",     "op": "gt",  "value": 0.3 },
      { "factor": "news_confidence", "op": "gte", "value": 0.5 }
  ]}
}
```

`entry/exit` 走行情通道，`event_entry/event_exit` 走新闻通道。
`event_requires_trend=true` 可要求"利好来了但趋势仍是空头就不买"。

---

## 五、目录结构

```
app/core/
├── market/        领域模型（Bar/Tick/Signal/Order/Position/Account/NewsItem）+ BarSeries 滚动窗口
├── indicator/     指标层：SMA/EMA/RSI/MACD/ATR/BIAS/STD/VOL_MA + **周线 WMA**，注册表可插拔
├── factor/        因子层：price/ma/ma_spread/rsi/momentum/volatility/news_*
│                  + 周线族 **wma / wma_spread / wma_cross_up / wma_cross_down
│                    / wma_trend_up / wma_trend_down**，FactorContext 统一输入
├── collect/       采集编排层（**不依赖 Twisted**，纯策略问题单独可测）
│   └── spec.py        频率解析 / 交易时段判断 / CollectJob / CollectorSpec / 配置加载
├── rule/          规则层：All/Any/Not 组合器 + 阈值/穿越规则，spec 支持声明式构造
├── strategy/      策略层：Combo/MaCross/NewsDriven，双通道 decide()
├── risk/          风控层：闸门链，任一否决即拒绝；支持 scale 缩仓
├── portfolio/     组合层：Portfolio 账本 + Sizer（fixed/percent/all_in）
├── execution/     执行层：SimulatedBroker（即时成交，无挂单）
├── task/          任务层：QuantTask 实例 + TaskRuntime 容器 + 声明式 spec
├── news/          新闻层：RSS/Atom 源 → 去重 → Keyword/LLM 分析 → NewsEvent
├── monitor/       可观测层
│   ├── dashboard.py   自包含面板 HTML/CSS/JS（零 CDN，canvas 画权益曲线）
│   ├── web.py         MonitorWebComponent + /api/* 路由（挂在引擎 reactor 上）
│   └── snapshot.py    SnapshotStore：采集 / 原子落盘 / 订单流 / 历史滚动
├── bus/           跨进程协作层
│   ├── filebus.py     FileBus：JSONL append + byte offset 增量读 + 头部指纹防错位
│   └── components.py  BusPublisher / BusSubscriber 两个标准引擎组件
├── hub/           多服务编排层
│   ├── spec.py        services.json 解析（ServiceSpec / HubSpec）
│   ├── worker.py      单个服务进程：搭引擎 + 心跳 + 收命令
│   ├── supervisor.py  hub 侧：拉起 / 看护 / 重启 / 投递指令
│   ├── panel.py       聚合面板 HTML + /api/*
│   └── hub.py         hub 入口
└── engine/
    ├── engine.py      引擎主体（组件生命周期、优雅退出、空闲看门狗、快照）
    ├── builder.py     装配器：config/tasks.json → 一台可运行的引擎
    ├── control.py     控制中心：增删任务 / 启停组件 / 指令入口 / 运行时任务持久化
    ├── service_runner.py  被守护进程拉起的那个"服务进程"
    ├── daemon.py      PID 文件、分离进程、停止标记、跨平台差异
    ├── components/    MarketCenter / StrategyManager / NewsCenter / Timer / TaskScheduler
    │   └── collector.py  DataCollectorComponent：分桶调度 + 线程池 + 失败隔离
    └── event.py       EventBus + StandardEvents
```

---

## 六、运行

```bash
# 后台常驻服务（poll 模式会自动挂上采集服务）
python main.py serve --mode SIMULATE --market-mode poll --interval 60
python main.py serve --market-mode poll --collect-interval 30s

# 离线回放验证（推完自动停，不联网；不启采集）
python main.py serve --mode BACKTEST --interval 0.01

# 真正丢到后台跑
python main.py start && python main.py status
python main.py ctl overview && python main.py ctl tasks

# 采集服务
python main.py ctl collector status
python main.py ctl collector interval 1m --symbol 600519
python main.py ctl collector add 600519 --period 5 --interval 1m
python main.py ctl collector now --symbol 000001

# 多服务协作
python main.py hub services
python main.py hub run

# 查看能力清单
python main.py factors      # 指标 + 因子
python main.py rules        # 规则类型 + 策略 + 风控
python main.py tasks        # 已配置的任务

# 复盘
python main.py snapshot --tasks --orders 20

# 单次回测（backtrader 链路，与实时链路独立）
python main.py backtest -s 000001 --start 2023-01-01 --end 2024-06-30
```

---

## 七、设计约束（踩过坑才写在这里）

1. **风控只能否决，不能下单**。强制平仓这类动作必须放在 `QuantTask._check_max_hold`，
   否则"冷却期"之类的规则会把平仓卡死。
2. **卖出量不允许超过持仓**。`Portfolio.apply_fill` 里做了二次截断，
   杜绝净做空（上一轮的僵尸止损单就是这么凭空做出 -7600 股的）。
3. **构造参数不能与基类方法同名**。`self.size = ...` 会把 `size()` 方法覆盖成 int，
   报错是 `'int' object is not callable`，极难定位。`IBaseComponent.initialize` 和
   `build_sizer` 里都加了显式检测。
4. **阻塞 IO 必须走线程池**。新闻抓取、LLM 调用、总线读取、快照落盘都是同步 IO，
   直接在 reactor 线程跑会卡死整个引擎（行情和交易一起停）。
5. **风控自身崩溃按放行处理**。宁可漏一次风控，也不能让风控把系统锁死。
6. **新闻分析失败要降级**。拿不到结论时给出 `confidence=0` 的事件，
   由下游 `news_confidence` 风控拦掉，而不是静默丢弃。
7. **控制面的返回值必须诚实**。"暂停成功"必须真的停下来了；
   没生效就返回失败并说明原因——谎报成功会让运维完全失去判断依据。
8. **枚举序列化要在基础类型之前判断**。本项目大量枚举是 `class Side(str, Enum)`，
   若先走 `isinstance(obj, str)` 分支，枚举会被原样返回，
   JSON 里就成了 `"<Side.BUY: 'BUY'>"` 而不是 `"BUY"`。
9. **状态采集与落盘必须分线程**。遍历任务字典取快照只能在 reactor 线程做，
   写盘则丢线程池；反过来就会在任务增删时抛
   `dictionary changed size during iteration`。
10. **回测服务跑完不算挂**。supervisor 自动重启前必须检查 `stopped_at`，
    否则一个正常结束的回测服务会被无限复活。
11. **节点不消费自己发布的消息**。否则两个服务互相转发，直接无限循环。
12. **删除要小心"看起来一样长度"的文件**。总线进度只记 byte offset 是不够的，
    必须配一个头部指纹，否则文件被重写后会静默读到错位数据。
13. **进程退出只认停止标记文件**。Windows 没有 SIGTERM，信号方案跨平台不成立。
14. **venv 的 python.exe 是壳**。`Popen.pid` 不等于引擎 PID，必须让子进程自报。
15. **面板探测不能跑在 reactor 线程上**。会等自己的 HTTP 响应而永久阻塞。
16. **采集不能全量重下**。前复权价格会因除权除息整体重算、停牌数据会回填，
    所以必须每次重扫一个 `lookback_days` 窗口，只看"最新一天"永远修不好旧账。
    但也不能每次全量——几十个标的每轮重下等于给数据源做压力测试，然后被限流。
17. **上一轮没跑完必须跳过本轮**。慢网络下堆积请求只会让数据源更慢，
    最后一个都采不到——漏采一次比雪崩式限流轻得多。
18. **定时器只能在组件 RUNNING 时起**。`set_interval` 若在 INITIALIZED 阶段就起定时器，
    `on_start` 又会按桶再起一轮，同一个频率出现两个定时器 → 实际频率翻倍。
19. **立即采集必须异步**。同步等待会把调用方线程堵死；若调用方恰好是 reactor，
    行情、策略、撮合、面板会一起停摆。所以 `collector_now` 默认是"丢进线程池即刻返回"。
20. **周线因子不能用 ffill/bfill 对齐**。`ffill` 整周滞后，`bfill` 是未来函数
    （周一就知道周五收盘）。本周就必须用"已有最后一根"，逐根因果推进。
    写参照实现时要逐根截断重算，否则验证本身就在骗人。
21. **配置与命令行的采集标的要合并，不要二选一**。二选一会让用户纳闷
    "我在命令行指定的标的为什么没被采集"；同时要按 `symbol:period` 去重，
    否则同一个标的会被采两遍，白白翻倍消耗数据源配额。
