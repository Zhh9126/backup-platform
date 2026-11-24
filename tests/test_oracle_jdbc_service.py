# -*- coding: utf-8 -*-
"""2026-11-23 Oracle 11g JDBC/拉库/预检 三项现场修复的契约回归。

问题背景（用户现场 192.168.163.195 Oracle 11g）：
1. JDBC 测试报 ORA-12261（Easy Connect 串 ``...:1521/`` service 为空）；
2. 任务向导「拉取库列表」失败——Oracle 引擎 list_databases 是空实现；
3. 预检归档模式检查抛 ``missing storage_root``（少传构造参数）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

from core import jdbc  # noqa: E402


def test_oracle_empty_service_falls_back_to_orcl():
    """Oracle 未填 service 时 build_url 必须补 ORCL，不留 ``1521/`` 空尾。"""
    url = jdbc.build_url("oracle", "192.168.163.195", 1521, "")
    assert url.endswith("/ORCL"), url
    assert not url.endswith("1521/"), url
    # 用户填了真实 service 时尊重原值
    assert jdbc.build_url("oracle", "h", 1521, "orcl11g").endswith("/orcl11g")


def test_jdbc_db_arg_other_types_unchanged():
    """达梦 URL 模板无 {db}，空值兜底不得影响其他库型。"""
    assert jdbc._jdbc_db_arg("dameng", "") == ""
    assert jdbc._jdbc_db_arg("mysql", None) == ""
    assert jdbc._jdbc_db_arg("oracle", "  ") == "ORCL"


def test_oracle_list_databases_no_longer_stub():
    """Oracle 引擎 list_databases 不再是恒空占位实现。"""
    import inspect
    from core.engines.oracle import OracleEngine
    src = inspect.getsource(OracleEngine.list_databases)
    assert "all_users" in src
    assert "select username" in src.lower()
    # 不再有直接 return [] 的桩
    assert src.strip().endswith("return [r for r in sorted(set(rows)) if r and not oracle_system_schema(r)]")


def test_precheck_archive_uses_constructed_engine():
    """预检归档探测复用已构造的 engine，不再 _OE(task) 少传参数。"""
    import inspect
    from core import backup_precheck
    src = inspect.getsource(backup_precheck.precheck_backup)
    # 必须有真实调用（复用已构造的 engine），且没有裸的 ``_eng = _OE(task)``
    assert "_eng = engine" in src
    assert "_eng = _OE(task)" not in src
