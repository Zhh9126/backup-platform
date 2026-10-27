# -*- coding: utf-8 -*-
"""源端传输去重（Source-Side Transfer Dedup）—— 对标鼎甲迪备 §2.4 / 中科热备"源端去重"。

与 core/global_dedup（落盘后切片记账 = "先写入后去重"）的分工：
- global_dedup：产物已全量走网络并落盘，再切片记账（省不了带宽）；
- 本模块：**数据离开源主机之前**先分块算指纹（零 agent，POSIX split/dd/sha256sum
  标准工具），重复块根本不走网络——平台侧从全局块库（.dedup_store，与
  global_dedup 共享）读出重复块重组产物，仅"新块"经 SFTP 拉取。

诚实边界（v1）：
- 带宽节省是真实的（实传字节数可测量）；
- 产物文件仍全量落盘（恢复/校验/克隆/镜像等下游链路零改动），
  存储级节省由 global_dedup 记账层呈现，块寻址存储+按需重组（真·省盘）为 v2；
- 数据库逻辑备份（dmp/SQL 文本）日间重复度高，去重收益最大；
  已压缩/加密数据收益低——与业界规律一致，见 docs/benchmark_dedup_20261026.md。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shlex
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("core.dedup_source")

DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024  # 4MB，与 global_dedup 保持一致


def _store_dir() -> str:
    from core import global_dedup as gd
    return gd._store_dir()


def _block_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 远端指纹（零 agent：stat/dd/sha256sum 均为 POSIX/核心工具）
# ---------------------------------------------------------------------------

def _fingerprint_cmd(remote_path: str, block_size: int) -> str:
    """生成远端分块指纹脚本。输出行：
    SIZE <bytes> | WHOLE <sha256> | B <seq> <sha256>
    dd bs/skip/count 精确寻址读块，不做临时文件复制（远端零额外空间）。
    """
    f = shlex.quote(remote_path)
    return (
        "f={f}; bs={bs}; "
        "sz=$(stat -c %s \"$f\" 2>/dev/null) || {{ echo FINGERPRINT_STAT_FAIL; exit 3; }}; "
        "echo \"SIZE $sz\"; "
        "echo \"WHOLE $(sha256sum \"$f\" | awk '{{print $1}}')\"; "
        "i=0; off=0; "
        "while [ $off -lt $sz ]; do "
        "h=$(dd if=\"$f\" bs=$bs skip=$i count=1 2>/dev/null | sha256sum | awk '{{print $1}}'); "
        "echo \"B $i $h\"; "
        "i=$((i+1)); off=$((off+bs)); "
        "done; echo FINGERPRINT_DONE"
    ).format(f=f, bs=int(block_size))


def parse_fingerprint_output(out: str, block_size: int) -> Dict:
    """解析指纹脚本输出 → {"size", "block_size", "sha256", "hashes": [..]}。

    独立成纯函数便于单测（无需 SSH）。
    """
    size = None
    whole = None
    hashes: List[str] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("SIZE "):
            size = int(line.split()[1])
        elif line.startswith("WHOLE "):
            whole = line.split()[1]
        elif line.startswith("B "):
            parts = line.split()
            hashes.append((int(parts[1]), parts[2]))
    if size is None or whole is None or not hashes:
        raise RuntimeError("远端指纹输出不完整（stat 失败或 sha256sum 缺失）")
    hashes.sort(key=lambda x: x[0])
    if [h[0] for h in hashes] != list(range(len(hashes))):
        raise RuntimeError("远端指纹序号不连续")
    n = (size + block_size - 1) // block_size
    if len(hashes) != n:
        raise RuntimeError(f"指纹块数 {len(hashes)} 与文件大小 {size} 不符")
    return {"size": size, "block_size": block_size, "sha256": whole,
            "hashes": [h[1] for h in hashes]}


def remote_fingerprint(exec_fn: Callable[[str], Tuple[str, str, int]],
                       remote_path: str,
                       block_size: int = DEFAULT_BLOCK_SIZE,
                       timeout: int = 3600) -> Dict:
    """对远端文件分块取指纹。exec_fn(cmd) -> (out, err, rc)。"""
    out, err, rc = exec_fn(_fingerprint_cmd(remote_path, block_size))
    if rc != 0:
        raise RuntimeError(f"远端指纹计算失败 rc={rc}: {(err or out or '')[-300:]}")
    return parse_fingerprint_output(out, block_size)


# ---------------------------------------------------------------------------
# 拉取 + 重组（平台侧）
# ---------------------------------------------------------------------------

def _read_block(path: str, expect_hash: str) -> Optional[bytes]:
    """读块并校验哈希；不存在/哈希不符返回 None（触发重拉兜底）。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if _block_hash(data) == expect_hash:
            return data
    except OSError:
        pass
    return None


