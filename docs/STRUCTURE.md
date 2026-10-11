# AlphaQuant 目录重构说明

> 这份文档回答一个问题：**改某个东西，我应该去哪个文件。**
>
> 如果你只想快速定位，直接看文末的「六、按需求查位置」对照表。
>
> 配套文档：
> - [ARCHITECTURE.md](ARCHITECTURE.md) —— 运行时数据流（一根行情怎么变成一笔成交）
> - [ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) —— 引擎的四类组件与服务化（服务/策略/工作单元/调度）
> - [DATA_FABRIC.md](DATA_FABRIC.md) —— L2 数据源层的详细设计（Provider/Capability/Router）
> - [DATA_SOURCES.md](DATA_SOURCES.md) —— 如何新增一个数据源
> - [DATA_INTEGRITY.md](DATA_INTEGRITY.md) —— 数据完整性校验与自动补全

---

## 一、为什么要重排目录

重构前 `app/` 下有这些问题，每一条都在增加"看懂代码"的成本：

| 问题 | 具体表现 |
|---|---|
| **包名不表达职责** | `core/` 一个包塞了 15 个子包，从配置到回测、从引擎到策略全在里面，`core` 等于"杂物间" |
| **同名目录两个** | `app/data/`（取数）和 `app/core/data/`（特征加工）都叫 data，看路径猜不出区别 |
| **回测与实盘两套策略** | `app/strategy/`（backtrader）和 `app/core/strategy/`（自研）平行存在，同一条 MA 金叉写了两遍 |
| **HTML 装在 Python 里** | 2501 行的 `dashboard.py` 里只有一条赋值语句，整个面板前端伪装成一个 Python 模块 |
| **单文件塞几百个命令** | `cli.py` 一个文件 2642 行、84 个命令，改一条 help 要在两千多行里翻 |
| **超大类** | `monitoring/server.py` 1871 行，`MonitorWebComponent` 一个类扛着生命周期 + 快照落盘 + 40 多个业务视图方法 |
| **脚手架模板残留** | 5 个 `__init__.py` 还是生成器的样例代码（内容为 `print('Hi')`），等于这些包没有任何对外契约 |
| **死代码零引用的包** | `analyzer/`（样例）、`core/signal/`（设计草稿）等从未被引用的抽象混在正式代码里 |

现在的组织方式严格按数据流向分层，**每层只依赖它下面的层**：

```
DataSource → Provider → Capability → Adapter → Router → DataService
          → Canonical Model → Storage → Factor → Strategy
```

---

## 二、分层地图

```
 L0  common            配置 / 日志 / JSON。零业务依赖，谁都能引
      │
 L1  domain            领域模型。Bar/Order/Signal/Position、交易日历、代码标准化
      │                 ★ 这一层不知道"数据从哪来"，也不知道"策略怎么用它"
      │
 L2  datasource        数据源全链路，对应 DataSource→Provider→Capability→
      │                Adapter→Router→DataService
      │                对外唯一出口是 service.DataService
      │
 L3  storage           ORM schema + Repository。业务层只见 Repository，不见 Session
      │
 L4  feature           指标(向量) → 因子(标量) → 挖掘
      │                ★ 因子不知道数据源的存在，只见 FactorContext
      │
 L5  alpha             多维度加权打分 + 候选池
      │
 L6  strategy          策略 + 规则 DSL
      │  ├ risk        风控闸门链
      │  └ portfolio   账户 / 持仓 / 仓位
      │
 L7  execution         撮合网关 / 券商接入
      │
 L8  engine            运行时：事件总线、组件、任务容器、采集调度、多进程编排
      │
 ───── 应用适配层 ─────
      monitoring       监控面板：resource.py（HTTP）+ api/*（业务视图 mixin）
      research         回测驱动（含自研策略 → backtrader 的桥）
      cli              命令行包：按业务域切成 9 个模块
```

### 三条硬规矩

1. **数据源是可替换的，数据标准不能替换。**
   外部格式 → Canonical Model 的转换只允许在 `datasource/adapters/` 里发生**一次**。
   因子、策略永远不该出现 `akshare` / `baostock` / "东方财富" 的字段名。

2. **因子不知道数据源的存在。**
   因子只见 `FactorContext`，策略只见 feature/alpha 的输出。
   一旦在 `feature/` 或 `strategy/` 里看到 `from app.datasource.xxx import`，那是设计被破坏了。

