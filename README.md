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

# 8. 事件 / 任务 / 执行池的运行时观测（详见 docs/EVENT_TASK.md）
python main.py ctl runtime status              # 池占用、队列积压、死信、统计
python main.py ctl runtime pools               # 8 个池的容量与在跑任务
python main.py ctl runtime rules               # 22 条调度规则及其开关状态
python main.py ctl runtime dead                # 错误队列：彻底失败的任务
python main.py ctl runtime dead-retry <id>     # 人工点"继续"（尝试次数清零后重投）
python main.py ctl runtime dead-retry --all    # 批量重投（下单类会跳过）
python main.py ctl runtime dead-discard <id>   # 认了，不重跑，从清单划掉
python main.py ctl runtime dead-clear          # 处理完这批死信后清掉
python main.py ctl runtime journal             # 流水账落盘状态 + 死信处置历史
python main.py ctl preflight                   # 启动预检结论（配置/目录/数据库/数据）
python main.py ctl preflight --rerun           # 改完配置当场再体检一遍
python main.py ctl runtime maint               # 维护计划（清理/归档/校验）与下次执行时间
python main.py ctl runtime maint-run validate  # 立刻跑一次（不看周期与开关）
python main.py ctl runtime tasks               # 任务明细：已建任务 + 成功/失败历史
python main.py ctl runtime tasks --status failed --type market.kline.collect
python main.py ctl runtime task <task_id>      # 单条：参数 / 结果 / 错误 / 耗时
python main.py ctl runtime events              # 事件指标：每种事件发布数/订阅者/异常
python main.py ctl runtime dispatch --task-type factor.calculate --params '{"symbols":["000001"]}'
python main.py ctl runtime rule off kline_updated_factor   # 临时关掉一条规则

# 9. 日志级别：运行中切换，不用重启（排查线上问题时最怕重启把现场冲掉）
python main.py ctl log show                    # 当前档位与节流统计
python main.py ctl log level DEBUG --file INFO # 控制台 DEBUG、文件只留 INFO
python main.py ctl log level INFO --no-persist # 只本次生效，不写进配置
```

可用能力清单：`python main.py strategies` / `factors` / `rules`

## 网页监控面板

引擎自带一个**零依赖的网页面板**（Twisted Web 挂在引擎自己的 reactor 上），
浏览器打开 `http://127.0.0.1:8787/` 即可。面板分成**五个页面**：

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

### 选股 · 股票池页

- **推荐列表**：排名 / 代码 / 总分 / 等级 + 资金·题材·筹码·技术·涨停 五个维度的分
- **权重提示**：直接显示当前 `config/score.json` 的权重与默认候选池来源
- **刷新**：就地触发一次"拉候选 → 同步 → 打分 → 落库"，带来源与条数上限
- **明细**：点任一行的「明细」看该票逐维度得分与扣分理由（无数据的维度会标出来）

### 回测监控页

- **新建回测**：填标的/区间/策略/资金/参数 → 提交后交给**调度器**，
  由 `backtest` 池（并发上限 1）排队执行，不阻塞实盘也不挤占行情线程。
  表单默认值来自 `config/backtest.json`，下面会写明"默认值来自哪、是多少"
  
  走调度器的好处是回测变成一个**可查、可重试、可恢复**的普通任务：
  面板「运行时 → 任务明细」看得见它、失败会进错误队列可人工重投、
  引擎重启能从流水账里捞回来。同一个任务重投也是安全的（按 task_id 幂等，
  不会重复写一条结果）
- **自定义策略**：下拉框自动包含 `strategies/` 里发现的策略（标 `[自定义]`）
- **声明式规则**：策略选 `declarative` 时会出现「规则定义(JSON)」输入框，
  点「填入示例规则」就有一份能跑的，不用写 Python
- **作业列表**：每个回测作业的状态、耗时、失败原因
- **历史结果**：从 SQLite 读，重启不丢。收益/回撤/夏普/成交/胜率一览
- **回测详情**：权益曲线 + 完整指标 JSON

### 运行时页

回答"现在系统在做什么"——服务互相直接调用时这个问题无解，有了队列与状态机
之后它是"读一张表"：

- **执行池**：8 个池各一张卡片（占用条 + 完成/失败/超时/满槽拒收计数）。
  池之间是**独立线程池**，回测跑满也吃不掉行情的线程
- **队列积压**：每个队列的积压量，连同它对应的池与容量一起显示
- **在跑任务 / 调度规则**（可逐条开关）/ **能力路由表**
- **错误队列**：死信逐条「继续 / 丢弃」。下单类任务的"继续"按钮置灰并说明原因
  （点了会重复下单）；带"重投 N 次""中断过"标记
- **任务持久化**：流水账路径、未完成数、本次启动恢复了多少、上次刷盘时间、
  落盘是否出错，外加死信处置历史（内存里清空后仍可追溯）
