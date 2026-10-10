# AlphaQuant 架构与数据流

> 这份文档回答一个问题：**从一根行情到一笔成交，中间到底发生了什么，每一跳由谁负责。**
>
> 建议阅读顺序：先看「一、全景」建立地图，再看「二、数据流闭环」理解一条数据的旅程，
> 然后按需要跳到具体章节。想动手就直奔「七、操作闭环」。

---

## 一、全景

### 1.1 一句话架构

**一个 Twisted reactor 进程 = 一个引擎 = N 个互相隔离的量化任务；
数据从采集流进来，经清洗、指标、因子、规则、策略、风控，变成订单送出去，
成交回报再流回来更新账本 —— 然后喂给下一轮采集，形成闭环。**

### 1.2 分层地图

```
┌─────────────────────────────────────────────────────────────────────────┐
│ L0 数据来源                                                               │
│   行情源(baostock/akshare/本地CSV/数据库)      新闻源(RSS/Atom)            │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ①
┌───────────────────────────▼─────────────────────────────────────────────┐
│ L1 采集与清洗                    app/core/collect/  app/core/market/clean │
│    行情采集服务（分桶定时器）→ 数据清洗（去重/排序/校验/标记）              │
│    新闻中心（RSS → 去重 → 关键词或大模型分析）                             │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ②
┌───────────────────────────▼─────────────────────────────────────────────┐
│ L2 存储                          app/db/  app/repository/                 │
│    SQLite: 日线/分钟线（行情）  +  订单/成交/权益/事件（交易审计）          │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ③
┌───────────────────────────▼─────────────────────────────────────────────┐
│ L3 计算（纯函数、无副作用、可复现）  app/core/indicator/  app/core/factor/ │
│    Indicator 指标 → Factor 因子     （带共享缓存，按序列版本号失效）        │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ④
┌───────────────────────────▼─────────────────────────────────────────────┐
│ L4 决策（只读、可组合）          app/core/rule/  app/core/strategy/        │
│    Rule 规则(All/Any/Not/阈值/穿越) → Strategy 策略 → Signal 交易意图      │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ⑤
┌───────────────────────────▼─────────────────────────────────────────────┐
│ L5 风控前置检查（闸门，只否决/缩仓，绝不下单）  app/core/risk/             │
│    回撤 / 仓位 / 资金 / 冷却 / 新闻置信度 / 单日亏损                       │
│    + A股实盘特有：T+1 可卖 / 涨跌停 / 单笔金额 / 单日笔数 / 可卖数量        │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ⑥
┌───────────────────────────▼─────────────────────────────────────────────┐
│ L6 仓位与执行                    app/core/portfolio/  app/core/execution/ │
│    Sizer 仓位计算 → Broker 撮合（模拟即时成交 / 实盘报单待回报）           │
│    实盘：IBrokerGateway 券商网关（下单/撤单/查询/回报）                    │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ⑦
┌───────────────────────────▼─────────────────────────────────────────────┐
│ L7 回报与记账                                                             │
│    成交回报 → Portfolio.apply_fill → 持仓/资金更新 → 事件总线 → 落库/面板 │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │ ⑧ 新数据落库后回到 ①
                            └──────────────►（闭环）
```

### 1.3 三种运行模式的关键差别

同一个引擎、同一套策略代码，靠 `RunMode` 切换运行环境。
**最容易出事的地方就是这张表的第三行**：

| | BACKTEST 回测 | SIMULATE 模拟盘 | LIVE 实盘 |
|---|---|---|---|
| 行情来源 | 历史回放（读完即停） | 实时抓取（poll） | 实时抓取（poll） |
| 撮合 | `SimulatedBroker`，submit 即成交 | 同左 | `LiveBroker`，submit 只是**报单** |
| 成交时机 | 下单那一刻 | 下单那一刻 | **之后的某个时刻**（回报驱动） |
| 持仓更新 | submit 之后立刻 | 同左 | `on_fill` 回调里 |
| 未结订单 | 永远为 0 | 永远为 0 | 会真实存在，可撤 |
| 采集服务 | 装配但停用 | 启用 | 启用 |
| 券商网关 | 无 | 无 | `LiveGatewayComponent`（必填） |

> ⚠️ **本节最容易踩的坑**：任务级 `run_mode` 默认是"跟随引擎"（空串）。
> 如果代码里硬编码成 SIMULATE，用 LIVE 起引擎时任务仍在模拟撮合 ——
> 面板上一切正常，但**一笔真单都没发出去**。所以 `TaskSpec.run_mode` 默认空串，
> 由 `TaskRuntime.run_mode`（引擎下沉）决定。

---

## 二、数据流闭环

下面是一条数据从产生到影响下一次决策的完整旅程。每一跳都标注了**责任文件**。

### 2.1 全景时序