3. **不许反向依赖。**
   `storage` 不能 import `feature`，`datasource` 不能 import `alpha`，
   `engine` 依赖最广，但也因此不能被下层引用。

---

## 三、每一层里有什么

### L0 `common/` — 公共基础设施

| 文件 | 职责 |
|---|---|
| `config.py` | 全局配置 `settings`，支持环境变量覆盖 |
| `logging.py` | 分级日志 + 重复日志节流（`log_throttled`） |
| `jsonio.py` | 原子写 JSON（先写 tmp 再 replace，避免半截文件） |

### L1 `domain/` — 领域模型

**零数据源依赖的一层。** 它定义"交易是什么"。

| 文件 | 职责 |
|---|---|
| `types.py` | `Bar` / `Tick` / `Signal` / `Order` / `Position` / `Account` / `NewsItem` + 枚举 |
| `series.py` | `BarSeries` 滑窗序列，因子与指标的公共输入 |
| `symbol.py` | `normalize_symbol`：`600519` / `sh600519` / `600519.SH` 统一归一 |
| `clean.py` | 脏数据清洗：停牌、异常价格、缺失值判定 |
| `calendar.py` | 交易日历（含节假日、闭市时段判断） |

### L2 `datasource/` — 数据源层（本次改动最大）

```
datasource/
├── model/        ★ Canonical Model：Kline / RankRow / FundFlowRow + Request + 枚举
├── transport/    统一 HTTP 传输：UA/Referer、代理→直连回退、域名健康
├── provider/     ★ Provider + Capability + 健康熔断 + ProviderRegistry
├── adapters/     ★ Adapter：外部格式 → Canonical Model（8 个数据源）
├── routing/      ★ Router（四层决策）+ 质量校验 + providers.json 配置
├── service/      ★ DataService（对外唯一出口）
├── raw/          Raw / Bronze 层：原始响应存档，供审计与重放
└── compat/       兼容期保留的旧注册表实现，会逐步下线，勿新增依赖
```

关键点：**按「能力」组织，而不是按「数据源」组织。**

```
DAILY_KLINE  → baostock, akshare, astockdata, mootdx
HOT_RANK     → emapp_hot, xueqiu_hot, local_hot(兜底)
FUND_FLOW    → akshare_flow
```

mootdx 没有人气榜**不是缺陷**，是它没声明 `HOT_RANK` 这个 Capability ——
路由压根不会去调它。同理，被限流/被封只影响**某一项能力**：
akshare 的日线被拦，不等于它的人气榜不能用。

### L3 `storage/` — 存储层

```
storage/
├── engine.py   会话管理 SessionLocal / init_db
├── schema.py   ORM 模型（SQLAlchemy）
└── repo/       Repository：kline_repo / minute_repo / stock_repo / trade_repo
```

### L4 `feature/` — 特征层

三段流水线，层层降维（**不是重复实现**）：

| 层级 | 输入 | 输出 | 位置 |
|---|---|---|---|
| indicator（向量） | `BarSeries` | 等长 `ndarray`，前导 NaN | `feature/indicator/` |
| factor（标量） | `FactorContext` | `Optional[float]` | `feature/base.py` + `library.py` |
| mining（挖掘） | 历史面板 | 候选因子与模型 | `feature/mining/` |

同样的 MA：indicator 给你整条曲线（画图用），factor 只关心最新值（判定用），
两者由 `FactorContext` 桥接。

### L5 `alpha/` — 打分与候选池

- `score.py` — 多维加权综合分（资金/题材/筹码/技术/涨停）
- `pool/` — 候选池同步 `sync.py`、管理 `pool.py`

### L6 `strategy/` + `risk/` + `portfolio/`

- `strategy/base.py` — `IBaseStrategy`，**双通道**：行情 `on_bar` + 事件 `on_event`
- `strategy/rule/` — 规则 DSL（All/Any/Not 组合），**纯只读判定，不许下单**
- `risk/` — 风控闸门链（T+1、涨跌停、限额）
- `portfolio/` — 账户账本与仓位计算

### L7 `execution/` — 交易执行

模拟撮合 `gateway.py`、HTTP 网关 `http_gateway.py`、券商接入 `endpoints.py` / `live_broker.py`

### L8 `engine/` — 运行时

