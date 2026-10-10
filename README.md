# AlphaQuant

A 股量化交易工具：行情采集 → **数据清洗** → 数据库 / SQLite → 指标因子 → 策略 →
**风控前置检查** → 订单执行 → 成交回报 → 持仓资金更新 → 回到采集，**完整闭环**。

支持**回测 / 模拟盘 / 实盘**三种运行模式、**一个引擎跑 N 个隔离任务**、
**后台常驻 + 网页监控面板（实盘页 + 回测页）+ 多进程协作 + 常驻行情采集**。

> 📖 **想深入理解运行原理（架构、数据流向、为什么这么设计）请看
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。**
> 本文只讲"怎么用"，那份讲"为什么"。

## 快速开始

所有能力都从统一 CLI 进入：

```bash
# 查看全部命令
python main.py --help

# 1. 初始化数据库（首次使用）
python main.py init-db

# 2. 手动补一次日线数据（幂等，可重复执行）
#    日常不用做 —— 采集服务会自动跑，这条只在首次装机/补历史时用
python main.py collect -s 000001 --start 2020-01-01 --end 2026-06-05
python main.py collect -s 000001 -s 600000 --save-csv   # 多标的 + 导出 CSV

# 3. 看库里有什么
python main.py list

# 4. 跑回测（backtrader 链路）
python main.py backtest -s 000001 --start 2023-01-01 --end 2026-06-05
python main.py backtest -s 000001 --strategy precise_ma_cross --cash 200000
python main.py backtest -s 000001 --strategy ma_cross --param fast=5 --param slow=20 --export

# 5. 前台启动交易服务（一个引擎跑多个任务 + 网页面板 + 自动采集）
python main.py serve                          # 常驻：读 config/tasks.json
python main.py serve --market-mode poll --collect-interval 30s   # 每 30 秒采一次
python main.py serve --mode BACKTEST --interval 0.01   # 离线回放，跑完自动停
python main.py tasks                          # 查看已配置的任务
python main.py ctl collector status           # 看采集跑了多少次、入库多少条

# 6. 放到后台跑（守护进程），然后随时看状态、下发指令
python main.py start --mode SIMULATE           # 后台启动（默认面板 :8787）
python main.py status                          # 看 PID / 内存 / 面板地址
python main.py ctl overview                    # 看引擎与组件状态
python main.py ctl add --spec scripts/example_task.json   # 运行中加任务
python main.py ctl task pause ma-000001        # 暂停某个任务
python main.py snapshot --tasks                 # 看最近一次运行的快照
python main.py stop                            # 优雅退出

# 7. 多服务协作（一个 hub 拉起 N 个独立引擎进程）
python main.py hub services                    # 看 config/services.json 定义
python main.py hub run                         # 前台跑 hub（聚合面板 :8899）
python main.py hub status --tasks              # hub + 各服务状态 + 所有任务
```

可用能力清单：`python main.py strategies` / `factors` / `rules`

## 网页监控面板

引擎自带一个**零依赖的网页面板**（Twisted Web 挂在引擎自己的 reactor 上），
浏览器打开 `http://127.0.0.1:8787/` 即可。面板分成**两个页面**：

### 实盘监控页

- **概览**：run_id / 状态 / 模式 / 已运行时间 / 组件健康 / **快照策略与落盘次数**
- **实盘账户**：接入点、账号、档位（只读/可下单）、网关连接状态、回报轮询统计、
  **券商源读到的资金与持仓**（总资产/可用/冻结/市值 + 每个标的的可卖数量）、
  **对账结论**（持仓/资金差异）、**未结订单表**，
  一键「立即对账」/「刷新账户」/「撤销全部未结订单」（LIVE 模式才有）
- **任务表**：每个任务的权益、盈亏、持仓、状态，可直接 暂停 / 恢复 / 删除 / 看详情
- **加任务**：页面上贴一段 JSON 就能新增任务，立刻纳入行情订阅并开始工作
- **行情采集**：装配状态、每个标的跑了多少次/入库多少条、频率就地改、立即补采
- **权益曲线**：canvas 手绘（红涨绿跌，跟随系统深浅色）
- **订单与审计**：成交/被拒订单流水，以及谁在什么时候下过什么控制指令
- **日志尾部**：不登服务器也能看日志

顶栏有**快照策略下拉框**（有操作才存 / 内容变了才存 / 定时存 / 关闭），
改完立即生效。注意：顶栏的 1s/2s/5s 控制的是**本页刷新频率**，与快照落盘无关。

### 回测监控页

- **新建回测**：填标的/区间/策略/资金/参数 → 提交到**后台线程**执行（不阻塞实盘）。
  表单默认值来自 `config/backtest.json`，下面会写明"默认值来自哪、是多少"
