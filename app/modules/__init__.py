#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Description : Modules —— 业务能力层
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
"""
Modules（业务模块层）
====================

Modules 是**引擎装配的业务单元**。每个 module 对外暴露一个或多个组件，
由 ``app/engine`` 通过 ``Cap`` 能力常量做依赖解析后注入。

数据流方向（研究决策链 → 执行链）::

    market        实时行情 / K线
      │
    collection    采集、清洗、完整性核验、落库
      │
    universe      全市场 → 股票池 → 候选股（含打分排序）
      │
    factor        因子计算 / 技术指标 / 因子挖掘
      │
    strategy      策略定义与实例，产出 Signal
      │
    risk          风控闸门链（决定"能不能下单"）
      │
    portfolio     组合视图 / 跨任务额度
      │
    trading       订单生命周期 → 网关 → 成交

约束（新增代码请遵守）::

    modules.*  可以 import → engine.{component,capability,context}、runtime、domain、
                            data、providers、common
    modules.* **禁止** import → app.engine.engine（引擎本体），否则形成容器与模块
                            的双向耦合。需要引擎能力请用构造注入，不要反向取。

具体各模块的导出见各自 ``__init__.py``。
"""