```
engine/
├── base/builder/control/event/settings   引擎主体与装配
├── component/   ★ 四类组件体系（下面单独展开）
│   ├── base.py        IBaseComponent：生命周期 + 进度 + 事件订阅
│   ├── kinds.py       四种组件基类 + ComponentKind + KIND_LAYER + Cap
│   ├── registry.py    ServiceRegistry：能力登记 / 拓扑排序 / 依赖注入
│   ├── service/       服务层：market / collection / trading / risk / news
│   ├── strategy.py    策略组件
│   ├── worker.py      工作单元（有并发上限）
│   └── scheduler.py   调度组件（任务队列 + 周期定时器）
├── tasking/     单进程内的多任务容器
├── collect/     采集调度：采什么、多久采、当前时点能不能采
├── bus/         跨进程文件总线（IPC）
├── hub/         多进程编排（supervisor / worker）
└── integrity.py 数据完整性体检与自动补全
```

引擎这一层的完整设计见 **«docs/ENGINE_ARCHITECTURE.md»**。
一句话版本：以前 11 个组件平铺在一个字典里，谁先启动靠 `builder.py` 的
注册顺序碰运气；现在按"在引擎里扮演的角色"分成
**服务 / 策略 / 工作单元 / 调度** 四类，启停顺序由 `depends_on`
与种类层级算出来，组件互相找人用**能力**而不是魔法字符串。

四层是**正交的**，不是并行实现：
`collect` 决定何时采 → `tasking` 管理任务 → `engine` 提供事件循环 → `hub` 拉起多个进程。

### 应用适配层

| 包 | 职责 |
|---|---|
| `research/` | 回测。**`adapter.py` 是自研策略 → backtrader 的桥**（见第四节） |
| `monitoring/` | 监控。前端在 `static/dashboard.html`，Python 侧只管路由与数据（见第四节） |
| `cli/` | 命令行（见第四节） |

---

## 四、两个超大文件的拆分

重构把目录排清楚了，但**文件级别**的"杂物间"还在：`cli.py` 2642 行、
`monitoring/server.py` 1871 行。这两个都做了拆分，且都是**纯代码搬移**。

### 4.1 `cli.py` → `cli/` 包

```
cli/
├── __init__.py        导出 cli；**导入子模块本身就是注册**（副作用，一个都不能少）
├── __main__.py        允许 python -m app.cli
├── base.py            主命令组 cli + 跨模块共用工具（不 import 任何 *_cmds，故无环）
├── data_cmds.py       init-db / collect / dataguard / sources / providers / list
├── strategy_cmds.py   strategies / strategy / backtest
├── engine_cmds.py     engine / serve / start / stop / status / restart / snapshot
├── control_cmds.py    ctl 全家（走监控面板 HTTP API）
├── research_cmds.py   factors / rules / tasks / flow / score / factor
├── pool_cmds.py       pool 全家
├── hub_cmds.py        hub 全家
└── broker_cmds.py     brokers / calendar
```

依赖方向单向无环：`__init__` → `base` → `app.common.*`，各 `*_cmds` 只依赖 `base`。

命令注册是 **import 的副作用**：`*_cmds` 用 `@cli.command(...)` 在导入时挂载。
所以 `__init__.py` 必须导入全部子模块，缺一个就少一批命令 —— 这也是这里
不用懒加载的原因。

入口三种等价：`python main.py …` / `python -m app.cli …` / 装包后 `alphaquant …`

### 4.2 `monitoring/server.py` → 传输层 + 组件核心 + 视图 mixin

```
monitoring/
├── server.py        组件核心：生命周期 / 快照落盘 / 事件订阅（491 行）
├── resource.py      MonitorResource：HTTP 传输层（538 行）
└── api/             按业务域划分的**视图 mixin**
    ├── overview.py    总览/能力清单/配置/统计/日志
    ├── live.py        对账 / 账户刷新 / 全撤
    ├── brokers.py     券商接入点
    ├── factors.py     因子实验室
    ├── pool.py        股票池
    ├── collect.py     采集编排 / 交易日历
    ├── strategies.py  策略管理
    └── backtest.py    回测作业（另有 BacktestJob/BacktestService）
```

**为什么用 mixin 而不是「自由函数 + 传参」**：视图方法重度依赖 `self._xxx`
（快照、事件总线、行情中心…）。改成自由函数就得把组件对象传进去，等于把
40 多个方法全部改写一遍。mixin 让方法留在类里，`self` 仍是同一个对象 ——
行为零变化，可以用 AST 逐方法比对来证明。

```
MonitorWebComponent(OverviewViewsMixin, LiveViewsMixin, …, BacktestViewsMixin, IBaseComponent)
```