- **自定义策略**：下拉框自动包含 `strategies/` 里发现的策略（标 `[自定义]`）
- **声明式规则**：策略选 `declarative` 时会出现「规则定义(JSON)」输入框，
  点「填入示例规则」就有一份能跑的，不用写 Python
- **作业列表**：每个回测作业的状态、耗时、失败原因
- **历史结果**：从 SQLite 读，重启不丢。收益/回撤/夏普/成交/胜率一览
- **回测详情**：权益曲线 + 完整指标 JSON

面板读的是**引擎内存里的真状态**（不是二手副本），控制指令直接进 `ControlCenter`，
与 CLI / hub 共用同一套语义，行为不会漂移。端口被占用时面板会降级为不可用，
**不会影响交易主链路**。

面板跨进程也能用：`python main.py ctl ...` 走的就是面板的 HTTP API，
所以既能在本机用，也能远程管一台跑着引擎的机器。

```bash
python main.py serve --no-monitor        # 不要面板（纯引擎）
python main.py serve --port 9000         # 换端口
python main.py serve --monitor-host 0.0.0.0   # 允许远程访问（注意安全）
```

## 数据清洗：脏数据不会流到策略里

数据源给你的东西**不等于**能喂给策略的东西。`app/core/market/clean.py` 负责中间这道工序：

| 检查 | 为什么必须有 |
|---|---|
| 时间升序 | 倒序数据会让所有"前一根"语义失效 |
| 同时间戳去重 | 重试导致重复行；分钟线还会让成交量累计翻倍 |
| 剔除无价格行 | 没有价格就没有一切（**唯一必须丢的**） |
| OHLC 自相矛盾 | `high < close` 这类数据不会报错，只会让策略在错价格上决策 |
| 停牌 / 涨跌停标记 | 实盘上这些状态根本买不进卖不出，标记出来让策略跳过 |
| 异常跳变标记 | 但**不丢弃** —— "波动大"和"数据错"是两件事 |

**默认只标记不丢弃**：丢弃不可逆，而且分不清"真异常"和"你没想到的合法情况"
（新股首日涨幅本来就大）。带标记的 bar 上 `is_tradable()` 返回 False，策略可以据此跳过。

清洗结果是一份 `CleanReport`（保留了多少、去重多少、各类标记几个），会记进日志与事件表。

## 交易数据落 SQLite

订单、成交、权益曲线、重要事件全部落库，可长期保留、可按条件查、可聚合：

```bash
python main.py ctl db orders --status REJECTED     # 所有被拒订单 + 状态聚合
python main.py ctl db trades --task-id ma-000001   # 某任务的成交明细
python main.py ctl db equity ma-000001             # 权益曲线采样点
python main.py ctl db events --category risk       # 风控类事件
python main.py ctl db backtests                    # 历史回测结果
```

**为什么不继续用 JSON**：「今天这个任务成交了几笔」「所有被风控否决的订单里哪种规则最多」
—— 这些答案天然是"按条件查一批行 / 做聚合"，文件方案一旦要过滤和 join 就崩了。

写入走**后台线程批量提交**：交易主链路绝不等 SQLite 的 fsync。

连接层开了 **WAL 模式 + 30s busy timeout**。这不是可选项：
本项目同时有"后台线程批量落库"和"面板/CLI 随时查询"两种访问，
默认的 rollback-journal 会让读把写挡回去，而默认 5 秒的等待时间
在磁盘慢的机器上根本不够 —— 表现就是 `database is locked`，
看起来像代码 bug，其实只是等得太短。

## 运行快照：事后复盘靠它

**先说一个容易搞混的点：面板每 2 秒刷新 ≠ 每 2 秒落一份快照。**
刷新读的是引擎内存里的实时状态，不写磁盘。快照是另一件事，默认
**只在"有操作"时才写**：

| 模式 | 什么时候写 | 什么时候用 |
| --- | --- | --- |
| `on_event`（默认） | **有操作才写**：下单/成交/撤单/拒单、任务增删改暂停恢复、组件启停、控制指令、引擎启停 | 日常。没操作 = 状态没变 = 写出来也是同一份 |
| `on_change` | 定期检查内容指纹，真变了才写 | 状态会自己漂移、又想留时间轴时 |
| `interval` | 到点就写，不管有没有变化 | 回放复盘要完整时间轴 |
| `off` | 不写历史快照 | 只要面板实时看 |

```bash
python main.py serve --snapshot-mode on_event    # 默认
python main.py ctl snapshot-mode                 # 看当前策略与落盘统计
python main.py ctl snapshot-mode interval        # 运行中切换，不用重启
python main.py serve --no-snapshot               # 完全不要历史快照
```

**行情自己跳动、权益随行情浮动不算"操作"** —— 那是同一份状态。
所以一个安静运行的服务不会往磁盘里堆一堆内容相同的快照。
一次回放可能瞬间产生上百笔订单，这些操作会被合并成一份
（`--snapshot-min-gap`，默认 2s；`on_event` 另有一个 1s 抖动窗口）。

