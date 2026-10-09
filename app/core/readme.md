# AlphaQuant 核心架构

> 目标：一个后台常驻服务，支持**自定义因子**、**策略自由组合**、**实时新闻事件触发交易**、
> **大模型分析新闻**、**可插拔风控**，并且**一个引擎同时跑多个量化任务**。

---

## 一、原架构的三个问题

原设计是一条自顶向下的单链：

```
MarketData → Indicator → Signal → Factor → Rule → Strategy → Risk → Portfolio → Execution → Broker
```

它对"跑一次回测"是够的，但撑不住上面那组目标。三个硬伤：

### 1. Signal 在 Factor 之前 —— 层次倒置

`Signal` 是**策略的产物**（买卖意图），不可能在因子计算之前就存在。

正确的因果链是：行情 → 指标（数学计算）→ 因子（可解释的业务量）→ 规则（布尔判断）→ 策略（决策）→ **Signal** → 风控 → 仓位 → 撮合。

原来的顺序会让 Factor 变成"对 Signal 的加工"，等于把决策前置到了数据层，策略就再也组合不起来了。

### 2. 只有一条输入通道 —— 新闻无处安放

"实时采集新闻，有事件就触发交易"在原架构里没有落点。新闻不是行情，它**不在K线闭合时到达**，
而且它带来的是**特征**（情感分、置信度），不是信号。

硬塞进 `MarketCenter` 会让行情层依赖新闻层；硬塞进 `Strategy` 会让每个策略都自己写一遍新闻解析。

### 3. 没有"任务"这一层 —— 一个引擎只能干一件事

原架构里 Portfolio 是全局单例。多任务场景下（同时跑 10 个标的、每个标的一套策略+风控+资金），
状态会互相污染：任务A的持仓会进入任务B的可用资金计算。

---

## 二、调整后的架构

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

## 三、双通道数据流（新闻触发交易怎么落）

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
├── indicator/     指标层：SMA/EMA/RSI/MACD/ATR/BIAS/STD/VOL_MA，注册表可插拔
├── factor/        因子层：price/ma/ma_spread/rsi/momentum/volatility/news_*，FactorContext 统一输入
├── rule/          规则层：All/Any/Not 组合器 + 阈值/穿越规则，spec 支持声明式构造
├── strategy/      策略层：Combo/MaCross/NewsDriven，双通道 decide()
├── risk/          风控层：闸门链，任一否决即拒绝；支持 scale 缩仓
├── portfolio/     组合层：Portfolio 账本 + Sizer（fixed/percent/all_in）
├── execution/     执行层：SimulatedBroker（即时成交，无挂单）
├── task/          任务层：QuantTask 实例 + TaskRuntime 容器 + 声明式 spec
├── news/          新闻层：RSS/Atom 源 → 去重 → Keyword/LLM 分析 → NewsEvent
└── engine/
    ├── engine.py      引擎主体（组件生命周期、优雅退出、空闲看门狗）
    ├── builder.py     装配器：config/tasks.json → 一台可运行的引擎
    ├── components/    MarketCenter / StrategyManager / NewsCenter / Timer / TaskScheduler
    └── event.py       EventBus + StandardEvents
```

---

## 六、运行

```bash
# 后台常驻服务（读 config/tasks.json）
python main.py serve --mode SIMULATE --market-mode poll --interval 60

# 离线回放验证（推完自动停，不联网）
python main.py serve --mode BACKTEST --interval 0.01

# 查看能力清单
python main.py factors      # 指标 + 因子
python main.py rules        # 规则类型 + 策略 + 风控
python main.py tasks        # 已配置的任务

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
4. **阻塞 IO 必须走线程池**。新闻抓取和 LLM 调用是同步 HTTP，
   直接在 reactor 线程跑会卡死整个引擎（行情和交易一起停）。
5. **风控自身崩溃按放行处理**。宁可漏一次风控，也不能让风控把系统锁死。
6. **新闻分析失败要降级**。拿不到结论时给出 `confidence=0` 的事件，
   由下游 `news_confidence` 风控拦掉，而不是静默丢弃。
