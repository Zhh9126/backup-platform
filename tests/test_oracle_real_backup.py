# -*- coding: utf-8 -*-
"""
真实 Oracle 备份端到端测试（不是仿真）。

通过 SSH 在 192.168.220.129（Oracle 19c 数据库服务器）以 oracle 用户真实执行：
  - 逻辑备份：expdp（Data Pump）导出 SCHEMAS=bkptest 或 FULL，SFTP 拉回 dmp
  - 物理备份：RMAN BACKUP DATABASE + ARCHIVELOG，SFTP 拉回备份片

直接 import 引擎运行，不经 8080 服务进程（无需重启服务即可验证引擎本身）。

用法：
  python tests/test_oracle_real_backup.py logical    # 仅逻辑(expdp, 快)
  python tests/test_oracle_real_backup.py physical   # 仅物理(rman 全库, 慢 5~15min)
  python tests/test_oracle_real_backup.py all        # 两者都跑（默认）
"""
import os
import sys
import json

# 确保项目根目录在 sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core.db as db
from core.db import encrypt_secret
from core.engines.oracle import OracleEngine
from core.engines.base import BackupType

ORACLE_HOST = "192.168.220.129"
ORACLE_PORT = 1521
ORACLE_SID = "orcl11g"
ORACLE_USER = "system"
ORACLE_PASS = "oracle"
# 测试用 schema（已通过 sqlplus 建好并插入数据）
TEST_SCHEMA = "bkptest"

STORAGE_ROOT = os.path.join(ROOT, "backups", "oracle_real_test")


def make_task(backup_mode: str, extra: dict) -> dict:
    # 生产环境中 task.extra_options 来自 DB 的 TEXT 列，是 JSON 字符串；
    # 引擎的 _parse_extra() 仅按字符串解析，故此处序列化为 JSON 字符串。
    return {
        "id": 999001,
        "name": f"oracle_real_{backup_mode}",
        "db_type": "oracle",
        "host": ORACLE_HOST,
        "port": ORACLE_PORT,
        "db_name": ORACLE_SID,
        "username": ORACLE_USER,
        "password": encrypt_secret(ORACLE_PASS),
        "backup_mode": backup_mode,
        "extra_options": json.dumps(extra),
    }


def run_logical():
    print("\n========== [逻辑备份] expdp via SSH ==========")
    # 用小 schema 快速验证端到端；如需全库可改 extra={"service": ORACLE_SID}
    extra = {"service": ORACLE_SID, "schemas": [TEST_SCHEMA]}
    task = make_task("logical", extra)
    engine = OracleEngine(task, STORAGE_ROOT)
    result = engine.backup(BackupType.FULL)
    _assert_result(result, label="逻辑备份(expdp)")
    return result


def run_physical():
    print("\n========== [物理备份] RMAN via SSH ==========")
    extra = {"service": ORACLE_SID}
    task = make_task("physical", extra)
    engine = OracleEngine(task, STORAGE_ROOT)
    result = engine.backup(BackupType.FULL)
    _assert_result(result, label="物理备份(RMAN)")
    return result


def _assert_result(result, label: str):
    print(f"\n--- {label} 结果 ---")
    print("success     :", result.success)
    print("status      :", result.status)
    print("backup_path :", result.backup_path)
    print("size_bytes  :", result.size_bytes, f"({db.human_size(result.size_bytes)})")
    print("checksum    :", result.checksum[:24], "..." if result.checksum else "(empty)")
    print("duration_sec:", result.duration_sec)
    print("message     :", result.message)
    if result.stderr:
        print("stderr(tail):", result.stderr[-600:])

    assert result.success is True, f"{label} 失败: {result.message}"
    assert result.backup_path and os.path.exists(result.backup_path), \
        f"{label} backup_path 不存在: {result.backup_path}"
    assert result.size_bytes > 0, f"{label} size_bytes 应为 >0，实际 {result.size_bytes}"
    assert result.checksum != "", f"{label} checksum 不应为空"
    print(f"\n✅ {label} 通过：真实文件已落盘并校验通过。")


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    os.makedirs(STORAGE_ROOT, exist_ok=True)
    if mode in ("logical", "all"):
        run_logical()
    if mode in ("physical", "all"):
        run_physical()
    print("\n========== 全部测试通过 ✅ ==========")


if __name__ == "__main__":
    main()
