# -*- coding: utf-8 -*-
"""
T01 单元测试：数据模型 + LogRepository + RecoveryJournal。

运行方式（必须用系统 Python 3.14.3，DEMO_MODE=on）：
    SET DEMO_MODE=on
    python tests/test_rt_journal.py

覆盖 T01 验收标准：
  1. 两张新表与 backup_tasks 6 个新列自动创建，重复 init_schema 幂等；
  2. append() 连续写入 1000 个恢复点（含同秒多点），pit_seq 正确自增；
  3. resolve_chain() 对「1 全量 + 20 增量」返回 21 且升序；缺失中间节点时
     validate_chain() 返回 (False, 具体原因)；
  4. LogRepository.seal() 对已存在的目标文件仍能原子替换；make_bundle() 可解包；
  5. prune() 删除过期点但不删仍被有效链引用的 full 链头，DB 行与磁盘同步；
  6. timeline() 桶聚合与缺口检测正确。
"""

import os
import sys
import shutil
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="rt_journal_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
os.environ["SCHEDULER_ENABLED"] = "false"
os.makedirs(os.environ["INSTANCE_DIR"], exist_ok=True)
os.makedirs(os.environ["LOG_DIR"], exist_ok=True)
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402

db.init_schema()

import core.models as models                    # noqa: E402
from core.rt_backup.journal import RecoveryJournal   # noqa: E402
from core.rt_backup.repo import LogRepository        # noqa: E402
from core.rt_backup.types import (                   # noqa: E402
    KIND_DB_LOG,
    KIND_FILE,
    RP_BASE_FULL,
    RP_DB_LOG,
    RP_FILE_INC,
    RecoveryPoint,
    RtConfig,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _mk_task(name: str, db_type: str = "file", **extra) -> int:
    """创建一个最小可用的备份任务并打开实时保护。"""
    task_id = models.create_task({
        "name": name,
        "db_type": db_type,
        "host": "127.0.0.1",
        "port": 3306,
        "username": "root",
        "password": "",
        "db_name": "demo",
        "backup_type": "full",
        "schedule_type": "manual",
        "enabled": 1,
        "demo_only": 1,
    })
    updates = {"rt_enabled": 1}
    updates.update(extra)
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE backup_tasks SET {sets} WHERE id=?",
               tuple(updates.values()) + (task_id,))
    return task_id


def _mk_file(path: str, content: bytes = b"x" * 128) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


# ============================ 1. Schema 迁移 ============================
class TestSchemaMigration(unittest.TestCase):
    """验收 1：新表 / 新列自动创建且幂等。"""

    def test_new_tables_exist(self):
        rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r["name"] for r in rows}
        self.assertIn("recovery_journal", names)
        self.assertIn("rt_capture_state", names)

    def test_indexes_exist(self):
        rows = db.query("SELECT name FROM sqlite_master WHERE type='index'")
        names = {r["name"] for r in rows}
        for idx in ("idx_rj_task_time", "idx_rj_kind", "idx_rj_obj"):
            self.assertIn(idx, names, f"缺少索引 {idx}")

    def test_backup_tasks_new_columns(self):
        cols = {r["name"] for r in db.query("PRAGMA table_info(backup_tasks)")}
        for col in ("rt_enabled", "rt_mode", "rt_interval_sec",
                    "rt_consistency", "rt_log_retention_days", "rt_rpo_target_sec"):
            self.assertIn(col, cols, f"缺少列 {col}")

    def test_init_schema_idempotent(self):
        """在已有库上重复迁移不报错、不丢数据。"""
        task_id = _mk_task("幂等校验任务")
        db.init_schema()
        db.init_schema()
        self.assertIsNotNone(models.get_task(task_id))


