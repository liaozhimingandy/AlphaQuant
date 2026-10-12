# 扩展指南：往哪儿加、怎么加

> 每条都按「**改哪些文件 → 最小示例 → 怎么验证 → 坑**」四段写。
> 读之前建议先扫一眼 `RUNTIME.md`，知道引擎是怎么跑起来的。

---

## 0. 扩展点总表

| 想加什么 | 入口 | 需要改几处 |
|---|---|---|
| 数据源 | `app/providers/market/adapters/` + `routing/config.py` | 2 |
| 因子 | `app/modules/factor/builtin.py`（或写公式） | 1 |
| 策略 | `app/modules/strategy/`、`config/factors.json`、或用户目录 | 1 |
| 风控规则 | `app/modules/risk/builtin.py` | 1 |
| 引擎组件 | `app/modules/<模块>/service.py` + `app/engine/builder.py` | 2~3 |
| 调度节拍 | `app/runtime/scheduler.py`（改节拍本身才动这里，加定时任务不用） | 1 |
| 面板 API | `app/monitoring/api/` + `resource.py` | 2 |
| 存储表 | `app/data/schema.py` + `repo/` | 2~3 |

> 判定放哪：`app/engine/` 只放**容器与契约**（生命周期、装配、依赖解析），
> `app/runtime/` 只放**怎么跑**（调度、并发、事件分发），其余业务一律进
> `app/modules/` 下对应的模块。完整取舍见 [STRUCTURE.md](STRUCTURE.md)。

---

## 1. 新增一个数据源

### 改哪些文件

1. `app/providers/market/adapters/<你的源>.py` —— 写一个 `DataProvider` 子类
2. `config/providers.json`（或 `routing/config.py:48-56` 的默认 routes）—— 把它挂进候选顺序

> 注册只需把类加进 `adapters/__init__.py` 的 `BUILTIN_PROVIDERS` 列表即可，
> **不必**再改 `_load()`。

### 最小示例

```python
# app/providers/market/adapters/mysource.py
from app.providers.market.provider.base import DataProvider, DataCapability, capability

@capability(DataCapability.DAILY_KLINE)
class MySourceProvider(DataProvider):
    name = "mysource"
    hosts = ("api.mysource.com",)     # 域名级熔断靠它，别写错

    def fetch_daily_kline(self, symbol, start, end, **kw):
        # 阻塞 IO 没关系：路由层会放进线程池
        ...
        return df
```

然后在 `app/providers/market/adapters/__init__.py`：

```python
BUILTIN_PROVIDERS: List[Type[DataProvider]] = [MySourceProvider]
```

再在 `config/providers.json` 的 `routes.daily_kline` 里加上 `"mysource"`。

### 怎么验证

```bash
python main.py data providers        # 看源是否注册、支持哪些能力
python main.py data order daily      # 看该能力的尝试顺序
python main.py data health           # 看健康分/熔断状态
```

### 坑

- **`hosts` 必须写对**。它是域名级熔断（`transport/net.py:51-53`，
  连续 2 次失败冷却 300s）的依据。写错的话要么熔断打不到真正的域名，
  要么误伤别人。
- **能力常量只声明不实现等于没有**。`provider/base.py:48-75` 声明了 15 个能力，
  目前只有 `daily_kline` / `hot_rank` / `fund_flow` / `flow_rank` 四个有实现。
  `MINUTE_KLINE` 甚至已经写好了质量校验器（`routing/quality.py:190`），
  但全仓没有任何 `@capability(MINUTE_KLINE)`——分钟线实际还是走
  `domain/symbol.py:486` 的老路子。
- **新旧双链路并存**：`app/modules/universe/pool/sync.py:25` 只依赖 `facade`（老层），
  新的 `DataRouter` 只被 `service/data_service.py`、`cli/data_cmds.py`、
  `monitoring/resource.py` 用。`router.py:379-412` 还反过来往老注册表
  补事件来"假装"两边一致——所以面板上看到的健康分，可能并不反映
  真实主链路。

---

## 2. 新增一个因子

三种方式，从轻到重。

### 2.1 最轻：界面上写一句公式

因子实验室里写表达式即可，存进 `config/factors.json`。
启动时 `library.py:239` 会自动注册（`register_all`）。

```json
{
  "name": "my_mom",
  "expr": "(close / delay(close, 20) - 1) * 100",
  "description": "20日动量"
}
```

### 2.2 标准：写一个因子类

```python
# app/modules/factor/builtin.py
from app.modules.factor.base import IBaseFactor
from app.modules.factor.context import FactorContext
from app.modules.factor.registry import register_factor

@register_factor
class MyFactor(IBaseFactor):
    name = "my_factor"
    description = "示例因子"

    def compute(self, ctx: FactorContext):
        # 数据不足必须返回 None，不能返回 0 ——
        # 返回 0 会被当成一个真实的"中性打分"，污染排序
        if ctx.series is None or len(ctx.series) < 20:
            return None
        return float(...)
```

**唯一硬约束**：`compute()` 数据不足时返回 `None`，不要返回 `0`。
原因见 `ARCHITECTURE.md` §4.17。