```
                    ┌─────────────────────────────────────────────┐
   ① 采集           │ DataCollectorComponent                       │
                    │  按 interval 分桶 → 一个频率一个 LoopingCall  │
                    │  线程池执行（绝不在 reactor 线程做 HTTP）      │
                    └───────────────┬─────────────────────────────┘
                                    │ MarketDataService.collect_*
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ② 清洗+入库      │ BarCleaner.clean_frame()                     │
                    │  排序 / 去重 / 剔除无价行 / OHLC 校验         │
                    │  标记 停牌·涨跌停·异常跳变（默认只标记不丢）   │
                    └───────────────┬─────────────────────────────┘
                                    │ StockRepository / MinuteRepository
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ③ 存储           │ SQLite                                       │
                    │  stock_daily / stock_minute    ← 行情        │
                    │  order_record / trade_record / equity_point  │
                    │  system_event / backtest_result ← 交易与审计 │
                    └───────────────┬─────────────────────────────┘
                                    │ MarketCenterComponent 读库
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ④ 推送行情       │ BAR_RECEIVED 事件（按 symbol 广播）           │
                    └───────────────┬─────────────────────────────┘
                                    │ EventBus
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑤ 指标与因子     │ FactorContext.ind("sma", period=20)          │
                    │  统一入口 → indicator_series 共享缓存         │
                    │  缓存键=(指标名,参数,序列 revision)           │
                    └───────────────┬─────────────────────────────┘
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑥ 策略决策       │ Strategy.decide(ctx, state, source)          │
                    │  Rule 组合（all/any/not + 阈值/穿越）         │
                    └───────────────┬─────────────────────────────┘
                                    │ Signal（标记来源 bar/news/llm）
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑦ 风控前置       │ RiskChain.check(signal, state, ctx, account) │
                    │  任一否决 → REJECTED（留痕，不下单）          │
                    │  可返回 scale 缩小仓位                        │
                    └───────────────┬─────────────────────────────┘
                                    │ RiskVerdict
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑧ 仓位计算       │ Portfolio.plan_size(side, price, strength,   │
                    │                      scale)                  │
                    │  100 股整手对齐；A股禁止裸做空                │
                    └───────────────┬─────────────────────────────┘
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑨ 订单执行       │ 回测/模拟: SimulatedBroker.submit()          │
                    │             └─ 即时成交，直接进 ⑩            │
                    │ 实盘:      LiveBroker.submit()               │
                    │             └─ 本地前置校验（价格笼子/金额）   │
                    │             └─ gateway.place_order() → SUBMITTED
                    └───────────────┬─────────────────────────────┘
                                    │
      ┌─────────────────────────────┘（实盘在这里分叉）
      ▼
                    ┌─────────────────────────────────────────────┐
   ⑨' 回报轮询      │ LiveGatewayComponent._poll_once()            │
                    │  gateway.poll_fills()      ← 成交回报        │
                    │  gateway.poll_order_updates() ← 撤单/拒单    │
                    │  按 client_order_id 路由到对应任务的 Broker   │
                    └───────────────┬─────────────────────────────┘
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑩ 记账           │ LiveBroker.on_fill()                         │
                    │  幂等（同一 fill_id 只处理一次）              │
                    │  卖出不超持仓（账本二次截断）                  │
                    │  → Portfolio.apply_fill() 持仓/资金更新       │
                    │  → QuantTask._on_broker_update() 计数器复位   │
                    └───────────────┬─────────────────────────────┘
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑪ 事件与落库     │ ORDER_FILLED / POSITION_UPDATED → EventBus   │
                    │ TradingStoreComponent 订阅 → TradingRepository│
                    │  后台线程批量写 SQLite（交易链路不等 fsync）   │
                    └───────────────┬─────────────────────────────┘
                                    ▼
                    ┌─────────────────────────────────────────────┐
   ⑫ 观测与干预     │ 网页面板 / CLI / 运行快照                     │
                    │  内存状态 → 面板（零拷贝，读的是真状态）       │
                    │  重要事件 → SQLite（可按条件查、可聚合）       │
                    │  内容变化 → 快照文件（复盘"当时长什么样"）     │
                    └───────────────┬─────────────────────────────┘
                                    │ 人工干预：加任务/改频率/撤单
                                    └──────────► 回到 ①
```

### 2.2 为什么"采集 → 清洗 → 存储 → 计算"顺序不能乱

| 顺序 | 如果调换会怎样 |
|---|---|
| 先清洗再入库 | ✅ 正确。库里应该是**干净**的数据，任何读库的路径都不用重复清洗 |
| 先入库再清洗 | ❌ 库里留下脏数据；分钟线尤其致命——重复行会让成交量累计翻倍 |
| 计算时再清洗 | ❌ 每个指标都要重洗一遍；而且策略直接读的是脏数据，等于没有这层 |

### 2.3 为什么"风控前置"必须在仓位计算**之前**

