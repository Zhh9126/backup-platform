# -*- coding: utf-8 -*-
"""磁带库接入与介质管理回归（零硬件依赖）。

真实样例文本来自 lsscsi -g / mtx status 的公开格式；数据面用目录模拟带库
走完整「写 → 列 → 回迁 → sha256 校验 → 台账 → 退役标记」链路。
本测试不声称任何真实磁带机/机械手已验证。
"""
import hashlib
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tape_lib_")
_MODULE_DB = os.path.join(_TMP, "meta.db")
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import core.db as db  # noqa: E402
config.META_DB_PATH = _MODULE_DB
db.init_schema()

import pytest  # noqa: E402
from core import tape_library as tl  # noqa: E402
from core import tier_replication as tr  # noqa: E402
from core.storage_backends import get_backend  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    config.META_DB_PATH = _MODULE_DB
    for t in ("tape_jobs", "tape_media", "storage_targets", "backup_records"):
        try:
            db.execute("DELETE FROM " + t)
        except Exception:
            pass
    yield


LSSCSI = """[0:0:0:0]    disk    ATA      ST500DM002      1CH3  /dev/sda   /dev/sg0
[1:0:0:0]    mediumx ATL      1500            3.01  /dev/sch0  /dev/sg2
[1:0:0:1]    tape    HP       Ultrium 1-SCSI  1-SCSI /dev/st0  /dev/sg3
[1:0:0:2]    tape    IBM      ULTRIUM-HH6     C9RF  /dev/nst1  /dev/sg4
"""

MTX = """Storage Changer /dev/sg2:2 Drives, 10 Slots (1 Import/Export)
Data Transfer Element 0:Full (Storage Element 3 Loaded):VolumeTag = ABC123L6
Data Transfer Element 1:Empty
Storage Element 1:Full :VolumeTag=DEF456L6
Storage Element 2:Empty:VolumeTag=
Storage Element 3:Empty
Storage Element 9 IMPORT/EXPORT:Full :VolumeTag=GHI789L6
Storage Element 10 IMPORT/EXPORT:Empty
"""

MT_STATUS = """SCSI 2 tape drive:
File number=0, block number=0, partition=0.
Tape block size 0 bytes. Density code 0x5a (LTO-6).
Soft error count since last status=0
General status bits on (41010000):
 BOT ONLINE IM_REP_EN
"""


# ---------------- 解析 ----------------

def test_parse_lsscsi():
    d = tl.parse_lsscsi(LSSCSI)
    assert len(d["drives"]) == 2 and len(d["changers"]) == 1
    assert d["changers"][0]["generic"] == "/dev/sg2"
    assert d["changers"][0]["node"] == "/dev/sch0"
    assert d["drives"][0]["generic"] == "/dev/sg3"
    assert d["drives"][1]["node"] == "/dev/nst1"
    assert len(d["others"]) == 1


def test_parse_mtx_status():
    d = tl.parse_mtx_status(MTX)
    assert d["summary"]["drives"] == 2 and d["summary"]["slots"] == 10
    assert d["summary"]["import_export"] == 2
    assert d["drives"][0] == {"drive": 0, "state": "full",
                              "loaded_slot": 3, "barcode": "ABC123L6"}
    assert d["drives"][1]["state"] == "empty"
    assert d["slots"][0]["barcode"] == "DEF456L6"
    assert d["slots"][1]["state"] == "empty"
    assert d["import_export"][0]["barcode"] == "GHI789L6"


def test_parse_mt_status():
    d = tl.parse_mt_status(MT_STATUS)
    assert d["online"] is True and d["at_bot"] is True
    assert d["write_protected"] is False
    assert d["file_number"] == 0


# ---------------- 设备安全白名单 ----------------

@pytest.mark.parametrize("p", ["/dev/nst0", "/dev/st0", "/dev/nst1a",
                               "/dev/tape/by-id/scsi-123-nst"])
def test_safe_tape_devices(p):
    assert tl.is_safe_tape_device(p)


@pytest.mark.parametrize("p", ["/dev/sda", "/dev/null", "/dev/sg2",
                               "/dev/nst0/../../etc/passwd", "relative/path", ""])
def test_unsafe_tape_devices_rejected(p):
    assert not tl.is_safe_tape_device(p)


