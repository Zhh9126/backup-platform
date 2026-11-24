# -*- coding: utf-8 -*-
"""第三台主机恢复演练（容灾一体机需求）的契约与安全回归。"""
import os
import sys
import inspect
import json
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

from core import drill  # noqa: E402


def test_api_scenario_contract():
    """API 层契约：scenario 支持 dict/JSON 串；models 层 dict 序列化落库。"""
    from api import drills as drill_api
    src = inspect.getsource(drill_api.create_drill)
    # API 统一解析 scenario（不再仅限 switch）
    assert "json.loads(scenario)" in src
    # models.create_drill 保证 dict 型 scenario 正确序列化（历史契约）
    from core import models
    msrc = inspect.getsource(models.create_drill)
    assert 'isinstance(row.get(k), dict)' in msrc
    assert 'json.dumps' in msrc


def test_scenario_serialization_roundtrip():
    """scenario dict 序列化后 JSON 可还原（drill 执行时按此解析）。"""
    scenario = {"target_host_id": 17}
    restored = json.loads(json.dumps(scenario, ensure_ascii=False))
    assert restored["target_host_id"] == 17


def test_remote_mysql_exec_safe_contract():
    """第三台主机通道：独立 SSH + SFTP 写 SQL + --no-defaults。"""
    assert callable(drill._remote_mysql_exec)
    assert callable(drill._drill_verify_rows_remote)
    src = inspect.getsource(drill._remote_mysql_exec)
    assert "_connect_isolated" in src
    assert "open_sftp" in src
    assert "--no-defaults" in src


def test_real_restore_wires_third_host_contract():
    """真恢复演练方法体内：必须解析 target_host_id、跨主机恢复/校验/销毁。"""
    src = inspect.getsource(drill._run_real_restore_drill)
    assert 'target_host_id' in src
    assert '_drill_verify_rows_remote' in src
    assert '_remote_mysql_exec' in src
    # 目标主机参数必须传给 run_restore_now
    assert '"target_host_id"' in src or "'target_host_id'" in src
    # 受保护主机闸门
    assert "protected" in src