# ============================ 2. RtConfig ============================
class TestRtConfig(unittest.TestCase):
    """RtConfig.from_task() 的默认值合成逻辑。"""

    def test_file_task_defaults(self):
        task_id = _mk_task("文件任务A", db_type="file")
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        self.assertEqual(cfg.capture_kind, KIND_FILE)
        self.assertEqual(cfg.interval_sec, config.RT_FILE_INTERVAL_SEC)
        self.assertEqual(cfg.rpo_target_sec, config.RT_FILE_RPO_TARGET_SEC)
        self.assertTrue(cfg.enabled)

    def test_mysql_task_defaults(self):
        task_id = _mk_task("MySQL任务A", db_type="mysql")
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        self.assertEqual(cfg.capture_kind, KIND_DB_LOG)
        self.assertEqual(cfg.rpo_target_sec, config.RT_DB_RPO_TARGET_SEC)
        self.assertEqual(cfg.log_retention_days, config.RT_DB_LOG_RETENTION_DAYS)

    def test_task_level_override(self):
        task_id = _mk_task("覆盖任务", db_type="file",
                           rt_interval_sec=60, rt_rpo_target_sec=90,
                           rt_log_retention_days=3, rt_consistency="fs")
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        self.assertEqual(cfg.interval_sec, 60)
        self.assertEqual(cfg.rpo_target_sec, 90)
        self.assertEqual(cfg.log_retention_days, 3)
        self.assertEqual(cfg.consistency, "fs")


