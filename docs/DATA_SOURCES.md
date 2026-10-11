# 数据源扩展指南：如何新增一个源并注册到引擎

> 面向"某个源挂了要能自动换下一个、以及我要加自己的源"这两件事。
> 配套代码：`app/datasource/compat/registry.py`（注册表 + 权重）、`app/datasource/service/facade.py`（内置适配器）、
> `app/datasource/kline_sources.py`（日线源注册入口）。

> **⚠️ 迁移中：本文描述的是旧链路（`REGISTRY` + 权重）。**
> 取数入口（`fetch_hot_rank` / `fetch_fund_flow` / `fetch_fund_flow_rank` /
> `fetch_daily`）已经改走新的 Provider Router，新增数据源请优先看
> **[docs/DATA_FABRIC.md](DATA_FABRIC.md)**（Provider / Capability / Router /
> Canonical / Raw 分层）。旧注册表在兼容期内保留，仍会被注册与探测，
> 但**不再是取数入口的决策者**。

---

## 1. 为什么要有这一层

取数失败是**常态**而不是例外。本项目真实踩到的坑：

| 现象 | 真实原因 |
| --- | --- |
| "资金流同步有 68 只失败" | 本地代理拦截了东财 `push2his.eastmoney.com`（全是 `ProxyError`，**不是** 429 限流） |
| 某个源某天突然全空 | 源站改版 / 非交易日 / 接口下线 |
| 某只票取不到 | 停牌、退市、该票无记录（源本身是好的） |

如果每个 `fetch_xxx()` 都自己写 `if/else` 切源，加一个新源要改 N 处代码，
而且失败原因永远说不清。所以把"注册 / 排序 / 切换 / 熔断"抽成一层。

## 2. 架构：注册 → 排序 → 自动切换

```
fetch_hot_rank(top) / fetch_fund_flow(symbol, days)
        │
        ▼
REGISTRY.fetch("hot_rank", top=...)
        │
        ├─ 找出所有注册了该能力的源，按优先级排序
        │
        ├─ 源 A ──第1次失败（瞬时）──▶ 退避 0.5s
        │       ├─第2次失败──▶ 退避 1.0s
        │       ├─第3次失败──▶ 退避 2.0s
        │       ├─第4次失败──▶ 用尽 → 记账（源级才熔断）→ 换下一个源
        │       └─确定性错误（缺列/接口改版）──▶ 不重试，立刻换源
        │       └─成功──▶ 返回，清空失败计数，记下"用了这个源"
        │
        ├─ 源 B ──同上──▶ ...
        │
        └─ 全挂 ──▶ WARNING 告警 + raise SourceError("所有数据源均失败 -> A: 原因 | B: 原因")
```

**重试次数** = `settings.DATA_FETCH_MAX_RETRY`（默认 3），即每个源最多 **1 + 3 = 4 次**尝试。
只有**瞬时错误**才重试（代理抽风、连接重置、超时、SSL 抖动、代理返回拦截页导致的
`JSONDecodeError`）；**确定性错误**（缺列、接口改版、返回空）重试多少次都一样，立刻换源。

> 注意别把重试放大：`_call()` 在**经由注册表调用时传 `retry=0`**（见 `_registry_retry()`），
> 底层只调一次，重试由注册表统一负责 —— 否则 3 次会变成 3×3=9 次请求，反而招来限流。

### 权重：**谁靠谱谁先上**

排序规则一句话：**先按权重降序，权重相同才看优先级**。
所有源初始权重都是 **1**（`DEFAULT_WEIGHT`），所以不改任何配置时
顺序 **==** `DATA_SOURCE_PRIORITY` 的顺序；权重只在真的出现失败后才开始起作用。

| 事件 | 权重变化 | 参数 |
| --- | --- | --- |
| 源级失败（连不上/被拦/鉴权失败） | `weight × 0.5` | `WEIGHT_DECAY` |
| 取数成功 | `weight + 0.1`，**最多回到基准** | `WEIGHT_RECOVER` |
| 数据缺失（`DataUnavailable`：停牌/没配 token） | **不变** | — |
| 权重触底 | 不低于 `0.1` | `WEIGHT_MIN` |

两个刻意的不对称：