指纹计算时剔除时间戳/序号/内存这类易变字段，只比**状态本体**
（权益/持仓/订单/组件状态）。不剔除的话"变了才写"永远成立，等于没做。

一个真实的对照：冒烟里 15 次操作只落了 3 份快照。按时间写的话，
同样的过程会在 30 秒里产生 15 份、其中大部分内容完全一样。

```
output/snapshots/<run_id>/
├── latest.json          最新一份完整快照（原子写，随时可读）
├── meta.json            这个 run 的基本信息
├── orders.jsonl         订单流水（append-only，只增不改）
└── history/*.json       历史快照，按数量滚动保留
```

每份快照含：引擎状态、所有组件状态与健康度、每个任务的权益曲线 / 成交 / 订单 / 新闻 /
风控规则 / 错误，以及监控自身的统计（含**本次是为什么写的**：`snapshot_reason`）。

```bash
python main.py snapshot                  # 最近一次运行
python main.py snapshot --list           # 所有 run
python main.py snapshot --tasks          # 每个任务的最终状态
python main.py snapshot --orders 50      # 最近 50 条订单
python main.py snapshot --export out.json
```

采集在 reactor 线程（保证不读到半截状态），落盘丢给线程池（不阻塞交易）。

## 实盘：从"下单"到"确认成交"

回测与实盘的分水岭只有一条：**回测里 submit 即成交，实盘里 submit 只是报单**。
成交在之后的某个时刻以**回报**形式到达。这个差别会连锁影响所有下游，
所以实盘用一套独立的撮合实现（`LiveBroker`），把生命周期管住：

```
submit() ──► 本地前置校验 ──► gateway.place_order() ──► SUBMITTED
                │ 价格笼子                             （未结，可撤）
                │ 单笔金额上限                              │
                ▼                                          ▼
            REJECTED                          poll_fills() → on_fill()
                                                           │
                                          ┌────────────────┴────────────────┐
                                          ▼                                 ▼
                                   幂等去重                          卖出不超持仓
                                          │                                 │
                                          └────────► Portfolio.apply_fill ◄─┘
                                                            │
                                                     持仓/资金更新
```

配套能力：

- **券商网关抽象**：`IBrokerGateway` 只做三件事——下单撤单、查询、拉回报。
  换券商 = 写一个网关类（7 个方法），撮合与策略代码一行不动。
  内置 `simulated` 网关，**没接券商也能把整条实盘链路跑通**。
- **账户级对账**：一个券商账户对应 N 个策略。把本地所有任务的持仓/资金汇总后
  和券商比一次（逐任务对账在账户里必然对不上，全是噪音）。
  `LIVE_STRICT_RECONCILE=true` 时对账不一致直接阻断交易。
- **A 股实盘前置风控**：`t_plus_one`（当日买入不可卖）、`price_limit`（涨停不追买、
  跌停不追杀）、`order_value_limit`、`daily_trade_limit`、`sellable_position`。
  这些在回测里无所谓，一上实盘就致命。
- **防堆单**：已有未结订单时不再下单。没有这道保护，策略会每根K线发一单，
  13 根K线就是 13 笔委托。
- **退出前先撤单**：不撤的话进程没了、单还在，第二天开盘才发现成交了。

先不接券商，用内置网关把链路跑通：

```bash
python main.py serve --mode LIVE --market-mode poll \
    --broker-endpoint sim --collect-interval 1m

python main.py ctl live status        # 网关状态 + 回报统计 + 对账结论
python main.py ctl live account       # 券商账户：资金 / 可用 / 持仓 / 可卖
python main.py ctl live account --refresh   # 立刻去券商拉一次
python main.py ctl live reconcile     # 立即对账
python main.py ctl live cancel-all    # 撤销全部未结订单
```

面板的「实盘监控」页直接显示券商账户与持仓（**来自券商源**，不是本地账本的推算），
还有一眼能确认"现在连的是哪个账号"的接入点与档位。

上线清单与排查路径见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 第七节。

## 券商接入点：接哪家、用哪个账号都是配置

`BROKER_GATEWAY=simulated` 只回答了"用哪种网关实现"，没回答"连谁"。
真实场景里会有模拟托盘、实盘主账户、实盘小号、另一家券商的备份通道 ——
它们是**同一个网关类型、不同接入点**。写死在代码或环境变量里，切换成本高，
而且容易连错账户（实盘里代价最高的错误之一）。

`config/brokers.json`：

```json
{
  "default_endpoint": "sim",
  "endpoints": {
    "sim": {
      "name": "本地模拟托盘",
      "gateway": "simulated",
      "account": "SIMULATED",
      "params": { "initial_cash": 100000 }
    },
    "real": {
      "name": "实盘主账户",
      "gateway": "你的网关名",
      "account": "你的资金账号",
      "readonly": true,
      "credential_env": { "password": "AQ_BROKER_PASSWORD" },
      "params": { "host": "127.0.0.1", "port": 0 }
    }
  }
}
```

