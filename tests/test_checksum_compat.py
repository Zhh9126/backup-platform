"""历史及带算法前缀的摘要都应保留真实篡改检测。"""
import pytest
from core import db, backup_anomaly


@pytest.mark.parametrize("prefix", ["", "sha256:", "SHA256:"])
def test_sha256_format_does_not_quarantine_intact_backup(tmp_path, prefix):
    path = tmp_path / "backup.sql"
    path.write_text("CREATE TABLE orders (id INTEGER);\n")
    record = {"id": 1, "db_type": "mysql", "backup_path": str(path),
              "checksum": prefix + db.sha256_file(str(path))}
    result = backup_anomaly.check_record(record)
    integrity = next(c for c in result["checks"] if c["item"] == "integrity")
    assert integrity["level"] == "clean"
    path.write_text("changed content")
    result = backup_anomaly.check_record(record)
    integrity = next(c for c in result["checks"] if c["item"] == "integrity")
    assert integrity["level"] == "suspect"