* **降权快、回涨慢**（0.5 vs 0.1）。一个抽风的源要连续成功好几次才配回到队首，
  否则"时好时坏"的源会反复插队，每批采集的头几个请求都撞在它身上。
* **降权 ≠ 熔断**。熔断是"冷却期内根本不试它"，降权是"冷却结束后再试，
  它也必须排在后面"。两者粒度不同，缺一个都挡不住反复撞墙。

权重会**落盘到 `data/source_weights.json`**，跨重启记忆：
昨晚不靠谱的源，今早不会又排第一。删掉这个文件即恢复默认。

```bash
# 想给人写死的初始权重（JSON），不配就是全部 1
DATA_SOURCE_WEIGHTS='{"mootdx":0.5,"astockdata":2}'
```

代码侧：

```python
REGISTRY.weights()                 # {源名: 当前权重}
REGISTRY.set_weight("mootdx", 0.5) # 手动设（同时把基准改成 0.5）
REGISTRY.reset_weight("mootdx")    # 回到基准 + 清熔断（修完代理端口后用）
REGISTRY.reset()                   # 全部恢复默认
```

面板：`GET /api/sources`（权重/熔断/最近采集事件）、`POST /api/sources/reset`。
CLI：`alphaquant sources`。

**优先级** = `settings.DATA_SOURCE_PRIORITY`（默认 `baostock,akshare`，逗号分隔）。
配置里没写、但已注册的源**自动排在后面当兜底**——所以加新源不强制改配置就能生效。

### 采集日志长什么样（排障时看这些行）

```
采集[hot_rank] 开始：可用数据源 3 个 -> akshare_em(w1) → xueqiu_hot(w1) → akshare_spot(w1)（每个源最多重试 3 次）
采集[hot_rank] · akshare_em 第1/4次失败（瞬时·可重试）: MarketXError: ... ProxyError: ... → 0.5s 后重试
采集[hot_rank] · akshare_em 尝试 4 次后仍不可用，切换下一个源 → xueqiu_hot；原因: ...
采集[hot_rank] 成功 · 数据源 **akshare_em**（第3次尝试，耗时 572ms，5 行）
```

全挂时会额外打一条 WARNING（界面也会弹出告警条）：

```
采集[hot_rank] 失败：3 个数据源已全部用尽 -> akshare_em: ... | xueqiu_hot: ... | akshare_spot: ...
```

体积控制：错误里东财/代理的 URL 动辄几百字符，日志统一用 `_brief()` 截断到 160 字符，
保证"为什么失败"不被 URL 淹没。

### 两级熔断（粒度不同，别混为一谈）

| 级别 | 位置 | 作用 |
| --- | --- | --- |
| 主源级 | `_fund_flow_akshare` 内 `_FF_PRIMARY_DEAD_UNTIL` | push2his 这一个域名被拦 → 整批剩余票直接走快照，别各自重试 3 次 |
| 源级 | `SourceRegistry` | akshare 整源（含快照）都不可用 → 熔断该源 5 分钟，交给 Tushare |

### 统一网络传输层 `app/datasource/transport/net.py`

三个源一起"被限流"时，问题往往不在源、而在**发请求的方式**。这一层把三件
本来散落在各适配器里的事收敛到一个文件：

| 能力 | 解决什么 |
| --- | --- |
| 按域名自动配 `User-Agent` / `Referer` | 东财、腾讯这类站点不带头会直接断连，报错长得极像"被限流"，极易误判 |
| 通道兜底 | 按环境代理试一次，失败自动改直连；某个域名上次靠哪条通道成功，下次优先走它 |
| **僵尸代理自动摘除** | 环境变量写了端口但没人监听时，直接把 `HTTP_PROXY` 等变量删掉。必须做在环境变量层——akshare / baostock 在自己内部发请求，不走我们的传输层，只有摘变量才对它们生效 |
| 域名健康度 | 连续失败 2 次即判"该域名暂不可达"，冷却 300s（`DATA_HOST_BLOCK_COOLDOWN`） |
| 离线开关 | `net.offline(True)`：单测里禁止一切真实联网 |

用法：

```python
from app.datasource import net

data = net.post_json(url, json_body=payload)      # 自动带头/换通道/记健康度
text = net.get_text("https://qt.gtimg.cn/q=sh600000", encoding="gbk")
```