风控拿到的是 `Signal`（意图），不是 `Order`（已定量的单）。
这个顺序决定了风控**无法**精确判断金额——所以：

- 风控层用 `可用资金 × signal.strength` **估算**意图规模（粗粒度早期拦截）
- `LiveBroker` 在拿到最终下单量后做**精确**的金额上限校验（终局拦截）

两层配合：风控层省下一次报单，Broker 层保证不会超。反过来把精确校验塞进风控层，
就得让风控先算仓位——那风控和 sizer 的职责就纠缠在一起了。

---

## 三、目录与责任划分

```
app/
├── cli.py                 统一命令行入口（唯一的人机接口）
├── core/
│   ├── config.py          全局配置（全部可用环境变量覆盖）
│   ├── collect/           采集编排：频率解析 / 交易时段 / 任务定义（不依赖 Twisted）
│   ├── market/
│   │   ├── types.py       领域模型：Bar/Tick/Signal/Order/Position/Account/News
│   │   ├── series.py      BarSeries 滚动窗口（revision 供缓存失效判断）
│   │   └── clean.py       数据清洗：排序/去重/校验/标记
│   ├── indicator/         指标层（注册表 + 参数化 + 结果缓存）
│   ├── factor/            因子层（FactorContext 是唯一数据入口）
│   ├── rule/              规则层（All/Any/Not + 阈值/穿越，声明式构造）
│   ├── strategy/          策略层（双通道 decide）+ discovery.py（用户策略发现）
│   ├── risk/              风控层（闸门链）
│   ├── portfolio/         组合层（账本 + Sizer）
│   ├── execution/
│   │   ├── broker.py      模拟撮合（submit 即成交）
│   │   ├── gateway.py     券商网关接口 + 内置模拟网关 + 网关注册表
│   │   ├── endpoints.py   券商接入点（网关类型 + 资金账号 + 凭据来源）
│   │   └── live_broker.py 实盘撮合（回报驱动 + 对账）
│   ├── task/              任务层（QuantTask + TaskRuntime）
│   ├── news/              新闻层（RSS → 去重 → 分析）
│   ├── engine/
│   │   ├── engine.py      引擎主体（组件生命周期/优雅退出/空闲看门狗）
│   │   ├── builder.py     装配器：config → 一台可运行的引擎
│   │   ├── control.py     控制中心（唯一控制面）
│   │   ├── daemon.py      后台守护（PID/分离进程/停止标记）
│   │   ├── service_runner.py  被守护进程拉起的服务进程
│   │   └── components/    全部引擎组件
│   ├── monitor/           可观测层（面板 HTML / API / 快照 / 回测作业）
│   ├── bus/               跨进程文件消息总线
│   └── hub/               多服务编排（supervisor）
├── db/                    SQLAlchemy 模型（行情 + 交易审计）
├── repository/            数据仓储（幂等 upsert / 批量异步写）
└── utils/                 jsonio（原子写）/ logger（分级 + 节流）

strategies/                用户自定义策略（自动发现；升级框架不覆盖）
config/brokers.json        券商接入点
config/backtest.json       回测默认参数
```

### 3.1 引擎组件一览（这就是"服务"的真身）

引擎只认组件，不认业务。所有能力都是组件，按注册顺序启动、逆序停止：

| 组件名 | 文件 | 职责 | 何时装配 |
|---|---|---|---|
| `data_collector` | `components/collector.py` | 定时把行情拉进库 | **总是**（回测模式装配但停用） |
| `market_center` | `components/market.py` | 读库/回放 → 发 `BAR_RECEIVED` | 除非外部总线供数 |
| `live_gateway` | `components/live_gateway.py` | 连券商 / 轮询回报 / 定期对账 | 仅 `LIVE` |
| `task_scheduler` | `component.py` | 定时任务与异步任务基座 | 总是 |
| `strategy_manager` | `components/strategy.py` | 持有 TaskRuntime，驱动全部任务 | 总是 |
| `news_center` | `components/news.py` | 抓新闻 → 分析 → 发 `NEWS_ANALYZED` | 可关 |
| `trading_store` | `components/trading_store.py` | 订单/成交/权益/事件落 SQLite | 可关 |
| `monitor` | `monitor/web.py` | 网页面板 + 快照落盘 | 可关 |
| （后台单例） | `monitor/backtest_api.py` | 回测作业：提交 → 线程池执行 → 结果落库 | 随面板 |

**"装配"与"启用"是两个概念**：装配 = 组件挂进引擎（面板能看到、能手动触发）；
启用 = 它的定时器真的在跑。采集服务在回测模式下就是"装配但停用"，
面板会显示"已装配 · 已停用（回测模式只读历史数据，不需要联网采集）"——看得见原因。

---

## 四、关键设计决策与理由

