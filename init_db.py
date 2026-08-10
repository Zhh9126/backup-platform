# -*- coding: utf-8 -*-
"""
初始化元数据数据库（SQLite）：创建任务、记录、恢复、日志等表。

用法：
    python init_db.py
"""
import config
import core.db as db

if __name__ == "__main__":
    db.init_schema()
    print("元数据数据库已初始化：", config.META_DB_PATH)