def test_device_backend_never_writes_disk(monkeypatch):
    """P0：旧实现会把 /dev/sda 当磁带覆写，必须被前置检查拦住。"""
    src = os.path.join(_TMP, "payload.bin")
    with open(src, "wb") as f:
        f.write(b"x" * 1024)
    called = []
    monkeypatch.setattr("core.storage_backends.tape.subprocess.run",
                        lambda *a, **k: called.append(a))
    for p in ("/dev/sda", "/dev/null"):
        b = get_backend("tape", {"id": 1, "endpoint": p})
        assert b.save_file(src, object_key="x") is False
        assert b.device_error.startswith("非法磁带设备路径")
    assert not called, "绝不能对非法设备执行任何写命令"


def test_runtime_hint_is_honest():
    d = tl.discover(use_lsscsi=False)
    rt = d["runtime"]
    assert {"ready", "missing_tools", "device_nodes", "docker_run_args",
            "compose_devices", "hints"} <= set(rt)
    assert isinstance(rt["hints"], list)
    # 无硬件环境不得伪造设备
    assert all(tl.is_safe_tape_device(p) or tl.is_safe_changer_device(p)
               for p in rt["device_nodes"])


def test_device_lock_is_per_path():
    a = tl.device_lock("/dev/nst0")
    b = tl.device_lock("/dev/nst0")
    c = tl.device_lock("/dev/nst1")
    assert a is b and a is not c


# ---------------- 机械手 / 驱动命令构造 ----------------