### 源可以声明"我依赖哪些域名"

```python
REGISTRY.register("akshare_spot", supports=["hot_rank"], fetch=_hot_rank_spot,
                  hosts=["82.push2.eastmoney.com"])
```

声明后，注册表在**动手前**先问一次网络层：这些域名是不是已经判死了。
是则直接跳过这个源，并留一条可读的采集事件：

```
采集[hot_rank] · akshare_spot 依赖的域名暂不可达，直接跳过 -> 82.push2.eastmoney.com(ProxyError …，288s 后重试)
```

不这么做的话，三个源里两个共用 `push2` 时，那一轮要各等一遍超时才失败，
白耗十几秒却拿不到任何数据。

akshare 之类第三方库内部发请求不会走 `net.request`，所以**源失败时会回写
域名健康度**（`SourceRegistry._mark_hosts`），否则 `hosts` 声明只是个摆设。

## 3. 关键设计：**源坏了** ≠ **没数据**

这是最容易搞错、也最影响稳定性的一点：

| 情况 | 该抛的异常 | 后果 |
| --- | --- | --- |
| 连不上 / 被代理拦 / 鉴权失败 / 接口结构变了 | `SourceError` | **计入熔断**，该源暂时不再尝试 |
| 停牌、退市、非交易日、这只票没记录、没配 token | `DataUnavailable` | **不熔断**，跳下一个源；源本身仍是健康的 |

反例：如果停牌票也抛 `SourceError`，连续几只停牌就会把健康的源熔断掉，
后面正常的票反而全取不到。

## 4. 内置源

| 源 | 支持类型 | 说明 |
| --- | --- | --- |
| `akshare` | `fund_flow` | 主源 = `stock_individual_fund_flow`（逐日，push2his）；失败自动切全市场快照 `stock_fund_flow_individual`（push2，实测在本机代理下可用） |
| `tushare` | `fund_flow` | `moneyflow` 接口，**不依赖东财**；需 `TUSHARE_TOKEN` 环境变量 + 2000 积分；没配 token 时静默跳过（抛 `DataUnavailable`） |
| `emapp_hot` | `hot_rank` | **人气榜首选**：直连 `emappdata.eastmoney.com/stockrank/getAllCurrentList`，名称与涨跌幅用腾讯批量报价 `qt.gtimg.cn` 补齐。见下方「为什么要有它」 |
| `akshare_em` | `hot_rank` | 东财人气榜 `stock_hot_rank_em`（内部还要再请求 push2，该域名常被拦） |
| `xueqiu_hot` | `hot_rank` | 雪球关注热度 `stock_hot_follow_xq`——**与东财不同域名**，东财被代理拦时它可能是唯一能通的源 |
| `akshare_spot` | `hot_rank` | 倒数第二兜底：全市场快照 `stock_zh_a_spot_em` 按**成交额**降序。严格说不是人气榜，但成交额前列 ≈ 关注度前列，用于保住候选池不为空（日志会标注降级） |
| `local_hot` | `hot_rank` | **本地兜底（`fallback=True`，恒排最后）**：外网全挂时用库里已有数据出榜——先用最近一天入库的人气榜，没有则用最近交易日日线按成交额合成。见下节 |
| `akshare_flow` | `flow_rank` | 东财全市场资金流排名 `stock_fund_flow_individual`（即时/3日/5日/10日） |

### 为什么 mootdx / a-stock-data **不在** `hot_rank` 里

这是被问到过的问题，值得单独写清楚：它们**没有**人气榜数据，
不是"忘了注册"。

- `mootdx` = 通达信行情协议，提供 K线 / 分时 / 财务数据；
- `a-stock-data` = 行情层（K线）+ 资金流；

两者都没有"人气 / 热度 / 关注人数"这类舆情字段。人气榜只有东财、雪球这类
**带社区属性**的站点才有。所以它们注册在 `daily`（以及 a-stock-data 的资金流），
`hot_rank` 里看不到它们是正常的。

`hot_rank` 的真实困境是：**它只有外网源**。东财 push2 / 雪球全挂时，
候选池直接 0 条 —— 于是加了 `local_hot` 兜底。

