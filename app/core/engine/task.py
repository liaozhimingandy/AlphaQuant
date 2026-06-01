#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/6/1 09:59
# @FileName    : task.py
# @Description : 任务接口
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Any, Optional, Tuple, Dict
from datetime import datetime, timezone
import uuid

from twisted.internet.defer import Deferred


# ------------------------------
# 1. 任务抽象接口（所有任务的统一标准）
# ------------------------------
class ITask(ABC):
    """
    任务抽象接口，定义所有可调度任务的标准能力
    所有调度器可执行的任务，都必须实现该接口
    """

    @property
    @abstractmethod
    def task_id(self) -> str:
        """任务唯一ID，全局唯一"""
        pass

    @property
    @abstractmethod
    def priority(self) -> TaskPriority:
        """任务优先级"""
        pass

    @property
    @abstractmethod
    def status(self) -> "TaskStatus":
        """任务当前状态"""
        pass

    @property
    @abstractmethod
    def create_time(self) -> datetime:
        """任务创建时间（带时区）"""
        pass

    @abstractmethod
    def run(self) -> Any | Deferred:
        """执行任务核心逻辑，支持同步/异步返回"""
        pass

    @abstractmethod
    def cancel(self) -> None:
        """取消任务"""
        pass

    @abstractmethod
    def is_finished(self) -> bool:
        """判断任务是否已结束（成功/失败/取消）"""
        pass

    @abstractmethod
    def can_retry(self) -> bool:
        """判断任务是否可以重试"""
        pass


class TaskPriority(Enum):
    """任务优先级，量化场景专属，带中文描述方便日志/监控"""
    HIGHEST = (0, "最高优先级-交易指令")  # 平仓/止损/撤单
    HIGH = (1, "高优先级-实时数据")     # 订单回报/实时Tick
    MEDIUM = (2, "中优先级-策略计算")   # K线处理/信号计算/风控
    LOW = (3, "低优先级-后台任务")      # 统计/巡检/日志

    def __init__(self, code: int, desc: str):
        self.code = code
        self.desc = desc

    # 支持直接和int比较，兼容原有排序逻辑
    def __lt__(self, other: "TaskPriority") -> bool:
        return self.code < other.code


class TaskStatus(Enum):
    """任务状态，带中文描述"""
    PENDING = ("pending", "待执行")
    RUNNING = ("running", "执行中")
    SUCCESS = ("success", "执行成功")
    FAILED = ("failed", "执行失败")
    CANCELLED = ("cancelled", "已取消")

    def __init__(self, code: str, desc: str):
        self.code = code
        self.desc = desc


