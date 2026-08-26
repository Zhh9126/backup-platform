"""全局重删（DBackup §2.4）回归测试：验证跨任务去重真实生效。

不连主库：临时切换 BACKUP_ROOT，并在独立临时库运行 dedup_index 记账。
断言：
1. 相同内容的两次写入（视为不同任务）第二次命中，saved_bytes>0；
2. global_stats 的 physical_bytes < logical_bytes，dedup_ratio_pct>0；
3. 不同内容的块不互相命中（saved=0 且 new_blocks 增加）。
"""
import os
import sys
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import core.db as db
from core import global_dedup as gd


_ORIG_ROOT = None
_CONN = None


def _setup_tmp_db_and_store():
    global _ORIG_ROOT, _CONN
    tmp = tempfile.mkdtemp(prefix="dedup_test_")
    # 独立 sqlite，避免污染主库
    db_path = os.path.join(tmp, "dedup_test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE dedup_index ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, block_hash TEXT NOT NULL, "
        "size_bytes INTEGER DEFAULT 0, ref_count INTEGER DEFAULT 1, "
        "first_task_id INTEGER, first_set_id INTEGER, object_key TEXT, "
        "created_at TEXT)")
    conn.commit()
    _CONN = conn

    # 替换 db 的执行接口指向临时库
    def _execute(sql, params=()):
        conn.execute(sql, params)
        conn.commit()
        return 0

    def _query_one(sql, params=()):
        cur = conn.execute(sql, params)
        return cur.fetchone()

    db.execute = _execute
    db.query_one = _query_one

    # 独立 dedup 存储目录
    _ORIG_ROOT = config.BACKUP_ROOT
    config.BACKUP_ROOT = tmp
    return tmp


def _teardown():
    global _ORIG_ROOT, _CONN
    if _CONN:
        _CONN.close()
    if _ORIG_ROOT is not None:
        config.BACKUP_ROOT = _ORIG_ROOT


def _mk_payload(n, base=b"X"):
    # 生成 n 个互不相同的 1KB 块（块内填充相同，块间用序号区分），
    # 便于第一次写入时全部为"新块"，第二次复用同一组块时全部命中。
    return b"".join((base + bytes([i])) * (1024 // max(1, len(base) + 1))
                    for i in range(n))


def main():
    tmp = _setup_tmp_db_and_store()
    try:
        # 块大小用 1KB 便于稳定命中
        BS = 1024
        payload = _mk_payload(8)  # 8KB，重复内容

        # 任务 A 首次写入
        r1 = gd.dedup_bytes(payload, task_id=1, set_id=1, block_size=BS)
        assert r1["saved_bytes"] == 0, f"首次写入不应有节省, got {r1}"
        assert r1["new_blocks"] == 8, f"应产生 8 个新块, got {r1}"

        # 任务 B 写入相同内容 → 全局命中
        r2 = gd.dedup_bytes(payload, task_id=2, set_id=2, block_size=BS)
        assert r2["saved_bytes"] == 8 * BS, f"第二次应省 8KB, got {r2}"
        assert r2["new_blocks"] == 0, f"不应有新块, got {r2}"

        stats = gd.global_stats()
        assert stats["unique_blocks"] == 8, stats
        assert stats["physical_bytes"] == 8 * BS, stats
        # 引用计数：任务A(8次) + 任务B(8次) = 16
        assert stats["total_references"] == 16, stats
        # logical = 各块原始大小×引用次数 = 8×1024×16 = 131072
        assert stats["logical_bytes"] == 8 * BS * 16, stats
        # saved = logical - physical = 131072 - 8192 = 122880
        assert stats["saved_bytes"] == 8 * BS * 15, stats
        assert stats["dedup_ratio_pct"] > 90, stats
        print(f"[OK] 跨任务重删: {stats}")

        # 不同内容（不同基线字母）→ 不互相命中
        other = _mk_payload(8, base=b"Y")
        r3 = gd.dedup_bytes(other, task_id=3, set_id=3, block_size=BS)
        assert r3["saved_bytes"] == 0, f"不同内容不应命中, got {r3}"
        assert r3["new_blocks"] == 8, r3
        print(f"[OK] 异内容不命中: {r3}")

        # 文件级 dedup 同样生效
        fpath = os.path.join(tmp, "dup.bin")
        with open(fpath, "wb") as f:
            f.write(payload)
        rf = gd.dedup_file(fpath, task_id=4, set_id=4, block_size=BS)
        assert rf["saved_bytes"] == 8 * BS, rf
        print(f"[OK] 文件级重删命中: {rf}")

        print("\n=== 全局重删回归测试全部通过 ===")
        return 0
    except AssertionError as e:
        print("FAIL:", e)
        return 1
    finally:
        _teardown()


if __name__ == "__main__":
    sys.exit(main())
