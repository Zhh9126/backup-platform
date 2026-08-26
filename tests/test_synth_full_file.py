"""合成全量（DBackup §3.2 永久增量 / CDM）文件引擎回归测试。

验证：
1. 文件引擎能真实合并「全量 + 增量」为新的完整归档（非占位 .sim）；
2. 合成产物可通过 verify_record 校验（存在/非空/checksum）；
3. synthesize_full_for_task 落库 synthetic_full 且 chain_status 诚实标注
   synthesized_real（真实合并）/ synthesized_sim（缺客户端逻辑重链）。

使用临时 storage_root 与临时 DB，不污染主库。
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


def _setup(task_id=9001, db_type="file"):
    global _CONN, _ORIG_ROOT
    tmp = tempfile.mkdtemp(prefix="synth_test_")
    conn = sqlite3.connect(os.path.join(tmp, "synth.db"))
    conn.row_factory = sqlite3.Row
    # 最小备份集表
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
        (9001, "file", "t_synth", "incremental",
         json.dumps({"source_type": "local",
                     "source_paths": [os.path.join(tmp, "src_full")]}), 1))
    conn.commit()
    _CONN = conn

    def _execute(sql, params=()):
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid

    def _query_one(sql, params=()):
        return conn.execute(sql, params).fetchone()

    def _rows_to_dicts(rows):
        return [dict(r) for r in rows]

    def _query_all(sql, params=()):
        return _rows_to_dicts(conn.execute(sql, params).fetchall())

    def _query(sql, params=()):
        return _rows_to_dicts(conn.execute(sql, params).fetchall())

    db.execute = _execute
    db.query_one = _query_one
    db.query_all = _query_all
    db.query = _query

    _ORIG_ROOT = config.BACKUP_ROOT
    config.BACKUP_ROOT = tmp
    return tmp


def _teardown():
    global _ORIG_ROOT, _CONN
    if _CONN:
        _CONN.close()
    if _ORIG_ROOT is not None:
        config.BACKUP_ROOT = _ORIG_ROOT


def _make_archives(eng, tmp, full_size=3, inc_updates=2):
    """生成全量归档 + 2 个增量归档（增量改同一个文件内容）。"""
    src_full = os.path.join(tmp, "src_full")
    os.makedirs(src_full, exist_ok=True)
    for i in range(full_size):
        with open(os.path.join(src_full, f"f{i}.txt"), "w") as f:
            f.write(f"full-{i}\n")
    full_res = eng.backup(BackupType.FULL)
    assert full_res.success, full_res.message

    inc_sets = []
    for k in range(inc_updates):
        # 增量：修改 f0 的内容（模拟变化）
        with open(os.path.join(src_full, "f0.txt"), "w") as f:
            f.write(f"inc{k}-{k}\n")
        # 同时新增一个文件
        with open(os.path.join(src_full, f"new{k}.txt"), "w") as f:
            f.write(f"newfile-{k}\n")
        inc_res = eng.backup(BackupType.INCREMENTAL)
        assert inc_res.success, inc_res.message
        # 为简化，直接从备份文件重建 set 记录（真实场景由调度器写库）
        inc_sets.append(inc_res.backup_path)
    return full_res.backup_path, inc_sets


def main():
    tmp = _setup()
    try:
        task = {"id": 9001, "db_type": "file", "task_name": "t_synth",
                "target_dirs": [tmp], "compress_algo": "gzip",
                "compress_level": 6, "storage_tier": 1,
                "extra_options": json.dumps({
                    "source_type": "local",
                    "source_paths": [os.path.join(tmp, "src_full")],
                })}
        eng = get_engine("file", task, tmp)
        # 生成全量 + 2 增量
        full_path, inc_paths = _make_archives(eng, tmp)
        # 构造 set 记录：base=全量，两个增量 parent=base
        db.execute(
            "INSERT INTO backup_sets (task_id,set_type,object_key,verified,size_bytes,checksum)"
            " VALUES (?,?,?,?,?,?)",
            (9001, "full", full_path, 1, os.path.getsize(full_path), "x"))
        base_id = _CONN.execute("SELECT last_insert_rowid()").fetchone()[0]
        for p in inc_paths:
            db.execute(
                "INSERT INTO backup_sets (task_id,set_type,object_key,parent_set_id,verified,size_bytes,checksum)"
                " VALUES (?,?,?,?,?,?,?)",
                (9001, "incremental", p, base_id, 1, os.path.getsize(p), "y"))
        # 直接用 synthesize_full_for_task 对任务 9001 做合成全量
        new_ids = synthesize_full_for_task(9001, target_storage_tier=1)
        assert new_ids, "未产生合成全量"
        row = db.query_one(
            "SELECT * FROM backup_sets WHERE id=?", (new_ids[0],))
        assert row["set_type"] == "synthetic_full", row
        # 真实合并 → synthesized_real
        assert row["chain_status"] == "synthesized_real", row["chain_status"]
        assert row["verified"] == 1, "合成产物未通过校验"
        assert os.path.isfile(row["object_key"]), "合成产物文件不存在"
        # 合成产物应已压缩且非空
        assert os.path.getsize(row["object_key"]) > 0
        print(f"[OK] 文件引擎合成全量: id={new_ids[0]} chain_status={row['chain_status']} "
              f"verified={row['verified']} size={os.path.getsize(row['object_key'])}")

        # 校验合成产物可恢复：解压检查包含最新增量内容
        import tarfile, tempfile, subprocess
        final = row["object_key"]
        ex = tempfile.mkdtemp(prefix="verify_")
        if final.endswith(".gz"):
            dec = eng.pipe_decompress("gzip")
            tpath = os.path.join(ex, "x.tar")
            p = subprocess.Popen(dec, stdin=open(final, "rb"),
                                 stdout=open(tpath, "wb"), stderr=subprocess.PIPE)
            p.communicate()
            final_tar = tpath
        else:
            final_tar = final
        names = []
        with tarfile.open(final_tar, "r:*") as tf:
            names = tf.getnames()
        # 最终合成全量应包含新增文件 new1.txt 与更新后的 f0.txt
        assert "new1.txt" in names, f"合成全量缺少增量新增文件: {names}"
        print(f"[OK] 合成全量可恢复，包含增量文件 new1.txt; 总文件数={len(names)}")
        print("\n=== 合成全量回归测试通过 ===")
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