**凭据不写在配置里**，只写"从哪个环境变量取"（`credential_env`）——
这样本文件可以安全入库，面板/CLI 里显示的也是打码后的值。

`readonly: true` 是首次接入的推荐档位：能查账户/持仓/收回报，但**不下单**。
先用它跑一两天确认账本与券商一致，再改成 `false` 放行交易。

```bash
python main.py brokers list                  # 列出接入点（凭据已脱敏）
python main.py brokers check                 # 试连接默认接入点（只查询，不下单）
python main.py brokers check real            # 试连接指定接入点
python main.py brokers gateways              # 看每种网关能配哪些参数

python main.py serve --mode LIVE --broker-endpoint real
python main.py ctl endpoints list            # 运行中的服务也能看
python main.py ctl endpoints check real
```

配置里的额外字段会被**按网关签名过滤**，不会因为"多配了一个字段"就报 TypeError
（那是接新券商时最容易踩的坑）。显式指定的接入点找不到会**直接报错**，
不会静默降级到别的账户 —— 静默降级是实盘里最危险的行为。

## 自定义策略：写代码或只写配置，都能回测

`strategies/` 目录下的 `.py` 会被**自动发现**，不需要注册、不需要改框架代码：

```bash
python main.py strategies                 # 列出全部（自定义的会标出来）
python main.py strategies --reload        # 改完代码重新扫描
python main.py strategy new my_idea       # 生成一个模板（回测版 + 实盘版）
python main.py strategy dir              # 打印用户策略目录
python main.py ctl strategies --reload    # 运行中的服务热重载
```

一个文件里可以同时写**回测策略**（继承 `backtrader.Strategy`）和
**实盘策略**（继承 `IBaseStrategy`）——同一个想法用同一套参数，
而不是"回测里用 5/20，实盘里手滑写成 5/30"。示例见 `strategies/dual_ma.py`。

**不想写代码？** 用内置的声明式策略 `declarative`，把规则写成 JSON：

```bash
python main.py backtest -s 000001 --strategy declarative \
  --param entry='{"cross_up":{"left":"ma","right":"ma","left_params":{"period":5},"right_params":{"period":20}}}' \
  --param exit='{"cross_down":{"left":"ma","right":"ma","left_params":{"period":5},"right_params":{"period":20}}}'
```

面板的「回测监控」页选中 `declarative` 后会多出一个「规则定义(JSON)」输入框，
点「填入示例规则」就有一份能跑的。用的是**和实盘同一套因子/规则**，
所以回测口径不会和实盘漂移。

一个写坏的策略文件**只会被跳过并记日志**，不会让程序起不来。

## 回测参数：初始资金/手续费/滑点都是配置

`config/backtest.json` 是回测默认值的唯一来源，CLI、网页面板、回测作业 API
三处入口都读它 —— 否则"命令行跑的"和"面板跑的"结果不一样，还很难查。

```json
{
  "symbol": "000001", "strategy": "ma_cross",
  "start": "2020-01-01", "end": "",
  "cash": 100000, "commission": 0.0003, "slippage": 0.001,
  "adjust": "qfq", "data_source": "auto",
  "risk_free_rate": 0.0, "trading_days_per_year": 252
}
```

优先级：**命令行/面板显式传入 > 该配置文件 > 内置兜底值**。`end` 留空表示"到今天"。

```bash
python main.py backtest --show-defaults   # 看当前生效的默认参数
python main.py backtest -s 000001         # 连 symbol 都可以不写
```

## 日志策略：默认只记重要的

```bash
python main.py serve                          # INFO：生命周期/成交/风控否决/异常
LOG_LEVEL=DEBUG python main.py serve          # 调试：逐根K线决策、缓存命中全记
LOG_FILE_LEVEL=WARNING python main.py serve   # 控制台看 DEBUG，文件只留 WARNING
```

| 级别 | 记什么 | 什么时候用 |
|---|---|---|
| INFO（默认） | 生命周期、成交、风控否决、采集结果、异常 | 生产常驻 |
| DEBUG | 逐根K线决策、指标缓存命中、中间值 | 只在排查问题时开 |

两条防膨胀措施：文件级别可单独设得更严（`LOG_FILE_LEVEL`）；
同一位置的重复日志按时间窗**节流**，窗口结束时补一条"被压缩了多少次"——
信息不丢，只是不重复。长跑服务里"每根K线告警一次"会让日志完全失去可读性。

## 多个服务互相配合

单进程引擎有天花板：新闻抓取、大模型分析、不同频率的策略混在一起会互相拖累。
所以支持**多进程 supervisor** 模型：一个 hub 进程拉起并看护 N 个独立引擎进程。

