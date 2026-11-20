"""并发审批只能启动一个克隆执行器。"""
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import Mock
import config
from core import db, models, clone_preflight
from core.clone_service import CloneService


def test_parallel_approvals_have_one_provisioner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DB_PATH", str(tmp_path / "clones.db"))
    monkeypatch.setattr(db, "_ACTIVE", dict(backend="sqlite"))
    db.init_schema()
    request_id = models.create_clone_request({"source_record_id":1,"target_env":"test",
                                             "status":"pending","requested_by":"qa"})
    monkeypatch.setattr(models, "get_record", lambda _: {"id":1,"db_type":"mysql"})
    monkeypatch.setattr(clone_preflight, "preflight", lambda *a,**k: {"ok":True})
    svc = CloneService()
    def launch(req):
        time.sleep(0.05)
        return 123
    launch_mock = Mock(side_effect=launch)
    monkeypatch.setattr(svc, "_launch_vdb", launch_mock)
    def approve(_):
        try:
            return svc.approve_clone(request_id)["status"]
        except ValueError:
            return "already_claimed"
        finally:
            db.reset_thread_conn()
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(approve, range(16)))
    assert launch_mock.call_count == 1
    assert models.get_clone_request(request_id)["status"] == "ready"