def pull_assemble_dedup(client, remote_path: str, local_path: str,
                        exec_fn: Callable[[str], Tuple[str, str, int]],
                        task_id=None,
                        block_size: int = DEFAULT_BLOCK_SIZE,
                        cleanup_remote: bool = True) -> Dict:
    """源端传输去重主流程。

    1. 远端分块指纹（dd+sha256sum，零临时文件）；
    2. 逐块比对全局块库（.dedup_store）：命中 → 本地读块（校验哈希）；
       未命中 → SFTP seek 读该块（**只有新块走网络**）→ 落块库 + 记账；
    3. 按序重组产物 → 整文件 sha256 与远端对账；
    4. 清理远端临时包。

    返回统计 {"logical_bytes","transferred_bytes","saved_bytes","ratio_pct",
              "blocks","hit_blocks","fetched_blocks","sha256"}。
    任何一步失败抛异常，由调用方回退整包流式通道（正确性不受影响）。
    """
    from core import global_dedup as gd

    fp = remote_fingerprint(exec_fn, remote_path, block_size)
    store = _store_dir()
    size = fp["size"]
    hashes = fp["hashes"]

    transferred = 0
    hits = 0
    fetches = 0
    sftp = client.open_sftp()
    try:
        with open(local_path, "wb") as out:
            for seq, h in enumerate(hashes):
                bsize = min(block_size, size - seq * block_size)
                data = None
                phys = os.path.join(store, h[:2], h)
                if os.path.isfile(phys):
                    data = _read_block(phys, h)  # 块库命中（含哈希校验）
                if data is None:
                    # 新块：SFTP seek 精确读该块（重复块不走网络）
                    with sftp.open(remote_path, "rb") as rf:
                        rf.seek(seq * block_size)
                        data = rf.read(bsize)
                    if not data or _block_hash(data) != h:
                        raise RuntimeError(
                            f"块 {seq} 传输后哈希不符（期望 {h[:12]}…）")
                    transferred += len(data)
                    fetches += 1
                    # 落块库 + 全局记账（与 global_dedup 同一套索引）
                    os.makedirs(os.path.dirname(phys), exist_ok=True)
                    tmp_phys = phys + ".part"
                    with open(tmp_phys, "wb") as pf:
                        pf.write(data)
                    os.replace(tmp_phys, phys)
                    gd._upsert_block(h, len(data), task_id, None, phys)
                else:
                    hits += 1
                out.write(data)
        # 整文件对账：重组产物必须与远端逐字节一致
        local_sha = _file_sha256(local_path)
        if local_sha != fp["sha256"]:
            raise RuntimeError(
                f"重组产物校验不符: local={local_sha[:12]}… remote={fp['sha256'][:12]}…")
    finally:
        try:
            sftp.close()
        except Exception:  # noqa: BLE001
            pass
        if cleanup_remote:
            try:
                exec_fn("rm -f %s" % shlex.quote(remote_path))
            except Exception:  # noqa: BLE001
                pass

    logical = size
    saved = max(logical - transferred, 0)
    ratio = round(saved / logical * 100.0, 1) if logical else 0.0
    return {"logical_bytes": logical, "transferred_bytes": transferred,
            "saved_bytes": saved, "ratio_pct": ratio,
            "blocks": len(hashes), "hit_blocks": hits,
            "fetched_blocks": fetches, "sha256": fp["sha256"]}


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def remote_free_space_ok(exec_fn: Callable[[str], Tuple[str, str, int]],
                         remote_path_dir: str, needed_bytes: int) -> bool:
    """远端目录剩余空间是否足够放临时包（不足则回退流式，避免撑爆源机）。"""
    try:
        out, _err, rc = exec_fn(
            "df -k --output=avail %s 2>/dev/null | tail -1" %
            shlex.quote(remote_path_dir))
        if rc != 0:
            return False
        avail_kb = int(out.strip().splitlines()[-1])
        return avail_kb * 1024 > needed_bytes + (64 * 1024 * 1024)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 文件级源端去重（v1 主方案，对 file 引擎远程源）
#
# 固定块流去重对 tar.gz 天然失效：gzip 头 mtime + 归档内任一文件变化导致
# 其后全部块移位（业界因此用变长块 CDC，但零 agent 的远端无法跑 rolling
# hash）。文件级粒度没有移位问题：**未变文件整文件跳过，与它在归档中的
# 位置无关**——日变化率 3% 的场景实传就是 ~3%，正对应"源端去重"叙事。
# ---------------------------------------------------------------------------