对外接口未变：`from app.monitoring.server import MonitorWebComponent, MonitorResource` 仍可用。

### 4.3 拆分过程中翻出来的既有 bug

机械拆分本身不引入 bug，但它把**名字的来源**摊开了，于是这些潜伏问题浮出水面：

| 问题 | 位置 | 说明 |
|---|---|---|
| `json_safe` 用了但从未导入 | `build_live_overview` | 只要走到该分支就是 NameError，此前没被触发 |
| `marketx_repository` 旧模块名 | `pool_top`、`pool_categories` | 仓库改名时漏掉；埋在 `try/except` 里，面板股票池**一直空白却不报错** |
| `_net` 悬空引用（3 处） | `datasource/compat/registry.py` | 局部 `import net` 却写 `_net.xxx`，被 `except: pass` 吞掉 → 域名健康度回写与代理清理**静默失效**，熔断一直在裸奔 |

---

## 五、回测与实盘统一（重要）

**以前的问题**：回测和实盘是两套平行的策略实现。

```
app/research/strategies/*   基于 backtrader —— 只能回测
app/strategy/*              自研运行时   —— 只能实盘
```

同一条"MA 金叉"写两遍，必然结果是：**回测跑出来的收益，证明的是另一份代码曾经有效。**

**现在**：新增 `research/adapter.py`，把自研策略桥接成 backtrader 认识的样子。

| 自研侧 | backtrader 侧 | 桥做了什么 |
|---|---|---|
| `on_bar(ctx, state)` | `next()` | 每根 K 线构造 Bar → BarSeries → FactorContext |
| `DecisionState` | `self.position` | 把 bt 持仓翻译成只读状态快照 |
| `Signal(side=BUY/SELL)` | `buy()/sell()` | 信号 → 下单，并处理 A 股整百股 |
| `on_init/on_start/on_stop` | 生命周期 | 对应转发 |

回测注册表里，**自研策略优先**；legacy backtrader 实现改名为 `*_bt`（如 `ma_cross_bt`），
仅作回归基线保留，**新代码不要再往这类里加**。

剩下回测 vs 实盘的差异（撮合精度、滑点、复权口径）是**环境差异**，不再是代码差异。

另一个收益：`strategies/` 目录下的**用户自写策略现在同时能回测和实盘** ——
发现机制优先识别 `IBaseStrategy` 子类并自动套桥。

---

## 六、拆分是怎么保证不出错的

两个拆分都用脚本做（`scripts/split_cli.py`、`scripts/split_monitoring.py`），
而不是手工剪切粘贴。脚本内置三道检查，任何一道不过就不落地：

1. **覆盖率**：原文件每个顶层名字（方法 / 类 / 常量）必须被分配且只分配一次。
2. **AST 逐节点比对**：每个搬走的片段，`ast.dump` 必须与原文件对应节点**完全相同**。
   只要 dump 一致，就证明这段函数体没有一个字符被改动。
3. **未定义名扫描**（`scripts/check_undefined.py`）：搬完之后哪些名字失去了来源。

第 2 道尤其关键，它抓出过两个真实错误：

- **多余缩进**：从类里切出的方法本身已带 4 格缩进，脚本又压了一次 →
  变 8 格不说，多行字符串（docstring）的**内容**被真的改写了。
- **漏掉非方法语句**：只切方法，把模块级常量（`_PROBE_ARGS`，带类型注解的
  `AnnAssign`）和类级属性（`SNAPSHOT_MODES` / `TRIGGER_EVENTS` / `name`）落下了。
  后者的症状是 `AttributeError: type object has no attribute 'SNAPSHOT_MODES'`。

### 顺带修掉的仓库 bug

`.gitignore` 里写的是 `data/`，它会匹配**任意层级**的 data 目录 ——
包括 `app/data/` 这个源码包。结果是整个数据源层长期游离在版本控制之外，
`git add` 也加不进去。已改成 `/data/`（只忽略项目根目录的数据文件夹）。

---

## 七、本次删了什么、改了什么

### 删除（零引用）

| 原路径 | 说明 |
|---|---|
| `app/analyzer/` | 脚手架样例，内容是 `print('Hi')` |
| `app/core/signal/` | 设计草稿，`IBaseSignal` 从未被引用 |
| `app/core/data/interfaces.py` | 已被 `datasource/provider/base.py` 取代 |
| `app/core/portfolio/base.py` | 与 `portfolio/sizer.py` 重复定义 `IBaseSizer` |
| `app/core/execution/base.py` | `IBaseExecution` 无人引用 |
| `app/data/collector/` | 整包零引用 |
| `app/db/init_db.py` | 真正的实现在 `storage/service.py`，此文件是孤儿 |