这一节记录"为什么这么写"。看懂了这些，改代码时才知道哪里不能碰。

### 4.1 阻塞 IO 一律走线程池

采集 HTTP、新闻抓取、LLM 调用、快照落盘、总线读取 —— 全在 worker 线程。
**reactor 线程一旦被阻塞，行情、策略、撮合、面板会一起停摆。**

推论：面板的探测请求也必须跑在工作线程（面板和引擎共用一个 reactor，
在 reactor 线程发 HTTP 会等自己的响应而永久死锁）。真实调用方（浏览器/CLI）
天然是另一个进程，不存在这个问题。

### 4.2 订单事件必须幂等

`StrategyManagerComponent._publish_order` 对 `(order_id, status, filled_size)` 去重。
原因：任务产出的订单会同时经由 `TaskRuntime._emit`（order_sink）和 `on_bar` 返回值
两条路径到达，两处都发布就是**每个订单发两次**。
落库之后成交记录直接翻倍，胜率、成交笔数全部失真。

### 4.3 批内先按主键收敛，单条失败用 savepoint 隔离

模拟撮合是同步的，所以同一个订单几乎**必然**在同一批落库记录里出现两次
（PENDING 然后立刻 FILLED）。而 `session.get()` 在 flush 之前看不到本批新加的行，
两次都会被判成"新记录" → 两次 INSERT → 唯一约束冲突 → **整批回滚**。

不处理的话，一条订单会让同一批里的运行记录、权益点、事件一起消失——
偏偏审计数据是出问题时最需要的。所以：批内去重 + 每条记录一个 savepoint。

### 4.4 实盘：账本拒绝入账时，订单**不能**标成已成交

`Portfolio.apply_fill` 在 `size<=0 or price<=0` 时会返回一条空记录并丢弃。
如果 `LiveBroker` 不看返回值就标记 FILLED，就会出现
**订单显示已成交、持仓却是空的** —— 上层所有后续决策都建立在错误前提上。
正确做法：保留未结状态 + 告警，交给对账兜底。

### 4.5 实时行情与实盘：已有未结订单就不再下单

回测里订单即时成交，这个判断永远为假、零影响；
实盘里订单会挂一会儿，没有这道保护策略会**每根K线都发一单**。
13 根K线就是 13 笔委托，全部成交后建出的仓位远超计划，资金占用完全不可控。

### 4.6 对账必须是账户级的

一个券商账户对应 N 个策略任务。柜台只会告诉你"账户里有 2900 股 000001"，
不会告诉你是哪个策略买的。逐任务拿自己那一份去比账户总额，
除了"只有一个任务"以外**永远对不上**——全是噪音，真差异反而被淹没。
所以 `LiveGatewayComponent` 把所有任务的持仓/资金汇总后与券商比一次。

### 4.7 快照"有操作才写"，不是"到点就写"

**先分清两件被混为一谈的事**：

| | 触发者 | 落磁盘？ |
|---|---|---|
| 面板轮询（默认 2s） | 浏览器 `setInterval` | **不落盘**，读引擎内存状态 |
| 快照落盘 | 引擎状态变更 | 落 `output/snapshots/<run_id>/` |

默认模式 `on_event` 的触发源是一份**白名单事件**（`TRIGGER_EVENTS`）：
下单/成交/撤单/拒单、任务增删改暂停恢复、组件启停、控制指令。
**`BAR_RECEIVED` 刻意不在里面** —— 把行情放进去就等于回到"行情一动就写盘"，
而行情自己跳动、权益随之浮动**不构成状态变更**（那是同一份状态的不同读数）。

一次回放可能在一秒内产生上百笔订单，逐个写盘纯属浪费。所以有两条合并闸：

- `snapshot_debounce`（默认 1s）：操作后等一小会儿再落盘，同一批操作并成一份
- `snapshot_min_gap`（默认 2s）：两次落盘的最小间隔

实测：冒烟里 15 次操作只落 3 份快照。按时间写的话同样的过程会产生 15 份、
其中大部分内容完全相同。

另外两个模式仍然保留：`on_change`（定期查指纹，覆盖"状态自己漂移"）、
`interval`（要完整时间轴时）。指纹计算剔除时间戳/序号/内存等易变字段，
只比**状态本体** —— 不剔除的话"变了才写"永远成立，等于没做。

> 踩过的坑：阈值型开关里 `snapshot_enabled=None` 表示"用默认"，
> 但代码里 `if snapshot_enabled else` 会把 None 当假值，日志于是写"快照关"
> 而实际开着。**"看起来是关的其实开着"这类日志比没有日志更害人。**

### 4.8 券商接入点：网关类型 ≠ 资金账号

`BROKER_GATEWAY=simulated` 回答的是"用哪种网关实现"，没回答"连谁"。
真实场景里模拟托盘/实盘主账户/实盘小号/备份通道是**同一网关类型、不同接入点**。
把账号写死在代码或环境变量里，切换成本高，而且**容易连错账户**。

