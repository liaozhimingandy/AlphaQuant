# 数据采集层架构：Provider / Capability / Router / Canonical

> 面向"数据源永远在变，而因子和策略一行都不该改"这件事。
> 配套代码：`app/providers/market/provider/`（Provider·能力·熔断·路由·质量）、
> `app/providers/market/model/`（标准模型）、`app/providers/market/service/`（对外统一入口）、
> `app/providers/market/raw/`（Raw 层）、`config/providers.json`（编排配置）。

---

## 0. 一句话结论

> **数据源是可替换的，数据标准不能替换；因子是核心资产，数据源只是基础设施。**

所以架构围绕一条硬边界来组织：

```
        外部数据源 ──Adapter──▶ 标准模型 ──▶ 因子 / 策略 / 回测
                        ▲
                    Router 决定用谁
```

因子里**不应该出现** akshare、baostock、东财、雪球这些名字。
一旦出现，"换数据源" 就等价于 "把所有因子重写一遍"。

---

## 1. 分层

```
                       ┌──────────────────────┐
                       │  Strategy / Backtest │
                       └──────────┬───────────┘
                                  │  Feature（Gold）
                       ┌──────────▼───────────┐
                       │      Factor Engine   │
                       └──────────┬───────────┘
                                  │  Canonical Model（标准模型）
                  ┌───────────────▼───────────────┐
                  │          DataService          │
                  │  get_kline / get_hot_rank /   │
                  │  get_flow_rank / get_fund_flow│
                  └───────────────┬───────────────┘
                                  │
                  ┌───────────────▼───────────────┐
                  │          DataRouter           │
                  │ ①能力匹配 ②排序 ③熔断 ④执行+校验│
                  └──┬──────────┬──────────┬──────┘
                     │          │          │
              ┌──────▼───┐ ┌────▼────┐ ┌───▼──────┐
              │ Baostock │ │ AkShare │ │ EastMoney│  …… Provider
              │  Provider│ │ Provider│ │  Provider│
              └──────┬───┘ └────┬────┘ └───┬──────┘
                     │          │          │  Adapter：外部格式 → 标准模型
                     └──────────┼──────────┘
                                │
                     ┌──────────▼──────────┐
                     │  RAW（Bronze）原始层 │  审计 / 重放
                     └─────────────────────┘
```

目录：

| 路径 | 职责 |
| --- | --- |
| `app/providers/market/model/` | **标准数据模型**：`Kline` / `RankRow` / `FundFlowRow` + 请求对象 + 枚举 |
| `app/providers/market/provider/base.py` | `DataCapability` 能力枚举、`ProviderErrorType` 错误分类、`DataProvider`、`@capability` |
| `app/providers/market/provider/health.py` | 能力级熔断器 + 健康分（成功率/延迟/新鲜度） |
| `app/providers/market/provider/registry.py` | 注册表：注册、按能力查、排序 |
| `app/providers/market/routing/router.py` | 路由器：四层决策、血缘、事件 |
| `app/providers/market/routing/quality.py` | 数据质量校验（OHLC 自洽、日期唯一、代码非空） |
| `app/providers/market/routing/config.py` | 读 `config/providers.json` |
| `app/providers/market/adapters/` | 各数据源的 Adapter（**新增数据源只动这里**） |
| `app/providers/market/service/data_service.py` | `data_service`：上层唯一入口 |
| `app/providers/market/raw/raw_store.py` | Raw / Bronze 原始响应存档 |

---

## 2. 按"能力"设计，而不是按"数据源"设计

不要写成 `AkShareService / BaoStockService` 然后上层 `akshare.get_kline()`。
反过来：定义能力，让每个 Provider 声明自己能干什么。

```python
class DataCapability(str, Enum):
    DAILY_KLINE = "daily_kline"
    MINUTE_KLINE = "minute_kline"
    HOT_RANK = "hot_rank"
    FLOW_RANK = "flow_rank"
    FUND_FLOW = "fund_flow"
    STOCK_BASIC = "stock_basic"
    FINANCIAL_STATEMENT = "financial_statement"
    ...
```

于是：

```
DAILY_KLINE ├─ baostock_daily ├─ akshare_daily ├─ astockdata └─ mootdx
HOT_RANK    ├─ emapp_hot ├─ xueqiu_hot ├─ akshare_em ├─ akshare_spot └─ local_hot(兜底)
FUND_FLOW   ├─ akshare └─ tushare
FLOW_RANK   └─ akshare_flow
```