@dataclass
class Task(ITask):
    """
    标准化可调度任务实现
    """
    # ------------------------------
    # 核心排序字段
    # ------------------------------
    priority: TaskPriority = field(default=TaskPriority.MEDIUM, doc="任务优先级")
    task_name: str = field(default="", doc="任务名称")
    create_time: datetime = field(
        default=datetime.now(timezone.utc),
        compare=True
    )

    # ------------------------------
    # 任务基础信息
    # ------------------------------
    task_id: str = field(
        default= str(uuid.uuid4()),
        compare=False,
        doc="任务全局唯一ID"
    )

    task_type: str = field(
        default="common",
        compare=False,
        doc="任务类型：bar/order/timer/common，用于统计"
    )
    source_component: str = field(
        default="unknown",
        compare=False,
        doc="任务提交的来源组件，用于排错"
    )

    # ------------------------------
    # 执行逻辑（支持同步/异步Twisted Deferred）
    # ------------------------------
    handler: Callable[..., Any | Deferred] = field(
        compare=False,
        default=None,
        repr=False,
        doc="任务执行函数，支持同步/异步"
    )
    args: Tuple[Any, ...] = field(
        default_factory=tuple,
        compare=False,
        doc="执行函数位置参数"
    )
    kwargs: Dict[str, Any] = field(
        default_factory=dict,
        compare=False,
        doc="执行函数关键字参数"
    )

    # ------------------------------
    # 执行控制配置
    # ------------------------------
    max_retry: int = field(
        default=0,
        compare=False,
        doc="最大重试次数,0表示不重试"
    )
    retry_interval: float = field(
        default=1.0,
        compare=False,
        doc="重试间隔（秒）"
    )
    timeout: Optional[float] = field(
        default=None,
        compare=False,
        doc="任务执行超时时间（秒）,None表示不限制"
    )

    # ------------------------------
    # 运行时状态
    # ------------------------------
    status: TaskStatus = field(
        default=TaskStatus.PENDING,
        compare=False,
        doc="任务当前状态"
    )
    current_retry: int = field(
        default=0,
        compare=False,
        doc="当前已重试次数"
    )
    result: Any = field(
        default=None,
        compare=False,
        doc="任务执行成功结果"
    )
    error: Optional[Exception] = field(
        default=None,
        compare=False,
        doc="任务执行失败异常"
    )
    start_time: Optional[datetime] = field(
        default=None,
        compare=False,
        doc="任务开始执行时间"
    )
    end_time: Optional[datetime] = field(
        default=None,
        compare=False,
        doc="任务结束执行时间"
    )

    # ------------------------------
    # 初始化校验（避免非法参数）
    # ------------------------------
    def __post_init__(self) -> None:
        # 校验优先级类型
        if not isinstance(self.priority, TaskPriority):
            raise ValueError(f"任务优先级必须是TaskPriority枚举，当前: {type(self.priority)}")
        # 校验执行函数不能为空
        if not callable(self.handler):
            raise ValueError("任务执行函数handler必须是可调用对象")
        # 校验重试参数合法性
        if self.max_retry < 0:
            raise ValueError(f"最大重试次数不能为负数，当前: {self.max_retry}")
        if self.retry_interval < 0:
            raise ValueError(f"重试间隔不能为负数，当前: {self.retry_interval}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"超时时间必须大于0，当前: {self.timeout}")

    # ------------------------------
    # 接口实现
    # ------------------------------
    def run(self) -> Any | Deferred:
        """执行任务核心逻辑，自动更新状态、记录时间"""
        if self.status != TaskStatus.PENDING:
            raise RuntimeError(f"任务状态异常，无法执行：{self.status.desc}")

        self.status = TaskStatus.RUNNING
        self.start_time = datetime.now(timezone.utc)
        try:
            result = self.handler(*self.args, **self.kwargs)
            # 支持异步Deferred
            if isinstance(result, Deferred):
                def on_success(res):
                    self.status = TaskStatus.SUCCESS
                    self.result = res
                    self.end_time = datetime.now(timezone.utc)
                    return res

                def on_fail(e):
                    self.status = TaskStatus.FAILED
                    self.error = e
                    self.end_time = datetime.now(timezone.utc)
                    return e

                result.addCallbacks(on_success, on_fail)
                return result
            # 同步返回
            self.status = TaskStatus.SUCCESS
            self.result = result
            self.end_time = datetime.now(timezone.utc)
            return result
        except Exception as e:
            self.status = TaskStatus.FAILED
            self.error = e
            self.end_time = datetime.now(timezone.utc)
            raise e

    def cancel(self) -> None:
        """取消任务，仅待执行的任务可取消"""
        if self.status == TaskStatus.PENDING:
            self.status = TaskStatus.CANCELLED
            self.end_time = datetime.now(timezone.utc)

    def is_finished(self) -> bool:
        """判断任务是否已结束"""
        return self.status in [TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED]

    def can_retry(self) -> bool:
        """判断是否可以重试"""
        return (
                self.status == TaskStatus.FAILED
                and self.current_retry < self.max_retry
        )

    # ------------------------------
    # 自定义排序逻辑
    # ------------------------------
    def __lt__(self, other: "Task") -> bool:
        """
        优先级排序规则：
        1. 优先级code越小，优先级越高，越先执行
        2. 优先级相同，创建时间越早，越先执行
        """
        if self.priority.code != other.priority.code:
            return self.priority.code < other.priority.code
        return self.create_time < other.create_time

    # ------------------------------
    # 辅助方法
    # ------------------------------
    def get_cost_time(self) -> Optional[float]:
        """获取任务执行耗时（秒）"""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    def to_dict(self) -> Dict[str, Any]:
        """转字典，用于日志/监控/持久化"""
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "task_type": self.task_type,
            "source_component": self.source_component,
            "priority": self.priority.desc,
            "status": self.status.desc,
            "create_time": self.create_time.isoformat(),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "cost_time": self.get_cost_time(),
            "current_retry": self.current_retry,
            "max_retry": self.max_retry,
            "error": str(self.error) if self.error else None
        }