`config/brokers.json` 把"网关 + 资金账号 + 连接参数"打包成命名接入点：

- **凭据只写"从哪个环境变量取"**（`credential_env`），所以配置文件可以入库；
  面板/CLI 显示的都是打码值（`mask_secret`）
- 显式指定的接入点找不到会**直接报错**，不静默降级到别的账户
- 接入点的字段按网关构造签名**过滤**后再传（`filter_kwargs`），
  不会因为"多配了一个字段"就 `TypeError` —— 那是接新券商时最容易踩的坑
- `readonly: true` 是首次接入的推荐档位：能查账户/持仓/收回报，但不下单

**实盘账户从券商源读**：`LiveGatewayComponent.refresh_account()` 定期
（默认 30s）查一次资金/持仓并缓存到内存。面板每 2 秒刷新一次，
不可能每次都去问券商（那是秒级网络 IO 且有频率限制）。

### 4.9 SQLite 必须开 WAL 并把等待时间放宽

`app/db/database.py` 在连接时设 `journal_mode=WAL`、`synchronous=NORMAL`、
`busy_timeout=30000`。

不是"优化"，是**必须**：本项目同时有

- 后台线程批量落库（`TRADE_FLUSH_INTERVAL`，默认 2s 一批）
- 面板/CLI 随时查询（`ctl db orders` 之类）

默认的 rollback-journal 模式下，读会拿到共享锁并把写挡回去；默认 5 秒的
`timeout` 在磁盘慢的机器上完全不够，于是报 `database is locked` ——
**看起来像代码 bug，其实只是等得太短**。WAL 让读写互不阻塞，30 秒等待兜住抖动。

### 4.10 指标周期是参数不是指标

`sma` 只有一个，周期靠 `ctx.ind("sma", period=20)`。注册 `sma5/sma10/sma20`
会让注册表爆炸、缓存失效、策略配置无法复现。

指标结果缓存挂在 `BarSeries` 上，用 `revision` 判断失效 ——
**不能用 `len()`**：`deque(maxlen=N)` 满了之后长度恒为 N 而内容在往前滑，
只看长度会读到"上一根K线"的指标值，是**静默的错误信号**。

### 4.11 日志分级：默认只记重要的

| 级别 | 记什么 |
|---|---|
| INFO（默认） | 生命周期、成交、风控否决、采集结果、异常 |
| DEBUG | 逐根K线决策、缓存命中、指标中间值 —— **只在排查问题时开** |

两条防膨胀措施：文件级别可单独设得更严（`LOG_FILE_LEVEL`）；
同一位置的重复日志按时间窗节流，窗口结束时补一条"被压缩了多少次"——信息不丢，只是不重复。

### 4.12 周线因子必须逐根因果

站在周三时，"本周"的代表价应该是**周三自己的收盘**：

- 用上一周的值 → 整周滞后，金叉信号晚一周
- 等周五再算 → 滞后到周末
- 偷看本周周五 → **未来函数**，回测赚的钱实盘一分拿不到

`weekly_ma_check.py` / `weekly_factor_check.py` 用独立参照实现交叉验证（偏差 1e-14）。

### 4.13 自定义策略：回测链路与实盘链路共用一套发现机制

项目里有**两套策略接口**，因为它们运行在完全不同的引擎上：

| 链路 | 基类 | 引擎 | 为什么不能用同一个 |
|---|---|---|---|
| 回测 | `backtrader.Strategy` | `cerebro` | backtrader 自带撮合/指标/分析器，重写代价太大 |
| 实盘/模拟 | `IBaseStrategy` | 本项目事件引擎（Twisted） | 需要回报驱动、T+1、账户级对账 |

但**发现机制是同一套**（`app/core/strategy/discovery.py`）：
扫描目录 → 按文件路径指纹导入模块 → 从模块里挑出各自基类的子类 → 按
`STRATEGY_NAME`（缺省由类名推导）注册。

这样用户可以把同一个想法写成两个类放进一个文件，回测验证过的参数直接用于实盘 ——
而不是"回测里用 5/20，实盘里手滑写成 5/30"。

三个刻意的设计：

1. **单个文件加载失败只跳过它并记日志**。一个写坏的策略不该让整个程序起不来。
   但一定要报出来，否则用户会以为"我的策略没被发现"。
2. **模块名带路径指纹**。不同目录下的同名文件必须互不覆盖 ——
   否则后加载的顶掉先加载的，排查起来莫名其妙。
3. **注册名优先用显式声明的 `STRATEGY_NAME`**。类名一改，配置文件里的引用就断了，
   而显式名字是稳定的。