- **启动预检**：逐项结果与处置建议，可当场重跑
- **手动投递任务**：任务类型下拉直接取已注册处理器；没注册的类型会被明确拒绝
- **任务明细**：按状态 / 类型 / 关键字查已建任务与终态历史，点「详情」看完整
  参数、结果与错误原文 —— 汇总数字说不清"这 1200 条成功分别是哪些"
- **事件指标**：每种事件的发布数、触达数、订阅者数、处理异常数与最近时间；
  「订阅者 = 0 而发布数在涨」意味着链路在跑起来之后断了
- **维护计划**：日志清理 / 快照归档 / 运行校验 的开关、周期、参数与「立即执行」。
  周期六种：**引擎启动时 / 每 N 小时 / 每天 / 每周几 / Cron 表达式 / 指定时刻一次**，
  切换周期时只留下该周期用得到的输入框。
  清理与归档**默认只报告不删**，要真的删必须显式勾选
- **日志等级**：控制台级 / 文件级 / 重复日志节流窗口，可勾"重启后仍生效"

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

数据源给你的东西**不等于**能喂给策略的东西。`app/domain/clean.py` 负责中间这道工序：

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

### 通用 HTTP 网关：不写代码接一家券商

每家券商的字段命名、鉴权方式、URL 形状都不一样。如果每接一家都要写一个
Python 类，"接券商"就永远是排期开发的事。所以内置了**配置驱动**的
`http` 网关（标准库 urllib，零新依赖）：把路径、字段名、返回值取值路径
写进接入点配置即可。

```json
{
  "id": "mybroker",
  "gateway": "http",
  "account": "你的账号",
  "params": {
    "base_url": "http://127.0.0.1:8080",
    "token_env": "BROKER_HTTP_TOKEN",
    "endpoints": {
      "place":      {"path": "/orders", "method": "POST"},
      "cancel":     {"path": "/orders/{broker_order_id}", "method": "DELETE"},
      "orders":     {"path": "/orders", "method": "GET"},
      "positions":  {"path": "/positions", "method": "GET"},
      "account":    {"path": "/account", "method": "GET"},
      "fills":      {"path": "/fills", "method": "GET"},
      "instrument": {"path": "/instruments/{symbol}", "method": "GET"}
    },
    "req_fields": { "size": "quantity", "order_type": "orderType" },
    "resp_fields": { "list": "data", "order_id": "orderId" },
    "status_map": { "new": "SUBMITTED", "dealt": "FILLED" }
  }
}
```

字段对不上就改 `req_fields` / `resp_fields` / `status_map` 三张映射表，不用碰代码。
返回 `{"data": {...}}` 嵌套和平铺两种形状都认。完整示例见
`config/brokers.json` 的 `http-demo` 接入点。

端到端验证（用本地迷你券商跑通下单→回报→持仓→撤单→拒绝→断网）：

```bash
python scripts/smoke_http_gateway.py        # 20 项
```

### 网关接口补齐

`IBrokerGateway` 在"下单/撤单/查/收回报"之外补齐了实盘必需的能力：

| 方法 | 用途 | 缺失时的后果 |
| --- | --- | --- |
| `query_trades(since)` | 查成交明细 | 轮询式网关丢了回报就再也拿不回来，重启对不上账 |
| `query_instrument(sym)` | 交易所口径的涨跌停价/最小价差/停牌 | 用"昨收×1.1"估算会在 ST(5%)、创业板(20%) 上判错 |
| `ensure_connected()` | 断线重连 | 连接静默断开后策略照跑，但一笔单都发不出去 |
| `capabilities()` / `health()` | 能力声明与连通性自检 | 面板只能猜这家券商能做什么 |

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

跨进程通信用**文件消息总线**（`app/runtime/bus/`）：发布者 append JSONL，订阅者按
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

## 交易日历：闭市就不再请求数据

闭市时还在按定时器请求数据是最典型的浪费：请求发出去了、限流风险占了、
日志刷了一屏，拿回来的还是和上一交易日一模一样的数据。更糟的是它掩盖真问题 ——
日志里"一切正常"，实际一天里绝大多数请求都是无意义的重复。

交易日历（`app/domain/calendar.py`）分三层兜底：

1. **内置表** —— 已公布的交易所休市安排（2025 / 2026 精确到日，如 2026 国庆 10/1–10/7）
2. **配置文件** —— `config/trade_calendar.json`（联网校准后写这里，之后离线可用）
3. **联网刷新** —— `python main.py calendar refresh`

刷新失败时**不动现有日历**：日历缺失会导致"该采的时候不采"，那比多采几次严重得多。

判断交易日**不能只看是不是周末** —— 国庆七天里有五个工作日，
按"周一到周五"判断会白采五天。

```bash
python main.py calendar status      # 当前市场状态、最近交易日、下次开盘
python main.py calendar refresh     # 联网校准（写 config/trade_calendar.json）
```