# ============================ 3. Journal 写入 ============================
class TestJournalAppend(unittest.TestCase):
    """验收 2：1000 个恢复点（含同秒多点），pit_seq 自增、唯一索引不冲突。"""

    @classmethod
    def setUpClass(cls):
        cls.task_id = _mk_task("批量写入任务")
        cls.jnl = RecoveryJournal()
        cls.work = os.path.join(_TMP, "append_work")
        os.makedirs(cls.work, exist_ok=True)

    def test_bulk_append_1000(self):
        base_dt = datetime(2026, 7, 31, 10, 0, 0).astimezone()
        total = 1000
        # 本类使用 setUpClass 共享同一 task_id，同类其他用例可能已写入恢复点，
        # 故以增量（delta）方式断言，保证用例顺序无关。
        baseline = self.jnl.count(self.task_id)
        for i in range(total):
            # 每 10 个点共用同一秒 → 强制走 pit_seq 自增分支
            pit_at = _iso(base_dt + timedelta(seconds=i // 10))
            obj = _mk_file(os.path.join(self.work, f"seg-{i:04d}.tar.gz"))
            self.jnl.append(self.task_id, {
                "rp_kind": RP_FILE_INC,
                "pit_at": pit_at,
                "object_key": obj,
                "changed_files": 1,
            })
        self.assertEqual(self.jnl.count(self.task_id) - baseline, total)

        # 同秒内 pit_seq 必须 0..9 各一个，无重复
        first_sec = _iso(base_dt)
        rows = models.list_recovery_points(task_id=self.task_id,
                                           start=first_sec, end=first_sec,
                                           limit=100, order="asc")
        seqs = sorted(int(r["pit_seq"]) for r in rows)
        self.assertEqual(seqs, list(range(10)))

    def test_append_auto_checksum_and_size(self):
        obj = _mk_file(os.path.join(self.work, "auto.tar.gz"), b"hello-rt" * 16)
        rp = self.jnl.append(self.task_id, {
            "rp_kind": RP_FILE_INC,
            "object_key": obj,
        })
        self.assertEqual(rp.size_bytes, os.path.getsize(obj))
        self.assertEqual(rp.checksum, db.sha256_file(obj))

    def test_append_idempotent_on_same_object_key(self):
        obj = _mk_file(os.path.join(self.work, "dup.tar.gz"))
        rp1 = self.jnl.append(self.task_id, {"rp_kind": RP_FILE_INC,
                                             "object_key": obj, "message": "v1"})
        rp2 = self.jnl.append(self.task_id, {"rp_kind": RP_FILE_INC,
                                             "object_key": obj, "message": "v2"})
        self.assertEqual(rp1.id, rp2.id, "同 object_key 应幂等更新而非新增")
        self.assertEqual(rp2.message, "v2")

    def test_expires_at_from_retention(self):
        obj = _mk_file(os.path.join(self.work, "exp.tar.gz"))
        pit_at = _iso(datetime(2026, 1, 1, 0, 0, 0))
        rp = self.jnl.append(self.task_id, {
            "rp_kind": RP_FILE_INC, "pit_at": pit_at,
            "object_key": obj, "retention_days": 7,
        })
        self.assertTrue(rp.expires_at.startswith("2026-01-08"))


# ============================ 4. 恢复链 ============================
class TestResolveChain(unittest.TestCase):
    """验收 3：链解析长度/顺序，以及缺口检测。"""

    def setUp(self):
        self.task_id = _mk_task("恢复链任务")
        self.jnl = RecoveryJournal()
        self.work = os.path.join(_TMP, f"chain_{self.task_id}")
        os.makedirs(self.work, exist_ok=True)
        self.base_dt = datetime(2026, 7, 31, 8, 0, 0).astimezone()

    def _build_chain(self, inc_count: int = 20):
        """造 1 个 base-full + inc_count 个 file-inc，返回全部 RecoveryPoint。"""
        points = []
        full_obj = _mk_file(os.path.join(self.work, "base.tar.gz"), b"F" * 256)
        full = self.jnl.append(self.task_id, {
            "rp_kind": RP_BASE_FULL, "rp_type": "full",
            "pit_at": _iso(self.base_dt), "object_key": full_obj,
        })
        points.append(full)
        parent = full.id
        for i in range(inc_count):
            obj = _mk_file(os.path.join(self.work, f"inc-{i:02d}.tar.gz"),
                           bytes([65 + i % 26]) * 64)
            point = self.jnl.append(self.task_id, {
                "rp_kind": RP_FILE_INC, "rp_type": "incremental",
                "pit_at": _iso(self.base_dt + timedelta(minutes=3 * (i + 1))),
                "parent_rp_id": parent, "object_key": obj, "changed_files": i + 1,
            })
            points.append(point)
            parent = point.id
        return points

    def test_chain_length_and_order(self):
        self._build_chain(20)
        target = _iso(self.base_dt + timedelta(hours=3))
        chain = self.jnl.resolve_chain(self.task_id, target)
        self.assertEqual(len(chain), 21)
        self.assertTrue(chain[0].is_full)
        stamps = [p.pit_at for p in chain]
        self.assertEqual(stamps, sorted(stamps), "恢复链必须按 pit_at 升序")

        ok, reason = self.jnl.validate_chain(chain)
        self.assertTrue(ok, f"完整链应校验通过，实际: {reason}")

    def test_chain_truncated_by_target_ts(self):
        self._build_chain(20)
        # 只取到第 5 个增量（base + 5）
        target = _iso(self.base_dt + timedelta(minutes=3 * 5 + 1))
        chain = self.jnl.resolve_chain(self.task_id, target)
        self.assertEqual(len(chain), 6)

    def test_missing_middle_node_detected(self):
        """删掉链中间一个节点 → validate_chain 必须报错并给出原因。"""
        points = self._build_chain(20)
        victim = points[10]
        models.delete_recovery_points([victim.id])
        target = _iso(self.base_dt + timedelta(hours=3))
        chain = self.jnl.resolve_chain(self.task_id, target)
        ok, reason = self.jnl.validate_chain(chain)
        self.assertFalse(ok)
        self.assertIn("断裂", reason)

    def test_missing_artifact_detected(self):
        """产物文件被删 → 校验失败并指出缺失路径。"""
        points = self._build_chain(5)
        os.remove(points[3].object_key)
        chain = self.jnl.resolve_chain(self.task_id,
                                       _iso(self.base_dt + timedelta(hours=1)))
        ok, reason = self.jnl.validate_chain(chain)
        self.assertFalse(ok)
        self.assertIn("产物缺失", reason)

    def test_checksum_mismatch_detected(self):
        points = self._build_chain(3)
        with open(points[2].object_key, "wb") as fh:
            fh.write(b"TAMPERED")
        chain = self.jnl.resolve_chain(self.task_id,
                                       _iso(self.base_dt + timedelta(hours=1)))
        ok, reason = self.jnl.validate_chain(chain)
        self.assertFalse(ok)
        self.assertIn("校验和不匹配", reason)

    def test_no_full_head_detected(self):
        """只有增量、没有全量 → 明确提示链头缺失。"""
        obj = _mk_file(os.path.join(self.work, "lonely.tar.gz"))
        self.jnl.append(self.task_id, {
            "rp_kind": RP_FILE_INC, "pit_at": _iso(self.base_dt),
            "object_key": obj,
        })
        chain = self.jnl.resolve_chain(self.task_id,
                                       _iso(self.base_dt + timedelta(hours=1)))
        ok, reason = self.jnl.validate_chain(chain)
        self.assertFalse(ok)
        self.assertIn("链头", reason)

    def test_empty_chain(self):
        ok, reason = self.jnl.validate_chain([])
        self.assertFalse(ok)
        self.assertIn("为空", reason)

    def test_nearest_before_and_latest(self):
        points = self._build_chain(10)
        # 目标点落在 points[4](base+12min) 与 points[5](base+15min) 之间，
        # 因此 nearest_before 应命中 points[4]。
        target = _iso(self.base_dt + timedelta(minutes=3 * 4, seconds=30))
        near = self.jnl.nearest_before(self.task_id, target)
        self.assertIsNotNone(near)
        self.assertEqual(near.id, points[4].id)
        latest = self.jnl.latest(self.task_id)
        self.assertEqual(latest.id, points[-1].id)


# ============================ 5. DB 段位点连续性 ============================
class TestDbSegmentContinuity(unittest.TestCase):
    """binlog / WAL 位点连续性校验。"""

    def setUp(self):
        self.task_id = _mk_task("MySQL链任务", db_type="mysql")
        self.jnl = RecoveryJournal()
        self.work = os.path.join(_TMP, f"dbchain_{self.task_id}")
        os.makedirs(self.work, exist_ok=True)
        self.base_dt = datetime(2026, 7, 31, 9, 0, 0).astimezone()

    def _seg(self, idx: int, start_pos: int, end_pos: int,
             file_name: str, parent: int = None):
        obj = _mk_file(os.path.join(self.work, f"{file_name}.{idx}"), b"B" * 100)
        return self.jnl.append(self.task_id, {
            "rp_kind": RP_DB_LOG, "rp_type": "log-segment",
            "pit_at": _iso(self.base_dt + timedelta(minutes=5 * idx)),
            "parent_rp_id": parent,
            "binlog_file": file_name, "binlog_pos": start_pos,
            "binlog_end_file": file_name, "binlog_end_pos": end_pos,
            "object_key": obj,
        })

    def test_continuous_segments_pass(self):
        full_obj = _mk_file(os.path.join(self.work, "full.sql"), b"D" * 200)
        full = self.jnl.append(self.task_id, {
            "rp_kind": "db-full", "rp_type": "full",
            "pit_at": _iso(self.base_dt), "object_key": full_obj,
        })
        s1 = self._seg(1, 4, 1000, "mysql-bin.000123", parent=full.id)
        self._seg(2, 1000, 2000, "mysql-bin.000123", parent=s1.id)
        chain = self.jnl.resolve_chain(self.task_id,
                                       _iso(self.base_dt + timedelta(hours=1)))
        ok, reason = self.jnl.validate_chain(chain)
        self.assertTrue(ok, reason)

    def test_position_gap_detected(self):
        full_obj = _mk_file(os.path.join(self.work, "full2.sql"), b"D" * 200)
        full = self.jnl.append(self.task_id, {
            "rp_kind": "db-full", "rp_type": "full",
            "pit_at": _iso(self.base_dt), "object_key": full_obj,
        })
        s1 = self._seg(1, 4, 1000, "mysql-bin.000200", parent=full.id)
        # 后段起点 5000 > 前段终点 1000 → 中间有缺口
        self._seg(2, 5000, 6000, "mysql-bin.000200", parent=s1.id)
        chain = self.jnl.resolve_chain(self.task_id,
                                       _iso(self.base_dt + timedelta(hours=1)))
        ok, reason = self.jnl.validate_chain(chain)
        self.assertFalse(ok)
        self.assertIn("不连续", reason)

    def test_lsn_parse(self):
        self.assertEqual(RecoveryJournal._lsn_to_int("0/1A2B3C48"), 0x1A2B3C48)
        self.assertEqual(RecoveryJournal._lsn_to_int("1/0"), 1 << 32)
        self.assertEqual(RecoveryJournal._lsn_to_int("bad"), 0)


# ============================ 6. LogRepository ============================
class TestLogRepository(unittest.TestCase):
    """验收 4：seal 原子替换 + make_bundle 可解包 + 容量守护。"""

    def setUp(self):
        self.task_id = _mk_task("仓库任务")
        self.root = os.path.join(_TMP, f"repo_{self.task_id}")
        self.repo = LogRepository(self.task_id, capture_kind=KIND_DB_LOG, root=self.root)

    def test_dir_layout(self):
        self.assertTrue(os.path.isdir(self.repo.live_dir()))
        self.assertTrue(os.path.isdir(self.repo.sealed_dir()))
        self.assertTrue(os.path.isdir(self.repo.base_dir()))
        self.assertTrue(os.path.isdir(self.repo.inc_dir()))
        self.assertTrue(os.path.isdir(self.repo.bundle_dir()))

    def test_seal_moves_and_hashes(self):
        src = _mk_file(os.path.join(self.repo.live_dir(), "mysql-bin.000001"),
                       b"S" * 512)
        expect_sum = db.sha256_file(src)
        info = self.repo.seal(src, kind="db-log")
        self.assertFalse(os.path.exists(src), "封存后 live 文件应被移除")
        self.assertTrue(os.path.isfile(info["path"]))
        self.assertEqual(info["size"], 512)
        self.assertEqual(info["checksum"], expect_sum)

    def test_seal_replaces_existing_target(self):
        """目标同名文件已存在时不覆盖原有段，而是加时间戳后缀。"""
        first = _mk_file(os.path.join(self.repo.live_dir(), "dup.bin"), b"1" * 32)
        info1 = self.repo.seal(first)
        second = _mk_file(os.path.join(self.repo.live_dir(), "dup.bin"), b"2" * 64)
        info2 = self.repo.seal(second)
        self.assertNotEqual(info1["path"], info2["path"])
        self.assertTrue(os.path.isfile(info1["path"]))
        self.assertTrue(os.path.isfile(info2["path"]))

    def test_seal_rejects_empty_segment(self):
        """R9：空包绝不入 journal —— seal 返回 None 而非抛异常。"""
        empty = _mk_file(os.path.join(self.repo.live_dir(), "empty.bin"), b"")
        self.assertIsNone(self.repo.seal(empty))
        self.assertIsNone(self.repo.seal(
            os.path.join(self.repo.live_dir(), "not-exist.bin")))

    def test_seal_keep_source(self):
        """keep_source=True 时源文件保留（子进程仍持有句柄的场景）。"""
        src = _mk_file(os.path.join(self.repo.live_dir(), "keep.bin"), b"K" * 64)
        info = self.repo.seal(src, keep_source=True)
        self.assertIsNotNone(info)
        self.assertTrue(os.path.isfile(src))
        self.assertTrue(os.path.isfile(info["path"]))

    def test_seal_routes_by_kind(self):
        """kind 决定落盘子目录：base-full → base/，file-inc → inc/。"""
        src_full = _mk_file(os.path.join(self.repo.live_dir(), "b.tar.gz"), b"B" * 32)
        info_full = self.repo.seal(src_full, kind="base-full")
        self.assertEqual(os.path.dirname(info_full["path"]), self.repo.base_dir())

        src_inc = _mk_file(os.path.join(self.repo.live_dir(), "i.tar.gz"), b"I" * 32)
        info_inc = self.repo.seal(src_inc, kind="file-inc")
        self.assertEqual(os.path.dirname(info_inc["path"]), self.repo.inc_dir())

    def test_state_roundtrip(self):
        self.assertEqual(self.repo.load_state(), {})
        self.repo.save_state({"binlog_file": "mysql-bin.000009", "pos": 4})
        state = self.repo.load_state()
        self.assertEqual(state["binlog_file"], "mysql-bin.000009")
        self.assertEqual(state["pos"], 4)
        self.assertTrue(state["saved_at"])

    def test_state_corrupted_returns_empty(self):
        with open(self.repo.state_path(), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(self.repo.load_state(), {})

    def test_make_bundle_and_extract(self):
        points = []
        for i in range(4):
            path = _mk_file(os.path.join(self.repo.sealed_dir(), f"seg{i}.bin"),
                            bytes([48 + i]) * 100)
            points.append({"id": 1000 + i, "object_key": path})
        bundle = self.repo.make_bundle(points, max_mb=64)
        self.assertIsNotNone(bundle)
        self.assertEqual(len(bundle["members"]), 4)
        self.assertEqual(bundle["checksum"], db.sha256_file(bundle["path"]))

        out = os.path.join(_TMP, f"unbundle_{self.task_id}")
        os.makedirs(out, exist_ok=True)
        with tarfile.open(bundle["path"], "r:gz") as tar:
            tar.extractall(out)
        self.assertTrue(os.path.isfile(os.path.join(out, "_manifest.json")))
        for i in range(4):
            self.assertTrue(os.path.isfile(os.path.join(out, f"seg{i}.bin")))

    def test_make_bundle_empty_returns_none(self):
        self.assertIsNone(self.repo.make_bundle([]))
        self.assertIsNone(self.repo.make_bundle(
            [{"id": 1, "object_key": os.path.join(_TMP, "not-exist.bin")}]))

    def test_disk_usage_levels(self):
        _mk_file(os.path.join(self.repo.inc_dir(), "a.bin"), b"A" * 4096)
        usage = self.repo.disk_usage()
        self.assertGreaterEqual(usage["bytes"], 4096)
        self.assertIn(usage["level"], ("ok", "warn", "full"))
        # LogRepository.disk_usage() 的契约暴露 quota_gb（非 quota_bytes），
        # 校验配额来源于 config.RT_DISK_QUOTA_GB 且占比换算正确。
        self.assertEqual(usage["quota_gb"], config.RT_DISK_QUOTA_GB)
        quota_bytes = config.RT_DISK_QUOTA_GB * 1024 * 1024 * 1024
        self.assertAlmostEqual(usage["used_percent"],
                               round(usage["bytes"] * 100.0 / quota_bytes, 2),
                               places=2)

    def test_remove_object_rejects_outside_path(self):
        outside = _mk_file(os.path.join(_TMP, "outside.bin"))
        self.assertFalse(self.repo.remove_object(outside))
        self.assertTrue(os.path.isfile(outside), "仓库外文件不得被删除")

    def test_prune_removes_orphan_only(self):
        """未登记 journal 的旧文件被清理；已登记的保留。"""
        jnl = RecoveryJournal()
        known = _mk_file(os.path.join(self.repo.sealed_dir(), "known.bin"), b"K" * 32)
        orphan = _mk_file(os.path.join(self.repo.sealed_dir(), "orphan.bin"), b"O" * 32)
        jnl.append(self.task_id, {"rp_kind": RP_DB_LOG, "object_key": known})
        old_time = (datetime.now() - timedelta(days=30)).timestamp()
        os.utime(known, (old_time, old_time))
        os.utime(orphan, (old_time, old_time))

        removed = self.repo.prune(_iso(datetime.now() - timedelta(days=1)))
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.isfile(known))
        self.assertFalse(os.path.exists(orphan))


# ============================ 7. prune 安全 ============================
class TestJournalPrune(unittest.TestCase):
    """验收 5：过期点被清理，但有效链头永不删除。"""

    def setUp(self):
        self.task_id = _mk_task("清理任务")
        self.jnl = RecoveryJournal()
        self.root = os.path.join(_TMP, f"prune_{self.task_id}")
        self.repo = LogRepository(self.task_id, capture_kind=KIND_FILE, root=self.root)

    def _point(self, kind: str, days_ago: float, name: str, parent=None):
        pit = _iso(datetime.now().astimezone() - timedelta(days=days_ago))
        obj = _mk_file(os.path.join(self.repo.inc_dir(), name), b"P" * 64)
        return self.jnl.append(self.task_id, {
            "rp_kind": kind,
            "rp_type": "full" if kind == RP_BASE_FULL else "incremental",
            "pit_at": pit, "parent_rp_id": parent, "object_key": obj,
        })

    def test_prune_keeps_referenced_full_head(self):
        # 20 天前的全量 + 15 天前的增量（都过期） + 2 天前的增量（未过期）
        full = self._point(RP_BASE_FULL, 20, "old-base.tar.gz")
        old_inc = self._point(RP_FILE_INC, 15, "old-inc.tar.gz", parent=full.id)
        fresh = self._point(RP_FILE_INC, 2, "fresh-inc.tar.gz", parent=old_inc.id)

        removed = self.jnl.prune(self.task_id, retention_days=7, repo=self.repo)
        self.assertEqual(removed, 1, "只应删除 15 天前那个过期增量")

        remaining = {p.id for p in self.jnl.list_points(self.task_id, limit=100)}
        self.assertIn(full.id, remaining, "有效链头（全量）不得被删除")
        self.assertIn(fresh.id, remaining)
        self.assertNotIn(old_inc.id, remaining)
        # DB 行与磁盘同步
        self.assertFalse(os.path.exists(old_inc.object_key))
        self.assertTrue(os.path.isfile(full.object_key))

    def test_prune_keeps_last_full_when_all_expired(self):
        full = self._point(RP_BASE_FULL, 30, "b1.tar.gz")
        inc = self._point(RP_FILE_INC, 25, "i1.tar.gz", parent=full.id)
        removed = self.jnl.prune(self.task_id, retention_days=7, repo=self.repo)
        self.assertEqual(removed, 1)
        remaining = {p.id for p in self.jnl.list_points(self.task_id, limit=100)}
        self.assertEqual(remaining, {full.id},
                         "全部过期时仍应保留最近一个全量作为兜底")
        self.assertNotIn(inc.id, remaining)

    def test_prune_noop_when_nothing_expired(self):
        self._point(RP_BASE_FULL, 1, "n1.tar.gz")
        self._point(RP_FILE_INC, 0.5, "n2.tar.gz")
        self.assertEqual(self.jnl.prune(self.task_id, retention_days=7,
                                        repo=self.repo), 0)


# ============================ 8. 时间轴 ============================
class TestTimeline(unittest.TestCase):
    """timeline() 桶聚合、缺口检测与出参结构。"""

    def setUp(self):
        self.task_id = _mk_task("时间轴任务")
        self.jnl = RecoveryJournal()
        self.work = os.path.join(_TMP, f"tl_{self.task_id}")
        os.makedirs(self.work, exist_ok=True)
        self.base_dt = datetime(2026, 7, 31, 0, 0, 0).astimezone()

    def test_buckets_and_totals(self):
        for i in range(30):
            obj = _mk_file(os.path.join(self.work, f"p{i}.tar.gz"), b"T" * 50)
            self.jnl.append(self.task_id, {
                "rp_kind": RP_FILE_INC,
                "pit_at": _iso(self.base_dt + timedelta(minutes=3 * i)),
                "object_key": obj,
            })
        data = self.jnl.timeline(
            self.task_id,
            start=_iso(self.base_dt - timedelta(minutes=5)),
            end=_iso(self.base_dt + timedelta(hours=2)),
            buckets=60)
        self.assertEqual(data["kind"], KIND_FILE)
        self.assertEqual(len(data["buckets"]), 60)
        self.assertEqual(data["total"], 30)
        self.assertEqual(sum(b["count"] for b in data["buckets"]), 30)
        self.assertEqual(data["total_bytes"], 30 * 50)

    def test_gap_detected(self):
        stamps = [0, 3, 6, 9, 12, 200, 203, 206]      # 分钟；12→200 是明显缺口
        for i, minute in enumerate(stamps):
            obj = _mk_file(os.path.join(self.work, f"g{i}.tar.gz"), b"G" * 20)
            self.jnl.append(self.task_id, {
                "rp_kind": RP_FILE_INC,
                "pit_at": _iso(self.base_dt + timedelta(minutes=minute)),
                "object_key": obj,
            })
        data = self.jnl.timeline(
            self.task_id,
            start=_iso(self.base_dt - timedelta(minutes=5)),
            end=_iso(self.base_dt + timedelta(hours=5)),
            buckets=100)
        self.assertTrue(data["gaps"], "应检测到时间轴缺口")
        self.assertTrue(any(b["has_gap"] for b in data["buckets"]))

    def test_timeline_empty_task(self):
        data = self.jnl.timeline(self.task_id, buckets=20)
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["buckets"]), 20)
        self.assertEqual(data["gaps"], [])


