# -*- coding: utf-8 -*-
"""物理增量链恢复校验回归。

2026-11-11 真机（MySQL170 全量 + 2 层增量）暴露：恢复校验策略取「最近一次
成功记录」= 增量包，而 verify_record 旧实现只对该增量包单独 --prepare，
必然报 "This target seems not to have correct metadata"。修复后校验通道
复用生产恢复的链组装语义（全量基备 + 时间序增量逐层合并）。
"""
import logging
import os

from core.engines.base import BackupMode, BackupStatus
from core.engines.mysql import MySQLEngine


def _engine(tmp_path):
    task = {
        "id": 9001, "name": "verify-chain-test", "db_type": "mysql",
        "host": "127.0.0.1", "port": 3306, "username": "root",
        "password": "", "db_name": "testdb", "backup_mode": "physical",
        "extra_options": "{}",
    }
    return MySQLEngine(task, str(tmp_path), logging.getLogger("test_verify_chain"))


def _artifacts(tmp_path, names):
    out = tmp_path / "artifacts"
    out.mkdir()
    for n in names:
        (out / n).write_bytes(b"fake")
    return out


def _stub(eng, calls):
    def materialize(src, dest, timeout=7200):
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "xtrabackup_checkpoints"), "w") as f:
            f.write("backup_type = full-prepared\n")

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return {"returncode": 0, "stdout": "", "stderr": ""}

    eng._materialize_xb = materialize
    eng._decompress_xtrabackup_dir = lambda work: 0
    eng._restore_parallel = lambda: 1
    eng._is_maria_tool = lambda path: False
    eng._run = run


def test_incremental_chain_is_prepared_as_whole(tmp_path):
    eng = _engine(tmp_path)
    calls = []
    _stub(eng, calls)
    out = _artifacts(tmp_path, [
        "xtrabackup_full_20261111_120000.tar.gz",
        "xtrabackup_inc_20261111_120100.tar.gz",
        "xtrabackup_inc_20261111_120200.tar.gz",
    ])
    target = str(out / "xtrabackup_inc_20261111_120200.tar.gz")

    res = eng._verify_physical_prepare(target, "/fake/xtrabackup",
                                       str(tmp_path / "verify"))

    assert res.success is True
    assert res.status == BackupStatus.SUCCESS
    assert "2 层增量" in res.message
    # 基备 prepare + 2 层增量 + 最终 prepare = 4 次
    assert len(calls) == 4
    assert "--apply-log-only" in calls[0]                      # 基备
    assert "--apply-log-only" in calls[1]                      # 中间增量层
    assert any(a.startswith("--incremental-dir=") for a in calls[1])
    assert "--apply-log-only" not in calls[2]                  # 最后一层完整 prepare
    assert any(a.startswith("--incremental-dir=") for a in calls[2])
    assert not any(a.startswith("--incremental-dir=") for a in calls[3])
    assert all(any(a.startswith("--target-dir=") for a in c) for c in calls)


def test_incremental_without_base_fails_clearly(tmp_path):
    eng = _engine(tmp_path)
    _stub(eng, [])
    out = _artifacts(tmp_path, ["xtrabackup_inc_20261111_120200.tar.gz"])

    res = eng._verify_physical_prepare(
        str(out / "xtrabackup_inc_20261111_120200.tar.gz"),
        "/fake/xtrabackup", str(tmp_path / "verify"))

    assert res.success is False
    assert "未找到全量基备" in res.message


def test_full_artifact_verify_has_no_incremental_dir(tmp_path):
    eng = _engine(tmp_path)
    calls = []
    _stub(eng, calls)
    out = _artifacts(tmp_path, ["xtrabackup_full_20261111_120000.tar.gz"])

    res = eng._verify_physical_prepare(
        str(out / "xtrabackup_full_20261111_120000.tar.gz"),
        "/fake/xtrabackup", str(tmp_path / "verify"))

    assert res.success is True
    assert len(calls) == 1
    assert not any(a.startswith("--incremental-dir=") for a in calls[0])


def test_verify_record_accepts_directory_artifact(tmp_path):
    """目录型物理产物：旧实现 os.path.getsize 抛 IsADirectoryError。"""
    eng = _engine(tmp_path)
    artifact = tmp_path / "xtrabackup_full_dir"
    artifact.mkdir()
    (artifact / "ibdata1").write_bytes(b"data" * 100)

    eng._pick_physical_bin = lambda version: ("/bin/true", "fake")
    eng._verify_physical_prepare = lambda path, xb, temp: type(
        "R", (), {"success": True, "status": BackupStatus.SUCCESS,
                  "message": "stub", "size_bytes": 0})()

    res = eng.verify_record({"backup_path": str(artifact),
                             "backup_mode": BackupMode.PHYSICAL})
    assert res.success is True
    assert res.size_bytes == 400
