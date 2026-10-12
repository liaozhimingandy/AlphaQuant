# 配置参考

所有配置集中在 `app/common/config.py` 的 `Settings`（pydantic-settings），
通过环境变量或 `.env` 覆盖。本文按分组列出**常用项**，
完整清单直接看源文件（分组注释即目录）。

---

## 1. 路径与产物目录

| 配置项 | 默认 | 说明 |
|---|---|---|
| `BASE_DIR` | 仓库根 | 其它路径的基准 |
| `DATA_DIR` | `<root>/data` | SQLite 与 CSV |
| `LOG_DIR` | `<root>/logs` | 日志 |
| `OUTPUT_DIR` | `<root>/output` | 快照 / 运行时 / 多服务状态 / 文件总线 |
| `SNAPSHOT_DIR` | `<root>/output/snapshots` | 每个 run 一个子目录 |
| `RUNTIME_DIR` | `<root>/output/runtime` | 运行期增删的任务，重启可重放 |
| `SERVICES_DIR` | `<root>/output/services` | 各服务心跳/命令/状态 |
| `BUS_DIR` | `<root>/output/bus` | 多服务文件消息总线 |
| `CONFIG_DIR` | `<root>/config` | tasks.json / factors.json 等 |

---

## 2. 数据库

| 配置项 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/alphaquant.db` | 已开 WAL、`busy_timeout=30s`（`storage/engine.py:36-41`），适配 Twisted 线程池并发写 |

---

## 3. 日志

| 配置项 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 控制台级别 |
| `LOG_FILE_LEVEL` | 空（跟随 `LOG_LEVEL`） | 文件级别，可单独调低 |
| `LOG_ROTATION` | `00:00` | 每天轮转 |
| `LOG_RETENTION` | `30 days` | 保留期 |
| `LOG_THROTTLE_WINDOW` | `5.0` | 同消息节流窗口（秒），防止刷屏 |
| `LOG_TRACE_KEEP` | `0` | ⚠️ **当前未接线**，预留 |

---

## 4. 数据源

| 配置项 | 默认 | 说明 |
|---|---|---|
| `DATA_SOURCE_PRIORITY` | `baostock,akshare` | 逗号分隔，决定候选顺序 |
| `DATA_FETCH_MAX_RETRY` | `3` | 单源重试次数 |
| `DATA_FETCH_MIN_INTERVAL` | `0.3` | 同类接口最小间隔（秒）；**会自适应**：瞬时失败翻倍，成功回落（封顶 5s） |
| `DATA_SOURCE_WEIGHTS` | 空 | `name=weight` 形式，不配就是全 1 |
| `PROVIDERS_CONFIG` | `config/providers.json` | 新采集层编排配置 |

---

## 5. 回测默认值

| 配置项 | 默认 | 说明 |
|---|---|---|
| `DEFAULT_CASH` | `100000.0` | 初始资金 |
| `DEFAULT_COMMISSION` | `0.0003` | 佣金率（**双边**，万三） |
| `DEFAULT_STAMP_DUTY` | `0.0005` | 印花税率（**仅卖出**，万五） |
| `MIN_COMMISSION` | `5.0` | 单笔最低佣金（元） |
| `TRANSFER_FEE` | `0.00001` | 过户费率（双边） |
| `DEFAULT_SLIPPAGE_PERC` | `0.001` | 滑点比例 |

> **成本模型**：回测用 `AStockCommissionInfo`（`research/commission.py`）按方向计费
> —— 买入只出佣金+过户费，卖出额外加印花税。这一条很关键：印花税比佣金还贵，
> 漏掉会让回测系统性高估收益，换手越高偏差越大。

---

## 6. 引擎

| 配置项 | 默认 | 说明 |
|---|---|---|
| `BACKTEST_IDLE_TIMEOUT` | `5.0` | 回测模式持续空闲多少秒后自动停 |
| `BACKTEST_AUTO_STOP` | `True` | 回测自动停机总开关 |
| `SCHEDULER_MAX_CONCURRENT` | `5` | 调度并发上限（回测模式强制串行） |
| `SHUTDOWN_TASK_WAIT` | `30` | 关停时最多等在跑任务多久（秒） |

---

## 7. 行情

| 配置项 | 默认 | 说明 |
|---|---|---|
| `MARKET_MODE` | `replay` | `replay`=历史回放 / `poll`=实时轮询 |
| `MARKET_INTERVAL` | `0.05` | 推送节拍（秒） |
| `MARKET_SYMBOLS` | 空 | 逗号分隔 |

> **poll 模式的交易日闸门**：轮询只在交易时段推进（`market.py:264`），
> 并且同一标的同一日期的 bar 只推一次（`_last_bar_date` 去重）。
> 否则盘后数据定稿后，每个周期都会把同一根 bar 再推给策略，
> 造成重复信号 / 模拟重复成交 / 实盘重复报单。

---

## 8. 行情采集服务（常驻）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `COLLECTOR_ENABLED` | `True` | 总开关 |
| `COLLECTOR_INTERVAL` | `5m` | K 线采集频率，**间隔即频率**，不套下限 |
| `COLLECTOR_PERIOD` | `1d` | K 线周期 |
| `COLLECTOR_MIN_INTERVAL` | `15.0` | 保护性下限 |
| `COLLECTOR_SYMBOLS` | 空 | 逗号分隔 |
| `COLLECTOR_CONFIG` | `config/collector.json` | 采集规格 |
| `EXT_DATA_INTERVAL` | `600.0` | 日频截面（个股）采集间隔（秒），内部兜 `max(30, ...)` |
| `EXT_DATA_DAILY_INTERVAL` | `3600.0` | 日频截面（市场级）间隔，内部兜 `max(300, ...)` |

---

## 9. 数据校验服务（体检）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `DATA_GUARD_ENABLED` | `True` | 总开关 |
| `DATA_GUARD_INTERVAL` | `3600.0` | 周期（秒） |
| `DATA_GUARD_START_DELAY` | `20.0` | **首检延后**（秒）。给 K 线留入库时间，否则会把"还没采到"误判成"数据缺口" |
| `DATA_GUARD_LOOKBACK_DAYS` | `120` | 回溯天数 |
| `DATA_GUARD_AUTO_REPAIR` | `True` | 自动补全 |
| `DATA_GUARD_MAX_SYMBOLS` | `50` | 单次最多扫多少只 |
| `DATA_GUARD_MAX_REPAIR` | `20` | 单次最多补多少 |
| `DATA_GUARD_CHECKS` | `daily,fund_flow,chip` | 检查项 |

---

## 10. 新闻

| 配置项 | 默认 | 说明 |
|---|---|---|
| `NEWS_ENABLED` | `True` | 总开关 |
| `NEWS_INTERVAL` | `60.0` | 采集间隔（秒） |
| `NEWS_SOURCES` | 空 | RSS 地址，逗号分隔 |
| `NEWS_ANALYZER` | `keyword` | `keyword` / `llm` |

---

## 11. 大模型（可选）

没配 key 就自动降级到规则版，不会报错。

| 配置项 | 默认 |
|---|---|
| `LLM_API_BASE` | `https://api.openai.com/v1` |
| `LLM_API_KEY` | 空 |
| `LLM_MODEL` | `gpt-4o-mini` |
| `LLM_TIMEOUT` | `20.0` |
| `LLM_MIN_CONFIDENCE` | `0.5` ⚠️ **当前未接线** |