### `emapp_hot`：人气榜为什么必须换一个源

**问题**：`akshare.stock_hot_rank_em` 是**两段式**的——

1. POST `emappdata.eastmoney.com/stockrank/getAllCurrentList` → 拿到代码 + 名次；
2. GET `push2.eastmoney.com/api/qt/ulist.np/get` → 补名称 + 涨跌幅。

第 1 段一直是通的，**第 2 段（push2）被代理拦死**，于是整个函数抛异常——
表现就是"数据就在门口，却报所有数据源均失败"。`akshare_spot` 同样压在
`82.push2` 上，等于三个源里有两条共用同一条死路。

**解法**（`app/datasource/hot_sources.py`）：只用第 1 段，名称和涨跌幅改用腾讯
批量报价 `qt.gtimg.cn`（一次请求可带 60 个代码）补齐。实测：

```
采集[hot_rank] 成功 · 数据源 **emapp_hot**（第1次尝试，耗时 283ms，10 行）
```

腾讯报价补不全时只降级（名称留空、涨跌幅留空），**不丢已经拿到的榜单**。

### 本地兜底源 `local_hot`

外网三源全挂时用它顶上，两级降级：

1. 库里存过人气榜 → 用最近一天那份（真实榜单，只是可能不是今天）；
2. 连人气榜都没有 → 用**最近交易日日线**按成交额合成
   （成交额前列 ≈ 关注度前列；停牌 / 零成交的票会被剔除）。
3. 两级都没有 → `DataUnavailable`（这是"没数据"，**不会降权**）。

它标了 `fallback=True`：**恒排在所有普通源之后，不参与权重竞争**。
这点很关键——本地源几乎不会失败，若按权重排，几次成功就会涨回队首、
抢在东财前面，于是变成"明明有网却一直用本地旧数据"，比拿不到数据更难发现。

落到兜底时日志会明确标注，同步结果里也会带 `source=local_hot`：

```
采集[hot_rank] 成功 · 数据源 **local_hot**（第1次尝试，耗时 60ms，3 行）
WARNING  人气榜由本地兜底源 local_hot 提供（外网源均不可用），内容可能是历史榜单或按成交额合成的近似排名
```

### 日线（K线）源：`daily`

日线原本走 `DataSourceFactory` 那条老链路，和注册表是两套机制 ——
结果是新注册的源对 K线根本不起作用。现在 K线也接进注册表了，
见 `app/datasource/kline_sources.py`（老链路保留不动，两条并存）。

| 源 | 说明 | 依赖 |
| --- | --- | --- |
| `baostock_daily` | Baostock 日线，稳定、无需 token，一贯首选 | baostock（已有） |
| `akshare_daily` | 东财 `stock_zh_a_hist`，数据全但与东财共用风控面 | akshare（已有） |
| `astockdata` | **a-stock-data** 的行情层主源：腾讯财经 K线（HTTP、零鉴权、零依赖） | 无（标准库 urllib） |
| `mootdx` | **mootdx / 通达信**：本地 `.day` 文件（配了 `MOOTDX_TDX_DIR`）优先，否则走线上 TCP 行情 | `pip install mootdx`（可选） |

统一入口：

```python
from app.datasource.compat.kline_sources import fetch_daily, daily_sources

df = fetch_daily("600000", start="2026-09-01", end="2026-10-10")
print(daily_sources())      # ['baostock_daily', 'akshare_daily', 'astockdata', 'mootdx']
```

#### a-stock-data 到底是什么（接之前先看懂这个）

https://github.com/simonlin1212/a-stock-data —— 它**不是一个 pip 包**，
而是一份自包含的 `SKILL.md`（结构化 Markdown + 内嵌 Python）,
把 34 个数据源 / 87 个端点的调用细节（腾讯 K线的分段参数、通达信盘后包的
二进制格式、东财的 Referer 头……）整理成可直接用的说明书。

所以本项目按它给出的端点实现适配器，而不是安装它。目前实现的是**行情层主源**
（腾讯财经 K线），理由：零鉴权、零依赖、和东财不同风控面。

已知边界（照其 V3.10.1 的说明）：

* 腾讯 K线**不返回成交额** → `amount` 列留空（不编数字）；
* 成交量单位是「手」，但**科创板（688/689）是股**。