# ============================ 9. rt_capture_state ============================
class TestRtState(unittest.TestCase):
    """UPSERT 语义：部分字段更新不清空其余列。"""

    def setUp(self):
        self.task_id = _mk_task("状态任务", db_type="mysql")

    def test_upsert_partial_update(self):
        models.upsert_rt_state(self.task_id, {
            "capture_kind": "db-log", "engine": "mysql",
            "daemon_status": "running", "pid": 4321,
            "last_binlog_file": "mysql-bin.000007", "last_binlog_pos": 154,
        })
        models.upsert_rt_state(self.task_id, {"lag_sec": 12, "health": "green"})
        state = models.get_rt_state(self.task_id)
        self.assertEqual(state["daemon_status"], "running")
        self.assertEqual(state["pid"], 4321)
        self.assertEqual(state["last_binlog_file"], "mysql-bin.000007")
        self.assertEqual(state["lag_sec"], 12)
        self.assertEqual(state["health"], "green")
        self.assertTrue(state["updated_at"])

    def test_list_and_delete(self):
        models.upsert_rt_state(self.task_id, {"daemon_status": "stopped"})
        states = models.list_rt_states([self.task_id])
        self.assertEqual(len(states), 1)
        models.delete_rt_state(self.task_id)
        self.assertIsNone(models.get_rt_state(self.task_id))

    def test_list_rt_tasks(self):
        rows = models.list_rt_tasks()
        self.assertTrue(any(r["id"] == self.task_id for r in rows))
        ids = {r["id"] for r in rows}
        # 未开启 rt_enabled 的任务不应出现
        plain = models.create_task({
            "name": "普通任务", "db_type": "mysql", "host": "127.0.0.1",
            "port": 3306, "username": "root", "db_name": "d",
            "backup_type": "full", "schedule_type": "manual", "enabled": 1,
        })
        self.assertNotIn(plain, ids)