### 2.3 最重：让机器自己挖

`app/modules/factor/mining/` 的遗传规划 + `app/modules/factor/mining/ml.py` 拟合。
产出仍是"因子"，落回同一个注册表和同一张通用因子值表。

### 怎么验证

```python
from app.modules.factor.registry import list_factors, create_factor
print(list_factors())                       # 有没有你的因子
print(create_factor("my_factor").compute(ctx))
```

---

## 3. 新增一个策略

### 3.1 配置式（不写代码）

在 `config/` 下写规则组合，界面可改、可一键上线。
适合"几个因子 + 阈值"这类。

### 3.2 代码式

```python
# app/modules/strategy/rule/builtin.py（或 USER_STRATEGY_DIR 指向的目录）
class MyStrategy:
    def on_bar(self, ctx, state):
        ...
        return signal      # BUY / SELL / HOLD
```

放在 `USER_STRATEGY_DIR`（默认配置见 `config.py:292`）里的策略会被自动加载
（`USER_STRATEGY_AUTOLOAD`，启动顺序细节见 `research/registry.py:44-84`——
内置与用户策略**分开加载**，否则先加载用户策略会让内置策略永远补不上）。

**回测与实盘共用一套发现机制**（`ARCHITECTURE.md` §4.13），
所以策略只需实现一次，两个环境都能跑。

---

## 4. 新增一条风控规则

```python
# app/modules/risk/builtin.py
@register_risk
class MyRule(IBaseRisk):
    name = "my_rule"
    def check(self, order, task) -> RiskVerdict:
        ...
```

规则被串进两道闸门：

1. **全局闸门**（`task.py:417-435`）—— 由 `RiskService.gate` 注入，
   对所有任务生效。**闸门自己抛异常时放行**（`:422-426`），避免风控故障把交易也一起搞挂。
2. **任务级风控链**（`task.py:438-442`）—— 各任务自己的 `risk_chain`。

两者都通过时，`scale` 取**更小**的那个（`:444`）。

---

## 5. 新增一个引擎组件

先决定**它是哪一类**（`kinds.py:54-95`），这决定了它在启停序列里的位置：

| 类别 | 什么时候用 | 层值 |
|---|---|---|
| `ServiceComponent` | 提供能力给别人用（行情/采集/风控…） | 20 |
| `StrategyComponent` | 消费行情、产生信号 | 30 |
| `WorkerComponent` | 执行一次性作业，需要并发限流 | 40 |
| `SchedulerComponent` | 决定什么时候跑什么 | 10 |
| `IBaseComponent` | 其它（如面板） | 50 |

最少实现：

```python
from app.engine.capability import ServiceComponent, Cap

class MyService(ServiceComponent):
    name = "my_service"
    provides = {Cap.MY_CAP}

    def on_initialize(self): ...
    def on_start(self): ...
    def on_stop(self, graceful=True): ...
    def snapshot(self) -> dict: ...
```

然后在 `builder.py` 里 `engine.register_component(MyService(...))`。

**如果想声明服务间依赖**：覆写 `depends_on = ("other_service",)`。
引擎会做拓扑排序，成环直接报错（`ENGINE_ARCHITECTURE.md` §5）。
注意目前六个内置服务都没用这个，机制是通的但还没人声明。

---

## 6. 新增一个面板 API

1. `app/monitoring/api/<name>.py` —— 写一个 `ViewsMixin`，提供 `xxx_view` 方法
2. `app/monitoring/resource.py` —— 注册路由（约 19 个路由 head 在此）
3. `app/monitoring/static/dashboard.html` —— 前端调用

> 已有 8 个 `ViewsMixin` 全部接线，可直接照抄；
> `/api/health`、`/api/providers`、`/api/sources` 三个端点目前**无任何 .py 调用**
> （留给 curl / 运维排查用），如果要正式使用建议在面板补入口。

---

## 7. 新增一张存储表

1. `app/data/schema.py` —— 定义模型（参照 `stock_factor_value` 的索引设计，
   `schema.py:596-608` 是写得最完整的一个，可作为模板）
2. `app/data/repo/` —— 加仓库方法，走 `session_scope`（`stock_repo.py:41`）
3. 需要的话在 `kline_repo._ensure_pool_tables()` 里加补建逻辑

**坑**：
- 日频截面表都要有**唯一约束 + `on_conflict_do_update`**，否则重复采集会插重复行
  （现有 `system_event` 就没有去重键，`schema.py:247`，长期只增不减）
- 批量写入优先用 `batch_upsert`（`stock_repo.py:83` / `minute_repo.py:71`），
  别在循环里逐条 commit——60 只票会变成 60 次连接 + 60 次 fsync

---

## 8. 已知待办（本次扫描发现，按优先级）

这些是**当前不修也能跑，但迟早会咬人**的项。前两项建议优先处理。

### P0 — 会导致错误结果或挂死

