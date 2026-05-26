#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/5/26 15:10
# @FileName    : config.py
# @Description : 核心配置文件
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

class Settings:
    DATABASE_URL = os.getenv(
        "DATABASE_URL",f"sqlite:///{BASE_DIR}/data/alphaquant.db"
    )

settings = Settings()


def main(name: str = ''):
    print(f'Hi, {name}')


if __name__ == '__main__':
    main()
