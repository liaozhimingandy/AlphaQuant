# AlphaQuant

A 股量化交易工具：行情采集 → 数据入库 → 策略回测 → **多任务事件驱动交易引擎** →
**后台常驻 + 网页监控面板 + 多服务协作 + 常驻行情采集**。

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
浏览器打开 `http://127.0.0.1:8787/` 即可：

- **概览**：run_id / 状态 / 模式 / 已运行时间 / 组件健康
- **任务表**：每个任务的权益、盈亏、持仓、状态，可直接 暂停 / 恢复 / 删除 / 看详情
- **加任务**：页面上贴一段 JSON 就能新增任务，立刻纳入行情订阅并开始工作
- **权益曲线**：canvas 手绘（红涨绿跌，跟随系统深浅色）
- **订单与审计**：成交/被拒订单流水，以及谁在什么时候下过什么控制指令
- **日志尾部**：不登服务器也能看日志

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

## 运行快照：事后复盘靠它

引擎按固定间隔把**完整运行状态**落盘，事故之后不用猜当时发生了什么：

```
output/snapshots/<run_id>/
├── latest.json          最新一份完整快照（原子写，随时可读）
├── meta.json            这个 run 的基本信息
├── orders.jsonl         订单流水（append-only，只增不改）
└── history/*.json       历史快照，按数量滚动保留
```

每份快照含：引擎状态、所有组件状态与健康度、每个任务的权益曲线 / 成交 / 订单 / 新闻 /
风控规则 / 错误，以及监控自身的统计。

```bash
python main.py snapshot                  # 最近一次运行
python main.py snapshot --list           # 所有 run
python main.py snapshot --tasks          # 每个任务的最终状态
python main.py snapshot --orders 50      # 最近 50 条订单
python main.py snapshot --export out.json
```

采集在 reactor 线程（保证不读到半截状态），落盘丢给线程池（不阻塞交易）。

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

两个自检脚本可以复算这一点：

```bash
python scripts/weekly_ma_check.py        # 周均线 vs pandas 逐点对齐（偏差 1e-14）
python scripts/weekly_factor_check.py    # 金叉/死叉/趋势 vs 因果参照实现，位置完全吻合
```

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

完整架构说明见 [`app/core/readme.md`](app/core/readme.md)。

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
    market/         领域模型 Bar/Tick/Signal/Order/Position/Account/News + BarSeries
    indicator/      指标层（可插拔注册表，含周均线 wma）
    factor/         因子层（含 wma_cross_up / wma_trend_up 等周线因子）
    rule/           规则层（All/Any/Not 组合器 + 声明式构造）
    strategy/       策略层（双通道：行情 + 事件）
    risk/           风控层（闸门链）
    portfolio/      组合层（账本 + 仓位计算）
    execution/      执行层（模拟撮合）
    task/           QuantTask + TaskRuntime（多任务隔离）
    collect/        采集编排：频率解析 / 交易时段 / 任务定义（不依赖 Twisted）
    news/           新闻层（RSS 采集 → 去重 → 关键词/大模型分析）
    monitor/        监控面板 + 运行快照：dashboard / web / snapshot
    bus/            跨进程文件消息总线 + 发布/订阅组件
    hub/            多服务编排：supervisor / worker / panel / spec
    engine/         Twisted 引擎 + 业务组件 + 装配器 + 守护进程 + 控制中心
  strategy/         backtrader 策略（单次回测链路）
  utils/            jsonio（原子写 JSON）/ logger
config/tasks.json   多任务配置（改这里就能增删策略）
config/services.json 多服务编排配置（hub 拉起哪些服务、怎么互相订阅）
config/collector.json 采集编排配置（每个标的采什么粒度、多久采一次）
scripts/            集成演示与冒烟脚本
output/
  snapshots/        运行快照（每个 run 一个目录）
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

## 测试

```bash
python -m unittest discover -s app/test -p "test_*.py"
```

测试全部使用合成数据、临时数据库与临时目录，**不依赖网络，也不会往工程的
`output/` 里写东西**。

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

