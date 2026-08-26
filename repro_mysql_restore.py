# -*- coding: utf-8 -*-
"""临时复现脚本：直接调用 MySQL 引擎对已存在的真实 .sql 备份执行恢复，暴露失败根因。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config, core.db as db
db.init_schema()

from core.engines import get_engine

# 找一个真实的 .sql 全量备份文件（task 34）
bp = r"E:\备份管理平台\backup_platform\backups\mysql\34_mysql核心交易库-192.168.220.133\20260811_153927__mysql核心交易库-192.168.220.133__full.sql"

task = {
    "id": 34, "name": "mysql核心交易库-192.168.220.133",
    "db_type": "mysql", "host": "192.168.220.133", "port": 3306,
    "username": "root", "password": "", "db_name": "", "backup_mode": "logical",
    "extra_options": {}, "demo_only": None,
}
eng = get_engine("mysql", task, config.BACKUP_ROOT, None)
print("=== check_client ===")
print(eng.check_client())
print("=== DEMO_MODE ===", config.DEMO_MODE)
print("=== simulate decision ===", eng._should_simulate())
print("=== restore() ===")
res = eng.restore(bp)
print("success=", res.success, "status=", res.status)
print("message=", res.message)
print("stderr=", (res.stderr or "")[:800])
