#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : config.py
# @Description : 回测配置：一次回测所需的全部参数集中在此，可序列化、可复现
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict

from app.core.config import settings
from app.data.datasource import normalize_date, normalize_symbol


@dataclass
class BacktestConfig:
    """回测配置。

    设计目标：一次回测 = 一份配置。配置可转成 dict 落盘，
    下次拿同一份配置重跑必然得到同一结果（数据不变的前提下）。
    """

    symbol: str
    start: str
    end: str

    strategy: str = "ma_cross"
    strategy_params: Dict[str, Any] = field(default_factory=dict)

    cash: float = settings.DEFAULT_CASH
    commission: float = settings.DEFAULT_COMMISSION
    slippage_perc: float = settings.DEFAULT_SLIPPAGE_PERC

    # 数据来源：auto(库里没有就联网) / db / csv / remote
    data_source: str = "auto"
    adjust: str = "qfq"

    # 分析参数
    risk_free_rate: float = 0.0
    trading_days_per_year: int = 252

    plot: bool = False
    export: bool = False
    output_dir: str | None = None

    # 是否打印逐根K线的策略日志。默认关闭：否则几百根K线的日志会淹没回测结论
    print_log: bool = False

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.start = normalize_date(self.start)
        self.end = normalize_date(self.end)
        if self.start > self.end:
            raise ValueError(f"开始日期不能晚于结束日期: {self.start} > {self.end}")
        if self.cash <= 0:
            raise ValueError(f"初始资金必须大于 0: {self.cash}")
        if self.commission < 0:
            raise ValueError(f"手续费率不能为负: {self.commission}")

    @property
    def start_iso(self) -> str:
        return f"{self.start[:4]}-{self.start[4:6]}-{self.start[6:8]}"

    @property
    def end_iso(self) -> str:
        return f"{self.end[:4]}-{self.end[4:6]}-{self.end[6:8]}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BacktestConfig":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)
