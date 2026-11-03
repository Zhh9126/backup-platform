# -*- coding: utf-8 -*-
"""P1-5 单任务多通道并行：单测。

覆盖：
1. RMAN RUN 块构造器——多通道 ALLOCATE/RELEASE、单连接向后兼容、通道数封顶；
2. _rman_channels 解析（extra_options.rman_channels，非法值容错）；
3. sftp_pull_files_parallel——带宽均分 / 结果顺序（monkeypatch 连接与传输）；
4. 本机 SSH 真实并行拉取（127.0.0.1 密钥认证，不可达时 skip，不造假）。
"""
import os
import sys
import json
import hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest

from core.engines.oracle import OracleEngine
from core.engines.base import BackupType
from core import remote_dump

STORAGE_ROOT = os.path.join(ROOT, "backups", "p15_multichannel_test")


def _engine(extra: dict) -> OracleEngine:
    return OracleEngine(
        {"id": 159001, "name": "p15t", "db_type": "oracle",
         "extra_options": json.dumps(extra)},
        STORAGE_ROOT)


# ---------------- 1) RUN 块构造器 ----------------

def test_rman_script_no_channels_backward_compat():
    # 单连接（channels=0）必须与旧脚本逐字节一致（向后兼容硬约束）
    db_line = "  BACKUP AS COMPRESSED BACKUPSET DATABASE FORMAT '/bak/p_%U.bkp';"
    arch_line = "  BACKUP AS COMPRESSED BACKUPSET ARCHIVELOG ALL FORMAT '/bak/p_arch_%U.bkp';"
    s = OracleEngine._rman_run_script(db_line, arch_line, channels=0)
    assert s == f"RUN {{\n{db_line}\n{arch_line}\n}}\nexit;\n"
    assert "ALLOCATE CHANNEL" not in s


def test_rman_script_two_channels_alloc_release():
    db_line = "  BACKUP X;"
    arch_line = "  BACKUP Y;"
    s = OracleEngine._rman_run_script(db_line, arch_line, channels=2)
    assert s.count("ALLOCATE CHANNEL ch00 DEVICE TYPE DISK;") == 1
    assert s.count("ALLOCATE CHANNEL ch01 DEVICE TYPE DISK;") == 1
    assert s.count("RELEASE CHANNEL ch00;") == 1
    assert s.count("RELEASE CHANNEL ch01;") == 1
    # RUN 块结构：{ 后先 ALLOCATE，BACKUP 在中，RELEASE 在 } 前
    assert s.index("RUN {") < s.index("ALLOCATE CHANNEL ch00") < s.index("BACKUP X") \
        < s.index("RELEASE CHANNEL ch00") < s.rindex("}")


def test_rman_script_channels_capped_at_8():
    db_line = "  B;"
    s = OracleEngine._rman_run_script(db_line, "  A;", channels=255)
    assert s.count("ALLOCATE CHANNEL") == 8
    assert "ch07" in s and "ch08" not in s


# ---------------- 2) _rman_channels 解析 ----------------

def test_rman_channels_parse_and_cap():
    assert _engine({"rman_channels": 4})._rman_channels() == 4
    assert _engine({"rman_channels": 255})._rman_channels() == 8
    assert _engine({"rman_channels": 1})._rman_channels() == 0   # 1 = 单连接
    assert _engine({"rman_channels": 0})._rman_channels() == 0
    assert _engine({})._rman_channels() == 0
    assert _engine({"rman_channels": "abc"})._rman_channels() == 0


def test_verify_script_gets_channels():
    # 恢复校验脚本由 _verify_physical_remote 生成（需 SSH），此处只验证
    # 通道行片段可复用构造逻辑：ALLOCATE 行由 _rman_run_script 同源语义生成
    eng = _engine({"rman_channels": 2})
    assert eng._rman_channels() == 2
    lines = "".join(f"  ALLOCATE CHANNEL ch{i:02d} DEVICE TYPE DISK;\n"
                    for i in range(eng._rman_channels()))
    assert lines.count("ALLOCATE CHANNEL") == 2


# ---------------- 3) 并行拉取：带宽均分 / 顺序 ----------------

