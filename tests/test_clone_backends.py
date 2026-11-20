"""新增克隆后端的资源所有权与失败回收契约。"""
from unittest.mock import Mock
import pytest
from core import clone_backends as backends
from core.clone_service import CloneService
import config


def test_capabilities_cover_every_database_engine():
    from core.engines import ENGINE_REGISTRY
    kinds = {r["db_type"] for r in backends.capabilities()}
    assert kinds == set(ENGINE_REGISTRY) - {"file", "synology", "object_storage", "vm"}
    assert all(r["agentless"] and not r["live_mount"] for r in backends.capabilities())


@pytest.mark.parametrize("kind", sorted(backends.ENGINE_TYPES))
def test_drop_rejects_foreign_resources_before_connecting(kind):
    with pytest.raises(ValueError, match="clone_N"):
        backends.operate(kind, "host", 1234, "production", "root", "pw", drop=True)


def test_restore_failure_preserves_allocated_resource(monkeypatch):
    from core import engines
    monkeypatch.setattr(backends, "target_task", lambda *args: {})
    conn = Mock()
    conn.cursor.return_value.fetchall.return_value = []
    monkeypatch.setattr(backends, "_sql_conn", lambda *args: conn)
    engine = Mock()
    engine.restore.return_value = Mock(success=False, simulated=False, message="restore failed")
    monkeypatch.setattr(engines, "get_engine", lambda *args: engine)
    allocated = Mock()
    with pytest.raises(RuntimeError, match="restore failed"):
        backends.launch({"db_type": "opengauss", "backup_path": "/backup"},
                        "clone_12", "host", 26000, "omm", "pw", "/tmp", allocated)
    allocated.assert_called_once()
    engine.restore.assert_called_once_with("/backup", target_db="clone_12")


def test_cross_host_source_ssh_cannot_be_reused(monkeypatch):
    monkeypatch.setattr(backends.models, "get_task", lambda *a, **k: {"host": "source"})
    with pytest.raises(ValueError, match="目标实例任务"):
        backends.target_task({"task_id": 1}, "target", 5432, "pw")


def test_failed_drop_keeps_metadata_and_request_retryable(monkeypatch):
    from core import clone_service as service
    request = {"id": 1, "status": "ready", "vdb_instance_id": 7}
    vdb = {"id": 7, "name": "clone_1", "database_name": "clone_1",
           "db_type": "mongodb", "host": "host", "port": 27017, "username": "u"}
    monkeypatch.setattr(config, "DEMO_MODE", "off")
    monkeypatch.setattr(service.models, "get_clone_request", lambda *a, **k: request)
    monkeypatch.setattr(service.models, "get_vdb", lambda *a: vdb)
    monkeypatch.setattr(CloneService, "_vdb_password", staticmethod(lambda *a: "pw"))
    monkeypatch.setattr(backends, "operate", Mock(side_effect=RuntimeError("offline")))
    delete = Mock()
    update = Mock()
    monkeypatch.setattr(service.models, "delete_vdb", delete)
    monkeypatch.setattr(service.models, "update_clone_request", update)
    with pytest.raises(ValueError, match="保留台账"):
        CloneService().destroy_clone(1)
    delete.assert_not_called()
    update.assert_not_called()
