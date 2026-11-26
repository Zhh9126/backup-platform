# -*- coding: utf-8 -*-
"""CDC 内容定义分块单测（2026-11-26 吸收 zbackup 去重思路）。

锁定 zbackup 式 CDC 的三个关键性质：
  1. **边界稳定**：文件头部/中部插入少量字节，绝大多数块指纹不变
     （固定 4MB 块在这种场景下块块全变，跨轮去重命中率趋近 0）；
  2. **块级全局去重**：同一内容段多次入库只存一份；
  3. **重组保真**：按引用重建后逐字节一致（含 sha256 校验）。
"""
import hashlib
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.cdc_chunk import (ChunkStore, chunk_bytes, iter_chunks,  # noqa: E402
                            MAX_CHUNK, MIN_CHUNK)


def _reused(a, b) -> float:
    sa = {s for s, _n in a}
    sb = {s for s, _n in b}
    return len(sa & sb) / max(1, len(sa))


def test_chunk_sizes_within_bounds_and_variable():
    data = os.urandom(2 * 1024 * 1024)
    chunks = chunk_bytes(data)
    assert sum(n for _s, n in chunks) == len(data), "分块必须无重无漏"
    for i, (_s, n) in enumerate(chunks):
        if i == len(chunks) - 1:
            continue           # 尾块可能不足 min_size（语义允许）
        assert MIN_CHUNK <= n <= MAX_CHUNK, "块长必须落在 [%s, %s]" % (MIN_CHUNK, MAX_CHUNK)
    sizes = {n for _s, n in chunks}
    assert len(sizes) > 3, "块长应为变长（内容定义分块），不是固定块"


def test_head_insert_keeps_most_chunks():
    """头部插入 1 字节：只影响插入点附近的块，其余块指纹不变。"""
    data = os.urandom(1024 * 1024)
    c1 = chunk_bytes(data)
    c2 = chunk_bytes(b"\x00" + data)
    r = _reused(c1, c2)
    assert r > 0.8, "CDC 头部插入后块复用率应 > 80%%，实测 %.1f%%" % (r * 100)


def test_middle_insert_keeps_most_chunks():
    data = os.urandom(1024 * 1024)
    mid = len(data) // 2
    c1 = chunk_bytes(data)
    c2 = chunk_bytes(data[:mid] + b"XXXX" + data[mid:])
    r = _reused(c1, c2)
    assert r > 0.8, "CDC 中部插入后块复用率应 > 80%%，实测 %.1f%%" % (r * 100)


def test_same_content_chunk_is_shared(tmp_path):
    """两个不同文件含相同内容段 → 块库里只存一份。"""
    st = ChunkStore(str(tmp_path / "store"))
    common = os.urandom(300 * 1024)
    f1 = tmp_path / "a.bin"
    f2 = tmp_path / "b.bin"
    f1.write_bytes(b"A" * 1000 + common + b"B" * 1000)
    f2.write_bytes(b"C" * 500 + common + b"D" * 500)
    r1 = st.ingest_file(str(f1), ref_name="t/a")
    r2 = st.ingest_file(str(f2), ref_name="t/b")
    s1 = {c["sha"] for c in r1["chunks"]}
    s2 = {c["sha"] for c in r2["chunks"]}
    assert s1 & s2, "相同内容段必须命中同一个块（跨文件去重）"
    stats = st.stats()
    assert stats["physical_bytes"] < stats["logical_bytes"], "去重后物理占用应小于逻辑量"


def test_rebuild_is_byte_identical(tmp_path):
    st = ChunkStore(str(tmp_path / "store"))
    data = os.urandom(1500 * 1024) + b"\x00" * 100 + os.urandom(200 * 1024)
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    ref = st.ingest_file(str(src), ref_name="t1/src")
    out = tmp_path / "out.bin"
    st.rebuild_file(ref, str(out))
    assert out.read_bytes() == data, "重组必须逐字节一致"
    assert hashlib.sha256(out.read_bytes()).hexdigest() == ref["sha256"]


def test_rebuild_detects_corruption(tmp_path):
    st = ChunkStore(str(tmp_path / "store"))
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(200 * 1024))
    ref = st.ingest_file(str(src))
    # 篡改块库里的一个块
    victim = ref["chunks"][0]["sha"]
    p = st._path(victim)
    with open(p, "r+b") as f:
        f.write(b"\xff")
    with pytest.raises(ValueError):
        st.rebuild_file(ref, str(tmp_path / "bad.bin"))