### 移动去重

- `core/engine/component.py` 与 `engine/component/base.py` 重复定义 `ComponentState`
  → **合并**（注意：原文件里另有唯一的 `TaskSchedulerComponent`，已提取到 `component/scheduler.py`）
- `2501` 行的 `dashboard.py` → `monitoring/static/dashboard.html` + 30 行的加载器
- 5 个脚手架 `__init__.py` → 全部重写为真正的包契约
- `cli.py`（2642 行）→ `cli/` 包（9 个模块，最大的 603 行）
- `monitoring/server.py`（1871 行）→ `server.py`(491) + `resource.py`(538) + `api/`(8 个 mixin)

### 迁移脚本（都在 `scripts/`，可复现、可重跑）

| 脚本 | 作用 |
|---|---|
| `migrate_files.py` | 权威的「旧路径 → 新路径」清单 `MOVES` |
| `finalize_imports.py` | 用 `MOVES` + git rename 推导映射，批量改写 `app.xxx` |
| `split_cli.py` | `cli.py` → `cli/` 包（含 AST 校验） |
| `split_monitoring.py` | `server.py` → 传输层 + 核心 + mixin（含 AST 校验） |
| `check_undefined.py` | AST 作用域分析，找「用到了但没有来源」的名字 |
| `check_lost_aliases.py` | 比对暂存区与工作区，找批量改写中被丢掉的 `as` 别名 |
| `run_tests_isolated.sh` | 逐文件跑测试，绕开宿主机临时目录的文件锁 |

### 顺手修掉的一个仓库 bug

`.gitignore` 里写的是 `data/`，它会匹配**任意层级**的 data 目录 ——
包括 `app/data/` 这个源码包。结果是整个数据源层长期游离在版本控制之外，
`git add` 也加不进去。已改成 `/data/`（只忽略项目根目录的数据文件夹）。

---

## 八、按需求查位置

| 我想… | 去哪 |
|---|---|
| 新增一个数据源 | `datasource/adapters/` 加适配器 + `config/providers.json` 注册（见 DATA_SOURCES.md） |
| 改取数顺序 / 看为什么先试某个源 | `datasource/routing/router.py`，`datasource/provider/health.py` |
| 加一个技术指标 | `feature/indicator/builtin.py` |
| 加一个标量因子 | `feature/library.py`（表达式）或 `feature/builtin.py`（代码） |
| 改打分权重 | `config/score.json` + `alpha/score.py` |
| 加/改交易规则 | `strategy/rule/` |
| 写一个新策略 | `strategies/` 目录（自动发现，回测/实盘通用）|
| 看某个源为什么被熔断 | `python main.py providers` / `--probe` |
| 加一道风控闸 | `risk/builtin.py` |
| 改引擎装配顺序 | `engine/builder.py` |
| 改面板页面 | `monitoring/static/dashboard.html`（纯前端）|
| 改面板的某个接口数据 | `monitoring/api/` 下对应业务域（overview / live / pool / factors …） |
| 改面板的 URL 路由或响应格式 | `monitoring/resource.py` |
| 改面板的快照落盘策略 | `monitoring/server.py`（组件核心） |
| 补一个交易日数据缺口 | `engine/integrity.py`，CLI `python main.py dataguard` |
| 加一条 CLI 命令 | 按业务域挑 `cli/*_cmds.py`；跨模块共用的工具放 `cli/base.py` |
| 改 CLI 的全局选项 | `cli/base.py` 的 `cli` 组 |

## 九、新旧位置对照