---

## 12. 实盘

| 配置项 | 默认 | 说明 |
|---|---|---|
| `EXECUTION_MODE` | `simulated` | ⚠️ **当前未接线**（实际由运行模式 `RunMode.LIVE` 决定） |
| `BROKER_GATEWAY` | 空 | 默认网关 |
| `BROKER_ENDPOINT` | 空 | 默认接入点 |
| `LIVE_MAX_ORDER_VALUE` | `0.0` | 单笔上限，0=不限 |
| `LIVE_MAX_DAILY_TRADES` | `0` | ⚠️ **当前未接线**（每日笔数上限走策略配置） |
| `LIVE_ORDER_TIMEOUT` | `30.0` | 订单超时（秒） |
| `LIVE_STRICT_RECONCILE` | `True` | 严格对账 |
| `LIVE_PRICE_LIMIT_PCT` | `0.02` | 价格笼子 |

---

## 13. 交易落盘

| 配置项 | 默认 | 说明 |
|---|---|---|
| `TRADE_PERSIST_ENABLED` | `True` | 订单/成交/权益入库 |
| `EQUITY_SAMPLE_INTERVAL` | `30.0` | 权益采样间隔（秒） |
| `TRADE_FLUSH_BATCH` | `50` | 攒够多少条刷一次 |
| `TRADE_FLUSH_INTERVAL` | `2.0` | 或多久刷一次 |