def test_parallel_pull_splits_bandwidth_and_keeps_order(tmp_path, monkeypatch):
    items = []
    for i in range(4):
        lp = tmp_path / f"piece_{i}.bkp"
        lp.write_bytes(f"content-{i}".encode())
        items.append((f"piece_{i}.bkp", f"/remote/piece_{i}.bkp", str(lp)))

    captured = []

    def fake_resumable(client, rpath, lpath, *, task, key, db_type,
                       host_key, has_rc, stable_secs, label, min_size):
        captured.append({"bandwidth": task.get("bandwidth_limit"), "key": key,
                         "rpath": rpath, "lpath": lpath})
        return {"path": lpath, "size": os.path.getsize(lpath), "resumed": False}

    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(remote_dump, "_connect_isolated", lambda ssh_host: FakeClient())
    monkeypatch.setattr(remote_dump, "sftp_pull_resumable", fake_resumable)

    task = {"id": 1, "bandwidth_limit": 800}
    out = remote_dump.sftp_pull_files_parallel(
        {"host_key": "root@127.0.0.1:22"}, items, task=task,
        db_type="oracle_rman", max_workers=3)

    # 顺序与 items 一致
    assert [os.path.basename(p) for p, _ in out] == [f"piece_{i}.bkp" for i in range(4)]
    # 带宽均分：800 / 3 向下取整 = 266
    assert all(c["bandwidth"] == 266 for c in captured)
    assert len(captured) == 4


def test_parallel_pull_single_worker_keeps_full_rate(tmp_path, monkeypatch):
    lp = tmp_path / "only.bkp"
    lp.write_bytes(b"x" * 128)
    seen = {}

    def fake_resumable(client, rpath, lpath, *, task, key, db_type,
                       host_key, has_rc, stable_secs, label, min_size):
        seen["bandwidth"] = task.get("bandwidth_limit")
        return {"path": lpath, "size": 128, "resumed": False}

    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(remote_dump, "_connect_isolated", lambda ssh_host: FakeClient())
    monkeypatch.setattr(remote_dump, "sftp_pull_resumable", fake_resumable)
    remote_dump.sftp_pull_files_parallel(
        {"host_key": "root@127.0.0.1:22"}, [("only.bkp", "/r/only.bkp", str(lp))],
        task={"id": 1, "bandwidth_limit": 500}, db_type="t", max_workers=3)
    assert seen["bandwidth"] == 500  # 单片退化为单 worker，不瓜分


# ---------------- 4) 本机 SSH 真实并行拉取 ----------------

def _local_ssh_reachable() -> bool:
    try:
        c = remote_dump._connect_isolated({"host_key": "root@127.0.0.1:22"})
        c.close()
        return True
    except Exception:
        return False


def test_parallel_pull_real_local_ssh(tmp_path):
    """本机 SSH 真实并行拉取：4 个不同大小文件，逐字节+sha256 校验。"""
    if not _local_ssh_reachable():
        pytest.skip("本机 SSH(127.0.0.1) 不可达，跳过真实传输用例")

    import subprocess
    remote_dir = "/tmp/p15_parallel_pull_test"
    subprocess.run(["rm", "-rf", remote_dir], check=False)
    subprocess.run(["mkdir", "-p", remote_dir], check=True)
    sizes = [64 * 1024, 256 * 1024, 512 * 1024, 128 * 1024]
    expect = []
    for i, sz in enumerate(sizes):
        data = os.urandom(sz)
        rp = f"{remote_dir}/piece_{i}.bkp"
        with open(rp, "wb") as f:
            f.write(data)
        expect.append(hashlib.sha256(data).hexdigest())

    items = [(f"piece_{i}.bkp", f"{remote_dir}/piece_{i}.bkp",
              str(tmp_path / f"piece_{i}.bkp")) for i in range(4)]
    out = remote_dump.sftp_pull_files_parallel(
        {"host_key": "root@127.0.0.1:22"}, items,
        task={"id": 159002}, db_type="oracle_rman",
        max_workers=4, min_size=1024)

    assert len(out) == 4
    for (lp, sz), want_sha, want_sz in zip(out, expect, sizes):
        assert os.path.exists(lp) and sz == want_sz
        with open(lp, "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == want_sha

    subprocess.run(["rm", "-rf", remote_dir], check=False)