```
config/services.json
   ├── market   行情服务：拉数据 → 发布到总线
   ├── alpha    策略服务：订阅总线 → 跑自己的策略（不自己拉行情）
   ├── beta     策略服务：订阅行情 + 新闻 → 跑另一套策略
   └── news     新闻服务：抓新闻 → 分析 → 发布到总线
                         ▲
                         │  文件消息总线 output/bus/*.jsonl
                         └─ 零依赖、可审计、进程崩了消息也不丢
```

跨进程通信用**文件消息总线**（`app/core/bus/`）：发布者 append JSONL，订阅者按
byte offset 增量读，进度落盘所以进程重启能接着读。语义是「至少一次」，
重复消费由下游幂等兜住（新闻去重、bar_count）。

设计取舍：量化服务天然是「低吞吐、强顺序、要能事后审计」的场景，文件足够，
换来的是零依赖、跨平台，以及出问题时 `cat output/bus/bar.jsonl` 就能看清一切。

hub 还负责：服务心跳与存活看护（挂了自动重启，回测服务正常跑完不算挂）、
把控制指令以命令文件的形式投递给指定服务、聚合所有服务的状态到一个面板。

```bash
python main.py hub run                # 前台跑（Ctrl-C 停）
python main.py hub start              # 后台跑
python main.py hub status --tasks     # 各服务状态 + 所有任务
python main.py hub cmd beta pause_task --task-id beta-rsi-000001
python main.py hub stop               # 停 hub 并带走所有服务
```

聚合面板在 `http://127.0.0.1:8899/`：服务卡片（健康灯 + 启停重启）、
跨服务任务总表、总线主题浏览器，点进卡片可直接跳到该服务自己的详细面板。

## 行情采集服务：数据自己长出来

策略要高频数据，就不能靠"需要了再手动跑一次 `collect`"。
`--market-mode poll` 时引擎会**自动挂上采集服务**，按你设定的频率往库里写数据。

这不是锦上添花，而是 poll 模式能否工作的前提：poll 每隔一段时间读一次"最新行情"，
但没人往库里写新数据的话，它读到的永远是同一批旧行——**服务像在跑，行情其实从未更新**。

### 频率自己定

改 `config/collector.json`，不用动代码：

```json
{
  "enabled": true,
  "interval": "5m",
  "min_interval": 15,
  "trading_hours_only": null,
  "backfill_on_start": true,
  "symbols": [
    "000001",
    { "symbol": "600000", "interval": "30m" },
    { "symbol": "000858", "period": "5", "interval": "5m", "keep_days": 30 }
  ]
}
```

频率写法随心：`30s` / `5m` / `15min` / `1h` / `1d`，或裸数字（按秒）。
粒度 `period` 支持 `1d`（日线）与 `1/5/15/30/60`（分钟线）。

也可以命令行覆盖：

```bash
python main.py serve --market-mode poll --collect-interval 30s
python main.py serve --collect-period 5 --collect-symbol 600519
```

### 运行中随时改

```bash
python main.py ctl collector status              # 采集了多少次、入库多少条、有没有失败
python main.py ctl collector interval 1m         # 全部任务改频率
python main.py ctl collector interval 30s --symbol 600519 --period 5   # 只改一个
python main.py ctl collector add 600519 --period 5 --interval 1m
python main.py ctl collector remove 600519 --period 5
python main.py ctl collector now --symbol 000001 # 立即补采一次
python main.py ctl collector reload              # 改了 json 后热加载
```

面板上也能做同样的事（「行情采集」区块，频率输入框回车即生效）。

### 三条设计约束

| 约束 | 为什么 |
| --- | --- |
| **增量采集，不全量重下** | 三年日线 3 万条 × 几十个标的，每分钟重下就是给数据源做压力测试，然后被限流。日线每次只回看 `lookback_days` 天——这段重复是必要的：前复权价格会因除权除息重算，停牌数据也会回填 |
| **分钟线只在一定时段采集** | 收盘后拿到的已经是死数据。`trading_hours_only` 默认为 `null`（自动）：日内数据限交易时段（收盘后留 30 分钟缓冲，避免漏掉当天最后一根），日线一天只变一次所以不限 |
| **频率有下限，上一轮没跑完就跳过本轮** | `min_interval` 默认 15 秒。两道闸都是从被封 IP 的教训里来的：网络慢时堆积请求只会让数据源更慢 |

单个标的失败（停牌/退市/代理不通）不影响兄弟任务，也不影响服务健康判定——
只有**全部**任务都失败才判定为不健康。

> 分钟线依赖 akshare 的东方财富接口。若你的网络访问不了，日线（baostock）照常可用。

## 周均线体系

`python main.py factors` 里可以看到这一组：