受它约束的：K线采集（只在交易时段 + 收盘缓冲内）、扩展数据采集
（资金流/筹码/基本面/涨停池/题材，**非交易日一次都不请求**）。

日频截面数据还有一条额外约束：**一天只采一次**（盘中一次 + 收盘定稿一次）。
这类数据一天只有"盘中波动"和"收盘定稿"两种状态，10 分钟采一次纯属浪费。

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

### 采集层是怎么组织的（Provider / Capability / Router）

数据源换过很多轮，每次都改因子是不可接受的。所以采集层按
**能力（Capability）+ 数据源（Provider）+ 路由（Router）+ 标准模型（Canonical）** 拆开：

```
因子/策略 ──▶ DataService ──▶ DataRouter ──▶ Provider(akshare/baostock/东财/雪球/腾讯/通达信)
                                  │                        │
                          能力匹配→排序→熔断→校验       Adapter：外部格式 → 标准模型
                                                            │
                                                    RAW（原始响应存档，可审计/重放）
```

- **按能力而不是按源设计**：mootdx 没有人气榜不是缺陷，是它没声明 `HOT_RANK`，
  路由压根不会调它（`NOT_SUPPORTED`，不是失败）。
- **熔断是能力级的**：akshare 的日线被拦 ≠ 它的人气榜不能用，
  熔断键是 `(provider, capability, route)`。
- **失败要分类**：限流/被封/超时要冷却重试，"这只票没数据"是中性事件（不惩罚源），
  "接口改版"不重试直接换源。
- **取到了不代表取对了**：过一遍质量校验（high ≥ max(open,close) 等），
  不通过按 `QUALITY_ERROR` 换下一个源。
- **标准模型 + 血缘**：`Kline` 带 `source/provider/fetched_at/raw_id`，
  一路能追回"这个价格是哪次请求、哪个源给的"。
- **顺序可配置**：`config/providers.json` 声明每种能力按什么顺序试；
  运行时按 `0.7×实测健康分 + 0.3×配置优先级` 自动向"当前真能用的源"收敛。

```bash
python main.py providers                  # 路由 / 能力 / 健康分 / 熔断状态
python main.py providers --probe          # 逐条路由实打一次，一眼看出是谁挂了
python main.py providers --capability hot_rank
```

详见 [docs/DATA_FABRIC.md](docs/DATA_FABRIC.md)。

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

## 资金流因子：不只看价格，还看谁在推动它

同样是涨 5%，**主力持续净流入**和**散户情绪拉抬**后续的持续性完全不是一回事。
K线只告诉你价格怎么走的，资金流告诉你谁在推动它。

数据链路：`akshare 日频截面` → `app/providers/market/service/facade.py`（封装+降级）→ SQLite
→ `FeatureProvider`（注入因子上下文，TTL 缓存）→ `Factor` → `ScoreEngine` → `StockPool`。

**新增的因子**（都可直接在规则/策略里引用）：

| 因子 | 含义 |
| --- | --- |
| `main_net_pct` | 主力净流入占成交额比例 |
| `capital_accel` | 资金流入加速度（今天比昨天更猛吗） |
| `capital_pulse` | 资金脉冲（单日异动强度） |
| `net_inflow_days` | 连续净流入天数 |
| `chip_profit_ratio` | 获利盘比例（上方套牢盘重不重） |
| `chip_concentration` | 筹码集中度 |
| `chip_peak_gap` | 现价与筹码峰的距离 |
| `theme_rank` / `theme_pct` | 所属题材的排名与涨幅（站没站在主线上） |
| `limitup_effect` | 涨停效应（连板数+封单额−炸板次数合成，"硬板"还是"烂板"） |
| `composite_score` | 六维综合分（资金/题材/筹码/技术/涨停/基本面） |

### 基本面因子

| 因子 | 含义 |
| --- | --- |
| `pe` / `pb` / `ps` | 市盈率 TTM / 市净率 / 市销率 |
| `dividend_yield` | 股息率 % |
| `market_cap` / `float_market_cap` | 总市值 / 流通市值（元） |
| `roe` | 净资产收益率 % |
| `gross_margin` / `net_margin` | 毛利率 / 净利率 % |
| `revenue_yoy` / `profit_yoy` | 营收同比 / 净利同比 % |
| `debt_ratio` | 资产负债率 % |
| `fundamental_score` | 基本面综合分：估值 30% + 成长 40% + 质量 30% |

营收同比和净利同比要**一起看**：营收涨、净利跌是靠降价换量；
营收平、净利涨可能是卖资产或降本 —— 质地完全不同。

数据缺失时因子返回 `None` 而不是 `0` —— "没数据"和"资金持平"必须能区分开，
否则打分会被一堆假 0 拉平。

## 多维度数据：题材 / 基本面 / 资金 / 舆情 / 宏观 / 板块

因子要有效，前提是**数据维度够全**。只靠K线能算的因子是有限的，
真正区分度高的信息往往在K线之外。

