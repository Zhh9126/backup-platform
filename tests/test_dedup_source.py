# -*- coding: utf-8 -*-
"""源端传输去重（core/dedup_source.py）单测。

用本机 bash 直跑指纹脚本（dd/stat/sha256sum 均为 POSIX 工具，与远端语义
一致），验证解析/重组/块库命中/损坏兜底/全局记账。
"""
import os
import subprocess
import sys
import tempfile

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="dedup_src_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
os.environ.setdefault("LD_LIBRARY_PATH", os.path.join(_BASE, "lib"))
sys.path.insert(0, _BASE)

from core import db as _db  # noqa: E402
_db.init_schema()

from core import dedup_source as ds  # noqa: E402
from core import global_dedup as gd  # noqa: E402

BS = 1024 * 1024  # 1MB 块，测试提速


def _local_exec(cmd, timeout=120):
    r = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


class FakeSFTP:
    """模拟 paramiko SFTPClient：open(path,'rb') + seek/read。"""

    def open(self, path, mode="rb"):
        return open(path, mode)

    def close(self):
        pass


class FakeClient:
    def open_sftp(self):
        return FakeSFTP()


def _make_src(path: str, size_mb: int = 10, tail_bytes: int = 5) -> None:
    """构造测试源文件：每块全唯一随机数据 + 尾部小尾巴。"""
    with open(path, "wb") as f:
        for _ in range(size_mb):
            f.write(os.urandom(BS))
        f.write(b"x" * tail_bytes)


def test_parse_fingerprint_output():
    out = "SIZE 11\nWHOLE " + "a" * 64 + "\nB 0 " + "b" * 64 + "\nB 1 " + "c" * 64 \
          + "\nFINGERPRINT_DONE\n"
    fp = ds.parse_fingerprint_output(out, 8)
    assert fp["size"] == 11
    assert fp["sha256"] == "a" * 64
    assert len(fp["hashes"]) == 2
    try:
        ds.parse_fingerprint_output("SIZE 5\n", 8)
        assert False, "应因缺 WHOLE 抛错"
    except RuntimeError:
        pass


def _stage(path: str) -> str:
    """复制出"远端文件"副本（pull_assemble_dedup 完成后会清理远端临时包）。"""
    import shutil
    remote = path + ".remote"
    shutil.copyfile(path, remote)
    return remote


def test_roundtrip_two_runs():
    src = os.path.join(_TMP, "src_r1.bin")
    out1 = os.path.join(_TMP, "prod_r1.tar")
    out2 = os.path.join(_TMP, "prod_r2.tar")
    _make_src(src, size_mb=6, tail_bytes=7)

    # 第一轮：块库为空 → 全部拉取
    remote1 = _stage(src)
    s1 = ds.pull_assemble_dedup(FakeClient(), remote1, out1, exec_fn=_local_exec,
                                task_id=1, block_size=BS)
    assert s1["transferred_bytes"] == s1["logical_bytes"]
    assert s1["fetched_blocks"] == s1["blocks"]
    assert not os.path.exists(remote1), "远端临时包应被清理"
    # 重组产物与源逐字节一致
    assert ds._file_sha256(src) == s1["sha256"]
    assert ds._file_sha256(out1) == s1["sha256"]

    # 第二轮：同内容 → 重复块全部本地命中，0 字节走网络
    out2_src = _stage(src)
    s2 = ds.pull_assemble_dedup(FakeClient(), out2_src, out2, exec_fn=_local_exec,
                                task_id=1, block_size=BS)
    assert s2["transferred_bytes"] == 0, s2
    assert s2["hit_blocks"] == s2["blocks"]
    assert s2["saved_bytes"] == s2["logical_bytes"]
    assert s2["ratio_pct"] == 100.0
    assert ds._file_sha256(out2) == ds._file_sha256(src)


def test_partial_change_refetches_only_affected():
    src = os.path.join(_TMP, "src_chg.bin")
    _make_src(src, size_mb=4, tail_bytes=1)
    out = os.path.join(_TMP, "prod_chg1.tar")
    ds.pull_assemble_dedup(FakeClient(), _stage(src), out, exec_fn=_local_exec,
                           task_id=2, block_size=BS)
    # 修改第 2 块中间的 1 字节 → 仅该块重拉
    with open(src, "r+b") as f:
        f.seek(BS + 100)
        f.write(b"\xff")
    out2 = os.path.join(_TMP, "prod_chg2.tar")
    s = ds.pull_assemble_dedup(FakeClient(), _stage(src), out2, exec_fn=_local_exec,
                               task_id=2, block_size=BS)
    assert s["fetched_blocks"] == 1, s
    assert 0 < s["transferred_bytes"] <= BS
    assert ds._file_sha256(out2) == ds._file_sha256(src)


def test_corrupted_store_block_refetched():
    src = os.path.join(_TMP, "src_corrupt.bin")
    _make_src(src, size_mb=3, tail_bytes=3)
    out1 = os.path.join(_TMP, "prod_c1.tar")
    ds.pull_assemble_dedup(FakeClient(), _stage(src), out1, exec_fn=_local_exec,
                           task_id=3, block_size=BS)
    # 破坏块库中本文件清单内的一个物理块 → 该块应被识别并从"源"重拉
    fp = ds.remote_fingerprint(_local_exec, _stage(src), BS)
    victim = os.path.join(gd._store_dir(), fp["hashes"][0][:2],
                          fp["hashes"][0])
    assert os.path.isfile(victim)
    with open(victim, "r+b") as f:
        f.write(b"CORRUPTED!")
    out2 = os.path.join(_TMP, "prod_c2.tar")
    s = ds.pull_assemble_dedup(FakeClient(), _stage(src), out2, exec_fn=_local_exec,
                               task_id=3, block_size=BS)
    assert s["fetched_blocks"] >= 1
    assert ds._file_sha256(out2) == ds._file_sha256(src)


def test_global_stats_accounting():
    src = os.path.join(_TMP, "src_stats.bin")
    _make_src(src, size_mb=4, tail_bytes=0)
    ds.pull_assemble_dedup(FakeClient(), src, os.path.join(_TMP, "p1.tar"),
                           exec_fn=_local_exec, task_id=4, block_size=BS)
    st = gd.global_stats()
    assert st["unique_blocks"] > 0
    assert st["total_references"] >= st["unique_blocks"]
