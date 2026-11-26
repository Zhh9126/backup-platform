# -*- coding: utf-8 -*-
"""内容定义分块（CDC）全局块库 —— zbackup 思路的纯 Python 落地。

对标 zbackup 的核心价值（GitHub: zbackup/zbackup）：

* **内容定义分块**：滚动哈希在字节流上"找内容边界"，切成**变长块**——同一段
  内容无论出现在哪个文件、哪个偏移，都被切成同一个块（这是我们此前 4MB
  固定块做不到的：归档里任一字节变化会让其后全部块指纹移位，跨轮命中率趋近
  0，gzip 归档尤其严重）。
* **块级全局去重**：块以 SHA256 内容寻址落库，重复内容只存一份；跨任务、跨
  时间、跨文件形态共享同一个块库。
* **引用重建**：备份产物 = 一串块引用，恢复时按引用从块库重组（块内容不重复
  存放，天然省空间）。

与 zbackup 的**差异（有意为之，保住我们比它强的地方）**：

* zbackup 不备份元数据（权限/属主/时间戳要另配 tar）；本模块配合 tar 成员
  语义工作，**元数据完整保留**。
* zbackup 是单线程 C++ 二进制（离线交付要带编译产物 + glibc 兼容矩阵）；本
  模块纯 Python，零外部依赖，天然满足离线自足与 PyInstaller 打包。
* zbackup 的 bundle 格式不与现有恢复链兼容；本模块只做"块库 + 引用"，产物
  仍是标准 tar(.gz/.zst)，下游恢复链零改动。

用法::

    from core.cdc_chunk import ChunkStore
    st = ChunkStore()                    # 默认 BACKUP_ROOT/.cdc_store
    ref = st.ingest_file("/path/big.bin")   # 返回块引用（已去重入库）
    st.rebuild_file(ref, "/path/out.bin")   # 按引用重组（逐块校验）
    st.stats()                          # {'logical_bytes', 'physical_bytes', ...}
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from typing import Dict, Iterator, List, Optional, Tuple

import config

# ---------------------------------------------------------------------------
# Gear 滚动哈希（zbackup 用 Rabin；Gear 更快、实现更简单，边界语义等价）
# ---------------------------------------------------------------------------
_GEAR = [
    0x2D35_2E1B_9A10_C74F, 0x1F4A_8C0D_5B23_E697, 0x3C61_D274_B895_0FE3,
    0x0A94_7C1E_D386_2B5F, 0x2E73_501C_B64D_8A19, 0x15D0_6F23_A947_3C8E,
    0x3B82_1E5A_C70D_46F2, 0x08C4_2B69_F31A_57ED, 0x2A51_7E38_C960_B1D4,
    0x14E7_2A31_8B5C_6FD0, 0x3D94_0B52_7A1E_8C63, 0x0F28_396D_4C5B_A1E7,
    0x2B6E_5410_8A2C_D3F5, 0x1C39_2F4D_6B70_5E8A, 0x37A5_2194_C83F_0D6B,
    0x06D1_8B43_E572_94C0,
]

MASK = 0x1FFF            # 平均块大小 ≈ 8KiB（1/8192 概率命中边界）
MIN_CHUNK = 2 * 1024
MAX_CHUNK = 64 * 1024


def _gear_roll(h: int, b: int) -> int:
    return ((h << 1) + _GEAR[b & 0x0F]) & 0xFFFF_FFFF_FFFF_FFFF


def iter_chunks(fp, min_size: int = MIN_CHUNK, max_size: int = MAX_CHUNK,
                mask: int = MASK) -> Iterator[bytes]:
    """从二进制文件对象读出 CDC 变长块。

    边界条件：滚动哈希低位命中 mask，且块长已过 min_size（防止碎块）；
    超过 max_size 强制切一刀（防止重复内容永不命中边界）。
    """
    cur = bytearray()
    h = 0
    while True:
        data = fp.read(65536)
        if not data:
            break
        for b in data:
            cur.append(b)
            h = _gear_roll(h, b)
            if len(cur) >= max_size:
                yield bytes(cur)
                cur = bytearray()
                h = 0
            elif len(cur) >= min_size and (h & mask) == 0:
                # 内容边界：变长块的关键——同一内容段在任何偏移都切在同一处
                yield bytes(cur)
                cur = bytearray()
                h = 0
    if cur:
        yield bytes(cur)


def chunk_bytes(data: bytes, **kw) -> List[Tuple[str, int]]:
    """内存里的字节流分块（测试/小对象用）。返回 [(sha256, size)]。"""
    out = []
    for blk in iter_chunks(io.BytesIO(data), **kw):
        out.append((hashlib.sha256(blk).hexdigest(), len(blk)))
    return out


# ---------------------------------------------------------------------------
# 传输级 CDC：远端算块边界（零安装：临时推送脚本执行后清理，与 xtrabackup
# 推送同构）。远端跑一段平台生成的 python3 脚本输出块清单，平台只拉"块库
# 里没有"的块 —— 大文件内部变化只传变化部分（固定块/整文件方案做不到）。
# ---------------------------------------------------------------------------
def _remote_script_source() -> str:
    """生成远端 CDC 分块脚本（python3）。

    边界算法与平台 :func:`iter_chunks` **完全同一套**（同一 GEAR 表、同一
    MASK、同一 min/max 语义）——否则远端块与本地块库对不上，命中率归零。
    输出 TSV：``offset<TAB>size<TAB>sha256``，末行 ``FILE<TAB>size<TAB>sha256``
    作整文件校验。
    """
    body = (
        "#!/usr/bin/env python3\n"
        "import hashlib, os, sys\n"
        "G = __GEAR__\n"
        "MASK = __MASK__\n"
        "def chunks(path, mn, mx):\n"
        "    h = 0; cur = bytearray(); start = 0; off = 0\n"
        "    with open(path, 'rb') as f:\n"
        "        while True:\n"
        "            b = f.read(65536)\n"
        "            if not b:\n"
        "                break\n"
        "            for x in b:\n"
        "                cur.append(x); off += 1\n"
        "                h = ((h << 1) + G[x & 0x0F]) & 0xFFFFFFFFFFFFFFFF\n"
        "                if len(cur) >= mx or (len(cur) >= mn and (h & MASK) == 0):\n"
        "                    yield (start, len(cur), hashlib.sha256(cur).hexdigest())\n"
        "                    start = off; cur = bytearray(); h = 0\n"
        "    if cur:\n"
        "        yield (start, len(cur), hashlib.sha256(cur).hexdigest())\n"
        "path = sys.argv[1]\n"
        "mn = int(sys.argv[2]); mx = int(sys.argv[3])\n"
        "for o, s, h in chunks(path, mn, mx):\n"
        "    sys.stdout.write('%d\\t%d\\t%s\\n' % (o, s, h))\n"
        "hh = hashlib.sha256()\n"
        "with open(path, 'rb') as f:\n"
        "    for b in iter(lambda: f.read(1 << 20), b''):\n"
        "        hh.update(b)\n"
        "sys.stdout.write('FILE\\t%d\\t%s\\n'\n"
        "                % (os.stat(path).st_size, hh.hexdigest()))\n"
    )
    # 用占位符替换而不是 % 格式化：脚本体里本来就有 %d/%s，会被外层吃掉
    return body.replace("__GEAR__", repr(_GEAR)).replace("__MASK__", str(MASK))


def remote_chunk_manifest(sftp, exec_fn, remote_path: str) -> list:
    """远端执行临时 python3 脚本算 CDC 块清单。返回 [(offset, size, sha256)]。

    零安装：脚本 SFTP 推送到远端 /tmp 执行，finally 删除；远端无 python3 时
    抛 RuntimeError，由调用方回退整文件 SFTP。
    """
    script = "/tmp/aidbm_cdc_%d.py" % os.getpid()
    body = _remote_script_source()
    with sftp.open(script, "w") as f:
        f.write(body)
    try:
        for py in ("python3", "python"):
            out, err, rc = exec_fn(
                "%s %s %s %d %d" % (py, shlex_quote(script),
                                    shlex_quote(remote_path), MIN_CHUNK, MAX_CHUNK))
            if rc == 0 and out.strip():
                rows = []
                for line in out.strip().splitlines():
                    p = line.split("\t")
                    if len(p) == 3 and p[0] != "FILE":
                        rows.append((int(p[0]), int(p[1]), p[2]))
                if rows:
                    return rows
                raise RuntimeError("远端 CDC 脚本未产出块清单")
            if rc != 0 and ("not found" in (err or "") or "No such file" in (err or "")):
                continue                       # 换下一个解释器
            raise RuntimeError("远端 CDC 分块失败 rc=%s: %s" % (rc, (err or out or "")[-200:]))
        raise RuntimeError("远端缺少 python3/python，无法做块级分块（可改用整文件传输）")
    finally:
        try:
            sftp.remove(script)
        except Exception:                      # noqa: BLE001
            pass


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def pull_file_cdc(sftp, exec_fn, remote_abs: str, local_path: str,
                  store: "ChunkStore", logger=None) -> Dict:
    """传输级 CDC 拉取：远端算块 → 只拉块库缺失的块 → 平台重组并校验。

    返回 {'logical_bytes','transferred_bytes','hit_blocks','fetched_blocks'}。
    任何异常向上抛出，调用方回退整文件 SFTP（正确性不受影响）。
    """
    rows = remote_chunk_manifest(sftp, exec_fn, remote_abs)
    tmp = local_path + ".cdc.tmp%d" % os.getpid()
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    transferred = hit = fetched = 0
    h = hashlib.sha256()
    try:
        with open(tmp, "wb") as out:
            with sftp.open(remote_abs, "rb") as rf:
                for off, size, sha in rows:
                    if store.has(sha):
                        blk = store.get(sha)
                        hit += 1
                    else:
                        rf.seek(off)
                        blk = rf.read(size)
                        if len(blk) != size:
                            raise RuntimeError("远端读块不完整 @%d+%d" % (off, size))
                        store.put(blk)
                        fetched += 1
                        transferred += size
                    h.update(blk)
                    out.write(blk)
        os.replace(tmp, local_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logical = sum(s for _o, s, _h in rows)
    if logger:
        logger.info("[cdc] %s: 块 %d（命中 %d / 拉取 %d），实传 %s / 逻辑 %s",
                    remote_abs, len(rows), hit, fetched,
                    human_size(transferred), human_size(logical))
    return {"logical_bytes": logical, "transferred_bytes": transferred,
            "hit_blocks": hit, "fetched_blocks": fetched,
            "sha256": h.hexdigest(), "blocks": len(rows)}


def human_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGB" % n


# ---------------------------------------------------------------------------
# 内容寻址块库
# ---------------------------------------------------------------------------
class ChunkStore:
    """全局去重块库（内容寻址，跨任务/跨时间共享）。"""

    def __init__(self, root: Optional[str] = None):
        base = root or os.path.join(
            os.environ.get("BACKUP_ROOT") or getattr(config, "BACKUP_ROOT", "/opt/backup-platform"),
            ".cdc_store")
        self.root = base
        self.chunks_dir = os.path.join(base, "chunks")
        self.refs_dir = os.path.join(base, "refs")
        self._lock = threading.Lock()
        os.makedirs(self.chunks_dir, exist_ok=True)
        os.makedirs(self.refs_dir, exist_ok=True)

    # ---- 块本体 ----
    def _path(self, sha: str) -> str:
        return os.path.join(self.chunks_dir, sha[:2], sha)

    def has(self, sha: str) -> bool:
        return os.path.exists(self._path(sha))

    def put(self, data: bytes) -> Tuple[str, bool]:
        """入库一块，返回 (sha256, 是否新块)。重复内容不重复落盘。"""
        sha = hashlib.sha256(data).hexdigest()
        p = self._path(sha)
        if os.path.exists(p):
            return sha, False
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp%d" % os.getpid()
        with open(tmp, "wb") as f:
            f.write(data)
        with self._lock:
            os.replace(tmp, p)          # 原子发布，杜绝半块
        return sha, True

    def get(self, sha: str) -> bytes:
        with open(self._path(sha), "rb") as f:
            return f.read()

    # ---- 文件级引用 ----
    def ingest_file(self, path: str, ref_name: str = "") -> Dict:
        """把一个文件按 CDC 分块入库，返回块引用（已全局去重）。"""
        chunks: List[Tuple[str, int]] = []
        logical = 0
        with open(path, "rb") as f:
            for blk in iter_chunks(f):
                sha, is_new = self.put(blk)
                chunks.append((sha, len(blk)))
                logical += len(blk)
        ref = {
            "chunks": [{"sha": s, "size": n} for s, n in chunks],
            "size": logical,
            "sha256": self._file_sha(path),
        }
        if ref_name:
            self.save_ref(ref_name, ref)
        return ref

    def ingest_bytes(self, data: bytes, ref_name: str = "") -> Dict:
        chunks = []
        for blk in iter_chunks(io.BytesIO(data)):
            sha, _new = self.put(blk)
            chunks.append((sha, len(blk)))
        ref = {"chunks": [{"sha": s, "size": n} for s, n in chunks],
               "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if ref_name:
            self.save_ref(ref_name, ref)
        return ref

    def rebuild_file(self, ref: Dict, out_path: str) -> str:
        """按引用重组文件，逐块校验后落盘（临时文件原子替换）。"""
        tmp = out_path + ".tmp%d" % os.getpid()
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        h = hashlib.sha256()
        total = 0
        try:
            with open(tmp, "wb") as out:
                for c in ref["chunks"]:
                    blk = self.get(c["sha"])
                    if len(blk) != c["size"]:
                        raise ValueError("块大小不符: %s" % c["sha"])
                    if hashlib.sha256(blk).hexdigest() != c["sha"]:
                        # 块级硬校验：块库单块损坏在重组时立刻暴露，绝不带病交付
                        raise ValueError("块内容损坏: %s" % c["sha"])
                    h.update(blk)
                    total += len(blk)
                    out.write(blk)
            if total != ref.get("size", total):
                raise ValueError("重组长度不符: %s != %s" % (total, ref.get("size")))
            if ref.get("sha256") and h.hexdigest() != ref["sha256"]:
                raise ValueError("重组内容校验失败: %s" % out_path)
            os.replace(tmp, out_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return out_path

    # ---- 引用持久化 ----
    def save_ref(self, name: str, ref: Dict) -> str:
        p = os.path.join(self.refs_dir, name + ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(ref, f)
        return p

    def load_ref(self, name: str) -> Optional[Dict]:
        p = os.path.join(self.refs_dir, name + ".json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    # ---- 统计 ----
    def stats(self) -> Dict:
        logical = physical = nchunks = 0
        for r, _d, files in os.walk(self.chunks_dir):
            for fn in files:
                if fn.endswith(".tmp") or ".tmp" in fn:
                    continue
                try:
                    physical += os.path.getsize(os.path.join(r, fn))
                    nchunks += 1
                except OSError:
                    pass
        for r, _d, files in os.walk(self.refs_dir):
            for fn in files:
                try:
                    with open(os.path.join(r, fn), encoding="utf-8") as f:
                        logical += int(json.load(f).get("size") or 0)
                except Exception:
                    pass
        return {
            "chunks": nchunks,
            "logical_bytes": logical,
            "physical_bytes": physical,
            "saved_bytes": max(0, logical - physical),
            "dedup_ratio": (round(logical / physical, 3) if physical else 0.0),
            "root": self.root,
        }

    @staticmethod
    def _file_sha(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()