**声明式策略 `declarative`** 是给"不写代码"的场景准备的：它把项目自己的
因子/规则层接到 backtrader 上（每根K线重建 `BarSeries` → `FactorContext` → 求值规则），
所以**回测和实盘用的是同一套判据**，口径不会漂。规则没配时用默认的 5/20 均线交叉，
但会在日志里明确说"现在跑的是默认规则"。

### 4.14 回测默认值必须有单一来源

初始资金、手续费、滑点、默认区间这类**业务参数**会变，但它们曾经散在三处：
CLI 的 `click` 默认值、面板 HTML 里的 `value="100000"`、作业 API 的硬编码。
三份迟早不一致，于是"命令行跑出来"和"面板跑出来"结果不一样，还很难查。

现在只有 `config/backtest.json` 一个来源，CLI / 面板 / 作业 API 都读它。
优先级：**显式传入 > 配置文件 > 内置兜底值**。
`BacktestDefaults.merge()` 还特意规定"空值不覆盖默认"——面板表单里没填的
字段就是这样传上来的（`None` / `""`），直接覆盖会把默认值冲掉。

---

## 五、观测体系：三种数据各司其职

不要混用，否则要么查不动、要么丢历史：

| | 内存（面板） | SQLite | 快照文件 |
|---|---|---|---|
| 存在哪 | 引擎进程内存 | `data/alphaquant.db` | `output/snapshots/<run_id>/` |
| 何时写 | 不写（直接读） | 事件驱动 + 定期采样 | **有操作时**（`on_event`，可切换） |
| 适合回答 | "现在什么状态" | "某天某任务成交了哪些笔" | "那一刻整体长什么样" |
| 能按条件查吗 | 不能 | ✅ 索引 + 聚合 + join | 不能（顺序读） |
| 重启后还在吗 | ❌ | ✅ | ✅ |
| 会被清理吗 | —— | 长期保留 | 滚动保留 N 份 |

**选择口径**：需要"筛一批行/做聚合"→ 查 SQLite；需要"看当时的完整快照"→ 读快照文件。

```bash
python main.py ctl db orders --status REJECTED      # 所有被拒订单（聚合）
python main.py ctl db trades --task-id ma-000001    # 某任务的成交明细
python main.py ctl db equity ma-000001              # 权益曲线
python main.py ctl db events --category risk        # 风控类事件
python main.py snapshot --tasks                     # 最近一次运行的整体快照
```

---

## 六、运行中能改什么（不用重启）

所有可干预项都收敛在 `ControlCenter`，面板 / CLI / hub 三条入口共用同一套语义。

| 想改什么 | CLI | 面板 |
|---|---|---|
| 加/删任务 | `ctl add` / `ctl task remove` | 任务表单 |
| 暂停/恢复任务 | `ctl task pause` / `resume` | 任务行按钮 |
| 采集频率 | `ctl collector interval 30s` | 采集区块的输入框 |
| 采集标的 | `ctl collector add/remove` | 采集区块表单 |
| 立即补采 | `ctl collector now` | 「立即采集」 |
| 启停组件 | `ctl component disable news_center` | 组件卡片 |
| 运行模式 | 不支持（模式决定撮合方式，改了会前后不一致） | — |
| 快照策略 | `ctl snapshot-mode on_event` | 顶栏下拉框 |
| 立即快照 | `ctl snapshot` | 「立即快照」 |
| 实盘撤单 | `ctl live cancel-all` | 「撤销全部未结订单」 |
| 实盘对账 | `ctl live reconcile` | 「立即对账」 |
| 刷券商账户 | `ctl live account --refresh` | 「刷新账户」 |
| 看接入点 | `ctl endpoints list` | `/api/endpoints` |
| 试连接接入点 | `ctl endpoints check real` | API |
| 热重载自定义策略 | `ctl strategies --reload` | API |
| 跑回测 | `backtest` 命令 | 「回测监控」页表单 |
| 优雅停机 | `stop` | 「停止引擎」 |

> **留痕**：所有控制指令都会记一条 `CONTROL_COMMAND` 审计事件（谁在什么时候改了什么），
> 面板「控制指令留痕」表可以看到。

---

## 七、操作闭环

### 7.1 第一次把系统跑起来

```bash
# 1. 建库（表结构 + 索引）
python main.py init-db

# 2. 补一批历史数据（日常不用做，采集服务会自动跑）
python main.py collect -s 000001 --start 2020-01-01

# 3. 确认库里有什么
python main.py list

# 4. 跑一次回测，确认策略链路通
python main.py backtest -s 000001 --start 2023-01-01 --end 2023-12-31 \
    --strategy precise_ma_cross --export

# 5. 起服务（前台，Ctrl+C 退出）
python main.py serve --market-mode poll
#    → 打开 http://127.0.0.1:8787/ 面板
```

### 7.2 日常：跑起来 → 观测 → 干预 → 复盘

