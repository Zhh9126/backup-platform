# -*- coding: utf-8 -*-
"""备份三级存储深度测试（真实 MinIO/S3 后端 + zstd 最高压缩）。

流程：
1. 生成混合测试数据（高重复文本 200MB + 随机二进制 20MB）
2. FileBackupEngine 全量备份（zstd level 19 与 22 各一轮）
3. replicate_to_tiers 三级复制（L1 MinIO 热 / L2 S3 冷 / L3 本地导出）
4. 从 local/MinIO/S3 三个后端取回备份文件，SHA256 与本地备份逐一比对
5. zstd 解压 + tar 恢复，与源目录做逐文件校验（模拟恢复）
6. 输出压缩率 / 耗时 / 各层一致性报告
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import time

import zstandard as zstd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DEMO_MODE"] = "off"
os.chdir(ROOT)

from core.engines.base import BackupType  # noqa: E402
from core.engines.file import FileBackupEngine  # noqa: E402
from core.storage_backends import get_backend  # noqa: E402
from core import db as coredb  # noqa: E402
from core import tier_replication  # noqa: E402

SRC = "/tmp/deep_src"
OUT = "/tmp/deep_out"
META_DB = os.path.join(ROOT, "instance", "meta.db")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_data():
    """~220MB 混合数据：高重复文本（体现 ultra 差异）+ 随机二进制 + 小文件。"""
    if os.path.isdir(SRC):
        shutil.rmtree(SRC)
    os.makedirs(SRC)
    line = ("[INFO] 2026-08-26 12:00:00 order=9527 user=zhangsan action=checkout "
            "amount=199.00 status=ok [KEY]" * 200) + "\n"
    with open(os.path.join(SRC, "app.log"), "w", encoding="utf-8") as f:
        # line 本身已是 200 行拼接（约 1.56 万字符），乘 10000 即约 1.56 亿字符(≈150MB)
        f.write(line * 10000)  # 约 150MB
    with open(os.path.join(SRC, "data.jsonl"), "w", encoding="utf-8") as f:
        for i in range(50000):
            f.write(json.dumps({"id": i, "name": f"用户{i}",
                                "tags": ["a", "b", "c"], "price": i * 1.5,
                                "desc": "这是一段可重复的中文描述文本" * 5}) + "\n")
    with open(os.path.join(SRC, "blob.bin"), "wb") as f:
        f.write(os.urandom(20 * 1024 * 1024))
    os.makedirs(os.path.join(SRC, "conf"), exist_ok=True)
    for i in range(50):
        with open(os.path.join(SRC, "conf", f"cfg_{i}.ini"), "w") as f:
            f.write(f"[sec{i}]\nkey=value_{i}\n" * 100)


def dir_manifest(path: str) -> dict:
    m = {}
    for root, _, files in os.walk(path):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, path)
            m[rel] = (os.path.getsize(p), sha256(p))
    return m


def load_targets():
    con = sqlite3.connect(META_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM storage_targets WHERE enabled=1 ORDER BY tier").fetchall()
    con.close()
    targets = []
    for r in rows:
        d = dict(r)
        if d.get("secret_key"):
            try:
                d["secret_key"] = coredb.decrypt_secret(d["secret_key"])
            except Exception:
                pass
        if d.get("extra_options") and isinstance(d["extra_options"], str):
            try:
                d["extra_options"] = json.loads(d["extra_options"])
            except Exception:
                pass
        targets.append(d)
    return targets


def build_task(level: int, name: str) -> dict:
    return {
        "id": 999,
        "name": name,
        "db_type": "file",
        "extra_options": {"source_paths": [SRC]},
        "compress": 1,
        "compress_level": level,
        "storage_pool": "",
    }


def find_and_fetch(tgt: dict, filename: str, dest: str):
    """在各存储层定位备份对象并取回本地，返回 (key, local_path) 或 None。"""
    if tgt["type"] == "local":
        root = tgt["endpoint"]
        if not root:
            return None
        for r, _, fs in os.walk(root):
            for f in fs:
                if f.endswith(filename):
                    src = os.path.join(r, f)
                    shutil.copyfile(src, dest)
                    return (os.path.relpath(src, root), dest)
        return None
    # minio / s3：用 MinIO SDK 直连（endpoint 均为 http://127.0.0.1:9000）
    from minio import Minio  # noqa: PLC0415
    ep = tgt["endpoint"].replace("http://", "").replace("https://", "")
    c = Minio(ep,
              access_key=tgt.get("access_key") or "minioadmin",
              secret_key=tgt.get("secret_key") or "minioadmin",
              secure=False)
    bucket = tgt["bucket"]
    if not bucket:
        return None
    for o in c.list_objects(bucket, recursive=True):
        if o.object_name.endswith(filename):
            c.fget_object(bucket, o.object_name, dest)
            return (o.object_name, dest)
    return None


def run_one(level: int, targets: list) -> dict:
    name = f"deep-storage-zstd{level}"
    task = build_task(level, name)
    report = {"level": level, "backup_path": None, "backup_sha": None,
              "backup_size": 0, "src_size": 0, "ratio": 0, "duration": 0,
              "tiers": {}, "restore": None}

    eng = FileBackupEngine(task, storage_root=os.path.join(OUT, "backups"))
    t0 = time.time()
    result = eng.backup(BackupType.FULL)
    report["duration"] = round(time.time() - t0, 2)
    bp = result.backup_path
    report["backup_path"] = bp
    report["backup_size"] = os.path.getsize(bp)
    report["src_size"] = result.original_size_bytes
    report["ratio"] = round(result.original_size_bytes / max(os.path.getsize(bp), 1), 2)
    report["backup_sha"] = sha256(bp)
    print(f"[zstd-{level}] 备份完成 size={os.path.getsize(bp)/1024/1024:.2f}MB "
          f"压缩率={report['ratio']:.2f}x 耗时={report['duration']}s", flush=True)

    rep = tier_replication.replicate_to_tiers(bp, task, record_id=None)
    print(f"[zstd-{level}] 三级复制: {json.dumps(rep, ensure_ascii=False)}", flush=True)

    fname = os.path.basename(bp)
    for tgt in targets:
        try:
            dest = os.path.join(OUT, f"fetch_{level}_{tgt['name']}.zst")
            got = find_and_fetch(tgt, fname, dest)
            if not got:
                report["tiers"][tgt["name"]] = {"status": "FAIL", "msg": "未找到对象"}
                print(f"  [{tgt['name']}] 未找到对象", flush=True)
                continue
            key, local = got
            ok = sha256(local) == report["backup_sha"]
            report["tiers"][tgt["name"]] = {"status": "PASS" if ok else "FAIL",
                                            "key": key, "size": os.path.getsize(local)}
            print(f"  [{tgt['name']}] key={key} 取回{'一致' if ok else '不一致'}"
                  f" ({os.path.getsize(local)}B)", flush=True)
        except Exception as e:  # noqa: BLE001
            report["tiers"][tgt["name"]] = {"status": "FAIL", "msg": str(e)}
            print(f"  [{tgt['name']}] 异常: {e}", flush=True)

    # 恢复校验：优先取 L3 本地导出对象，否则用备份本体
    try:
        fetched = bp
        for tgt in targets:
            if tgt["type"] == "local":
                dest = os.path.join(OUT, f"restore_{level}.zst")
                got = find_and_fetch(tgt, fname, dest)
                if got:
                    fetched = got[1]
                break
        restore_dir = os.path.join(OUT, f"restore_{level}")
        if os.path.isdir(restore_dir):
            shutil.rmtree(restore_dir)
        os.makedirs(restore_dir)
        dctx = zstd.ZstdDecompressor()
        with open(fetched, "rb") as fz:
            raw = dctx.stream_reader(fz)
            with tarfile.open(fileobj=raw, mode="r|") as tar:
                tar.extractall(restore_dir)
        inner = restore_dir
        if len(os.listdir(inner)) == 1 and os.path.isdir(
                os.path.join(inner, os.listdir(inner)[0])):
            inner = os.path.join(inner, os.listdir(inner)[0])
        src_m = dir_manifest(SRC)
        rst_m = dir_manifest(inner)
        missing = [k for k in src_m if k not in rst_m]
        diff = [k for k in src_m if k in rst_m and src_m[k] != rst_m[k]]
        ok_restore = not missing and not diff
        report["restore"] = {"status": "PASS" if ok_restore else "FAIL",
                             "files": len(src_m),
                             "missing": len(missing), "diff": len(diff)}
        print(f"[zstd-{level}] 恢复校验: {'PASS' if ok_restore else 'FAIL'} "
              f"{len(src_m)} 文件一致 (missing={len(missing)} diff={len(diff)})", flush=True)
    except Exception as e:  # noqa: BLE001
        report["restore"] = {"status": "FAIL", "msg": str(e)}
        print(f"[zstd-{level}] 恢复校验异常: {e}", flush=True)

    return report


def main():
    for d in (OUT,):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    make_data()
    src_size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _, fs in os.walk(SRC) for f in fs)
    print(f"测试数据: {SRC} 共 {src_size/1024/1024:.1f}MB", flush=True)

    targets = load_targets()
    print("存储目标:", [(t["name"], t["type"], t["tier"]) for t in targets], flush=True)

    reports = [run_one(lv, targets) for lv in (19, 22)]

    print("\n================ 深度测试报告 ================", flush=True)
    print(f"{'级别':<7}{'源大小':>10}{'备份大小':>10}{'压缩率':>8}{'耗时':>8}  "
          f"L1-MinIO  L2-S3  L3-本地  恢复", flush=True)
    all_ok = True
    for r in reports:
        t = r["tiers"]

        def s(name):
            return t.get(name, {}).get("status", "-")

        print(f"zstd-{r['level']:<3}{r['src_size']/1024/1024:>8.1f}M "
              f"{r['backup_size']/1024/1024:>8.2f}M {r['ratio']:>7.2f}x "
              f"{r['duration']:>7.1f}s   "
              f"{s('L1-MinIO-热'):<8} {s('L2-S3-冷'):<7} {s('L3-本地导出'):<9} "
              f"{r['restore'].get('status', '-')}", flush=True)
        tier_ok = all(v.get("status") == "PASS" for v in t.values())
        rest_ok = r["restore"].get("status") == "PASS"
        if not (tier_ok and rest_ok):
            all_ok = False
    print(f"\n整体结论: {'PASS' if all_ok else 'FAIL'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
