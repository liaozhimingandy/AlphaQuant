#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaQuant —— A 股量化交易系统。

## 怎么读这份代码

按数据流自底向上看，每一层**只依赖它下面的层**：

```
 L0  common           配置 / 日志 / JSON，零业务依赖
      ↓
 L1  domain           领域模型：Bar/Order/Signal/Position + 交易日历 + 代码标准化
      ↓
 L2  datasource       DataSource → Provider → Capability → Adapter → Router → DataService
      │                 └ model(Canonical) / adapters / routing / transport / raw / service
      ↓
 L3  storage          ORM schema + Repository（业务层只见 Repository，不见 Session）
      ↓
 L4  feature          指标(向量) → 因子(标量) → 挖掘；三者层层降维，不是重复实现
      ↓
 L5  alpha            多维度加权打分 + 候选池
      ↓
 L6  strategy         策略 + 规则 DSL（回测与实盘共用同一份）
      ↓
 L7  execution        撮合网关 / 券商接入；portfolio 负责账户与仓位；risk 是闸门链
      ↓
 L8  engine           运行时：事件总线、组件、任务容器、采集调度、多进程编排
      ↓
 app  monitoring      监控面板  ·  research 回测驱动  ·  cli 命令行
```

## 三条最重要的规矩

1. **数据源是可替换的，数据标准不能替换。**
   外部格式转成 Canonical Model 的动作只允许在 ``datasource/adapters/`` 里发生一次。
   因子、策略永远不该看见 akshare / baostock / 东财的字段名。

2. **因子不知道数据源的存在。**
   因子只见 ``FactorContext``；策略只见 feature/alpha 的输出。
   出现 ``from app.datasource.something import`` 的地方都值得怀疑。

3. **反向依赖是不允许的。**
   storage 不许 import feature，datasource 不许 import alpha，
   engine 依赖最广但也因此不能被下层引用。

## 常用入口

```
python main.py providers              # 数据源路由 / 能力 / 健康分 / 熔断状态
python main.py providers --probe      # 逐条实打，一眼看出谁挂了
python main.py engine                 # 启动引擎
```

架构说明见 docs/ARCHITECTURE.md；数据源扩展见 docs/DATA_SOURCES.md。
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
