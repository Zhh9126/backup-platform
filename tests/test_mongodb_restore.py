# -*- coding: utf-8 -*-
"""MongoDB 备份/恢复真机暴露的缺陷回归（2026-11-13）。

首次对 MongoDB 做真机"备份→恢复→核对"时连爆三个问题：

1. **空产物假成功**：数据库主机没有 mongodump，远端 `mongodump | zstd` 管道
   的退出码取的是 zstd 的状态 → 13 字节空归档被当成"备份成功"。
   → 远端 shell 加 `set -o pipefail`；引擎再校验产物体积下限。
2. **archive 模式忽略 --db**：`mongorestore --archive=... --db=新库` 实际仍写回
   归档里的源库 namespace，与已有 _id 冲突 → 2 个文档全部失败，但 rc=0。
   → 改用 `--nsFrom/--nsTo` 重命名；并按输出统计判定失败。
3. **认证库缺失**：账号建在 admin 库，任务没填 authSource 时 mongodump 认证失败。
   → 用户名非空且未配置时默认 `--authenticationDatabase admin`。
"""
import re
from pathlib import Path

from core.engines.mongodb import MongoEngine

SRC = Path(__file__).resolve().parent.parent


def _engine(extra="{}", username="root"):
    t = {"id": 1, "name": "m", "db_type": "mongodb", "host": "127.0.0.1",
         "port": 27017, "username": username, "password": "x",
         "db_name": "src_db", "extra_options": extra}
    eng = MongoEngine(t, "/tmp")
    return eng


def test_auth_database_defaults_to_admin():
    cmd = _engine()._build_auth_args([])
    assert cmd == ["--authenticationDatabase", "admin"]


def test_auth_database_explicit_override():
    cmd = _engine('{"authSource": "myauth"}')._build_auth_args([])
    assert cmd == ["--authenticationDatabase", "myauth"]


def test_auth_database_skipped_without_username():
    cmd = _engine(username="")._build_auth_args([])
    assert cmd == []


def test_remote_shell_uses_pipefail():
    """mongodump | zstd 必须 pipefail，否则上游失败会被下游吞掉。"""
    src = (SRC / "core" / "remote_dump.py").read_text(encoding="utf-8")
    seg = src.split("def _remote_mongodb_dump_shell", 1)[1][:4000]
    assert "set -o pipefail" in seg, "MongoDB 远端导出缺 pipefail"
    # pipefail 必须放在管道命令之前
    idx = seg.find("set -o pipefail")
    pipe = seg.find("| zstd")
    assert pipe > idx > 0


def test_restore_uses_nsfrom_nsto_for_archive(tmp_path, monkeypatch):
    """archive 模式恢复到异名库：必须用 --nsFrom/--nsTo，且不能带 --db。"""
    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ("2026-11-13T23:47:31+0800 finished restoring e2e_mongo_r.t1 "
                  "(2 documents, 0 failures)\n"
                  "2 document(s) restored successfully. 0 document(s) failed to restore.\n")

    def fake_run(cmd):
        calls.append(list(cmd))
        return {"returncode": 0, "stdout": _R.stdout, "stderr": _R.stderr}

    eng = _engine()
    monkeypatch.setattr(eng, "_run", fake_run)
    monkeypatch.setattr(eng, "check_client", lambda: (True, "ok"))
    monkeypatch.setattr(eng, "_build_auth_args", lambda c: c)

    f = tmp_path / "x.archive"
    f.write_bytes(b"archive")
    res = eng.restore(str(f), target_db="dst_db")
    assert res.success, res.message
    cmd = calls[0]
    assert "--nsFrom=src_db.*" in cmd and "--nsTo=dst_db.*" in cmd, cmd
    assert "--db" not in cmd, "archive 模式下 --db 会被忽略，不应出现"
    assert any(a.startswith("--archive=") for a in cmd)


def test_restore_fails_when_documents_failed(tmp_path, monkeypatch):
    """rc=0 但存在 failed to restore 时，必须判失败（防假成功）。"""
    stderr = ("2026-11-13T23:47:31+0800 finished restoring src.t1 "
              "(0 documents, 2 failures)\n"
              "0 document(s) restored successfully. 2 document(s) failed to restore.\n")

    eng = _engine()
    monkeypatch.setattr(eng, "_run",
                        lambda cmd: {"returncode": 0, "stdout": "", "stderr": stderr})
    monkeypatch.setattr(eng, "check_client", lambda: (True, "ok"))
    monkeypatch.setattr(eng, "_build_auth_args", lambda c: c)
    f = tmp_path / "x.archive"
    f.write_bytes(b"archive")
    res = eng.restore(str(f), target_db="dst_db")
    assert not res.success
    assert "写入失败" in res.message
