# -*- coding: utf-8 -*-
"""zBackup 特性吸收回归：scan_fs / Oracle 发现 / VDB 预检 / Agent 能力协商。

真机 170/171 验证见 /root/e2e/e2e_zbackup_features.py。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="zb_feat_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)

import pytest  # noqa: E402

from core import clone_preflight, oracle_discover, scan_fs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _FakeChannel:
    def recv_exit_status(self):
        return 0


class _FakeStream:
    def __init__(self, text=""):
        self._text = text
        self.channel = _FakeChannel()

    def read(self):
        return self._text.encode()


class _FakeClient:
    """按命令关键字返回预设 stdout，用于替代真实 SSH。"""

    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def exec_command(self, cmd, timeout=30):
        self.commands.append(cmd)
        for key, text in self.responses.items():
            if key in cmd:
                return None, _FakeStream(text), _FakeStream("")
        return None, _FakeStream(""), _FakeStream("")

    def close(self):
        pass


# ---------------------------------------------------------------- scan_fs
def test_scan_fs_kind_classification():
    assert scan_fs._kind("/u01/oradata/ORCL/system01.dbf") == "oracle_artifact"
    assert scan_fs._kind("/data/redo01.log") == "db_log"
    assert scan_fs._kind("/etc/hosts") == "file"
    assert scan_fs._kind("/u01/app/oracle/product/19.0.0/dbhome_1/dbs/spfileorcl.ora") == "oracle_artifact"


def test_scan_fs_rejects_unsafe_roots():
    with pytest.raises(scan_fs.ScanFSError):
        scan_fs.scan_host({"host": "h"}, roots=["../../etc"])
    with pytest.raises(scan_fs.ScanFSError):
        scan_fs.scan_host({"host": "h"}, roots=["/data;rm -rf /"])


def test_scan_fs_builds_catalog(monkeypatch):
    cli = _FakeClient({
        "lsblk": '{"blockdevices":[{"name":"sda","type":"disk","size":"100G",'
                 '"fstype":null,"mountpoint":null,"children":[{"name":"sda1",'
                 '"type":"part","size":"100G","fstype":"xfs","mountpoint":"/"}]}]}',
        "find ": ("/data/system01.dbf\t1048576\t1730000000.0\n"
                  "/data/app.log\t2048\t1730000001.0\n"),
        "oratab": "orcl:/u01/app/oracle/product/19.0.0/dbhome_1:Y\n"
                  "#comment\n",
    })
    monkeypatch.setattr(scan_fs, "_client", lambda task: cli)
    r = scan_fs.scan_host({"host": "192.0.2.5"}, roots=["/data"])
    assert r["ok"] and r["file_count"] == 2
    assert r["files"][0]["kind"] == "oracle_artifact"
    assert r["volumes"][0]["name"] == "sda"
    assert r["oracle_homes"][0]["sid"] == "orcl"
    assert r["oracle_candidates"][0]["path"] == "/data/system01.dbf"
    assert "不读裸块" in r["method"]


def test_scan_fs_pull_requires_absolute_paths():
    with pytest.raises(scan_fs.ScanFSError):
        scan_fs.pull_objects({"host": "h"}, paths=["relative/file"])


# ---------------------------------------------------------------- Oracle
def test_oratab_parsing():
    cli = _FakeClient({"oratab": "# comment\norcl:/u01/app/oracle/product/19.0.0/dbhome_1:Y\n"
                                 "orcldg:/u01/app/oracle/product/11.2.0/dbhome_1:N\n"})
    homes = oracle_discover._oratab(cli)
    assert homes["orcl"].endswith("19.0.0/dbhome_1")
    assert homes["orcldg"].endswith("11.2.0/dbhome_1")


def test_version_from_home_path():
    assert "19.0" in oracle_discover._version(
        _FakeClient({}), "/u01/app/oracle/product/19.0.0/dbhome_1")
    assert "11.2" in oracle_discover._version(
        _FakeClient({}), "/u01/app/oracle/product/11.2.0/dbhome_1")


def test_pdbs_11g_honestly_marked_unsupported():
    cli = _FakeClient({"sqlplus": "ORA-00942: table or view does not exist\n"})
    r = oracle_discover._pdbs(cli, "/u01/app/oracle/product/11.2.0/dbhome_1",
                              "ORCL11G")
    assert r["supported"] is False and r["items"] == []
    assert "11g" in r["note"] or "v$pdbs" in r["note"]


def test_pdbs_19c_parsed():
    cli = _FakeClient({"sqlplus": "PDB$SEED|READ ONLY\nORCLPDB1|READ WRITE\n"})
    r = oracle_discover._pdbs(cli, "/u01/app/oracle/product/19.0.0/dbhome_1",
                              "ORCLCDB")
    assert r["supported"] is True
    assert {x["name"] for x in r["items"]} == {"PDB$SEED", "ORCLPDB1"}


# ---------------------------------------------------------------- VDB 预检
def test_clone_preflight_missing_artifact_rejected():
    r = clone_preflight.preflight({"status": "success", "backup_path": "/no/such/file"})
    assert r["ok"] is False
    assert any("产物不存在" in e for e in r["errors"])
    assert any(c["item"] == "source_artifact" for c in r["checks"])


def test_clone_preflight_live_clone_skips_artifact():
    r = clone_preflight.preflight({"status": "success", "backup_path": ""},
                                  clone_mode="template")
    assert r["ok"] is True
    assert any("活库克隆" in c["message"] for c in r["checks"])


def test_clone_preflight_quarantined_source_rejected(tmp_path):
    f = tmp_path / "backup.sql"
    f.write_text("select 1;", encoding="utf-8")
    r = clone_preflight.preflight(
        {"status": "success", "backup_path": str(f), "quarantined": 1})
    assert r["ok"] is False and any("隔离" in e for e in r["errors"])


def test_clone_preflight_port_check(monkeypatch, tmp_path):
    f = tmp_path / "backup.sql"
    f.write_text("data", encoding="utf-8")
    rec = {"status": "success", "backup_path": str(f)}
    monkeypatch.setattr("socket.create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    r = clone_preflight.preflight(rec, target_host="127.0.0.1", target_port=3308)
    assert r["ok"] is False and any("端口不可达" in e for e in r["errors"])
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: None)
    assert clone_preflight.preflight(
        rec, target_host="127.0.0.1", target_port=3308)["ok"] is True


# ---------------------------------------------------------------- 契约
def test_full_chain_contract():
    agent = _src("agent/backup_agent.py")
    assert '"os": sys.platform' in agent and '"features"' in agent
    client = _src("core/agent_client.py")
    assert "use_gz" in client and "zlib.decompressobj" in client

    clone = _src("core/clone_service.py")
    assert "_clone_preflight" in clone and "_cpf.preflight" in clone

    init = _src("api/__init__.py")
    assert "scan_fs)" in init or "scan_fs," in init
    perm = _src("api/perm_map.py")
    assert '"scan-fs"' in perm and "scan-fs/scan" in perm

    api = _src("api/scan_fs.py")
    assert "/scan-fs/scan" in api and "/scan-fs/pull" in api
    assert "oracle-inventory" in api