# ============================ 10. RecoveryPoint 数据类 ============================
class TestRecoveryPointType(unittest.TestCase):
    def test_from_row_defaults(self):
        rp = RecoveryPoint.from_row({})
        self.assertEqual(rp.id, 0)
        self.assertEqual(rp.rp_kind, RP_FILE_INC)
        self.assertEqual(rp.storage_tier, 1)
        self.assertIsNone(rp.parent_rp_id)

    def test_position_label(self):
        self.assertEqual(
            RecoveryPoint(binlog_file="mysql-bin.000001", binlog_pos=154)
            .position_label(), "mysql-bin.000001:154")
        self.assertEqual(
            RecoveryPoint(wal_lsn="0/1A2B3C48").position_label(), "0/1A2B3C48")
        self.assertEqual(
            RecoveryPoint(changed_files=3, deleted_files=1).position_label(),
            "+3/-1")
        self.assertEqual(RecoveryPoint().position_label(), "-")

    def test_is_full(self):
        self.assertTrue(RecoveryPoint(rp_kind=RP_BASE_FULL).is_full)
        self.assertTrue(RecoveryPoint(rp_kind="db-full").is_full)
        self.assertTrue(RecoveryPoint(rp_type="full").is_full)
        self.assertFalse(RecoveryPoint(rp_kind=RP_FILE_INC).is_full)

    def test_to_dict_normalizes_path(self):
        rp = RecoveryPoint(object_key="C:\\data\\a.tar.gz")
        self.assertEqual(rp.to_dict()["object_key"], "C:/data/a.tar.gz")


def _main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n通过率: {total - failed}/{total}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    code = 1
    try:
        code = _main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
