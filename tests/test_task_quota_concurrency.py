"""创建任务的配额检查与写入必须按一个操作串行化。"""
from concurrent.futures import ThreadPoolExecutor

from core import db, license as licensing, models
import config


def test_concurrent_creates_respect_last_quota_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DB_PATH", str(tmp_path / "quota.db"))
    monkeypatch.setattr(db, "_ACTIVE", dict(backend="sqlite"))
    db.init_schema()
    monkeypatch.setattr(licensing, "FREE_MAX_QUOTA", 3)
    monkeypatch.setattr(licensing, "is_licensed", lambda: False)
    monkeypatch.setattr(licensing, "check_server_quota", lambda _: None)
    def create(index):
        try:
            models.create_task({"name": f"quota-{index}", "db_type": "mysql",
                                "host": "127.0.0.1", "db_name": "fixture"})
            return True
        except licensing.QuotaExceededError:
            return False
        finally:
            db.reset_thread_conn()
    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(create, range(32)))
    assert sum(accepted) == 3
    assert db.query_one("SELECT COUNT(*) AS n FROM backup_tasks")["n"] == 3