| # | 位置 | 问题 |
|---|---|---|
| 1 | `engine/component/scheduler.py` | **已修**：`on_stop` 在 reactor 线程里 `time.sleep` 且无 deadline，任务卡住即永久挂死 |
| 2 | `storage/repo/kline_repo.py:105` | **已修**：`except` 块里用了未导入的 `logger`，建表失败会从"忽略"变成崩溃 |
| 3 | `engine/component/service/market.py` | **已修**：poll 模式无交易日闸门，非交易日反复用旧 bar 驱动策略 |
| 4 | `research/runner.py` | **已修**：回测成本模型漏算印花税（卖出 0.05%），系统性高估收益 |
| 4a | `risk/registry.py` + `tasking/spec.py` | **已修**：默认风控链没有 T+1 / 涨跌停 / 可卖量，照默认配置上实盘全是废单 |
| 4b | `alpha/pool/sync.py` | **已修**（资金流/筹码）：逐票各开一次事务，60 票 × 9 类 ≈ 540 次 commit |

### P1 — 会误导排查方向

| # | 位置 | 问题 |
|---|---|---|
| 5 | `control.py:467` | **已修**：guard 块缺失时把整个采集快照当体检状态返回 |
| 6 | `control.py:490-506` | **已修**：wait 分支无 errback，失败要干等 300s |
| 7 | `provider/base.py` `_KEYWORDS` | **已修**：缺 `HTTPError` 类名识别，本该重试的被判不重试 |
| 8 | `engine/component/registry.py:294` vs `base.py` | 同一服务状态两处取值方式不同，快照可能显示不一致 |
| 8a | `datasource/service/facade.py` `_throttle` | **已修**：限速器无锁，多线程下 N 个线程同时发车，限速与熔断形同虚设 |
| 8b | `facade.py` `_ff_snapshot` | **已修**：缓存击穿——68 只票同时 miss 会一起翻 100+ 页 |
| 8c | `engine/settings.py` `RunMode` | **已修**：与 `domain/types.py` 各定义一份，跨模块 `==` 恒 False，回测自动停止失效 |
| 8d | `engine/component/service/portfolio.py` | **已修**：过期预留只在 `reserve` 里清理，最后一笔过期后 `is_busy()` 永远为真 |
| 8e | `execution/live_broker.py` | **已修**：`order_timeout` 是死参数，回报丢失会让任务永久静默停摆 |
| 8f | `engine/component/service/trading.py` | **已修**：`sum(1 for ... if b.cancel_all())` 数的是 broker 个数不是撤单数 |

### P2 — 整洁性 / 性能

| # | 位置 | 问题 |
|---|---|---|
| 9 | `scheduler.py:104-108` | 事件叫 `TIMER_3MIN` 但 interval=5（秒），且无订阅者空转 |
| 10 | `engine/collect/spec.py` | 截面循环用 `max(30/300, ...)` 兜下限，与 kline 的"间隔即频率"口径相反 |
| 11 | `domain/symbol.py` | 名义是"代码标准化"，实际塞了完整取数层（含 HTTP、单例、线程锁），导致 `storage → domain → 网络` 的依赖倒置 |
| 12 | `alpha/pool/sync.py` | **部分已修**（资金流/筹码已攒批）：基本面/因子/新闻/宏观/题材仍是逐票独立事务 |
| 13 | `routing/router.py:423` | 每次调用 `inspect.signature` 且无缓存 |
| 14 | `datasource/service/facade.py` | 模块导入期就 `REGISTRY.register`，与 `compat/*_source.py` 重复注册 |
| 15 | `market.py:_fetch_latest` | 未经 `clean_frame` 清洗，与 `_load_symbol_history` 路径不一致 |
| 16 | `engine/component/service/collection.py` | **已修**：`collect_cross_now` 无忙碌保护，面板连点会并发跑两轮全市场采集 |
| 17 | `common/config.py` | 9 个"假配置项"定义了但无人读取（其中 `LIVE_MAX_DAILY_TRADES` 尤其危险：文档让人以为有每日下单闸门，实际没有） |
| 18 | `execution/live_broker.py` / `task.py` | 重启后账本、订单表、去重键、预留全是内存态，**无启动对账与状态恢复** |
| 19 | `risk/builtin.py:85` | `CashReserveRisk` 读的 `planned_size` 从未被写入，恒等于 100 股 |
| 20 | `portfolio/sizer.py:75` | `equity = account.cash`，与 `PositionLimitRisk` 的真实 equity 口径不一致 |

### 命名层面（不影响运行，但会持续误导读代码的人）

`data_guard_*` / `ext_data_*` / `no_ext_data` 这些名字是**旧组件时代**的产物，
重构后它们指向的其实是 `collection_service` 的内部单元：

- `builder.py:162-166, 267-291` —— 参数名 `with_ext_data` / `with_data_guard`
- `cli/engine_cmds.py:121, 125, 177-180, 325` —— CLI 选项 `--ext-data` / `--data-guard`
- `control.py:451-517` —— 方法名 `data_guard_check` 等（注释已解释）
- `news.py:54` —— 实例变量 `self.news_center` 与旧组件名同名

CLI / 配置 key 属于**对外契约**，建议保留；内部方法名可以改成 `guard_*` / `cross_*`，
否则以后每次 grep 旧名都会误报，白名单机制形同虚设。