```bash
### 采集时间窗口（默认盘前 30 分钟 + 闭市后 30 分钟）

```json
"schedule": {
  "pre_open_minutes": 30,      // 开盘前多少分钟开始采（→ 9:00）
  "post_close_minutes": 30,    // 闭市后延迟多少分钟采定稿（→ 15:30）
  "grace_minutes": 120,        // 盘后窗口宽限时长
  "intraday_enabled": false    // 盘中是否也采日频截面数据
}
```

写在 `config/collector.json`，也可以在**监控看板「因子实验室 → 采集时间窗口」**里直接改。

为什么是这两个点：

- **盘前 30 分钟**：数据源已整理完上一交易日数据，且还没进竞价 ——
  网络不挤、接口不限流，适合补齐昨日漏采
- **闭市后 30 分钟**：交易所与数据源需要结算时间。15:00 整去拉拿到的
  是**未定稿**数据（当天资金流、涨停池都还在变），早了拿到脏数据
- **盘中默认不采日频截面**：一天一条的东西盘中拉十次是纯浪费，
  还会把自己送进限流名单（分钟级数据仍正常采）

### 选了标的马上就能看

面板「因子实验室 → 数据采集」填标的 → **立即采集（全类别）**，
会跑：日线 → 资金流 → 筹码 → 基本面 → 龙虎榜 → 两融 → 业绩预告 →
股东户数 → 机构评级 → 新闻 → 宏观 → 市场宽度。单类别失败不影响其它。

### 因子总目录（按类别，含数据可用性）

面板因子表列出**全部 63 个因子**并分组：量价技术 / 表达式因子 / 题材概念 /
筹码结构 / 资金类 / 基本面 / 舆情 / 宏观市场。
灰色的表示库里还没有对应数据 —— 用了会得到空值，先去采一次。

```bash
python main.py pool categories          # 看支持哪些类别 + 库里已有什么
python main.py pool sync lhb --symbol 000001
python main.py pool sync macro           # 市场级，不需要标的
python main.py pool sync all
```

| 类别 | 内容 | 频率 | 说明 |
| --- | --- | --- | --- |
| `lhb` | 龙虎榜 | 日频 | **唯一能看到"谁在买"的公开数据**：机构席位 vs 游资营业部 |
| `margin` | 融资融券 | 日频 | 杠杆资金温度计，反映风险偏好 |
| `forecast` | 业绩预告 | 季频 | 财报之前唯一的业绩前瞻，领先财报 1~2 个月 |
| `holders` | 股东户数 | 季频 | 户数减少 = 筹码集中（有人悄悄收集） |
| `rating` | 机构评级 | 不定期 | 单一评级不可信，**评级变化密度**才有价值 |
| `news` | 新闻舆情 | 日频 | 带情感分（-1~1）；条数本身也是关注度信号 |
| `macro` | 宏观指标 | 月/季 | CPI / PPI / PMI / M2 / LPR |
| `breadth` | 市场宽度 | 日频 | 涨跌家数、涨停家数、市场情绪 |

### 为什么用"通用因子值表"而不是每类一张表

```
stock_factor_value(symbol, date, category, name, value, extra_json)
```

要接的类别会不断加。每加一类就建表 + 写仓储 + 写查询，
三个月后就是十几张结构完全相同的表、十几套几乎一样的代码 ——
而它们都是 **(标的, 日期, 指标名, 数值)**。

统一成一张表后：**新增一类数据 = 加一个获取函数**，
不动表结构、不动仓储、不动查询。放不进 float 的信息进 `extra_json`。

### 宏观与市场宽度是"环境开关"，不是选股维度

这两类对**所有票同值**，所以它衡量的是"现在该不该出手"：

- PMI < 50 收缩区间，个股策略胜率系统性下降
- 全市场普跌时（breadth_sentiment < -30），再好的票也该降仓
- LPR 上行周期，高估值成长股系统性承压

所以它们在打分里权重**刻意压到 0.06** —— 给高了会稀释掉真正的选股信息。

## 策略库：界面上自定义策略并一键上线

面板 **策略库** 页可以不写代码地建策略、保存、上线为实盘任务。

三种类型：

| 类型 | 用途 | 关键参数 |
| --- | --- | --- |
| `declarative` 规则 | **多因子组合**：多条条件用 全部满足/任一满足 组合 | 入场/出场各可加多条规则 |
| `score` 综合评分 | 按八维综合分决策 | entry_score / exit_score |
| `multi_factor` 多因子加权 | 因子库里的因子标准化后加权 | factors / entry / exit |

规则策略的因子是**分组下拉**（量价/题材/筹码/资金/基本面/舆情/宏观），
可以加多条并选"全部满足 / 任一满足"，生成的结构是：

```json
{"combine": "any", "rules": [
  {"factor": "main_net_pct", "op": "gt", "value": 0.05},
  {"combine": "all", "rules": [
    {"factor": "theme_rank", "op": "lt", "value": 20},
    {"factor": "limitup_effect", "op": "gt", "value": 2}]}]}