要接它的其它层（资金流 / 研报 / 龙虎榜 / 公告 …）完全同构：
按 SKILL.md 对应章节写适配器 + 一行 register，见下面「新增一个数据源：三步」。

#### mootdx 的两条路径

1. **本地**（配了 `MOOTDX_TDX_DIR`，如 `C:/new_tdx`）：`Reader.daily()` 直接读
   通达信 `.day` 二进制文件 —— 不联网、不限流、断网也能用，**优先走它**；
2. **线上**：`Quotes.factory(market="std").bars(symbol, frequency=9)`，
   TCP 直连通达信行情服务器（需国内 IP，海外环境会超时）。

安装：`pip install mootdx`。注意它上游锁了 `httpx<0.26`，与其它依赖冲突时
可 `pip install --no-deps mootdx` 后手动装回高版本 httpx
（mootdx 取行情走 TCP，运行时不经过 httpx）。

### 成交量单位：一个别踩的坑

**各源单位不一致，本层刻意不做换算**：

| 源 | 成交量单位 |
| --- | --- |
| baostock、通达信(mootdx) | 股 |
| akshare（东财）、腾讯(a-stock-data) | 手 |

换算需要确知每个源的口径，猜错就是 100 倍的错；所以原样透传，
并在这里标注。数据完整性校验**只比对日期与价格，不比对成交量**——
那是唯一安全的做法（见 `docs/DATA_INTEGRITY.md`）。

---

想调整顺序，把源名写进 `DATA_SOURCE_PRIORITY` 即可；没写进去的按注册顺序排后面。
比如想让雪球优先：`DATA_SOURCE_PRIORITY=xueqiu_hot,akshare,tushare`。

---

## 4.5 采集去重：库里已有的不重复采

同一交易日的数据拉第二次拿到的还是同一份，白白消耗请求额度还容易被限流。
所以所有 sync 都先看库：

| 数据 | 去重依据 | 代码位置 |
| --- | --- | --- |
| 人气榜 / 资金榜 / 涨停池 | 该交易日 `pool_count(kind, day) > 0` 就跳过 | `sync_hot_rank` / `sync_flow_rank` / `sync_limitup` |
| 板块（概念/行业） | 该交易日已有 `themes` 记录 | `sync_boards` |
| 个股资金流 / 筹码 | 该股最近入库日期 ≥ 目标交易日就跳过（一次 group_by 批量查） | `sync_fund_flow` / `sync_chip` |
| 名称对照表 | `stock_info_count() > 0` 就跳过 | `refresh()` 内 |

跳过的数量会写进同步结果（`skipped` / `reason`），并汇总到刷新报告的
`report["skipped"]`，界面上显示为"跳过重复采集: …"。

**需要强制重采**时：

```python
sync_hot_rank("20261009", force=True)      # 代码
refresh_pool(..., force=True)              # HTTP API
```

面板上也有「强制重采」勾选框（默认不勾）。

另外：指定交易日没采过时，会**先复用库里最近一天已采到的榜单**
（`R.latest_pool`），复用不上才联网 —— 界面提示形如"人气榜 100 只（复用 2026-10-08）"。

---

## 5. 新增一个数据源：三步

### 第 1 步：写适配器（一个函数）

契约：

- **入参**：用关键字参数，名字与 `REGISTRY.fetch(kind, **kwargs)` 传的一致
  （如 `symbol`、`days`）。⚠️ **参数不能叫 `kind`** —— `fetch()` 的第一个位置参数
  已经叫 `kind`，撞名会报 `got multiple values for argument 'kind'`
  （注册表会直接抛一条带说明的 `TypeError` 提示你改名，如 `rank_kind`）；
- **返回**：`pandas.DataFrame`，列要和目标数据类型的既有结构一致
  （资金流见下方表）；空表视为失败；
- **异常**：源坏了抛 `SourceError`，没数据抛 `DataUnavailable`。

资金流的返回列（与 `fetch_fund_flow` 现有结构一致）：

```
date, close, pct_change, main_net, main_net_pct,
super_large_net, large_net, medium_net, small_net, turnover, amount
```

日线（`daily`）的返回列固定为 `app.datasource.compat.kline_base.DAILY_COLUMNS`：