```bash
# ---- 起 ----
python main.py start --market-mode poll --collect-interval 5m
python main.py status                                  # PID / 内存 / 面板地址

# ---- 看 ----
#   浏览器: http://127.0.0.1:8787/   （实盘监控 / 回测监控 两个页面）
python main.py ctl overview                            # 引擎 + 组件状态
python main.py ctl tasks                               # 全部任务
python main.py ctl collector status                    # 采集跑了多少、入库多少
python main.py ctl db events --level WARNING           # 值得注意的事

# ---- 改（不用重启）----
python main.py ctl collector interval 1m --symbol 600519
python main.py ctl add --spec '{"task_id":"rsi-1","symbol":"000001",
  "strategy":{"type":"combo","entry":{"factor":"rsi","op":"lt","value":35}}}'
python main.py ctl task pause rsi-1
python main.py ctl snapshot-mode on_change             # 想连"状态自己漂移"也留档就切这个
python main.py ctl strategies --reload                 # 改完自定义策略热加载

# ---- 复盘 ----
python main.py ctl snapshot                            # 立刻落一份
python main.py snapshot --list                         # 历史 run
python main.py snapshot --tasks --orders 20            # 看最近一次的任务与订单
python main.py ctl db trades --task-id ma-000001       # 按任务查成交
python main.py ctl db backtests                        # 历史回测结果（库里）

# ---- 停 ----
python main.py stop
```

### 7.3 实盘上线路径

```bash
# 1. 先用内置模拟网关把整条实盘链路跑通（下单→回报→账本→对账）
python main.py serve --mode LIVE --market-mode poll \
    --broker-endpoint sim --collect-interval 1m

# 2. 接真实券商：实现一个 IBrokerGateway 子类并注册
#    （见 app/core/execution/gateway.py，只需实现 7 个方法 + 加一个装饰器）
#    然后在 config/brokers.json 里加一个接入点指向它。
CUSTOM_GATEWAY=your_broker   # 环境变量名随你的网关而定

# 3. 先跑只读档位：能查账户/持仓/回报，但不下单
#    brokers.json 里把 "readonly": true，然后：
python main.py brokers check real        # 确认连得上、账号对不对
python main.py serve --mode LIVE --broker-endpoint real
python main.py ctl live account          # 看券商账户是不是你预期的那个

# 4. 确认账本与券商一致后再放行交易（readonly 改成 false）

# 5. 上线前的自检清单
#    □ brokers check 通过，且账号是你以为的那个
#    □ LIVE_MAX_ORDER_VALUE 已设置（单笔金额上限）
#    □ LIVE_MAX_DAILY_TRADES 已设置（单日笔数上限）
#    □ LIVE_PRICE_LIMIT_PCT 合理（价格笼子，默认 2%）
#    □ LIVE_STRICT_RECONCILE=true（对账不一致就阻断交易）
#    □ 策略风控链里包含 t_plus_one / price_limit / daily_trade_limit
#    □ 先用最小仓位跑通一个完整交易日
```

### 7.4 出问题时的排查路径

| 现象 | 先看哪里 |
|---|---|
| 面板显示"未启用" | `ctl overview` 看组件状态；采集停用会带原因 |
| 任务在跑但不下单 | `ctl db orders --status REJECTED` 看是否被风控否决；`ctl db events --category risk` |
| 实盘下单了但持仓没变 | `ctl live status` 看 `unknown` 回报数；确认任务已挂到网关 |
| 连的账号不对 | `ctl endpoints list` 看"使用中"那个；`ctl live account` 看账号 |
| 连不上券商 | `brokers check <endpoint>` 单独试；看它报的是网关未注册还是凭据缺失 |
| 实盘对账不一致 | `ctl live reconcile` 看持仓/资金差异；检查是否有人工交易 |
| 回测赚钱实盘亏钱 | 检查 `t_plus_one` / `price_limit` 是否启用；检查是否有未来函数 |
| 回测结果和以前不一样 | `backtest --show-defaults` 看默认参数是否被 `config/backtest.json` 改过 |
| 自定义策略没出现在下拉框 | `ctl strategies --reload`；看日志里有没有"加载用户策略 xxx 失败" |
| 数据看起来不对 | `ctl db events --category collect`；日志里搜"清洗" |
| 磁盘涨得快 | `ctl snapshot-mode on_event`（默认）；`LOG_LEVEL` 别开 DEBUG；调 `EQUITY_SAMPLE_INTERVAL` |
| 快照太多 / 以为在按秒写 | `ctl snapshot-mode` 看是哪个模式；顶栏刷新频率 ≠ 快照频率 |
| 日志刷屏 | 日志有重复节流；确认 `LOG_LEVEL` 不是 DEBUG |

---

## 八、设计约束速查（踩过坑才写在这里）

按主题分组，每条都是"改了会出真问题"的地方。