FILE_STORE_SUBDIR = "files"  # .dedup_store/files/<sha>（与 4MB 块库分区）


def _file_store_dir() -> str:
    d = os.path.join(_store_dir(), FILE_STORE_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _manifest_cmd(src_path: str) -> str:
    """远端清单脚本（零 agent，只读）：文件 sha256 + 元数据 + 目录 + 链接。"""
    p = shlex.quote(src_path)
    return (
        f"cd {p} || {{ echo MANIFEST_CD_FAIL; exit 3; }}; "
        "find . -type f -print0 2>/dev/null | xargs -0 -r sha256sum 2>/dev/null; "
        "echo MSTAT; "
        "find . -type f -print0 2>/dev/null | xargs -0 -r stat -c '%n|%s|%Y|%a' 2>/dev/null; "
        "echo MLNK; "
        "find . -type l -print0 2>/dev/null | xargs -0 -r -n1 sh -c "
        "'printf \"%s|%s\\n\" \"$1\" \"$(readlink \"$1\")\"' sh 2>/dev/null; "
        "echo MDONE"
    )


def parse_file_manifest(out: str, src_path: str) -> Dict:
    """解析清单输出 → {"files": {rel: {sha,size,mtime,mode}}, "dirs": [..],
    "links": {rel: target}}。rel 为相对源路径（'./' 开头，与 tar -C 语义一致）。
    独立纯函数便于单测。"""
    files: Dict[str, Dict] = {}
    dirs: List[str] = []
    links: Dict[str, str] = {}
    stage = "SUM"
    for line in (out or "").splitlines():
        line = line.rstrip("\r")
        if line == "MSTAT":
            stage = "STAT"
            continue
        if line == "MLNK":
            stage = "LNK"
            continue
        if line == "MDONE":
            stage = "DONE"
            continue
        if stage == "SUM" and len(line) > 66 and line[64:66] in ("  ", " *"):
            h, path = line[:64], line[66:]
            if h == "MANIFEST_CD_FAIL" or path == "":
                continue
            files[path] = {"sha": h, "size": None, "mtime": 0, "mode": 0o644}
        elif stage == "STAT" and "|" in line:
            parts = line.split("|")
            if len(parts) == 4 and parts[0] in files:
                try:
                    files[parts[0]].update({
                        "size": int(parts[1]), "mtime": int(parts[2]),
                        "mode": int(parts[3], 8) or 0o644})
                except ValueError:
                    pass
        elif stage == "LNK" and "|" in line:
            rel, _, target = line.partition("|")
            if rel.startswith("./"):
                links[rel] = target
    if not files and not dirs:
        # 至少要求命令跑通（目录可能真的为空——允许空清单，由调用方判断）
        pass
    return {"files": files, "dirs": dirs, "links": links, "src_path": src_path}


def remote_file_manifest(exec_fn: Callable[[str], Tuple[str, str, int]],
                         src_paths: List[str]) -> Dict:
    """远端只读取清单（逐源）：{"sources": [{src_path, files, links}]}。

    files: {rel_path: {sha,size,mtime,mode}}，rel 以 './' 开头（与 tar -C 语义
    一致）；links: {rel_path: target}。远端全程只读，零临时文件。
    """
    sources = []
    for p in src_paths or []:
        o, e, r = exec_fn(_manifest_cmd(p))
        if r != 0:
            raise RuntimeError(f"远端清单获取失败（{p}）rc={r}: {(e or o or '')[-300:]}")
        part = parse_file_manifest(o, p)
        part.pop("dirs", None)
        sources.append(part)
    return {"sources": sources}


def _excluded(rel: str, excludes: List[str]) -> bool:
    """平台侧排除过滤（glob 近似 tar --exclude 语义：完整路径或 basename）。"""
    import fnmatch
    base = os.path.basename(rel)
    for ex in excludes or []:
        ex = (ex or "").strip()
        if not ex:
            continue
        if fnmatch.fnmatch(base, ex) or fnmatch.fnmatch(rel, ex) \
                or fnmatch.fnmatch(rel.lstrip("./"), ex):
            return True
    return False


def pull_files_dedup(client, manifest: Dict, task_id=None,
                     excludes: Optional[List[str]] = None) -> Tuple[Dict, Dict]:
    """文件级源端去重拉取：每个文件按内容哈希比对文件库
    （.dedup_store/files/<sha>），命中 → 本地读；未命中 → SFTP 拉取并入库。

    manifest 结构见 remote_file_manifest。
    返回 ({rel_path: 本地库路径}, 统计)。只有新/变文件走网络。
    """
    sftp = client.open_sftp()
    store = _file_store_dir()
    local_map: Dict[str, str] = {}
    transferred = 0
    hits = 0
    fetches = 0
    total = 0
    try:
        for source in manifest["sources"]:
            src_root = (source.get("src_path") or "/").rstrip("/")
            for rel, meta in sorted(source["files"].items()):
                if _excluded(rel, excludes):
                    continue
                total += int(meta.get("size") or 0)
                sha = meta["sha"]
                phys = os.path.join(store, sha[:2], sha)
                if not os.path.isfile(phys):
                    remote_abs = src_root + "/" + rel.lstrip("./")
                    os.makedirs(os.path.dirname(phys), exist_ok=True)
                    tmp = phys + ".part"
                    sftp.get(remote_abs, tmp)
                    got = _file_sha256(tmp)
                    if got != sha:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        raise RuntimeError(
                            f"文件 {rel} 传输后哈希不符"
                            f"（期望 {sha[:12]}…实得 {got[:12]}…）")
                    os.replace(tmp, phys)
                    transferred += int(meta.get("size") or 0)
                    fetches += 1
                else:
                    hits += 1
                local_map[rel] = phys
            # 链接：目标字符串作为内容（哈希寻址），不 follow
            for rel, target in source.get("links", {}).items():
                if _excluded(rel, excludes):
                    continue
                data = target.encode("utf-8")
                sha = _block_hash(data)
                phys = os.path.join(store, sha[:2], sha)
                if not os.path.isfile(phys):
                    os.makedirs(os.path.dirname(phys), exist_ok=True)
                    with open(phys + ".part", "wb") as f:
                        f.write(data)
                    os.replace(phys + ".part", phys)
                    transferred += len(data)
                    fetches += 1
                else:
                    hits += 1
                local_map[rel] = phys
    finally:
        try:
            sftp.close()
        except Exception:  # noqa: BLE001
            pass
    saved = max(total - transferred, 0)
    ratio = round(saved / total * 100.0, 1) if total else 0.0
    return local_map, {"logical_bytes": total, "transferred_bytes": transferred,
                       "saved_bytes": saved, "ratio_pct": ratio,
                       "blocks": len(local_map), "hit_blocks": hits,
                       "fetched_blocks": fetches}


def build_tar_from_manifest(manifest: Dict, local_map: Dict[str, str],
                            tar_path: str, excludes: Optional[List[str]] = None,
                            compresslevel: int = 6) -> Dict:
    """平台侧重建标准 tar.gz（内容/元数据与远端一致，下游恢复链零改动）。

    打包后逐成员回读校验哈希（硬核对，杜绝"重组产物静默损坏"）。
    """
    import tarfile

    with tarfile.open(tar_path, "w:gz", compresslevel=compresslevel) as tf:
        for source in manifest["sources"]:
            # 目录条目（含中间目录）：从文件/链接 rel 推导
            dirs = set()
            rels = list(source["files"].keys()) + list(source.get("links", {}).keys())
            for rel in rels:
                if _excluded(rel, excludes):
                    continue
                parts = rel.lstrip("./").split("/")[:-1]
                acc = "."
                for part in parts:
                    acc = f"{acc}/{part}"
                    dirs.add(acc)
            for d in sorted(dirs):
                if _excluded(d, excludes):
                    continue
                ti = tarfile.TarInfo(name=d)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.mtime = 0
                tf.addfile(ti)
            for rel, target in source.get("links", {}).items():
                if _excluded(rel, excludes):
                    continue
                ti = tarfile.TarInfo(name=rel)
                ti.type = tarfile.SYMTYPE
                ti.linkname = target
                ti.mode = 0o777
                ti.mtime = 0
                tf.addfile(ti)
            for rel, meta in sorted(source["files"].items()):
                if _excluded(rel, excludes):
                    continue
                phys = local_map[rel]
                ti = tarfile.TarInfo(name=rel)
                ti.size = os.path.getsize(phys)
                ti.mtime = int(meta.get("mtime") or 0)
                ti.mode = int(meta.get("mode") or 0o644) & 0o7777
                with open(phys, "rb") as f:
                    tf.addfile(ti, f)
    # 回读校验：tar 内每个常规文件的内容哈希必须与清单一致
    verified = 0
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf:
            if not member.isfile():
                continue
            meta = None
            for source in manifest["sources"]:
                if member.name in source["files"]:
                    meta = source["files"][member.name]
                    break
            if not meta:
                continue
            f = tf.extractfile(member)
            data = f.read() if f else b""
            if _block_hash(data) != meta["sha"]:
                raise RuntimeError(f"tar 回读校验失败: {member.name}")
            verified += 1
    return {"verified_files": verified}