**"mootdx 没有 K 线以外的数据" 不是缺陷，是 Capability 声明。**
路由看到它没有声明 `HOT_RANK`，压根不会去调它 —— 这是 `NOT_SUPPORTED`，不是失败。

### Provider 长什么样

刻意**不做**万能接口（`get_kline/get_financial/get_news` 全写一遍然后
`raise NotImplementedError`）。那种写法每加一个源就要补十几个空方法，
而且"到底实现了没有"只能靠读代码。这里改成**按能力注册**：

```python
class AkShareProvider(DataProvider):
    name = "akshare"
    priority = 80
    hosts = ("push2his.eastmoney.com",)

    @capability(DataCapability.DAILY_KLINE, name="akshare_daily",
                hosts=("push2his.eastmoney.com",))
    def daily(self, symbol, start=None, end=None, adjust="qfq"):
        from app.providers.market.compat.kline_sources import _akshare_daily
        return _akshare_daily(symbol=symbol, start=start, end=end, adjust=adjust)

    @capability(DataCapability.HOT_RANK, name="akshare_em", priority=40)
    def hot_rank_em(self, top=None): ...
```

一个 Provider 可以有**同一能力的多个实现**（不同端点），比如 akshare 的
`akshare_em` 与 `akshare_spot` —— 它们风控面不同，必须能各自独立熔断。

---

## 3. Router：四层决策

```
       数据请求
          │
          ▼
   ① 能力匹配        没有 Provider 声明该能力 → NOT_SUPPORTED（不是失败）
          │
          ▼
   ② 排序            0.7 × 实测健康分 + 0.3 × 配置优先级；兜底源恒最后
          │
          ▼
   ③ 熔断检查        (provider, capability, route) 级熔断 / 半开探测
          │
          ▼
   ④ 执行            重试（仅可重试类型）→ 成功 → 质量校验
          │
     ┌────┴────┐
   成功       失败
     │          │
  质量校验   错误分类 → 换下一个路由
     │
 ┌───┴───┐
通过    不通过（QUALITY_ERROR → 计入健康度 → 换下一个）
```

### 排序公式

```
最终分 = 0.7 × 健康分(0~100) + 0.3 × 配置优先级(0~100)

健康分 = 70% 成功率 + 20% 恢复度 + 10% 延迟
配置优先级 = routes 里的位置（第1个 100、第2个 90 …）；没写进 routes 的封顶 50
```

关于"恢复度"这个命名的来由（踩过的坑）：它算的是**距上次失败**多久，
不是距上次成功。后者会让一个一直好好干活的源随时间自己掉分，
于是一个从未被验证过的源（满分 100）就能把它挤下去 ——
表现为"配好的首选源莫名其妙换人了，而没人动过配置"。
恢复度只负责一件事：让**坏过的源**慢慢涨回来（半衰期 30 分钟）。

- 初始大家都没数据（健康分都是 100）→ **顺序完全由 `config/providers.json` 决定**，可预期；
- 一旦某个源连续失败，健康分掉下来 → 它自动排到后面，
  不会出现"被封的源还一直排第一"这种最浪费请求额度的失败模式；
- **兜底源不参与竞争**：本地库兜底几乎不会失败，若按分排序它迟早涨到队首，
  变成"明明有网却一直用本地旧数据"—— 比拿不到数据更难发现。
  所以兜底源**只在正常源全部失败之后**才尝试（两轮）。

### 参数按适配器签名裁剪

路由手里的参数是"给所有源的并集"（人气榜要 `trade_date`、资金榜要 `rank_kind`）。
全量透传会让只认 `top` 的适配器直接 `TypeError`。
所以路由按 `inspect.signature` 裁剪后再调用 —— **加新源不用回头改调用方**。

---

## 4. 错误分类：不同失败 = 不同处理

| 类型 | 含义 | 重试 | 计入健康度 | 额外冷却 |
| --- | --- | --- | --- | --- |
| `NETWORK_ERROR` | 连不上 / 连接重置 / 代理拦截页 | ✅ | ✅ | — |
| `TIMEOUT` | 超时 | ✅ | ✅ | — |
| `SERVER_ERROR` | 源站 5xx | ✅ | ✅ | — |
| `RATE_LIMIT` | 限流 | ✅ | ✅ | 10 min |
| `BLOCKED` | 被封 IP / 反爬 / 403 | ❌ | ✅ | 15 min |
| `AUTH_ERROR` | token 无效 / 积分不足 | ❌ | ✅ | 30 min |
| `PARSE_ERROR` | 接口改版 / 缺列 | ❌ | ✅ | — |
| `QUALITY_ERROR` | 数据自相矛盾 | ❌ | ✅ | — |
| `NO_DATA` | 源是好的，只是这份数据没有（停牌/退市/非交易日） | ❌ | ❌ | — |
| `NOT_SUPPORTED` | 这个源没这个能力 | ❌ | ❌ | — |
| `INVALID_REQUEST` | 我们自己参数写错 | ❌ | ❌ | — |