```
date, symbol, open, high, low, close, volume, amount
```

适配器不用自己拼这些列 —— 把源返回的原始表丢给
`normalize_daily(raw, symbol, start, end)` 即可（列名映射、类型转换、
区间裁剪、去重排序都在里面做）。

### 第 2 步：注册

注册时可以给权重（**默认 1**）：

```python
REGISTRY.register("my_src", supports=["daily"], fetch=_my_fetch, weight=0.8)
```

不给就是 1，顺序完全交给 `DATA_SOURCE_PRIORITY` + 运行时健康度。

推荐放在**你自己的模块**里（不污染 `marketx.py`），例如 `app/datasource/my_sources.py`：

```python
# app/datasource/my_sources.py
from __future__ import annotations

import os
from typing import Optional

import pandas as pd

from app.datasource.compat.registry import REGISTRY, DataUnavailable, SourceError


@REGISTRY.register("csv_local", supports=["fund_flow"])
def _fund_flow_csv(symbol: str, days: Optional[int] = None) -> pd.DataFrame:
    """本地 CSV 兜底源（零依赖示例）。

    约定：data/fund_flow/<symbol>.csv，列 = date,main_net,amount
    """
    code = "".join(ch for ch in str(symbol) if ch.isdigit()).zfill(6)
    path = os.path.join("data", "fund_flow", f"{code}.csv")
    if not os.path.exists(path):
        raise DataUnavailable(f"本地无 {code} 的资金流文件")

    try:
        df = pd.read_csv(path)
    except Exception as exc:
        raise SourceError(f"读取 {path} 失败: {type(exc).__name__}: {exc}") from exc

    if df.empty:
        raise DataUnavailable(f"{path} 为空")

    df["symbol"] = code
    # main_net_pct 缺失时用"净额 / 成交额"补，与内置源同款兜底
    if "main_net_pct" not in df.columns and "amount" in df.columns:
        df["main_net_pct"] = df["main_net"] / df["amount"]
    df = df.sort_values("date")
    return df.tail(int(days)).reset_index(drop=True) if days else df
```

注册装饰器也支持非装饰器写法：

```python
REGISTRY.register("csv_local", supports=["fund_flow"], fetch=_fund_flow_csv)
```

一个函数可以注册多种能力（一个源能取多类数据）：

```python
REGISTRY.register("my_src", supports=["fund_flow", "chip"], fetch=_my_fetch)
```

### 第 3 步：让它被引擎加载 + 配置优先级

**注册发生在 import 时**，所以关键是让你的模块被引擎导入。引擎本来就
`import app.datasource.service.facade`，因此在 `marketx.py` 末尾加一行即可：

```python
# app/datasource/service/facade.py 末尾
import app.datasource.my_sources  # noqa: F401  触发注册
```

> 放在文件末尾可避开循环导入。

可选：把它写进优先级（不写也会作为兜底排在最后）：

```bash
# .env
DATA_SOURCE_PRIORITY=baostock,akshare,csv_local,tushare
```

**不需要改引擎任何代码**——`fetch_fund_flow` 走注册表，新源自动参与切换。

---

## 6. 注册到引擎后怎么验证

```python
from app.datasource.compat.registry import REGISTRY

# 1) 源是否已注册、支持哪些类型
print([(s["name"], s["supports"]) for s in REGISTRY.health()])

# 2) 该类型的尝试顺序（权重降序 + 优先级）
print([(s.name, s.weight) for s in REGISTRY.sources_for("fund_flow")])
print([(s.name, s.weight) for s in REGISTRY.sources_for("daily")])

# 3) 每个源的健康状态（是否熔断、连续失败次数、最后错误）
import json; print(json.dumps(REGISTRY.health(), ensure_ascii=False, indent=2))

# 4) 手动恢复（清掉所有熔断）
REGISTRY.reset()

# 5) 这批数据到底来自哪个源？（成功条目）
print(REGISTRY.used_sources())       # {"hot_rank": "akshare_em", ...}

# 6) 最近 20 次采集明细：用了哪个源、试了几次、耗时、几行、失败原因
for e in REGISTRY.recent_events(20):
    print(e)
```

单测里建议用独立注册表，避免污染全局（也避免把真实权重文件写脏）：