---

## 14. 组合服务

| 配置项 | 默认 | 说明 |
|---|---|---|
| `PORTFOLIO_BUDGET` | `None` | 跨任务总预算；不配就取聚合可用现金 |
| `PORTFOLIO_RESERVE_TTL` | `180.0` | 额度预留过期时间（秒），防在途预留泄漏 |

---

## 15. 多服务协作 / 守护

| 配置项 | 默认 |
|---|---|
| `SERVICES_CONFIG` | `config/services.json` |
| `HUB_HOST` / `HUB_PORT` | `127.0.0.1` / `8899` |
| `SERVICE_HEARTBEAT_INTERVAL` | `3.0` |
| `SERVICE_HEARTBEAT_TIMEOUT` | `15.0` |
| `PID_FILE` / `STOP_FILE` / `DAEMON_LOG` | `<logs>/alphaquant.pid` 等 |
| `DAEMON_STOP_TIMEOUT` | `20.0` |

---

## 16. 其它

| 配置项 | 默认 | 说明 |
|---|---|---|
| `TASK_CONFIG` | `config/tasks.json` | 任务定义 |
| `WORKER_ENABLED` / `WORKER_CAPACITY` | `True` / `2` | 一次性作业并发 |
| `MONITOR_ENABLED` | `True` | 面板开关 |
| `MONITOR_HOST` / `MONITOR_PORT` | `127.0.0.1` / `8787` | |
| `MONITOR_SNAPSHOT_MODE` | `on_event` | `on_event`=有操作才写 / 定时 |
| `CLEAN_MAX_MOVE_PCT` | `0.21` | 单日涨幅超过此值判为异常 |
| `CLEAN_ACTION` | `mark` | `mark`=标记 / `drop`=丢弃 |
| `USER_STRATEGY_DIR` / `USER_STRATEGY_AUTOLOAD` | `strategies` / `True` | 用户策略目录 |
| `BACKTEST_DEFAULTS_CONFIG` | `config/backtest_defaults.json` | 回测默认参数单一来源 |

---

## 17. 标为「未接线」的项怎么来的

扫描全项目引用后确认：这些配置项**只出现在 `config.py` 的定义处**，
没有任何业务代码读取它们。保留的原因是它们提示了"这里本该有功能"
（印花税、执行模式、每日交易上限、LLM 置信度阈值、日志保留份数），
直接删掉会让意图丢失。

如果要用，接线位置通常是：

| 配置项 | 建议接线位置 |
|---|---|
| `DEFAULT_STAMP_DUTY` / `MIN_COMMISSION` / `TRANSFER_FEE` | ✅ 已接（`research/commission.py`） |
| `LIVE_MAX_DAILY_TRADES` | `app/risk/builtin.py` 加一条 `DailyTradeLimitRisk` |
| `EXECUTION_MODE` | `app/engine/builder.py` 决定网关是否启用 |
| `LLM_MIN_CONFIDENCE` | `app/news/` 的 LLM 分析器判定处 |
| `LOG_TRACE_KEEP` | `app/common/logging.py` 的 sink 配置 |