两个最关键的区分：

1. **接口改版 ≠ 网络抖动**。以前只有"瞬时/确定性"两分法，
   于是出现"明明是接口改版，却重试 3 次再换源，一轮白白耗掉十几秒"。
2. **没这只票的数据 ≠ 源坏了**。`NO_DATA` 只记调用次数、不算失败，
   否则连续查几只停牌股就能把一个好源熔断掉。

`JSONDecodeError` 单列说明：代理返回拦截页时接口拿到的是一段 HTML，
解析方报 JSONDecodeError —— 这是**传输层**问题，重试大概率能拿到正常响应，
所以判成 `NETWORK_ERROR` 而不是 `PARSE_ERROR`。

---

## 5. 熔断：**能力级**，不是数据源级

```
HEALTHY ──连续失败≥阈值──▶ OPEN ──冷却结束──▶ HALF_OPEN
   ▲                                             │
   └──────────成功◀────探测成功────────────────────┘
                           │
                        探测失败
                           ▼
                         OPEN（重新计时）
```

熔断键是 `(provider, capability, route)`。为什么必须这么细：

```
akshare_daily（push2his）被限流  ≠  akshare_em（push2）也不能用
```

实测就出现过"日线被拦、人气榜正常"。把整个 akshare 判死，
等于白白扔掉一半还能用的能力。

`HALF_OPEN` 只放**一个**探测请求：放两个的话，源刚好在被封边缘，
两个请求一起失败会让它永远起不来。

---

## 6. Canonical Model（标准数据模型）

```python
@dataclass(slots=True)
class Kline:
    symbol: str
    date: date
    open, high, low, close: float
    volume, amount: float | None
    frequency: Frequency      # 1d / 1m / ...
    adjust: AdjustType        # none / qfq / hfq
    # ---- 血缘 ----
    source: str               # 具体路由（akshare_daily）
    provider: str             # 数据源身份（akshare）
    fetched_at: float
    raw_id: str               # Raw 层存档，可回放原始响应
```

标准模型解决的是：baostock 给中文列、通达信给小写英文列且**没有成交额**、
腾讯没有成交额且量纲是"手"。外部格式 → 标准模型**只在适配器里发生一次**。

与 `app/providers/market/compat/kline_base.py` 的分工：

| | 口径 | 用途 |
| --- | --- | --- |
| `kline_base.DAILY_COLUMNS` + `normalize_daily` | **列式**契约 | 整表搬运：适配器、入库、因子批量计算 |
| `models.Kline` | **行式**契约 + 血缘 | 追溯"这一根 K 线是谁给的、什么时候给的" |

两者列完全一致，可无损互转（`Kline.from_frame` / `Kline.to_frame`）。

**成交量单位不做换算**：baostock/通达信给"股"，akshare/腾讯给"手"。
换算需要知道每个源的确切口径，猜错就是 100 倍的错，所以原样透传并在文档标注。

---

## 7. Raw → Standard → Feature（Bronze / Silver / Gold）

```
外部源 → RAW（Bronze，原样存档）→ 标准化/清洗 → MARKET（Silver，入库）→ 因子 → FEATURE（Gold）
```

为什么要存 Raw：

> "为什么 2025-08-01 的贵州茅台收盘价和历史数据库不一样？"

没有 Raw 层，这个问题只能靠重新请求外部接口来回答 —— 而外部接口往往
已经变了、限流了、或者根本不认这段历史。有了 Raw 层，链路是完整的：

```
因子 → 特征 → 标准 K 线 → raw_id → 原始响应 → 那次请求的参数
```

落盘策略（刻意轻量）：一天一个目录、一条记录一个 JSON；
默认只存前 200 行 + 列名 + 总行数（目的是**排查**不是做数据湖）；
写失败一律忽略。保留 7 天，`RawStore.purge()` 清理。

---