```python
reg = SourceRegistry(circuit_threshold=2, cooldown=60.0, persist_path=None)
```

## 7. 把其它数据类型也接入注册表

目前已走注册表的类型：`fund_flow`、`hot_rank`、`flow_rank`、`daily`。
其它类型（`chip` / `limitup` / `themes` …）迁移方式完全一致，两步：

1. 把现有取数函数包成适配器，用 `REGISTRY.register(..., supports=["chip"])` 注册；
2. 把 `fetch_chip()` 改成 `REGISTRY.fetch("chip", symbol=symbol)`。

之后这些类型同样获得"自动换源 + 熔断 + 失败原因可追溯"的能力。

## 8. 排障

### 看到 `xxx: 所有数据源均失败`

先跑**探针**，一眼分清是"某个源挂了"还是"这台机器出不去网"：

```bat
alphaquant sources --probe --kind hot_rank
alphaquant sources --probe --kind daily
```

```text
数据源                   结果      耗时  说明
emapp_hot             可用   255ms  50 行
akshare_em           不可用   336ms  MarketXError: ... ProxyError: HTTPSConnectionPool(host='push2.eastmoney.com', port=443)...
xueqiu_hot           不可用   353ms  MarketXError: ... JSONDecodeError: Expecting value: line 1 column 1 (char 0)
akshare_spot         不可用  4669ms  MarketXError: ... ProxyError: HTTPSConnectionPool(host='82.push2...
local_hot             可用   490ms  3 行
3/5 个源不可用，其余可用（会自动换源，不影响取数）

依赖域名（网络层实测）
  emappdata.eastmoney.com        正常
  qt.gtimg.cn                    正常
  push2.eastmoney.com            不可达(283s)   ProxyError ...
  82.push2.eastmoney.com         不可达(291s)   ProxyError ...
  ⚠️ 代理端口无程序监听（僵尸代理），请求会全部超时
```

末尾那张**域名表**比源列表更能说明问题：多个源常常共用同一域名，
`push2` 一挂，三个人气榜源一起挂——只看源列表会误判成"数据源都不行"。

三种结论：

| 现象 | 结论 | 怎么办 |
| --- | --- | --- |
| 只有一个源不可用 | 该源的问题 | 不用管，权重会把它排到后面 |
| **全部**都是连接类错误（ProxyError / Timeout / JSONDecodeError） | **本机网络/代理**出问题 | 见下节查代理端口 |
| 全部 `DataUnavailable` | 源都通，只是此刻没数据 | 非交易日 / 停牌，正常 |

异常信息里现在也会直接附上排查提示（含当前代理环境变量的实际值）：

```
hot_rank: 所有数据源均失败 -> akshare_em: ... | xueqiu_hot: ... ||
  3 个源中 2 个是网络层失败（连接被重置/超时/非JSON响应）；
  排查：当前代理环境变量 HTTP_PROXY=http://127.0.0.1:59258 —— 端口若无程序
  监听即为僵尸代理，改指向可用端口或临时取消；验证命令：alphaquant sources --probe
```

### 看到 `候选池来源 hot 获取失败: ...`

先看日志里 `采集[hot_rank]` 那几行，它们已经写清了：试了哪些源、每个源试了几次、
最后是哪个源给的（或全挂）。常见结论：

- 三个源都是 `ProxyError / HTTPSConnectionPool(...)` → 代理把东财 + 雪球都拦了
  （见下一节排查端口）；
- `xueqiu_hot` 报 `JSONDecodeError` → 雪球返回了非 JSON（反爬页/代理拦截页），
  已按瞬时错误重试，仍失败会换下一个源；
- 出现 `熔断中(剩XXXs)` → 该源连续失败已触发冷却，等 5 分钟或 `REGISTRY.reset()`；
- 全挂 → 界面股票池上方会显示红色告警条，内容就是各源的原因；
  现在还会自动落到 `local_hot`（本地兜底），所以**列表不会是空的**，
  但内容是历史榜单或按成交额合成的近似排名，日志里会明确标注。

### 看到 `资金流同步有 N 只失败`

现在告警会带**原因样例**，请先区分两类：

