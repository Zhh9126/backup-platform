# -*- coding: utf-8 -*-
"""P0-2 高危操作防护回归：操作验证码闸 + 资源受保护标记。

覆盖：
1. /api/verify-code 下发 4 位码；/api/op-guard/config 默认关
2. 开关关闭时高危端点零破坏（不要求验证码）
3. 开关开启后：无码/错码 403 verify_code_required，对码放行（一次性）
4. 受保护主机：protect 开关生效 + run_restore_now 硬闸 ValueError
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="op_guard_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest


@pytest.fixture(scope="module")
def app():
    from app import create_app
    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture(scope="module")
def client(app):
    with app.test_client() as c:
        r = c.post("/login", data={"username": "admin", "password": "admin123"})
        assert r.status_code in (200, 302)
        yield c


def _create_host(client, name="guard-host"):
    r = client.post("/api/hosts", json={
        "name": name, "hostname": "192.0.2.99", "port": 22,
        "username": "root", "password": "x"})
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()["id"]


def _enable(client, on=True):
    r = client.post("/api/op-guard/config", json={"verify_code_enabled": on})
    assert r.status_code == 200
    assert r.get_json()["verify_code_enabled"] is on


def _get_code(client):
    r = client.get("/api/verify-code")
    assert r.status_code == 200
    code = r.get_json()["code"]
    assert isinstance(code, str) and len(code) == 4 and code.isdigit()
    return code


# ---- 配置与验证码下发 ----

def test_config_default_off(client):
    r = client.get("/api/op-guard/config")
    assert r.status_code == 200
    assert r.get_json()["verify_code_enabled"] is False


def test_verify_code_issue(client):
    assert _get_code(client)


# ---- 开关关闭：零破坏 ----

def test_gate_off_passthrough(client):
    hid = _create_host(client, "gate-off-host")
    r = client.delete(f"/api/hosts/{hid}")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


# ---- 开关开启：验证码闸 ----

def test_gate_on_requires_code(client):
    hid = _create_host(client, "gate-on-host")
    _enable(client)
    try:
        r = client.delete(f"/api/hosts/{hid}")
        assert r.status_code == 403
        body = r.get_json()
        assert body["verify_code_required"] is True
        # 错码也拒
        r = client.delete(f"/api/hosts/{hid}", json={"verify_code": "0000"})
        assert r.status_code == 403
        # 对码放行（一次性消耗）
        code = _get_code(client)
        r = client.delete(f"/api/hosts/{hid}", json={"verify_code": code})
        assert r.status_code == 200
        # 同码重放：拒绝（一次性）
        r = client.delete(f"/api/hosts/{hid}", json={"verify_code": code})
        assert r.status_code in (403, 404)
    finally:
        _enable(client, False)


def test_gate_cleanup_run_blocks_real_run(client):
    _enable(client)
    try:
        # dry_run 预览不拦
        r = client.post("/api/backup-cleanup/run", json={"dry_run": True})
        assert r.status_code == 200
        # 真实执行无码 → 403
        r = client.post("/api/backup-cleanup/run", json={"dry_run": False})
        assert r.status_code == 403
        assert r.get_json()["verify_code_required"] is True
    finally:
        _enable(client, False)


def test_gate_restore_blocks_without_code(client):
    _enable(client)
    try:
        r = client.post("/api/restores", json={"record_id": 999999})
        assert r.status_code == 403
        assert r.get_json()["verify_code_required"] is True
    finally:
        _enable(client, False)


# ---- 资源受保护标记 ----

def test_protect_toggle_and_flag(client):
    hid = _create_host(client, "protect-host")
    from core import ssh_hosts
    r = client.post(f"/api/hosts/{hid}/protect", json={"protected": True, "note": "生产库"})
    assert r.status_code == 200 and r.get_json()["protected"] is True
    assert ssh_hosts.is_protected(hid) is True
    assert ssh_hosts.get_host(hid)["protected"] == 1
    assert ssh_hosts.get_host(hid)["protected_note"] == "生产库"
    # 列表透出
    rows = client.get("/api/hosts").get_json()
    hit = [h for h in rows if h["id"] == hid][0]
    assert hit["protected"] == 1
    # 解除
    r = client.post(f"/api/hosts/{hid}/protect", json={"protected": False})
    assert r.get_json()["protected"] is False
    assert ssh_hosts.is_protected(hid) is False


def test_restore_blocked_by_protected_host(client, monkeypatch):
    """run_restore_now 对受保护目标主机抛 ValueError（覆盖全部恢复入口）。"""
    from core import scheduler, ssh_hosts
    import core.models as models

    monkeypatch.setattr(models, "get_record",
                        lambda rid: {"id": rid, "task_id": 1, "backup_path": "/tmp/x"})
    monkeypatch.setattr(models, "get_task",
                        lambda tid, include_secret=False: {"id": tid, "name": "t"})
    monkeypatch.setattr(ssh_hosts, "get_host", lambda hid, include_secret=False: {
        "id": hid, "name": "prod", "host_key": "root@10.0.0.1:22",
        "hostname": "10.0.0.1", "port": 22, "protected": 1})
    with pytest.raises(ValueError, match="受保护"):
        scheduler.run_restore_now(1, target_host_id=7)
    # 未保护主机不拦（走到后续逻辑前抛出其它错误也算放行过了闸）
    monkeypatch.setattr(ssh_hosts, "get_host", lambda hid, include_secret=False: {
        "id": hid, "name": "normal", "host_key": "root@10.0.0.2:22",
        "hostname": "10.0.0.2", "port": 22, "protected": 0})
    try:
        scheduler.run_restore_now(1, target_host_id=7)
    except ValueError as e:
        assert "受保护" not in str(e)
    except Exception:
        pass  # 闸已放行，后续失败允许
