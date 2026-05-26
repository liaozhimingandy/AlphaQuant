#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 15:15
# @FileName    : init_db.py
# @Description : 本文件功能描述
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
from app.db.database import engine
from app.db.models import Base

from app.utils.logger import logger

def init_db():

    Base.metadata.create_all(bind=engine)

    print("数据库初始化完成")


if __name__ == "__main__":
    init_db()
