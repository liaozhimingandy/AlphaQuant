#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : __init__.py
# @Description : 账户域 —— 资金、持仓、清算
#
#                按项目约定（每个包 ``__init__.py`` 都写一个担当说明），
#                这里用 PEP 562 延迟导出，避免 import 本包时就把
#                Twisted / SQLAlchemy 依赖全部拽起来 ——
#                那种写法曾经制造过"测试全绿但单独 import 会炸"的循环依赖。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from typing import Any

__all__ = ["AccountService", "store", "VIRTUAL_ACCOUNT_ID", "VIRTUAL_INITIAL_CASH"]


def __getattr__(name: str) -> Any:
    if name == "AccountService":
        from app.modules.account.service import AccountService

        return AccountService
    if name == "store":
        from app.account import store

        return store
    if name == "VIRTUAL_ACCOUNT_ID":
        from app.account.store import VIRTUAL_ACCOUNT_ID

        return VIRTUAL_ACCOUNT_ID
    if name == "VIRTUAL_INITIAL_CASH":
        from app.account.store import VIRTUAL_INITIAL_CASH

        return VIRTUAL_INITIAL_CASH
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
