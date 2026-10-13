#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : migrate.py
# @Description : 在线加列 —— 给**已经存在**的库补上后加的字段
#
#                为什么需要这个文件
#                ============================================================
#                项目没有迁移框架（没有 Alembic），建表走的是
#                ``Base.metadata.create_all(checkfirst=True)``。
#                而 create_all 的语义是"**缺的表**建起来"，对**已存在的表**
#                一个字节都不动 —— 于是在老库上给模型加一列会有这样的结果：
#
#                    代码里 ``profile.industry`` 明明存在，
#                    一查库 ``OperationalError: no such column: industry``。
#
#                而且这个错只在**老库**上出现：新起的临时库是全新建的，
#                列天然齐全。于是它典型的发作方式是
#                "测试全绿 → 一上生产就炸"，且报错点（数据库）与真正的
#                改动点（模型里的新字段）相隔甚远，排查成本极高。
#
#                这里的办法很朴素：启动期拿现在的列清单与实际库比对，
#                缺的列用 ``ALTER TABLE ... ADD COLUMN`` 补上。
#
#                为什么这样是安全的
#                ============================================================
#                * SQLite 的 ADD COLUMN 只能加**可空**列（不能有 NOT NULL
#                  约束、不能有主键）—— 所以本文件补出来的列一律可空，
#                  正好对应"这一列还没同步过"这一语义；
#                * ADD COLUMN 是 O(1) 元数据操作，不动已有行，**十万行的表
#                  瞬间完成**；
#                * 幂等：先 PRAGMA table_info 查列，缺才补；
#                * 加了 ``check_for_uniqueness`` 之外什么也不做 —— 不建索引、
#                  不删列、不改类型。**删列/改类型**必须显式的人为迁移，
#                  自动化在这件事上只会把数据弄丢。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.logging import logger


def existing_columns(engine: Engine, table: str) -> List[str]:
    """读数据库里实际存在的列（SQLite 专用路径，其他方言走 inspector）。"""
    try:
        if getattr(engine.dialect, "name", "") == "sqlite":
            with engine.connect() as conn:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            # PRAGMA table_info 的第 2 列是 name
            return [str(r[1]) for r in rows]
    except Exception as exc:  # pragma: no cover - 库打不开时退化
        logger.debug(f"读取表结构失败 {table}: {exc}")
        return []
    try:  # pragma: no cover - 非 sqlite 环境
        from sqlalchemy import inspect

        return [c["name"] for c in inspect(engine).get_columns(table)]
    except Exception as exc:
        logger.debug(f"读取表结构失败 {table}: {exc}")
        return []


def ensure_columns(
    engine: Engine,
    table: str,
    specs: Dict[str, str],
    quiet: bool = True,
) -> List[str]:
    """把 ``specs`` 里缺的列补上。

    :param specs: ``{列名: SQL 类型}``，如 ``{"industry": "VARCHAR(64)"}``
    :return:      本次**实际新增**的列名列表（幂等：第二次调用返回空列表）

    单列失败不影响其他列：少一列是"这个功能用不了"，
    一次性崩掉是"引擎起不来"。宁可带着缺口启动并把缺口写在日志里。
    """
    if engine is None or not specs:
        return []
    try:
        have = set(existing_columns(engine, table))
    except Exception as exc:  # pragma: no cover
        logger.warning(f"比对表结构失败 {table}: {exc}")
        return []
    if not have:
        # 表还不存在 —— 交给 create_all 去建，这里不要自作主张 CREATE TABLE：
        # 那样会绕过模型里定义的索引与约束，建出一张"看起来能用、
        # 实际和别的库不一样"的表。
        if not quiet:
            logger.debug(f"表 {table} 尚不存在，跳过加列（由 create_all 负责）")
        return []

    added: List[str] = []
    for col, decl in specs.items():
        if col in have:
            continue
        stmt = f'ALTER TABLE {table} ADD COLUMN {col} {decl}'
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
            added.append(col)
            logger.info(f"表 {table} 已补充新列 {col} {decl}")
        except Exception as exc:
            logger.warning(f"表 {table} 补充列 {col} 失败: {exc}")
    return added


def ensure_schema_of(model: Any, engine: Optional[Engine] = None,
                     specs: Optional[Dict[str, str]] = None) -> List[str]:
    """从一个 SQLAlchemy 模型出发补列（类型由模型自己导出）。

    :param model: 表对应的模型类
    :param specs: 手工指定的类型覆盖（SQLite 类型名可能与通用名不同时用它）
    """
    if model is None:
        return []
    if engine is None:
        from app.data.engine import engine as default_engine  # 延迟导入，避免循环

        engine = default_engine
    table = str(getattr(model, "__tablename__", "") or "")
    if not table:
        return []
    wanted: Dict[str, str] = {}
    for col in model.__table__.columns:
        if specs and col.name in specs:
            wanted[col.name] = specs[col.name]
            continue
        try:
            wanted[col.name] = col.type.compile(engine.dialect)
        except Exception:  # pragma: no cover - 未知方言
            wanted[col.name] = "TEXT"
    return ensure_columns(engine, table, wanted)


__all__ = ["ensure_columns", "ensure_schema_of", "existing_columns"]