| 因子 | 含义 |
| --- | --- |
| `wma` | 周均线值，`period` 为周数（默认 5） |
| `wma_spread` | 快线减慢线的差值（归一化，可当强度用） |
| `wma_cross_up` | **周金叉**：快线上穿慢线，仅交叉当日为真 |
| `wma_cross_down` | **周死叉**：快线下穿慢线，仅交叉当日为真 |
| `wma_trend_up` | **趋势向上**：快线在慢线上方（持续为真） |
| `wma_trend_down` | 趋势向下 |

纯配置即可用，5 周/10 周金叉买入：

```json
{
  "type": "combo",
  "entry": { "all": [
    { "factor": "wma_cross_up", "op": "eq", "value": 1,
      "params": { "fast": 5, "slow": 10 } },
    { "factor": "wma_trend_up", "op": "eq", "value": 1,
      "params": { "fast": 5, "slow": 10 } }
  ]},
  "exit": { "factor": "wma_cross_down", "op": "eq", "value": 1,
            "params": { "fast": 5, "slow": 10 } }
}
```

**周线是逐根因果推进的**：站在周三时，"本周"的代表价就是周三本身的收盘价，
既不等到周五（`ffill` 口径会整周滞后），也不会偷看周五的收盘（`bfill` 口径是未来函数，
回测用它赚到的钱实盘一分都拿不到）。交叉因此在周内就能被捕捉到。

## 后台服务：一个引擎，多个任务

任务全部声明在 `config/tasks.json`，改配置即可增删策略，**不需要改代码**：

```json
{
  "task_id": "ma-000001",
  "symbol": "000001",
  "initial_cash": 100000,
  "strategy": { "type": "ma_cross", "params": { "fast": 5, "slow": 20 } },
  "risk": [
    { "type": "cooldown", "min_bars": 1 },
    { "type": "max_drawdown", "max_drawdown": 0.2 }
  ]
}
```

每个任务有独立的行情窗口、账本、撮合与风控链，互不干扰。
行情按标的广播，新闻按关注列表定向投递，`news_impact` 超阈值即触发交易。

**运行中也能加任务**，重启后还在：

```bash
python main.py ctl add --spec '{"task_id":"rsi-000001","symbol":"000001",
  "strategy":{"type":"combo","entry":{"factor":"rsi","op":"lt","value":35}}}'
python main.py ctl tasks                       # 确认已生效
python main.py ctl task pause rsi-000001
python main.py ctl component disable news_center
```

运行时新增的任务会落到 `output/runtime/<namespace>/tasks.json`，下次启动自动重放；
新增任务的标的会自动纳入行情订阅（否则它永远等不到 K 线）。

完整架构说明见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（推荐的深入阅读入口）
与 [`app/core/readme.md`](app/core/readme.md)（核心层设计取舍）。

## 回测数据来源

`--data-source` 控制取数方式：

| 取值 | 含义 |
| --- | --- |
| `auto` | 库中无数据则自动联网采集并入库（默认） |
| `db` | 只读本地数据库，缺数据直接报错 |
| `csv` | 只读本地 `data/stock/<symbol>.csv` |
| `remote` | 强制联网拉取并入库 |

数据源优先级由环境变量 `DATA_SOURCE_PRIORITY` 控制（默认 `baostock,akshare`），
前者失败自动降级到后者。

## 目录结构

```
app/
  cli.py            统一命令行入口（serve / start / stop / status / ctl / snapshot / hub）
  backtest/         回测闭环：config / runner / result / report / registry / analyzers
  data/             数据源与采集：datasource / service / collector
  repository/       数据仓储（幂等 upsert、查询、覆盖度）
  db/               SQLAlchemy 模型与会话
  core/
    config.py       全局配置（路径/数据库/日志/行情/新闻/大模型/监控，支持环境变量覆盖）
    market/         领域模型 Bar/Tick/Signal/Order/Position/Account/News + BarSeries + clean（数据清洗）
    indicator/      指标层（可插拔注册表、参数化、共享缓存，含周均线 wma）
    factor/         因子层（含 wma_cross_up / wma_trend_up 等周线因子）
    rule/           规则层（All/Any/Not 组合器 + 声明式构造）
    strategy/       策略层（双通道：行情 + 事件）
    risk/           风控层（闸门链 + A股实盘特有规则 T+1/涨跌停/限额）
    portfolio/      组合层（账本 + 仓位计算）
    execution/      执行层：broker（模拟撮合）/ gateway（券商网关）/ live_broker（实盘撮合）
    task/           QuantTask + TaskRuntime（多任务隔离）
    collect/        采集编排：频率解析 / 交易时段 / 任务定义（不依赖 Twisted）
    news/           新闻层（RSS 采集 → 去重 → 关键词/大模型分析）
    monitor/        可观测层：dashboard / web / snapshot / backtest_api（回测作业）
    bus/            跨进程文件消息总线 + 发布/订阅组件
    hub/            多服务编排：supervisor / worker / panel / spec
    engine/         Twisted 引擎 + 业务组件 + 装配器 + 守护进程 + 控制中心
      components/   data_collector / market_center / live_gateway /
                    strategy_manager / trading_store / news_center
  strategy/         backtrader 策略（单次回测链路）
  utils/            jsonio（原子写 JSON）/ logger（分级 + 重复节流）
docs/ARCHITECTURE.md 架构与数据流（深入理解运行原理看这份）
strategies/        用户自定义策略（自动发现，升级框架不会覆盖这里）
config/tasks.json   多任务配置（改这里就能增删策略）
config/services.json 多服务编排配置（hub 拉起哪些服务、怎么互相订阅）
config/collector.json 采集编排配置（每个标的采什么粒度、多久采一次）
config/brokers.json 券商接入点（网关 + 资金账号 + 连接参数，凭据走环境变量）
config/backtest.json 回测默认参数（资金/手续费/滑点/区间，CLI 与面板共用）
scripts/            集成演示、冒烟脚本、算法自检
data/alphaquant.db  SQLite：行情 + 订单/成交/权益/事件/回测结果
output/
  snapshots/        运行快照（每个 run 一个目录，默认只在有操作时写）
  runtime/          运行时新增的任务（重启自动重放）
  services/         各服务的心跳 / 命令 / 日志 / PID
  bus/              文件消息总线（*.jsonl + .offsets/）
```