def test_ref_roundtrip(tmp_path):
    st = ChunkStore(str(tmp_path / "store"))
    src = tmp_path / "s.bin"
    src.write_bytes(b"hello world " * 5000)
    ref = st.ingest_file(str(src), ref_name="job1/file")
    again = st.load_ref("job1/file")
    assert again == ref
    st.rebuild_file(again, str(tmp_path / "r.bin"))
    assert (tmp_path / "r.bin").read_bytes() == src.read_bytes()


def test_iter_chunks_handles_small_and_empty():
    assert list(iter_chunks(io.BytesIO(b""))) == []
    out = list(iter_chunks(io.BytesIO(b"abc")))
    assert out == [b"abc"], "不足最小块的尾部应整块返回"


# ---------------------------------------------------------------------------
# 传输级 CDC（远端算块 → 只拉新块）
# ---------------------------------------------------------------------------
def test_remote_script_chunking_matches_local(tmp_path):
    """远端脚本与平台 iter_chunks 必须同一套边界，否则块库命中率归零。"""
    import subprocess
    from core.cdc_chunk import _remote_script_source
    data = os.urandom(500 * 1024)
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    script = tmp_path / "cdc.py"
    script.write_text(_remote_script_source())
    out = subprocess.run([sys.executable, str(script), str(f),
                          str(MIN_CHUNK), str(MAX_CHUNK)],
                         capture_output=True, text=True)
    rows = [(int(a), int(b), c) for a, b, c in
            (ln.split("\t") for ln in out.stdout.splitlines()
             if not ln.startswith("FILE"))]
    assert [(h, n) for _o, n, h in rows] == chunk_bytes(data), \
        "远端脚本的块边界/哈希必须与平台逐块一致"


class _FakeSftp:
    """本地目录模拟远端 SFTP（写脚本 + 读远端文件）。"""

    def __init__(self, root):
        self.root = root
        self.script_local = "/tmp/_cdc_test_%d.py" % os.getpid()

    def open(self, path, mode="r"):
        if "aidbm_cdc" in path:
            return open(self.script_local, mode)
        return open(self.root + path, mode)

    def remove(self, path):
        try:
            os.unlink(self.script_local)
        except OSError:
            pass


def test_pull_file_cdc_only_fetches_changed_blocks(tmp_path):
    """传输级 CDC：改动 4KB 后只拉少量块（大文件内部变化不整传）。"""
    from core.cdc_chunk import ChunkStore, pull_file_cdc
    import shlex
    import subprocess

    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root)
    src = remote_root + "/big.bin"
    with open(src, "wb") as f:
        f.write(os.urandom(1024 * 1024))
    store = ChunkStore(str(tmp_path / "store"))

    def fake_exec(cmd, timeout=600):
        parts = shlex.split(cmd)
        # parts[1] 是远端脚本路径，本地等价文件由 _FakeSftp 落在 script_local
        out = subprocess.run([sys.executable, sftp.script_local,
                              remote_root + parts[2], parts[3], parts[4]],
                             capture_output=True, text=True)
        return out.stdout, out.stderr, out.returncode

    sftp = _FakeSftp(remote_root)
    out1 = str(tmp_path / "o1.bin")
    st1 = pull_file_cdc(sftp, fake_exec, "/big.bin", out1, store)
    assert st1["fetched_blocks"] > 0 and st1["hit_blocks"] == 0
    assert open(out1, "rb").read() == open(src, "rb").read()

    # 改中间 4KB → 只应拉少量块
    with open(src, "r+b") as f:
        f.seek(512 * 1024)
        f.write(os.urandom(4 * 1024))
    out2 = str(tmp_path / "o2.bin")
    st2 = pull_file_cdc(sftp, fake_exec, "/big.bin", out2, store)
    assert st2["hit_blocks"] > st2["fetched_blocks"], "绝大多数块应命中块库"
    assert st2["transferred_bytes"] < 64 * 1024, \
        "改动 4KB 实传应 < 64KB，实传 %d" % st2["transferred_bytes"]
    assert open(out2, "rb").read() == open(src, "rb").read()

    # 脚本已清理（零安装）
    assert not os.path.exists(sftp.script_local), "远端临时脚本必须用完即删"