```

即"资金流好 **或者**（题材热 **且** 涨停）"，嵌套组合也支持。

策略存在 `config/strategies.json`（**声明式配置，不是代码**）——
为了把阈值从 30 改成 35 去改 .py 文件再热加载，摩擦太大，
最后的结果就是大家都不去调。需要写复杂逻辑时仍然去 `strategies/` 写 Python，
两条路并存。

保存时做结构校验（例如 `exit_score ≥ entry_score` 会被拒绝并说明"否则刚买就卖"），
上线时若没配风控会自动补一条 `max_drawdown` 兜底。

```bash
python main.py ctl strategies --reload      # 重新扫描 strategies/ 目录
```

## 因子实验室：写公式加因子、检验、机器学习挖因子

> 策略是量化的核心，**因子是策略的核心**。

这个闭环解决的是"想到一个因子 → 变成验证过的因子"之间的摩擦。

### ① 界面上写一句公式 = 一个因子

面板 **因子实验室** 页（或命令行 `main.py factor add`）直接写表达式：

```
close / MA(close, 20) - 1                 乖离率
-(close / REF(close, 20) - 1) * 100       20 日反转
RANK(TS_RANK(volume, 10))                 量能位置
CORR(close, volume, 10)                   量价相关性
```

29 个算子（`MA` `EMA` `STD` `REF` `DELTA` `RANK` `ZSCORE` `TS_RANK` `CORR` `TS_MAX` …）
+ 6 个变量（`open` `high` `low` `close` `volume` `amount`）。
`main.py factor ops` / `factor vars` 可以列出全部，面板里也能点开速查。

**不用 eval**：表达式来自输入框，所以是自己写的 tokenizer + 白名单函数表。
`__import__` / `exec` 之类一律拒绝。写错变量名会提示"是不是想写 close"。

**所有算子都是因果的** —— `RANK` 是截至当前的扩展窗口百分位，
不是全样本排名（后者用未来数据给历史打分，是最隐蔽的一类未来函数）。

### ② 存进因子库，`config/factors.json`

启动即注册：规则、策略、回测、面板下拉框都能直接引用，不用改代码。

```bash
python main.py factor list                 # 列出因子库
python main.py factor presets              # 装 8 个预置经典因子
python main.py factor add my_mom "close / MA(close, 20) - 1" --desc "均线乖离"
python main.py factor remove my_mom
```

### ③ 检验：这个因子到底有没有预测力

```bash
python main.py factor check --horizon 5 -- "-(close / REF(close, 20) - 1) * 100"
```

```
样本 2315 行 / 2 个标的（口径：时间序列）
IC     +0.2371（标准差 0.2151）
ICIR   +1.102       t 值 +51.67
IC>0 占比 86.3%
综合 97.4 分 → 可用
```

判据：**IC**（相关系数）、**RankIC**（秩相关，抗极值）、**ICIR**（稳定性，
衡量"是不是稳定有效"而不是"偶尔很准"）、**分层收益单调性**、**t 值**。

刻意把**稳定性**放在强度前面：IC 高但 IR 低的因子，实盘表现是
"偶尔大赚、长期磨损"，比没有更危险。

截面口径至少要 3 个标的（2 个点的相关系数恒为 ±1）。标的不足时退到
时间序列口径，并**在结论里写明口径更弱**。

### ④ 机器学习找因子

**遗传算法挖掘**（`main.py factor mine`）：在语法树上做交叉变异，
所以生成的每个候选都保证合法。

```bash
python main.py factor mine --generations 12 --seconds 120 --min-score 45
python main.py factor mine --save          # 显式才会入库（source=mined）
```

实测：约 4 秒评估 230 个表达式，能挖到 IC 0.66 / ICIR 2.58 的因子。

⚠️ **挖出来的因子绝大多数是过拟合的**：在有限样本上搜几千个表达式，
总能找到几个历史表现极好的。所以默认不入库，入库也标 `mined` 并带上检验指标。

**线性模型筛因子与配权重**（`main.py factor fit`）：零依赖 numpy 实现，
岭回归（闭式解）+ Lasso（坐标下降）。

```bash
python main.py factor fit --method lasso   # lasso=会筛因子 / ridge=只缩不放零
```

```
lasso | alpha=0.001 | 训练 1620 / 测试 695
  样本内 R² 0.0141   样本外 R² -0.0128   样本外 IC +0.0284
   ✓ ma_bias_20  -1.0434    ✓ price_pos_20  +0.3581    ...