## 配置

通过环境变量覆盖，无需改代码：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///data/alphaquant.db` | 数据库连接 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `DATA_SOURCE_PRIORITY` | `baostock,akshare` | 数据源优先级 |
| `DEFAULT_CASH` | `100000` | 回测初始资金 |
| `DEFAULT_COMMISSION` | `0.0003` | 手续费率 |
| `DEFAULT_SLIPPAGE_PERC` | `0.001` | 滑点 |
| `BACKTEST_IDLE_TIMEOUT` | `5` | 引擎回测模式空闲自动停止秒数 |
| `MARKET_MODE` | `replay` | `replay`=回放历史推完自动停；`poll`=定时抓最新（常驻） |
| `MARKET_INTERVAL` | `0.05` | 行情推送间隔（秒） |
| `NEWS_INTERVAL` | `60` | 新闻采集间隔（秒） |
| `NEWS_SOURCES` | 空 | 逗号分隔的 RSS/Atom 地址 |
| `NEWS_ANALYZER` | `keyword` | `keyword`=关键词（离线）；`llm`=大模型 |
| `LLM_API_BASE` | `https://api.openai.com/v1` | OpenAI 兼容端点 |
| `LLM_API_KEY` | 空 | **未配置时自动降级到关键词分析**，服务照常运行 |
| `LLM_MODEL` | `gpt-4o-mini` | 模型名 |
| `TASK_CONFIG` | `config/tasks.json` | 多任务配置文件路径 |
| `MONITOR_ENABLED` | `true` | 是否启用内嵌网页监控面板 |
| `MONITOR_HOST` | `127.0.0.1` | 面板监听地址（改 `0.0.0.0` 才能远程访问） |
| `MONITOR_PORT` | `8787` | 面板端口 |
| `MONITOR_SNAPSHOT_INTERVAL` | `5` | 快照落盘间隔（秒） |
| `MONITOR_SNAPSHOT_KEEP` | `300` | 每个 run 保留多少份历史快照 |
| `COLLECTOR_CONFIG` | `config/collector.json` | 采集编排配置文件 |
| `COLLECTOR_ENABLED` | `true` | 采集服务总开关（关掉后 `--collector` 也不生效） |
| `COLLECTOR_INTERVAL` | `5m` | 默认采集频率（30s / 5m / 1h / 1d 均可） |
| `COLLECTOR_PERIOD` | `1d` | 默认数据粒度：`1d` 日线，`1/5/15/30/60` 分钟线 |
| `COLLECTOR_MIN_INTERVAL` | `15` | 频率下限（秒），防止误配成 1s 把数据源打挂 |
| `COLLECTOR_SYMBOLS` | 空 | 逗号分隔的默认采集标的 |
| `SERVICES_CONFIG` | `config/services.json` | 多服务编排配置文件 |
| `HUB_HOST` / `HUB_PORT` | `127.0.0.1` / `8899` | hub 聚合面板监听地址与端口 |
| `SERVICE_HEARTBEAT_TIMEOUT` | `15` | 多久收不到心跳判定服务失联（秒） |
| `TRADE_PERSIST_ENABLED` | `true` | 订单/成交/权益落 SQLite 的总开关 |
| `EQUITY_SAMPLE_INTERVAL` | `30` | 权益曲线采样间隔（秒），**不逐笔存** |
| `TRADE_FLUSH_BATCH` / `TRADE_FLUSH_INTERVAL` | `50` / `2` | 落库批量大小与刷盘间隔 |
| `MONITOR_SNAPSHOT_MODE` | `on_event` | 快照策略：`on_event`/`on_change`/`interval`/`off` |
| `MONITOR_SNAPSHOT_ENABLED` | `true` | 快照总开关 |
| `MONITOR_SNAPSHOT_MIN_GAP` | `2` | 两次落盘最小间隔（秒），合并突发用 |
| `MONITOR_SNAPSHOT_DEBOUNCE` | `1` | `on_event` 的抖动窗口（秒），把同一批操作并成一份 |
| `MONITOR_SNAPSHOT_INTERVAL` | `5` | `on_change` 的检查间隔（秒），不是写入间隔 |
| `MONITOR_SNAPSHOT_KEEP` | `300` | 每个 run 保留多少份历史快照 |
| `BROKER_ENDPOINTS_CONFIG` | `config/brokers.json` | 券商接入点配置文件 |
| `BROKER_ENDPOINT` | 空 | 默认使用哪个接入点（空则用 `default_endpoint`） |
| `BACKTEST_DEFAULTS_CONFIG` | `config/backtest.json` | 回测默认参数配置文件 |
| `USER_STRATEGY_DIR` | `strategies/` | 用户自定义策略目录（自动发现 + 热重载） |
| `USER_STRATEGY_AUTOLOAD` | `true` | 是否自动发现用户策略（测试环境可关） |
| `LOG_FILE_LEVEL` | 空（同 `LOG_LEVEL`） | 文件日志级别，可比控制台更严 |
| `LOG_THROTTLE_WINDOW` | `5` | 重复日志节流窗口（秒）；DEBUG 下不节流 |
| `LOG_ROTATION` | `00:00` | 日志轮转点（按天） |
| `EXECUTION_MODE` | `simulated` | 撮合模式：`simulated` / `live` |
| `BROKER_GATEWAY` | 空 | 券商网关名（不用接入点配置时的简写），如 `simulated` |
| `LIVE_MAX_ORDER_VALUE` | `0`（不限） | 单笔委托金额上限 |
| `LIVE_MAX_DAILY_TRADES` | `0`（不限） | 单日成交笔数上限 |
| `LIVE_PRICE_LIMIT_PCT` | `0.02` | 价格笼子：偏离现价超过此比例直接拒单 |
| `LIVE_ORDER_TIMEOUT` | `30` | 多久没收到回报就去券商查一次（秒） |
| `LIVE_STRICT_RECONCILE` | `true` | 对账不一致时是否阻断交易 |
| `CLEAN_MAX_MOVE_PCT` | `0.21` | 异常跳变阈值（≈一个主板涨跌停） |
| `CLEAN_ACTION` | `mark` | 清洗策略：`mark` 只标记 / `drop` 丢掉不可用数据 |

