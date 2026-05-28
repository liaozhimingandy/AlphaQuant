# 架构
Market Data（行情）
    ↓
Indicator（指标）
    ↓
Signal（信号）
    ↓
Factor（因子）
    ↓
Rule（规则）
    ↓
Strategy（策略）
    ↓
Risk（风控）
    ↓
Portfolio（仓位）
    ↓
Execution（执行）
    ↓
Broker（券商）


# 框架
```
                    ┌─────────────────────┐
                    │     Engine          │
                    │   (核心调度引擎)     │
                    └─────────┬───────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  MarketCenter         EventBus              TaskScheduler
  行情中心               事件总线                定时调度器
        │                     │                     │
        ▼                     ▼                     ▼
  DataSource           StrategyManager        CronJobs
  数据源                策略管理器              定时任务
                              │
                              ▼
                        Strategy
                              │
                              ▼
                          Signal
                              │
                              ▼
                        RiskManager
                              │
                              ▼
                            Sizer
                              │
                              ▼
                          Portfolio
                              │
                              ▼
                          Execution
                              │
                              ▼
                        BrokerAdapter
```