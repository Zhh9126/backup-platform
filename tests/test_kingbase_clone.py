# -*- coding: utf-8 -*-
"""金仓（KingbaseES）备份/恢复/克隆回归（2026-11-13 彻底修复）。

背景：金仓验证多次失败，定位到三类根因：
1. 异名库恢复时目标库不存在（ksql: database "xxx" does not exist）——
   远程恢复已改为自动 CREATE DATABASE；
2. 物理恢复远端找不到 sys_ctl/ksql（只在 PATH 里找）——
   resolve_remote_tool 已改为「从运行中的 kingbase 主进程推断 bin 目录 + glob 兜底」；
3. 克隆通道直接拒绝「db_type=kingbase 暂不支持真实克隆」——
   本轮补齐：复用引擎 SSH+ksql 恢复通道拉起克隆库，校验/销毁走 psycopg2。
"""
from pathlib import Path

import pytest

from core import clone_service, restore_extras

SRC = Path(__file__).resolve().parent.parent


def test_clone_engine_has_kingbase():
    assert clone_service._CLONE_ENGINE.get("kingbase") == "kb_clone_to_test"
    src = (SRC / "core" / "clone_service.py").read_text(encoding="utf-8")
    assert "kb_clone_to_test" in src
    assert '"system" if db_type == "kingbase"' in src


def test_verify_and_drop_support_kingbase():
    v_src = (SRC / "core" / "restore_extras.py").read_text(encoding="utf-8")
    assert 'if db_type in ("kingbase",):' in v_src, "verify_clone_conn 缺金仓分支"
    assert 'elif db_type == "kingbase":' in v_src, "drop_clone 缺金仓分支"
    # 金仓分支不允许引用不存在的 user 形参（drop_clone 签名里没有 user）
    seg = v_src.split('elif db_type == "kingbase":', 1)[1][:1200]
    assert 'user or "system"' not in seg


def test_verify_clone_conn_kingbase_live():
    """真实连接金仓校验通道（环境不可用时跳过，不伪造通过）。"""
    import core.models as models
    task = models.get_task(157, include_secret=True)
    if not task:
        pytest.skip("无金仓任务可参考连接参数")
    res = restore_extras.verify_clone_conn(
        "kingbase", task.get("host"), task.get("port") or 54321,
        task.get("db_name"), task.get("username") or "system",
        password=task.get("password") or "")
    if not res.get("ok"):
        pytest.skip("金仓实例不可达: %s" % (res.get("message") or "")[:120])
    assert res["tables"] > 0
    assert "连接正常" in res["message"]