## 8. 数据质量校验

> **"取到了"不等于"取对了"。**

自动换源解决了"拿不到数据"，却引入更隐蔽的风险：某源接口改版/缓存污染，
返回一批"看起来正常"的错数据，路由开心地判定成功，错数据一路流进因子，
而日志里什么异常都没有。所以取到数据后、返回前必须过一遍自洽性检查：

- K 线：`high ≥ max(open, close)`、`low ≤ min(open, close)`、`close > 0`、
  `volume ≥ 0`、日期非空且唯一、OHLC 无空值；
- 榜单：代码非空、名次为正（有则查）、代码不重复。

校验失败按 `QUALITY_ERROR` 处理：**计入健康度并换下一个源，但不重试**
（同一个源再问一次只会拿到同一批错数据）。

不比对成交量（量纲不统一，见第 6 节）。

---

## 9. 配置：`config/providers.json`

```jsonc
{
  "providers": { "baostock": {"enabled": true, "priority": 90}, ... },
  "routes": {
    "daily_kline": ["baostock_daily", "akshare_daily", "astockdata", "mootdx"],
    "hot_rank":    ["emapp_hot", "xueqiu_hot", "akshare_em", "akshare_spot"],
    "flow_rank":   ["akshare_flow"],
    "fund_flow":   ["akshare", "tushare"]
  },
  "router": { "retries": 3, "circuit_threshold": 3, "cooldown": 300.0,
              "quality_check": true, "raw_enabled": true, "raw_max_rows": 200 }
}
```

- 删掉文件或写错格式 → 退化成内置默认值，**不会让取数链起不来**；
- 热重载：`alphaquant providers --reload`（不用重启进程）。

---

## 9.5 采集方式全景（有哪些、各几条、怎么扩）

> 这一节回答两个高频问题：**"我现在到底采了哪些东西"**、
> **"某个数据想再加一个源，动哪里"**。

### 三条链路并存，别改错地方

| 链路 | 入口 | 覆盖范围 | 有没有多源降级 |
|---|---|---|---|
| **新链路 Router** | `datasource/routing/router.py` | **只有 4 种能力**（见下表） | ✅ 有：多源排序 + 重试 + 熔断 + 质量校验 |
| **老链路 facade** | `datasource/service/facade.py` | 20 个 `fetch_*` 函数 | ❌ **没有**：单源直连，挂了就是挂了 |
| **股票池同步** | `alpha/pool/sync.py` | 14 个 `sync_*` 类别 | ❌ 没有，直接调 facade / 直连仓储 |

**这是当前最大的结构性缺口**：真正走多源降级的只有 4 种能力，
其余 16 个取数函数（筹码、涨停池、估值、新闻、宏观、龙虎榜、融资融券…）
都是单源直连 —— 它们一挂就全挂，而且报出来的错和网络层故障长得一模一样。
这也是"股票池采集还是有问题"的深层原因：`flow_rank` 这类**进了 Router 的**
能力至少有熔断兜底，没进 Router 的连兜底都没有。

### 新链路 Router：4 种能力

| 能力 | 路由条数 | 源（按尝试顺序） | 备注 |
|---|---|---|---|
| `daily_kline` | **4** | baostock_daily → akshare_daily → astockdata → mootdx | 最稳的一条，东财被拦时有后手 |
| `hot_rank` | **4** | emapp_hot → xueqiu_hot → akshare_em → akshare_spot | 跨了东财/雪球两个风控面 |
| `fund_flow` | **2** | akshare → tushare | tushare 需 token |
| `flow_rank` | **1** ⚠️ | akshare_flow | **单点**：唯一的源挂了就"1 条路由已全部用尽" |

> `flow_rank` 只有一条，是**当前最容易整片失败的能力**。
> 你看到的 `路由[flow_rank] 失败：1 条路由已全部用尽` 就是它 ——
> 不是"所有源都试过了"，而是**压根没有第二个源可试**。

### 老链路 facade：20 个取数函数（单源）

```
资金面  fetch_fund_flow / fetch_fund_flow_rank / fetch_chip
        fetch_board_fund_flow / fetch_margin / fetch_dragon_tiger
行情面  fetch_spot / fetch_limitup_pool / fetch_boards / fetch_concept_cons
        fetch_market_breadth / fetch_trade_dates
基本面  fetch_valuation / fetch_financial_indicator / fetch_forecast
        fetch_holders / fetch_rating
消息面  fetch_news / fetch_macro
人气面  fetch_hot_rank
```