## 测试

```bash
python -m unittest discover -s app/test -p "test_*.py"
```

测试全部使用合成数据、临时数据库与临时目录，**不依赖网络，也不会往工程的
`output/` 里写东西**。

两个自检脚本可以复算关键算法的正确性：

```bash
python scripts/weekly_ma_check.py        # 周均线 vs pandas 独立实现，逐点对齐
python scripts/weekly_factor_check.py    # 金叉/死叉/趋势 vs 因果参照实现
python scripts/smoke_engine_monitor.py   # 端到端：引擎 + 面板 + 落库 + 采集 + 接入点（55 项）
python scripts/smoke_collector.py        # 采集服务集成冒烟
python scripts/smoke_live_loop.py        # 实盘闭环冒烟（39 项，用内置网关，不碰真钱）
```

## 已知约定

- A 股一手 100 股，回测默认禁止裸做空（`set_shortcash(False)` + `set_checksubmit(True)`）
- 回测报告会输出「最小持仓」，多头策略该值应恒为 0；出现负数说明有僵尸止损单等缺陷
- 夏普比率按日收益年化计算（默认无风险利率 0，可用 `risk_free_rate` 调整）
- 实时链路（`serve`）默认**不做挂单**，收到即成交 —— 从根上避免撤不掉的僵尸单
- 风控是「闸门」：只否决/缩仓，不下单。强制平仓由 `QuantTask` 的持股超时检查负责
- 新闻分析拿不到结论时返回 `confidence=0`，由 `news_confidence` 风控拦掉，不静默丢弃
- 控制指令要么真生效、要么明确报错：暂停一个没在跑的任务会返回失败并说明原因，
  绝不假装成功（面板上会显示"已暂停"但任务照旧下单，那比报错危险得多）
- 停服务走**停止标记文件**而不是信号：Windows 没有 SIGTERM，信号方案跨平台不可靠
- 所有阻塞 IO（新闻抓取、文件总线读取、快照落盘）都丢线程池，绝不占 reactor 线程