| 旧路径 | 新路径 |
|---|---|
| `app/utils/logger.py` | `app/common/logging.py` |
| `app/utils/jsonio.py` | `app/common/jsonio.py` |
| `app/core/config.py` | `app/common/config.py` |
| `app/core/market/types.py` | `app/domain/types.py` |
| `app/core/market/calendar.py` | `app/domain/calendar.py` |
| `app/core/market/clean.py` | `app/domain/clean.py` |
| `app/core/market/series.py` | `app/domain/series.py` |
| `app/data/datasource.py` | `app/domain/symbol.py` |
| `app/data/models/*` | `app/datasource/model/*` |
| `app/data/net.py` | `app/datasource/transport/net.py` |
| `app/data/providers/base.py` | `app/datasource/provider/base.py` |
| `app/data/providers/health.py` | `app/datasource/provider/health.py` |
| `app/data/providers/registry.py` | `app/datasource/provider/registry.py` |
| `app/data/providers/config.py` | `app/datasource/routing/config.py` |
| `app/data/providers/router.py` | `app/datasource/routing/router.py` |
| `app/data/providers/quality.py` | `app/datasource/routing/quality.py` |
| `app/data/providers/adapters/*` | `app/datasource/adapters/*` |
| `app/data/services/data_service.py` | `app/datasource/service/data_service.py` |
| `app/data/marketx.py` | `app/datasource/service/facade.py` |
| `app/data/sources.py` | `app/datasource/compat/registry.py` |
| `app/data/kline_*.py`、`*_source.py` | `app/datasource/compat/*`（逐步下线） |
| `app/data/storage/raw_store.py` | `app/datasource/raw/raw_store.py` |
| `app/db/database.py` | `app/storage/engine.py` |
| `app/db/models.py` | `app/storage/schema.py` |
| `app/repository/*_repository.py` | `app/storage/repo/*_repo.py` |
| `app/core/indicator/*` | `app/feature/indicator/*` |
| `app/core/factor/*` | `app/feature/*` |
| `app/core/data/features.py` | `app/feature/features.py` |
| `app/core/data/integrity.py` | `app/engine/integrity.py` |
| `app/core/score/*` | `app/alpha/score.py` |
| `app/core/pool/*` | `app/alpha/pool/*` |
| `app/core/strategy/*` | `app/strategy/*` |
| `app/core/rule/*` | `app/strategy/rule/*` |
| `app/core/risk/*` | `app/risk/*` |
| `app/core/portfolio/*` | `app/portfolio/*` |
| `app/core/execution/*` | `app/execution/*` |
| `app/core/engine/*` | `app/engine/*` |
| `app/core/engine/components/*` | `app/engine/component/*` |
| `app/core/task/*` | `app/engine/tasking/*` |
| `app/core/collect/*` | `app/engine/collect/*` |
| `app/core/bus/*` | `app/engine/bus/*` |
| `app/core/hub/*` | `app/engine/hub/*` |
| `app/core/news/*` | `app/news/*` |
| `app/core/monitor/*` | `app/monitoring/*` |
| `app/core/monitor/dashboard.py` | `app/monitoring/static/dashboard.html` + `app/monitoring/dashboard.py`（加载器） |
| `app/monitoring/server.py`（HTTP 部分） | `app/monitoring/resource.py` |
| `app/monitoring/server.py`（视图方法） | `app/monitoring/api/*.py`（mixin） |
| `app/cli.py`（单文件 2642 行） | `app/cli/` 包（base + 8 个 `*_cmds.py`） |
| `app/backtest/*` | `app/research/*` |
| `app/strategy/{factor,ma_cross,spec_strategy,strategy,trend_ma_cross}.py` | `app/research/strategies/*`（legacy backtrader） |
| `app/test/*` | `app/tests/*` |

---

## 十、遗留事项

这几项本次**没有**做完，留作后续迭代（不影响现有功能）：

1. **`datasource/compat/` 是过渡产物**，等调用方全部迁到 `datasource/service` 后可删除。
   目前 `kline_sources.py` 与 `storage/service.py` 仍在用它。
2. **几处潜在的循环依赖**被函数内延迟 import 掩盖了
   （如 `domain/calendar` ⇄ `datasource/service/facade`），打破前不要给这些包加 eager 导出。
3. **`domain/symbol.py` 里混装了两类东西**：`normalize_symbol` / `normalize_date`（领域层，
   放在这里是对的）与 `AkshareDataSource` / `DataSourceFactory`（具体数据源实现，
   L1 不该知道数据源存在）。后者属于 `datasource/compat/`，宜整体搬过去。
4. **面板 HTML 的测试散落在 `test_net_transport.py` 里**（`TestPoolRankAndBanner`
   其实测的是 dashboard 的 JS/CSS）。功能正常，但该归入 `test_monitor.py`。
5. **全量测试跑不完**：宿主机（Windows）上系统临时目录的文件删除会**无限期阻塞**
   （`os.unlink` / `os.rmdir` / SQLite `close` 都中招，无异常、直接卡住）。
   这不是代码问题 —— 用 `scripts/run_tests_isolated.sh` 逐文件跑可以拿到完整结果。