**交易正确性**
1. 风控只能否决、不能下单。强制平仓放在 `QuantTask._check_max_hold`。
2. 卖出量不允许超过持仓。`Portfolio.apply_fill` 二次截断，杜绝净做空。
3. 已有未结订单时不再下单（实盘防堆单）。
4. 账本拒绝入账时订单不能标成已成交。
5. 成交回报必须幂等（券商可能重复推送）。
6. 部分成交的日内计数用**累计量差值**，不能"成交一次加一次"。
7. T+1 的 `bought_today` 必须跨日归零，否则第二天所有卖出都会被误拦。

**实盘**
8. 一个券商账户 N 个策略 → 对账必须账户级。
9. 网关共用一条连接（券商不允许同账号多连接）。
10. 撤单是异步的：返回 True 只代表受理，状态要等回报。
11. 下单通信失败 ≠ 没下出去。状态留 SUBMITTED 去对账，**绝不能重发**。
12. 停服务前先撤单，否则会留下"孤儿单"第二天开盘才成交。
13. 连不上券商是致命错误，必须让引擎起不来（不能假装在交易）。

**数据**
14. 采集不能全量重下：前复权价格会因除权除息整体重算，必须回看一个窗口。
15. 上一轮没跑完就跳过本轮（慢网络下堆积请求会导致雪崩式限流）。
16. 清洗默认只标记不丢弃（丢不可逆，且分不清真异常和没想到的合法情况）。
17. 没有价格的行是唯一必须丢的。

**工程**
18. 阻塞 IO 必须走线程池。
19. 枚举序列化要在基础类型之前判断（`class Side(str, Enum)` 会先命中 `str`）。
20. 状态采集与落盘必须分线程（否则任务增删时字典迭代会炸）。
21. 指标缓存不能用 `len()` 判断失效（deque 满了长度不变）。
22. 构造参数不能与基类方法同名（`self.size = ...` 会覆盖 `size()`）。
23. 定时器只能在组件 RUNNING 时起，否则同频率会出现两个定时器。
24. 进程退出只认停止标记文件（Windows 没有 SIGTERM）。
25. venv 的 `python.exe` 是壳，`Popen.pid` 不等于引擎 PID。

**可观测与配置**
26. 快照只能由"操作"触发，`BAR_RECEIVED` **不能**进触发白名单。
27. `snapshot_enabled=None` 表示"用默认"，不能当假值用（会写出骗人的日志）。
28. 接入点的凭据只存环境变量名；面板/CLI 一律打码输出。
29. 显式指定的接入点找不到要**直接报错**，不能静默降级到别的账户。
30. 接入点参数要按网关构造签名过滤，不能原样透传（多一个字段就 TypeError）。
31. 券商账户查询要缓存（秒级 IO + 频率限制），不能每次面板刷新都问一遍。
32. 回测默认值只能有一个来源（`config/backtest.json`），且空值不覆盖默认。
33. 用户策略加载失败只跳过该文件，但**必须记日志**。
34. SQLite 必须开 WAL + 放宽 busy timeout（并发批量写 + 面板读）。
35. `load()` 式的惰性初始化不能用 `if 容器非空: return` 做守卫 ——
    先注册用户扩展会让内置项永远补不上（内置策略曾因此全部消失）。

---

## 九、从哪读代码

按"想搞清楚什么"给路径：

| 想搞清楚 | 从这里开始 |
|---|---|
| 一根K线怎么变成一笔订单 | `app/core/task/task.py` 的 `on_bar` → `_process_signal` |
| 实盘的成交怎么进账本 | `app/core/execution/live_broker.py` 的 `on_fill` |
| 引擎怎么把组件串起来 | `app/core/engine/engine.py`（生命周期）+ `builder.py`（装配） |
| 风控到底怎么判的 | `app/core/risk/builtin.py`（每条规则都很短，读起来快） |
| 回测为什么赚钱实盘亏钱 | `app/core/execution/broker.py` vs `live_broker.py` 的注释对比 |
| 面板 API 有哪些 | `app/core/monitor/web.py` 的 `_route_get` / `_route_post` |
| 快照到底什么时候写 | `app/core/monitor/web.py` 的 `TRIGGER_EVENTS` + `mark_dirty` |
| 采集的频率怎么算的 | `app/core/collect/spec.py` |
| 数据被清洗过什么 | `app/core/market/clean.py` 的 `FLAG_*` 常量与 `CleanReport` |
| 怎么接一家新券商 | `app/core/execution/gateway.py`（接口） + `endpoints.py`（配置） |
| 自定义策略怎么被发现的 | `app/core/strategy/discovery.py` |
| 不写代码怎么定义策略 | `app/strategy/spec_strategy.py`（声明式策略） |
| 回测默认值从哪来 | `app/backtest/defaults.py` |
