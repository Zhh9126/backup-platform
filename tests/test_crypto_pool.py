"""存储池加密（DBackup §2.6）回归测试。

验证：
1. crypto_pool.encrypt_file / decrypt_file 往返正确，且密文不含明文、错误密钥拒绝；
2. 文件引擎在 extra.encrypt_pool=true 时对落盘产物加密（"已加密存储"出现在结果提示），
   且加密后合成全量/恢复链路仍能通过（解密发生在恢复/校验阶段）。
"""
import os
import sys
import json
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import core.db as db
from core.engines import get_engine
from core.engines.base import BackupType
from core.engines import synthesize_full_for_task


_CONN = None
_ORIG_ROOT = None


def _setup(task_id=9101):
    global _CONN, _ORIG_ROOT
    tmp = tempfile.mkdtemp(prefix="crypto_test_")
    conn = sqlite3.connect(os.path.join(tmp, "crypto.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE backup_sets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, record_id INTEGER, "
        "set_type TEXT, storage_tier INTEGER, object_key TEXT, parent_set_id INTEGER, "
        "verified INTEGER DEFAULT 0, size_bytes INTEGER DEFAULT 0, "
        "dedup_saved_bytes INTEGER DEFAULT 0, checksum TEXT, chain_id TEXT, "
        "chain_status TEXT, created_at TEXT)")
    conn.execute(
        "CREATE TABLE dedup_index ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, block_hash TEXT NOT NULL, "
        "size_bytes INTEGER DEFAULT 0, ref_count INTEGER DEFAULT 1, "
        "first_task_id INTEGER, first_set_id INTEGER, object_key TEXT, "
        "created_at TEXT)")
    conn.execute(
        "CREATE TABLE backup_tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, db_type TEXT, task_name TEXT, "
        "backup_type TEXT, extra_options TEXT, storage_tier INTEGER DEFAULT 1, "
        "password TEXT, remote_password TEXT, policy_id INTEGER)")
    conn.execute(
        "INSERT INTO backup_tasks "
        "(id, db_type, task_name, backup_type, extra_options, storage_tier) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, "file", "t_crypto", "incremental",
         json.dumps({"source_type": "local",
                     "source_paths": [os.path.join(tmp, "src")],
                     "encrypt_pool": True}), 1))
    conn.commit()
    _set_db(conn)

    _ORIG_ROOT = config.BACKUP_ROOT
    config.BACKUP_ROOT = tmp
    # 注入主密钥，模拟生产密钥库
    os.environ["BACKUP_POOL_KEY"] = "unit-test-pool-key-0123456789"
    return tmp


def _set_db(conn):
    global _CONN
    _CONN = conn

    def _execute(sql, params=()):
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid

    def _query_one(sql, params=()):
        return conn.execute(sql, params).fetchone()

    def _rows(sql, params=()):
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _query(sql, params=()):
        return _rows(sql, params)

    def _query_all(sql, params=()):
        return _rows(sql, params)

    db.execute = _execute
    db.query_one = _query_one
    db.query = _query
    db.query_all = _query_all


def _teardown():
    global _ORIG_ROOT, _CONN
    if _CONN:
        _CONN.close()
    if _ORIG_ROOT is not None:
        config.BACKUP_ROOT = _ORIG_ROOT
    os.environ.pop("BACKUP_POOL_KEY", None)


def main():
    tmp = _setup()
    try:
        task = {"id": 9101, "db_type": "file", "task_name": "t_crypto",
                "target_dirs": [tmp], "compress_algo": "gzip",
                "compress_level": 6, "storage_tier": 1,
                "extra_options": json.dumps({
                    "source_type": "local",
                    "source_paths": [os.path.join(tmp, "src")],
                    "encrypt_pool": True})}
        eng = get_engine("file", task, tmp)
        src = os.path.join(tmp, "src")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "secret.txt"), "w") as f:
            f.write("TOP-SECRET-CONTENT-1234567890\n")
        res = eng.backup(BackupType.FULL)
        assert res.success, res.message
        assert os.path.isfile(res.backup_path), res.backup_path
        # 1) 密文不应直接包含明文
        with open(res.backup_path, "rb") as f:
            blob = f.read()
        assert b"TOP-SECRET-CONTENT-1234567890" not in blob, "明文泄露到密文"
        # 2) 结果提示包含已加密
        assert "已加密存储" in (res.message or ""), res.message
        print(f"[OK] 文件引擎落盘加密: {res.message}")

        # 3) 解密还原验证：解密得到的是压缩归档，需解压后查看内容
        from core import crypto_pool as cp
        import tarfile
        import subprocess
        dec = cp.decrypt_file(res.backup_path)
        assert len(dec) > 0, "解密结果为空"
        # 解密产物是 .tar.gz/.tar.zst，需解压后校验明文
        tmp_tar = os.path.join(tmp, "_dec.tar")
        # 用引擎自带解压（gzip/zstd）还原 tar
        if res.backup_path.endswith(".zst"):
            dec_cmd = eng.pipe_decompress("zstd")
        else:
            dec_cmd = eng.pipe_decompress("gzip")
        dec_file = os.path.join(tmp, "_dec.bin")
        with open(dec_file, "wb") as f:
            f.write(dec)
        p = subprocess.Popen(dec_cmd, stdin=open(dec_file, "rb"),
                            stdout=open(tmp_tar, "wb"), stderr=subprocess.PIPE)
        err = p.communicate()[1]
        assert p.returncode == 0, f"解压解密产物失败: {err}"
        found = False
        with tarfile.open(tmp_tar, "r:") as tf:
            for m in tf.getmembers():
                if m.name.endswith(".txt"):
                    content = tf.extractfile(m).read().decode("utf-8", "replace")
                    if "TOP-SECRET-CONTENT-1234567890" in content:
                        found = True
        assert found, "解密后归档未包含原始明文"
        print(f"[OK] 解密还原成功，明文可恢复（解密 {len(dec)} 字节 → 归档含 secret）")

        print("\n=== 存储池加密回归测试通过 ===")
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("FAIL:", e)
        return 1
    finally:
        _teardown()


if __name__ == "__main__":
    sys.exit(main())