- `ProxyError / HTTPSConnectionPool(...) / Max retries` → **代理或网络问题**，
  不是限流（限流会是 429）。检查 `HTTP_PROXY` / `HTTPS_PROXY` 指向的端口是否真的在监听：

  ```bat
  netstat -ano | findstr :59258
  ```

  若**没有任何 LISTENING**，说明环境变量指向了僵尸端口（本机就踩过：
  `HTTP_PROXY=127.0.0.1:59258` 无人监听，而真实代理在 `7890`）。
  修法：把环境变量改成实际端口，或 `set HTTP_PROXY=` 取消代理。

- `返回空 / 未找到 / 停牌` → 数据本身没有，属正常降级。

### 某个源一直不生效

按顺序查：

1. `REGISTRY.health()` 里有没有它（没有 = 模块没被 import）；
2. `supports` 里有没有目标类型（写错类型名会直接跳过）；
3. `enabled` 是否为 `True`；
4. `circuit_remaining > 0` 说明正被熔断，等冷却或 `REGISTRY.reset()`；
5. `weight` 明显低于其它源 → 它之前失败过被降权了，
   确认环境修好后 `REGISTRY.reset_weight()` 让它重新公平竞争。

### 权重文件被测试写脏了

`data/source_weights.json` 是运行时产物。跑测试不会动它
（`app/tests/conftest.py` 会关掉全局注册表的落盘），
但如果你本地调试时手动造过大量失败，直接删掉这个文件即可恢复默认。

## 9. 相关文件

| 文件 | 职责 |
| --- | --- |
| `app/datasource/transport/net.py` | **统一网络传输层**：请求头、通道兜底、僵尸代理摘除、域名健康度 |
| `app/datasource/compat/registry.py` | 注册表（含权重）、`SourceError` / `DataUnavailable`、`is_transient`、采集事件、域名级跳过 |
| `app/datasource/service/facade.py` | 内置适配器（akshare / tushare / 人气榜源 / 资金榜）+ 注册入口 |
| `app/datasource/hot_sources.py` | `emapp_hot`：不依赖 push2 的可用人气榜源 + 腾讯批量报价 |
| `app/datasource/kline_base.py` | 日线统一契约：列名、别名映射、归一化（含成交量单位的坑） |
| `app/datasource/kline_sources.py` | `daily` 源注册入口 + `fetch_daily()` 统一入口 |
| `app/datasource/mootdx_source.py` | mootdx（通达信）日线适配器 |
| `app/datasource/astockdata_source.py` | a-stock-data 行情层（腾讯 K线）适配器 |
| `app/datasource/local_source.py` | 本地兜底人气榜（`local_hot`，外网全挂时顶上） |
| `app/engine/integrity.py` | 数据完整性校验 + 缺口自动补全（见 `docs/DATA_INTEGRITY.md`） |
| `app/engine/component/dataguard.py` | 数据校验服务组件（引擎启动后定期体检） |
| `app/alpha/pool/sync.py` | 各 sync 的采集去重（`force` 开关）与失败日志 |
| `app/alpha/pool/pool.py` | 候选池读库复用 + 刷新报告里的 `warnings` / `data_sources` |
| `app/common/config.py` | `DATA_SOURCE_PRIORITY`、`DATA_SOURCE_WEIGHTS`、`DATA_FETCH_MAX_RETRY`、`DATA_FETCH_MIN_INTERVAL`、`DATA_GUARD_*` |
| `app/tests/test_marketx_retry_sources.py` | 重试、换源、熔断、注册表的回归测试 |
| `app/tests/test_source_retry_and_dedup.py` | 重试 N 次后换源、全挂告警、已采集不重复采 |
| `app/tests/test_source_weights.py` | 权重：初值 1、降权、恢复、排序、落盘 |
| `app/tests/test_kline_sources.py` | mootdx / a-stock-data 适配器与 daily 链路 |
| `app/tests/test_data_integrity.py` | 缺口检出、坏数据、自动补全、采集服务的完整性单元 |
| `app/tests/test_component_progress.py` | 组件任务进度（已完成/全部）、本地兜底源、`--probe` 诊断 |
| `app/tests/test_net_transport.py` | 传输层：请求头、通道兜底、僵尸代理摘除、域名熔断、`emapp_hot` 解析 |
