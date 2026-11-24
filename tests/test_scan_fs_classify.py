# -*- coding: utf-8 -*-
"""特殊恢复（scan-fs）多库型识别回归：库型判定 + 对象类型 + 默认根目录覆盖。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import scan_fs  # noqa: E402

CASES = [
    # (路径, 期望库型, 期望对象类型)
    ("/var/lib/mysql/ibdata1", "mysql", "db_data"),
    ("/var/lib/mysql/shop/orders.ibd", "mysql", "db_data"),
    ("/data/mysql3306/shop/orders.frm", "mysql", "db_data"),
    ("/var/log/mysql/mysql-bin.000012", "mysql", "db_log"),
    ("/data/postgresql/PG_VERSION", "postgresql", "db_data"),
    ("/data/postgresql/base/16384/1259", "postgresql", "db_data"),
    ("/data/postgresql/pg_wal/000000010000000000000001", "postgresql", "db_log"),
    ("/opt/Kingbase/ES/V9/data/PG_VERSION", "kingbase", "db_data"),
    ("/opt/Kingbase/ES/V9/data/sys_wal/000000010000000000000002", "kingbase", "db_log"),
    ("/opt/Kingbase/ES/V9/data/sys_hba.conf", "kingbase", "db_config"),
    ("/dm/data/DAMENG/DAMENG01.DBF", "dameng", "db_data"),
    ("/data/dm2/DAMENG/dm.ctl", "dameng", "db_control"),
    ("/dm8/data/DAMENG/dm.ini", "dameng", "db_config"),
    ("/u01/app/oracle/oradata/ORCL/system01.dbf", "oracle", "db_data"),
    ("/u01/app/oracle/product/19.0.0/dbhome_1/dbs/spfileORCL.ora", "oracle", "db_config"),
    ("/u01/app/oracle/fast_recovery_area/ORCL/archivelog/1_1.arc", "oracle", "db_log"),
    ("/u01/app/oracle/oradata/ORCL/control01.ctl", "oracle", "db_control"),
    ("/var/opt/mssql/data/shop.mdf", "mssql", "db_data"),
    ("/var/opt/mssql/data/shop_log.ldf", "mssql", "db_data"),
    ("/var/lib/redis/dump.rdb", "redis", "db_data"),
    ("/var/lib/redis/appendonly.aof", "redis", "db_log"),
    ("/var/lib/mongo/WiredTiger", "mongodb", "db_data"),
    ("/var/lib/mongo/collection-0-123.wt", "mongodb", "db_data"),
    ("/opt/app/data/app.db", "sqlite", "db_data"),
    ("/tmp/notes.txt", "unknown", "file"),
    ("/backup/dump/shop.dmp", "unknown", "backup_artifact"),
]


def test_db_type_and_kind_matrix():
    bad = []
    for path, want_db, want_kind in CASES:
        got_db = scan_fs._db_type(path)
        got_kind = scan_fs._kind(path, got_db)
        if got_db != want_db or got_kind != want_kind:
            bad.append((path, got_db, got_kind, want_db, want_kind))
    assert not bad, "库型/类型识别不符：\n" + "\n".join(str(b) for b in bad)


def test_default_roots_cover_all_db_types():
    """默认扫描根目录必须覆盖各库型的常见数据目录（多库型可恢复的前提）。"""
    roots = " ".join(scan_fs.DEFAULT_ROOTS)
    for token in ("/oradata", "/var/lib/mysql", "/var/lib/postgresql",
                  "/opt/Kingbase", "/dm", "/var/opt/mssql",
                  "/var/lib/redis", "/var/lib/mongo"):
        assert token in roots, "默认根目录缺少 %s" % token


def test_db_labels_cover_instances():
    for db_type in ("oracle", "mysql", "postgresql", "kingbase", "opengauss",
                    "dameng", "mssql", "redis", "mongodb", "sqlite", "unknown"):
        assert db_type in scan_fs.DB_TYPE_LABELS, "缺少库型显示名: %s" % db_type


def test_kind_values_match_ui_options():
    """前端筛选下拉的 kind 取值必须与本模块产出一致（防再次对不上）。"""
    html = (Path(__file__).resolve().parents[1]
            / "templates" / "scan_fs.html").read_text(encoding="utf-8")
    for kind in ("db_data", "db_log", "db_control", "db_config",
                 "backup_artifact", "file"):
        assert 'value="%s"' % kind in html, "页面筛选缺少类型 %s" % kind


def test_page_and_menu_renamed():
    base = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")
    page = (Path(__file__).resolve().parents[1] / "templates" / "scan_fs.html").read_text(encoding="utf-8")
    assert "特殊恢复" in base and "无备份救援（特殊恢复）" not in base
    # 2026-11-24：NAS 菜单现文案为「NAS备份」（历史断言「NAS存储」已过期）
    assert "NAS备份" in base and "NAS 存储（群晖）" not in base
    assert "特殊恢复" in page and "无备份救援" not in page
