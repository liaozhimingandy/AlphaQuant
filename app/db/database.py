#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 15:12
# @FileName    : database.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import settings


# ===== 自动创建 SQLite 目录 =====
if settings.DATABASE_URL.startswith("sqlite"):

    db_path = settings.DATABASE_URL.replace("sqlite:///", "")

    db_dir = os.path.dirname(db_path)

    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

#: 生产环境的 SQLite 连接配置。**测试也必须用同一套** ——
#: 否则"测试通过"验证的是另一套连接配置，生产上才会遇到的锁问题照样发生。
SQLITE_CONNECT_ARGS = {"check_same_thread": False, "timeout": 30.0}
SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",     # 读写互不阻塞
    "PRAGMA synchronous=NORMAL",   # WAL 下的安全/速度平衡点
    "PRAGMA busy_timeout=30000",   # 与 connect_args timeout 双保险
)


def apply_sqlite_pragmas(target: Engine) -> Engine:
    """给任意 SQLite engine 挂上生产同款 PRAGMA。幂等，可重复调用。

    为什么需要公开这个函数：测试常用临时库，如果各自 `create_engine` 就
    拿不到 WAL —— 于是"批量写 + 并发读"的场景在测试里根本不会被触发，
    真出问题时也复现不出来。
    """
    if getattr(target.dialect, "name", "") != "sqlite":
        return target
    if getattr(target, "_aq_pragmas_applied", False):
        return target

    @event.listens_for(target, "connect")
    def _set_pragmas(dbapi_conn, _connection_record):  # pragma: no cover - 驱动层回调
        cur = dbapi_conn.cursor()
        try:
            for stmt in SQLITE_PRAGMAS:
                cur.execute(stmt)
        except Exception:
            # PRAGMA 失败不该让整个进程起不来（比如只读文件系统、
            # 或某些不支持 WAL 的网络文件系统）
            pass
        finally:
            cur.close()

    target._aq_pragmas_applied = True  # type: ignore[attr-defined]
    return target


def create_sqlite_engine(url: str, **kwargs) -> Engine:
    """按生产配置创建 SQLite engine（WAL + 30s busy timeout）。"""
    params = dict(kwargs.pop("connect_args", None) or {})
    if url.startswith("sqlite"):
        params = {**SQLITE_CONNECT_ARGS, **params}
    engine = create_engine(
        url,
        connect_args=params,
        pool_pre_ping=kwargs.pop("pool_pre_ping", True),
        **kwargs,
    )
    return apply_sqlite_pragmas(engine)


# ===== Engine =====
#
# timeout：SQLite 遇到写锁时的等待秒数（默认只有 5s，太短）。
# 本项目有"后台线程批量落库 + 面板/CLI 随时查询"的并发形态，
# 加上某些机器上磁盘/文件锁本身就慢，5 秒经常不够 —— 表现为
# `sqlite3.OperationalError: database is locked`，看起来像代码 bug，其实是等得太短。
engine = create_sqlite_engine(settings.DATABASE_URL)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


if __name__ == '__main__':
    pass