### 股票池同步：14 个类别

`sync_fund_flow` / `sync_chip` / `sync_fundamentals` / `sync_factors` /
`sync_news` / `sync_macro` / `sync_breadth` / `sync_category` /
`sync_limitup` / `sync_hot_rank` / `sync_flow_rank` / `sync_stock_info` /
`sync_boards` / `sync_concept_members` / `sync_board_members`

### 想扩展：看清你要扩的是哪一层

| 你想做的事 | 改哪里 | 几处 |
|---|---|---|
| 给**已有能力**加一个备选源（如给 flow_rank 加第二个源） | 写 Adapter + 加进 `config/providers.json` 的 `routes` | **2** |
| 加一个**新能力**（如"北向资金"） | 加 `DataCapability` 常量 + Adapter + `routes` | 3 |
| 加一个**新的 sync 类别**（如"大宗交易"） | facade 写 `fetch_*` + sync 写 `sync_*` + 建表 | 3~4 |
| 把某个老 `fetch_*` **迁进 Router**（获得多源降级） | 加 Capability + Adapter 包一层 + `routes` | 3 |

最小可用的"加一个备选源"见下一节 §10。

---

## 10. 新增一个数据源：两步

### 第 1 步：写 Adapter

```python
# app/providers/market/adapters/mysource.py
class MySourceProvider(DataProvider):
    name = "mysource"
    priority = 60
    hosts = ("api.mysource.com",)      # 声明依赖域名：被判死时直接跳过，不等超时

    @capability(DataCapability.DAILY_KLINE, name="mysource_daily")
    def daily(self, symbol, start=None, end=None, adjust="qfq"):
        raw = ...                       # 你自己的取数
        return normalize_daily(raw, symbol, start=start, end=end)
```

约定：

- **取不到** → 抛 `DataUnavailable`（源没坏，不降分）；
- **源坏了** → 抛 `SourceError` / 任意异常（会被分类后计入熔断）；
- 返回 `DataFrame`，列名走 `app/providers/market/compat/kline_base.DAILY_COLUMNS`。

### 第 2 步：挂进装配列表

```python
# app/providers/market/adapters/__init__.py
BUILTIN_PROVIDERS / _load() 里加上 MySourceProvider
```

路由、熔断、排序、质量校验、Raw 存档**全部自动生效**，
引擎/策略/因子一行都不用改。

---

## 11. 可观测

```bash
alphaquant providers                     # 路由 / 能力 / 健康分 / 熔断状态
alphaquant providers --capability hot_rank
alphaquant providers --probe             # 逐条路由实打一次
alphaquant providers --reset             # 清健康分与熔断（修完网络环境用）
alphaquant providers --reload            # 热重载配置
```

HTTP：`GET /api/providers`（`routes` / `capabilities` / `config` / `used`）。

日志长这样（排障时看这些行）：

```
路由[hot_rank] 开始...
路由[hot_rank] · akshare_em 第1/4 次失败（network_error·可重试）: ProxyError ... → 0.5s 后重试
路由[hot_rank] · akshare_em 失败（network_error·不重试）: ...
路由[hot_rank] 成功 · **emapp_hot**（eastmoney，第1次尝试，329ms，50 行）
```

---

## 12. 迁移状态（重要）

| 入口 | 链路 | 说明 |
| --- | --- | --- |
| `fetch_hot_rank` / `fetch_fund_flow` / `fetch_fund_flow_rank` | ✅ 新 Router | 走 `config/providers.json` 的 routes |
| `kline_sources.fetch_daily`（含数据校验补全） | ✅ 新 Router | 同上 |
| `app/providers/market/compat/registry.py` 的 `REGISTRY` | ⏸ 兼容期保留 | 仍在注册（面板 / 旧调用方在读）；新链路会把采集事件**镜像**给它，保证"最近成功的源"继续可查 |
| `DataSourceFactory` / `MarketDataService`（老链路） | ⏸ 兼容期保留 | 分钟线、部分采集组件仍在用 |

等老链路全部迁完，删掉 `DataRouter._mirror()` 与 `sources.py` 即可。

**旧链路的两条经验已被新链路吸收并加强**：

- 权重降权 → 升级为"健康分（成功率+延迟+新鲜度）"；
- 域名级快速跳过 → 保留，并回写给 `net` 层；
- 兜底源恒最后 → 从"排序时置底"升级为"两轮，正常源全挂才试"。