```

三个刻意的设计：
- **先去共线再拟合** —— 高度相关的因子 Lasso 会随机留一个，不先剔除
  你以为是模型选的，其实只是它先撞上了哪个；
- **惩罚强度用验证集选**（训练段里再切最后 20%），不用测试集调参；
- **按时间切分**不打乱，否则训练集里会混进未来。

### ⑤ 因子直接用进策略

新增 **多因子策略** `multi_factor`：把因子库里的因子各自标准化后加权求和，
按阈值开平仓。

```json
"strategy": {
  "type": "multi_factor",
  "params": {
    "factors": [{"name": "mom_20", "weight": 1.0},
                {"name": "vol_ratio", "weight": 0.5}],
    "entry": 0.55, "exit": -0.30
  }
}
```

`factors` 省略时用因子库里全部启用的因子（等权）；
权重可以直接把 `factor fit` 的结果拷进来。
每个因子**先各自标准化再加权** —— 动量是百分比、量能比是倍数，
直接相加等于让量纲大的那个独占话语权。
算不出的因子跳过并重分配权重，而不是当 0 分（那会凭空造出看空信号）。

回测侧：因子注册后，`declarative` 声明式策略的 JSON 规则可以直接引用它，
例如 `{"factor": "mom_20", "op": "gt", "value": 0}`。

## 多维度打分与股票池

`ScoreEngine` 把六个维度加权成总分 + 等级（A/B/C/D），权重在 `config/score.json`：

```json
{ "weights": { "capital": 0.28, "theme": 0.22, "chip": 0.16,
               "technical": 0.14, "limitup": 0.08, "fundamental": 0.12 } }
