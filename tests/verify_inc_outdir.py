# -*- coding: utf-8 -*-
"""临时验证脚本：确认本地目标时全量与增量归档都直接落在 target_path 根目录。

验证点：
1. 全量归档生成于 dst_path 根目录（*_full.tar.gz）
2. 增量归档同样生成于 dst_path 根目录（*_inc.tar.gz），不再有 backups/ 子目录
"""
import os
import sys
import tempfile
import time
import json
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

_TMP = tempfile.mkdtemp(prefix="verify_inc_")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "storage_root")
os.environ["DEMO_MODE"] = "off"
os.environ["DB_PATH"] = os.path.join(_TMP, "verify.db")

import config
config.DEMO_MODE = "off"
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]

from core.engines.file import FileBackupEngine
from core.engines.base import BackupType


def main() -> int:
    src_dir = os.path.join(_TMP, "src")
    dst_dir = os.path.join(_TMP, "dst")
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(dst_dir, exist_ok=True)

    with open(os.path.join(src_dir, "a.txt"), "w", encoding="utf-8") as f:
        f.write("hello")

    task = {
        "id": 999,
        "name": "verify-inc-outdir",
        "db_type": "file",
        "demo_only": 0,
        "extra_options": json.dumps({
            "source_type": "local",
            "source_paths": [src_dir],
            "target_type": "local",
            "target_path": dst_dir,
        }),
    }
    engine = FileBackupEngine(task, os.environ["BACKUP_ROOT"])

    # 1) 全量
    r1 = engine.backup(BackupType.FULL)
    assert r1.success, f"全量备份失败: {r1.message}"
    assert os.path.dirname(r1.backup_path) == dst_dir, \
        f"全量归档不在目标根目录: {r1.backup_path}"
    print(f"[OK] 全量归档位置正确: {r1.backup_path}")

    # 2) 修改源文件后做增量
    time.sleep(1)
    with open(os.path.join(src_dir, "b.txt"), "w", encoding="utf-8") as f:
        f.write("world")

    r2 = engine.backup(BackupType.INCREMENTAL)
    assert r2.success, f"增量备份失败: {r2.message}"
    assert r2.backup_path, f"增量未生成归档: {r2.message}"
    assert os.path.dirname(r2.backup_path) == dst_dir, \
        f"增量归档不在目标根目录: {r2.backup_path}"
    assert "__inc.tar.gz" in os.path.basename(r2.backup_path), \
        f"增量归档命名异常: {r2.backup_path}"
    assert not os.path.exists(os.path.join(dst_dir, "backups")), \
        "目标目录下不应再出现 backups/ 子目录"
    print(f"[OK] 增量归档位置正确: {r2.backup_path}")
    print("[OK] 目标目录下无 backups/ 子目录")

    print("VERIFY PASS")
    return 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
