# -*- coding: utf-8 -*-
"""启动健壮性门禁：模块导入期不得因备份目录异常导致全站起不来。

2026-11-11 清理 /opt/backup-platform/backups 旧根后，项目内 backups 软链
变成断链；core/data_mining.py 在模块级执行 data_miner = DataMiner()，
__init__ 里的 mkdir 抛 FileExistsError，导致 create_app 直接失败。
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_module_level_data_miner_instance():
    src = (ROOT / "core" / "data_mining.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    eager = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
            continue
        value = getattr(node, "value", None)
        if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "DataMiner":
            eager.append(node.lineno)
    assert not eager, (
        "core/data_mining.py 不应在模块级实例化 DataMiner（行 %s）："
        "导入期磁盘 IO 会让备份目录异常演变成全站启动失败" % eager)


def test_import_survives_unusable_backup_root(tmp_path):
    blocker = tmp_path / "backup_root_is_a_file"
    blocker.write_text("not a directory", encoding="utf-8")
    env = os.environ.copy()
    env["BACKUP_ROOT"] = str(blocker)
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    proc = subprocess.run(
        [sys.executable, "-c", "import core.data_mining; print('IMPORT_OK')"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0 and "IMPORT_OK" in proc.stdout, (
        "备份根不可用时导入 core.data_mining 失败: " + (proc.stderr or "")[-600:])