```

基本面只给 12% 是刻意的：它是**过滤器不是择时器**，一份财报要用一个季度，
权重给高了打分结果会被几个月前的数据主导。它的作用是把垃圾股挡在池子外面。

**缺维度时权重会在可用维度之间重新归一化**，而不是按 0 分算 ——
一只票没有题材归属，只说明数据缺失，不说明它差。

全市场 5000 多只票逐只拉数据既慢又必被限流，所以是两段式：
先用候选池收窄到几十~几百只，再精细打分。候选池拉失败时**返回空列表**，
绝不静默退化成全市场（那会把几十秒变成几十分钟）。

```bash
python main.py score 600519                # 单只票的逐维度评分明细
python main.py flow 600519 --days 10       # 资金流与筹码原始值
python main.py pool refresh --source limitup --limit 30   # 刷新股票池
python main.py pool top --limit 20         # 看推荐（离线读库，不联网）
python main.py pool sync fundflow --symbol 600519         # 只同步某一类数据
python main.py pool sync fundamental --symbol 600519      # 只同步基本面
python main.py pool demo                   # 无网络时用合成数据验证打分链路
```

候选池来源：`limitup`（涨停池）/ `hot`（人气榜）/ `theme`（热门题材成分）/
`flow`（资金流排名）/ `watch`（自选，配在 `config/pool.json`）/ `all`（全市场，慎用）。

`score` 策略同时注册在实盘与回测两条链路上，共用同一套打分口径，
按 `entry_score` / `exit_score` 阈值开平仓。

### 实盘里的多维度闸门

光是"选出来"不够，下单那一刻也要过一遍多维度。两条新风控规则：

| 规则 | 作用 |
| --- | --- |
| `min_score` | 综合评分低于阈值不放行买入 |
| `fundamental` | 基本面排雷：亏损股 / 超高负债 / 基本面分过低 |

```json
"risk": [
  { "type": "min_score", "min_score": 60, "allow_unknown": true },
  { "type": "fundamental", "reject_loss": true, "max_debt_ratio": 70 }
]
```

两条都**只拦买入** —— 卖出不能被评分挡住，评分掉下来正是该卖的理由。
没数据时默认放行但会在理由里写明"未做多维度校验"：
一律拒绝会让策略莫名其妙一整天不下单，一律放行则闸门形同虚设。

### 常驻采集

由采集服务（`collection_service`）的日频截面单元承担，**非交易日一次都不请求**，日频数据一天只采一次：

| 数据 | 节奏 |
| --- | --- |
| 资金流 / 筹码 | 盘中一次 + 收盘定稿一次 |
| 基本面（估值+财务） | 收盘后一天一次 |
| 涨停池 / 题材 | 一天一次 |

回测模式默认不装 —— 那段行情是死的，去抓"今天的涨停池"对回放没有意义。

```bash
python main.py serve --no-ext-data         # 不采扩展数据
python main.py serve --ext-interval 300    # 资金流/筹码改成 5 分钟
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
python main.py ctl component disable news_service
```

运行时新增的任务会落到 `output/runtime/<namespace>/tasks.json`，下次启动自动重放；
新增任务的标的会自动纳入行情订阅（否则它永远等不到 K 线）。

## 文档导航

文档按"你此刻想干什么"来分，不必从头读到尾：

| 想解决什么 | 看哪份 |
|---|---|
| **怎么用**（命令、面板、配置） | 本文 README |
| **运行原理**：按下回车之后发生了什么，三条链路怎么走 | [`docs/RUNTIME.md`](docs/RUNTIME.md) |
| **事件 / 任务 / 执行池**：标准事件清单、21 条调度规则、8 个线程池怎么分 | [`docs/EVENT_TASK.md`](docs/EVENT_TASK.md) |
| **为什么这么设计**：28 条决策及其理由 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| **引擎四类组件**：服务化重构的来龙去脉 | [`docs/ENGINE_ARCHITECTURE.md`](docs/ENGINE_ARCHITECTURE.md) |
| **采集层**：Provider / Capability / Router / Canonical | [`docs/DATA_FABRIC.md`](docs/DATA_FABRIC.md) |
| **加一个数据源** | [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) |
| **加因子 / 策略 / 风控 / 组件 / API** | [`docs/EXTENDING.md`](docs/EXTENDING.md) |
| **查某个配置项是什么意思** | [`docs/CONFIG_REFERENCE.md`](docs/CONFIG_REFERENCE.md) |
| **改某个东西去哪个文件** | [`docs/STRUCTURE.md`](docs/STRUCTURE.md)（五层边界 + 新旧路径对照） |
| **数据校验 / 补全服务** | [`docs/DATA_INTEGRITY.md`](docs/DATA_INTEGRITY.md) |

**第一次读建议**：README（跑起来）→ `RUNTIME.md`（知道它怎么转）
→ `ARCHITECTURE.md`（知道为什么这样转）。

> 注：本文早期版本曾指向 `app/readme.md`，那个文件并不存在（链接悬空），已修正。

引擎里的东西按角色分四类，**加东西前先看这张图**：

```
IBaseComponent
├── ServiceComponent    提供能力：market / collection / trading / risk / portfolio / news
├── StrategyComponent   消费行情与风控，产出订单
├── WorkerComponent     执行一次性作业（有并发上限）
└── SchedulerComponent  决定"什么时候跑"
```

其中 `PortfolioService` 提供**账户级**的两件事：
把所有任务的账本聚合出"整个账户有多少钱、总回撤多少"（`Portfolio` 是
per-task 的，以前没人能回答这个问题），以及在报单前把预计占用从全账户
额度里扣住——风控是在单个任务的账本上做判断的，看不见别的任务刚放行了一单，
实盘多策略共享一个券商账户时那会导致超额下单。

判定标准只有一条：**它消费别人的输出吗？** 不消费、只提供 → 服务；
要消费 → 策略或工作单元。启停顺序由 `depends_on` + 种类层级算出来，
不靠 `builder.py` 的注册顺序。

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

> 完整说明见 [docs/STRUCTURE.md](docs/STRUCTURE.md)（含新旧路径对照表）。

按**五层边界**分层（Engine → Runtime → Modules → Account → Provider/Data），
每层只依赖它下面的层：

```
app/
├── domain/                 共享内核：Bar/Order/Signal/Position、交易日历、代码标准化
├── common/                 配置 / 日志 / JSON（零业务依赖）
│
├── engine/                 ── 容器层：只管生命周期与装配 ──
│   ├── engine.py           BaseQuantEngine
│   ├── builder.py          配置 → 组件的唯一装配入口
│   ├── component.py        IBaseComponent 契约
│   ├── capability.py       Cap 能力常量 + 四种组件角色
│   ├── registry.py         ServiceRegistry 依赖解析
│   ├── preflight.py        启动预检：配置/目录/数据库/数据/磁盘/网关
│   └── hub/                多进程编排（一个 hub 拉起 N 个引擎进程）
│
├── runtime/                ── 运行时层：怎么跑，不含业务 ──
│   ├── scheduler.py        调度中枢：订阅事件 → 建任务 → 投队列 → 领活
│   ├── maintenance.py      维护计划：清理/归档/校验的周期与参数（时间源）
│   ├── executor.py         WorkerComponent（旧作业执行器）
│   ├── event_bus.py        薄转发兼容层（真身在 events/）
│   ├── events/             事件契约层：types(常量唯一真源) / schema(EngineEvent) / bus
│   ├── scheduling/         调度层：rules(规则) / router(能力路由) / defaults(22 条规则)
│   ├── tasking/            任务层：models(EngineTask 八态) / store(队列+幂等+死信)
│   │                       journal(SQLite 流水账：崩溃恢复 + 错误队列)
│   │                       + runtime/spec/QuantTask（作业实例）
│   ├── workers/            执行层：pools(8 个分池线程池) / registry / handlers(15 个)
│   └── bus/                FileBus 跨进程事件总线
│
├── modules/                ── 业务模块层：跑什么 ──
│   ├── market/             实时行情 → 标准 Bar
│   ├── collection/         采集 / 清洗 / 落库 / 完整性核验
│   ├── universe/           全市场 → 股票池 → 候选股 + 打分
│   ├── factor/             指标 → 因子 → mining 挖掘
│   ├── strategy/           策略本体 + rule/(规则 DSL)
│   ├── risk/               风控闸门链
│   ├── portfolio/          跨任务额度预留与组合视图
│   ├── trading/            订单生命周期 → 网关 → 成交
│   ├── news/               舆情：采集 → 去重 → 分析
│   └── backtest/           回测 runner/result/report + strategies/
│
├── account/                资金 / 持仓账本 / 仓位 sizing
├── data/                   SQLite engine / schema(ORM) / repo(四个仓储)
├── providers/
│   ├── market/             行情数据源全链路（详见 docs/DATA_FABRIC.md）
│   │   ├── model/          Canonical Model：Kline / RankRow / FundFlowRow
│   │   ├── transport/      统一 HTTP：UA/Referer、代理→直连回退、域名健康
│   │   ├── provider/       Provider + Capability + 能力级熔断
│   │   ├── adapters/       外部格式 → 标准模型（8 个源）
│   │   ├── routing/        Router 四层决策 + providers.json
│   │   ├── service/        facade：对上层的唯一出口
│   │   ├── raw/            Raw/Bronze 原始响应存档
│   │   └── compat/         兼容期保留的旧注册表，逐步下线
│   └── broker/             券商网关 / 端点配置
│
├── control/                ControlCenter：任务增删、组件启停、采集遥控
├── monitoring/             面板 server.py + resource.py(HTTP)
│   ├── api/                业务视图 mixin：overview/live/brokers/factors/pool/…
│   └── static/             dashboard.html（面板前端，自包含单文件）
├── cli/                    命令行包：data/strategy/engine/control/
│                           research/pool/hub/broker 八组 *_cmds.py
└── tests/                  测试
```

docs/                       文档
  STRUCTURE.md              目录结构与五层边界（**改东西去哪找**）
  ARCHITECTURE.md           运行时数据流（推荐深入阅读入口）
  RUNTIME.md                启动流程、三条链路、线程模型
  EVENT_TASK.md             事件 / 任务 / 执行池契约（改 runtime 前必读）
  ENGINE_ARCHITECTURE.md    引擎四类组件与能力注入
  DATA_FABRIC.md            数据源层设计
  DATA_SOURCES.md           如何新增数据源
  EXTENDING.md              加因子/策略/风控/组件/API 与已知坑
  CONFIG_REFERENCE.md       配置项速查
  DATA_INTEGRITY.md         数据完整性校验与自动补全
strategies/                 用户自定义策略（自动发现，回测与实盘通用）
config/
  providers.json            数据源 provider / route 配置
  tasks.json                多任务配置（改这里就能增删策略）
  services.json             多服务编排配置（hub 拉起哪些服务）
  collector.json            采集编排配置（采什么粒度、多久采一次）
  brokers.json              券商接入点（凭据走环境变量）
  backtest.json             回测默认参数（资金/手续费/滑点/区间）
  pool.json                 股票池配置     score.json 打分权重
data/alphaquant.db          SQLite：行情 + 订单/成交/权益/事件/回测结果
output/
  snapshots/                运行快照（每个 run 一个目录）
  runtime/                  运行时新增的任务（重启自动重放）
  services/                 各服务心跳 / 命令 / 日志 / PID
  bus/                      文件消息总线（*.jsonl + .offsets/）
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
| `EXT_DATA_ENABLED` | `true` | 扩展数据采集总开关（资金流/筹码/基本面/涨停池/题材） |
| `EXT_DATA_FUNDAMENTAL` | `true` | 是否采集基本面（估值 + 财务） |
| `EXT_DATA_INTERVAL` | `600` | 资金流/筹码采集间隔（秒） |
| `EXT_DATA_DAILY_INTERVAL` | `3600` | 涨停池/题材等市场级数据的同步间隔（秒） |
| `EXT_DATA_THEME_TOP` | `20` | 题材映射最多建多少个热门板块的成分（全建必触发限流） |
| `EXT_FEATURES_ENABLED` | `true` | 是否把截面特征注入因子上下文（关掉就是纯量价策略） |
| `EXT_FEATURE_TTL` | `300` | 特征缓存时间（秒），打分要遍历候选池，不缓存会打爆数据库 |

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
python scripts/smoke_http_gateway.py     # HTTP 券商网关冒烟（20 项，本地迷你券商）
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
- 截面数据（资金流/筹码/题材）只能按天对齐，不能 ffill 到分钟级 ——
  那是拿未来一天的信息解释当天的分钟K线
- 缺数据的因子返回 `None` 而不是 `0`；打分时缺维度会重新分配权重而不是按 0 分算
- 从库里读出的评分字典同时带 `total` 与 `total_score`（库列名 vs 消费端用名）
- 停服务走**停止标记文件**而不是信号：Windows 没有 SIGTERM，信号方案跨平台不可靠
- 所有阻塞 IO（新闻抓取、文件总线读取、快照落盘）都丢线程池，绝不占 reactor 线程

