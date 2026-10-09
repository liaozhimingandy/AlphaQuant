# AlphaQuant

A 股量化交易工具：行情采集 → 数据入库 → 策略回测 → **多任务事件驱动交易引擎**。

## 快速开始

所有能力都从统一 CLI 进入：

```bash
# 查看全部命令
python main.py --help

# 1. 初始化数据库（首次使用）
python main.py init-db

# 2. 采集日线数据（幂等，可重复执行）
python main.py collect -s 000001 --start 2020-01-01 --end 2026-06-05
python main.py collect -s 000001 -s 600000 --save-csv   # 多标的 + 导出 CSV

# 3. 看库里有什么
python main.py list

# 4. 跑回测（backtrader 链路）
python main.py backtest -s 000001 --start 2023-01-01 --end 2026-06-05
python main.py backtest -s 000001 --strategy precise_ma_cross --cash 200000
python main.py backtest -s 000001 --strategy ma_cross --param fast=5 --param slow=20 --export

# 5. 启动后台交易服务（一个引擎跑多个任务）
python main.py serve                          # 常驻：读 config/tasks.json
python main.py serve --mode BACKTEST --interval 0.01   # 离线回放，跑完自动停
python main.py tasks                          # 查看已配置的任务
```

可用能力清单：`python main.py strategies` / `factors` / `rules`

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
  cli.py            统一命令行入口（含 serve 后台服务）
  backtest/         回测闭环：config / runner / result / report / registry / analyzers
  data/             数据源与采集：datasource / service / collector
  repository/       数据仓储（幂等 upsert、查询、覆盖度）
  db/               SQLAlchemy 模型与会话
  core/
    config.py       全局配置（路径/数据库/日志/行情/新闻/大模型，支持环境变量覆盖）
    market/         领域模型 Bar/Tick/Signal/Order/Position/Account/News + BarSeries
    indicator/      指标层（可插拔注册表）
    factor/         因子层（自定义因子的接入点）
    rule/           规则层（All/Any/Not 组合器 + 声明式构造）
    strategy/       策略层（双通道：行情 + 事件）
    risk/           风控层（闸门链）
    portfolio/      组合层（账本 + 仓位计算）
    execution/      执行层（模拟撮合）
    task/           QuantTask + TaskRuntime（多任务隔离）
    news/           新闻层（RSS 采集 → 去重 → 关键词/大模型分析）
    engine/         Twisted 引擎 + 业务组件 + 装配器
  strategy/         backtrader 策略（单次回测链路）
  utils/logger.py   全局日志
config/tasks.json   多任务配置（改这里就能增删策略）
scripts/            集成演示脚本
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

## 测试

```bash
python -m unittest discover -s app/test -p "test_*.py"
```

测试全部使用合成数据或临时数据库，不依赖网络，也不污染真实数据库。

## 已知约定

- A 股一手 100 股，回测默认禁止裸做空（`set_shortcash(False)` + `set_checksubmit(True)`）
- 回测报告会输出「最小持仓」，多头策略该值应恒为 0；出现负数说明有僵尸止损单等缺陷
- 夏普比率按日收益年化计算（默认无风险利率 0，可用 `risk_free_rate` 调整）
- 实时链路（`serve`）默认**不做挂单**，收到即成交 —— 从根上避免撤不掉的僵尸单
- 风控是「闸门」：只否决/缩仓，不下单。强制平仓由 `QuantTask` 的持股超时检查负责
- 新闻分析拿不到结论时返回 `confidence=0`，由 `news_confidence` 风控拦掉，不静默丢弃