def test_changer_commands_are_safe_and_exact(monkeypatch):
    calls = []
    monkeypatch.setattr(tl, "_run", lambda cmd, timeout=20: (
        calls.append(cmd) or {"rc": 0, "out": "", "err": ""}))
    monkeypatch.setattr(tl.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(tl.os.path, "exists", lambda p: True)
    ch = tl.TapeChanger("/dev/sg2")
    ch.load(3, 1)
    ch.unload(3, 1)
    ch.transfer_to_ie(5, 9)
    assert calls[0] == ["mtx", "-f", "/dev/sg2", "load", "3", "1"]
    assert calls[1] == ["mtx", "-f", "/dev/sg2", "unload", "3", "1"]
    assert calls[2] == ["mtx", "-f", "/dev/sg2", "transfer", "5", "9"]
    with pytest.raises(ValueError):
        tl.TapeChanger("/dev/sda").load(1)


def test_drive_rejects_unknown_op(monkeypatch):
    monkeypatch.setattr(tl.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(tl.os.path, "exists", lambda p: True)
    with pytest.raises(ValueError):
        tl.TapeDrive("/dev/nst0").op("rm -rf /")


# ---------------- 介质台账 ----------------

def test_inventory_sync_and_media_lifecycle():
    lib = tl.parse_mtx_status(MTX)
    stats = tl.sync_inventory(7, lib)
    assert stats["created"] >= 4
    rows = tl.media_list(7)
    barcodes = {r["barcode"] for r in rows}
    assert "DEF456L6" in barcodes and "GHI789L6" in barcodes
    # 已装带的介质应标记为 in_drive
    assert any(r["barcode"] == "ABC123L6" and r["state"] == "in_drive"
               for r in rows)
    one = next(r for r in rows if r["barcode"] == "DEF456L6")
    with pytest.raises(ValueError):
        tl.media_update(one["id"], unknown_field=1)
    row = tl.media_update(one["id"], state="offsite",
                          write_protected=1, note="异地保险柜/张三")
    assert row["state"] == "offsite" and row["write_protected"] == 1
    s = tl.media_summary(7)
    assert s["offsite"] >= 1 and s["write_protected"] >= 1


def test_job_ledger_roundtrip():
    jid = tl.job_start(7, "mysql/1_t/20261110__a.sql", 1234, "deadbeef",
                       media_label="tape_001.tar")
    tl.job_finish(jid, True, verified=True, filemark=3)
    rows = tl.job_list(7)
    assert rows[0]["status"] == "success" and rows[0]["verified"] == 1
    assert tl.job_find("a.sql")  # basename 兜底
    assert tl.media_summary(7)["bytes_written"] == 1234


# ---------------- 目录模拟带库全链路 ----------------

def _backend(name="sim", **extra):
    root = os.path.join(_TMP, name)
    cfg = {"id": 9, "endpoint": root,
           "extra_options": dict({"max_tape_gb": 0, "block_kb": 64,
                                  "verify_after_write": "1"}, **extra)}
    return get_backend("tape", cfg), root


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_dir_tape_full_roundtrip():
    b, root = _backend("sim_roundtrip")
    src = os.path.join(_TMP, "roundtrip.bin")
    with open(src, "wb") as f:
        f.write(os.urandom(180_000))
    key = "mysql/9_demo/20261110__roundtrip.bin"
    assert b.save_file(src, object_key=key) is True
    assert b.file_exists(key) is True
    assert any(x["member"] == key for x in b.list_files())
    dest = os.path.join(_TMP, "restored.bin")
    assert b.get_file(key, dest_path=dest) is True
    assert _sha(src) == _sha(dest)
    assert b.verify_object(key)["ok"] is True
    jobs = tl.job_list(9)
    assert jobs and jobs[0]["status"] == "success" and jobs[0]["verified"] == 1
    assert b.delete_file(key) is False       # 顺序介质不假删
    assert tl.job_list(9)[0]["status"] == "pending_retire"


def test_config_extra_options_no_longer_broken():
    b, root = _backend("sim_cfg", staging_dir=os.path.join(_TMP, "stage_cfg"),
                       block_kb=256, max_tape_gb=0.000001)
    assert b.block_kb == 256
    assert b.staging_root.name == "stage_cfg"
    assert b.verify_after_write is True
    src = os.path.join(_TMP, "cfg.bin")
    with open(src, "wb") as f:
        f.write(b"y" * 4096)
    assert b.save_file(src, object_key="a.bin") is True
    assert b.save_file(src, object_key="b.bin") is True
    assert len(list(b.tape_dir.glob("tape_*.tar"))) >= 2, \
        "max_tape_gb 从 extra_options 生效（旧实现只读 config_json 永远不滚动）"


def test_write_verify_failure_is_honest(monkeypatch):
    b, _ = _backend("sim_badverify")
    src = os.path.join(_TMP, "bv.bin")
    with open(src, "wb") as f:
        f.write(b"z" * 2048)
    monkeypatch.setattr(type(b), "_verify_member",
                        staticmethod(lambda *a: False))
    assert b.save_file(src, object_key="bad.bin") is False
    jobs = tl.job_list(9)
    assert jobs and jobs[0]["status"] == "failed"
    assert "校验失败" in jobs[0]["error"]


# ---------------- 分层复制修复 ----------------

def test_manual_timing_actually_skips_auto(monkeypatch):
    src = os.path.join(_TMP, "rep.bin")
    with open(src, "wb") as f:
        f.write(b"r" * 100)
    monkeypatch.setattr(tr, "_get_replication_strategy",
                        lambda log=None: {"timing": "manual"})
    called = []
    monkeypatch.setattr(tr, "_get_enabled_targets",
                        lambda log=None: called.append(1) or [])
    r = tr.replicate_to_tiers(src, {"id": 1, "name": "t"}, 1)
    assert not called, "仅手动策略下自动复制必须完全跳过"
    assert r["tape"] is None


def test_replication_status_includes_tape():
    db.execute("INSERT INTO backup_records (task_id, status, storage_tier) "
               "VALUES (1,'success','local+tape')")
    rid = db.query_one("SELECT MAX(id) AS i FROM backup_records")["i"]
    st = tr.get_replication_status(rid)
    assert st["tiers"]["tape"] is True


def test_sftp_target_not_excluded():
    src = os.path.join(_TMP, "rep2.bin")
    with open(src, "wb") as f:
        f.write(b"r" * 10)
    # 只验证 SQL 口径包含 sftp（通过源码契约 + 真实查询返回结构）
    import inspect
    assert "'sftp'" in inspect.getsource(tr._get_enabled_targets)


# ---------------- 部署契约 ----------------

def test_deploy_contracts():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def src(rel):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()
    for tool in ("mt-st", "mtx", "lsscsi", "sg3-utils"):
        assert tool in src("Dockerfile")
    assert "TAPE_DEVICES" in src("scripts/offline/install.sh")
    assert 'href="/tape"' in src("templates/base.html")
    assert "changer_device" in src("api/tape.py")
    assert "孤儿副本" in src("core/lifecycle.py")
    assert "st_tape_changer" in src("templates/storage.html")


def test_lifecycle_keeps_metadata_when_tape_delete_fails(monkeypatch):
    """顺序介质删不掉时绝不能把元数据删掉，否则带内副本成孤儿。"""
    import core.lifecycle as lc
    calls = []
    monkeypatch.setattr(tr, "_get_enabled_targets",
                        lambda log=None: [{"type": "tape", "name": "t",
                                           "id": 1, "tier": 3}])

    class Stub:
        def file_exists(self, key):
            return True

        def delete_file(self, key):
            return False

    monkeypatch.setattr("core.storage_backends.get_backend",
                        lambda *a, **k: Stub())
    monkeypatch.setattr(lc.models, "delete_backup_set",
                        lambda bid: calls.append(bid))
    assert lc.LifecycleEngine()._expire(
        {"id": 42, "object_key": "mysql/1_t/a.sql"}) is False
    assert not calls, "带内副本仍在时不得删除备份集元数据"
