# -*- coding: utf-8 -*-
"""
T01 单元测试：数据模型 + 日志仓库 + PIT Journal。

运行方式（必须用系统 Python 3.14.3，DEMO_MODE=on）：
    SET DEMO_MODE=on
    python tests/test_rt_t01.py

覆盖 T01 验收标准：
  1. db init 后三张新表（recovery_journal / rt_tasks / log_repository）存在
  2. create/get/update/delete rt_task 往返一致
  3. create/list_recovery_points 正常工作
  4. find_chain 返回有序恢复链
  5. LogRepository init_repo 创建目录、check_quota 正常
  6. RecoveryJournal record + list_by_time_range 正常
"""

import os
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="rt_t01_")
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
from core.rt.log_repo import LogRepository      # noqa: E402
from core.rt.journal import RecoveryJournal     # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _mk_task(name: str, db_type: str = "file") -> int:
    """创建一个最小可用的备份任务。"""
    return models.create_task({
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


def _mk_file(path: str, content: bytes = b"x" * 128) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


# ============================ 1. Schema ============================
class TestSchema(unittest.TestCase):
    """验收 1：三张新表与索引自动创建且幂等。"""

    def test_new_tables_exist(self):
        """recovery_journal、rt_tasks、log_repository 三张表必须存在。"""
        rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r["name"] for r in rows}
        self.assertIn("recovery_journal", names)
        self.assertIn("rt_tasks", names)
        self.assertIn("log_repository", names)

    def test_indexes_exist(self):
        """recovery_journal 的索引必须存在。"""
        rows = db.query("SELECT name FROM sqlite_master WHERE type='index'")
        names = {r["name"] for r in rows}
        for idx in ("idx_rj_task_time", "idx_rj_kind", "idx_rj_obj"):
            self.assertIn(idx, names, f"缺少索引 {idx}")

    def test_init_schema_idempotent(self):
        """重复 init_schema 不报错、不丢数据。"""
        task_id = _mk_task("幂等校验")
        db.init_schema()
        db.init_schema()
        self.assertIsNotNone(models.get_task(task_id))

    def test_rt_tasks_columns(self):
        """rt_tasks 表的列完整。"""
        cols = {r["name"] for r in db.query("PRAGMA table_info(rt_tasks)")}
        for col in ("task_id", "rt_mode", "capture_interval",
                    "db_log_retention_days", "file_inc_retention_days",
                    "db_flush_interval", "is_running", "last_tick_at",
                    "health_status", "rpo_current_seconds", "disk_quota_gb"):
            self.assertIn(col, cols, f"rt_tasks 缺少列 {col}")

    def test_log_repository_columns(self):
        """log_repository 表的列完整。"""
        cols = {r["name"] for r in db.query("PRAGMA table_info(log_repository)")}
        for col in ("task_id", "repo_root", "db_log_dir", "file_inc_dir",
                    "current_size_bytes", "quota_bytes"):
            self.assertIn(col, cols, f"log_repository 缺少列 {col}")


# ============================ 2. rt_tasks CRUD ============================
class TestRtTaskCrud(unittest.TestCase):
    """验收 2：create/get/update/delete rt_task 往返一致。"""

    def setUp(self):
        self.task_id = _mk_task("RT任务A")

    def test_create_and_get(self):
        """创建 rt_tasks 扩展行后能正确读取。"""
        rid = models.create_rt_task({"task_id": self.task_id})
        self.assertGreater(rid, 0)
        rt = models.get_rt_task(self.task_id)
        self.assertIsNotNone(rt)
        self.assertEqual(rt["task_id"], self.task_id)
        self.assertEqual(rt["rt_mode"], "file_polling")
        self.assertEqual(rt["health_status"], "unknown")

    def test_create_with_custom_values(self):
        """自定义参数写入后读取一致。"""
        models.create_rt_task({
            "task_id": self.task_id,
            "rt_mode": "db_cdc",
            "capture_interval": 60,
            "db_flush_interval": 120,
            "disk_quota_gb": 100,
        })
        rt = models.get_rt_task(self.task_id)
        self.assertEqual(rt["rt_mode"], "db_cdc")
        self.assertEqual(rt["capture_interval"], 60)
        self.assertEqual(rt["db_flush_interval"], 120)
        self.assertEqual(rt["disk_quota_gb"], 100)

    def test_update(self):
        """更新 rt_tasks 扩展行字段。"""
        models.create_rt_task({"task_id": self.task_id})
        models.update_rt_task(self.task_id, {
            "health_status": "healthy",
            "rpo_current_seconds": 12,
            "is_running": 1,
        })
        rt = models.get_rt_task(self.task_id)
        self.assertEqual(rt["health_status"], "healthy")
        self.assertEqual(rt["rpo_current_seconds"], 12)
        self.assertTrue(rt["is_running"])

    def test_delete(self):
        """删除 rt_tasks 扩展行。"""
        models.create_rt_task({"task_id": self.task_id})
        models.delete_rt_task(self.task_id)
        self.assertIsNone(models.get_rt_task(self.task_id))

    def test_task_id_unique_constraint(self):
        """同一 task_id 不能创建两行 rt_tasks。"""
        models.create_rt_task({"task_id": self.task_id})
        with self.assertRaises(Exception):
            models.create_rt_task({"task_id": self.task_id})

    def test_update_ignores_task_id(self):
        """update_rt_task 不允许改 task_id 关联主键。"""
        models.create_rt_task({"task_id": self.task_id})
        # 尝试改 task_id 应无效
        models.update_rt_task(self.task_id, {"task_id": 99999})
        rt = models.get_rt_task(self.task_id)
        self.assertEqual(rt["task_id"], self.task_id)


# ============================ 3. log_repository CRUD ============================
class TestLogRepoCrud(unittest.TestCase):
    """log_repository 表 CRUD 往返。"""

    def setUp(self):
        self.task_id = _mk_task("仓库任务")

    def test_create_and_get(self):
        """创建日志仓库记录后能正确读取。"""
        rid = models.create_log_repo({
            "task_id": self.task_id,
            "repo_root": "/data/rt_logs/7",
            "db_log_dir": "/data/rt_logs/7/db_logs",
            "file_inc_dir": "/data/rt_logs/7/file_inc",
        })
        self.assertGreater(rid, 0)
        repo = models.get_log_repo(self.task_id)
        self.assertIsNotNone(repo)
        self.assertEqual(repo["task_id"], self.task_id)
        self.assertEqual(repo["repo_root"], "/data/rt_logs/7")
        self.assertEqual(repo["quota_bytes"], 214748364800)

    def test_update(self):
        """更新日志仓库记录字段。"""
        models.create_log_repo({
            "task_id": self.task_id,
            "repo_root": "/data/old",
        })
        models.update_log_repo(self.task_id, {
            "repo_root": "/data/new",
            "current_size_bytes": 1024000,
        })
        repo = models.get_log_repo(self.task_id)
        self.assertEqual(repo["repo_root"], "/data/new")
        self.assertEqual(repo["current_size_bytes"], 1024000)

    def test_nonexistent_returns_none(self):
        """不存在时返回 None。"""
        self.assertIsNone(models.get_log_repo(99999))


# ============================ 4. RecoveryJournal ============================
class TestRecoveryJournal(unittest.TestCase):
    """验收 3+6：record、list_by_time_range、list_by_task、get_latest。"""

    def setUp(self):
        self.task_id = _mk_task("Journal任务")
        self.jnl = RecoveryJournal()
        self.work = os.path.join(_TMP, "journal_work")
        os.makedirs(self.work, exist_ok=True)
        self.base_dt = datetime(2026, 8, 1, 10, 0, 0).astimezone()

    def test_record_and_list_by_task(self):
        """record 写入恢复点后 list_by_task 能读到。"""
        obj = _mk_file(os.path.join(self.work, "seg1.tar.gz"))
        rp_id = self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(self.base_dt),
            "object_key": obj,
        })
        self.assertGreater(rp_id, 0)
        points = self.jnl.list_by_task(self.task_id, limit=10)
        self.assertTrue(len(points) >= 1)
        self.assertEqual(points[0]["id"], rp_id)

    def test_list_by_task_kind_filter(self):
        """list_by_task 按 kind 过滤。"""
        obj1 = _mk_file(os.path.join(self.work, "inc1.tar.gz"))
        obj2 = _mk_file(os.path.join(self.work, "base1.tar.gz"))
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(self.base_dt),
            "object_key": obj1,
        })
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "base-full",
            "pit_at": _iso(self.base_dt + timedelta(hours=1)),
            "object_key": obj2,
        })
        inc_points = self.jnl.list_by_task(self.task_id, kind="file-inc")
        self.assertTrue(all(p["rp_kind"] == "file-inc" for p in inc_points))

    def test_list_by_time_range(self):
        """按时间范围查恢复点。"""
        for i in range(10):
            obj = _mk_file(os.path.join(self.work, f"p{i}.tar.gz"))
            self.jnl.record({
                "task_id": self.task_id,
                "rp_kind": "file-inc",
                "pit_at": _iso(self.base_dt + timedelta(minutes=3 * i)),
                "object_key": obj,
            })
        start = _iso(self.base_dt + timedelta(minutes=6))
        end = _iso(self.base_dt + timedelta(minutes=18))
        points = self.jnl.list_by_time_range(self.task_id, start, end)
        # 应包含 pit_at 在 [6min, 18min] 范围内的点（索引 2-6）
        self.assertTrue(len(points) >= 3)
        # 升序排列
        stamps = [p["pit_at"] for p in points]
        self.assertEqual(stamps, sorted(stamps))

    def test_get_latest(self):
        """获取最新恢复点。"""
        obj1 = _mk_file(os.path.join(self.work, "l1.tar.gz"))
        obj2 = _mk_file(os.path.join(self.work, "l2.tar.gz"))
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(self.base_dt),
            "object_key": obj1,
        })
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(self.base_dt + timedelta(hours=2)),
            "object_key": obj2,
        })
        latest = self.jnl.get_latest(self.task_id)
        self.assertIsNotNone(latest)
        self.assertTrue(latest["pit_at"] >= _iso(self.base_dt))

    def test_get_latest_kind_filter(self):
        """get_latest 按 kind 过滤。"""
        obj1 = _mk_file(os.path.join(self.work, "b1.tar.gz"))
        obj2 = _mk_file(os.path.join(self.work, "i1.tar.gz"))
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "base-full",
            "pit_at": _iso(self.base_dt),
            "object_key": obj1,
        })
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(self.base_dt + timedelta(hours=1)),
            "object_key": obj2,
        })
        latest_inc = self.jnl.get_latest(self.task_id, kind="file-inc")
        self.assertIsNotNone(latest_inc)
        self.assertEqual(latest_inc["rp_kind"], "file-inc")

    def test_delete_by_task(self):
        """任务删除时清理所有恢复点。"""
        for i in range(5):
            obj = _mk_file(os.path.join(self.work, f"d{i}.tar.gz"))
            self.jnl.record({
                "task_id": self.task_id,
                "rp_kind": "file-inc",
                "pit_at": _iso(self.base_dt + timedelta(minutes=i)),
                "object_key": obj,
            })
        count = self.jnl.delete_by_task(self.task_id)
        self.assertEqual(count, 5)
        points = self.jnl.list_by_task(self.task_id)
        self.assertEqual(len(points), 0)


# ============================ 5. find_chain ============================
class TestFindChain(unittest.TestCase):
    """验收 4：find_chain 返回有序恢复链。"""

    def setUp(self):
        self.task_id = _mk_task("链任务")
        self.jnl = RecoveryJournal()
        self.work = os.path.join(_TMP, "chain_work")
        os.makedirs(self.work, exist_ok=True)
        self.base_dt = datetime(2026, 8, 1, 8, 0, 0).astimezone()

    def test_file_chain_ordered(self):
        """1 个 base-full + 20 个 file-inc → find_chain 返回 21 且升序。"""
        full_obj = _mk_file(os.path.join(self.work, "base.tar.gz"), b"F" * 256)
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "base-full",
            "rp_type": "full",
            "pit_at": _iso(self.base_dt),
            "object_key": full_obj,
        })
        for i in range(20):
            obj = _mk_file(os.path.join(self.work, f"inc-{i:02d}.tar.gz"),
                           bytes([65 + i % 26]) * 64)
            self.jnl.record({
                "task_id": self.task_id,
                "rp_kind": "file-inc",
                "rp_type": "incremental",
                "pit_at": _iso(self.base_dt + timedelta(minutes=3 * (i + 1))),
                "object_key": obj,
            })
        target = _iso(self.base_dt + timedelta(hours=3))
        chain = self.jnl.find_chain(self.task_id, target)
        self.assertEqual(len(chain), 21)
        # 升序
        stamps = [p["pit_at"] for p in chain]
        self.assertEqual(stamps, sorted(stamps))
        # 链头为 base-full
        self.assertEqual(chain[0]["rp_kind"], "base-full")

    def test_chain_truncated_by_target(self):
        """目标时间在链中间时只截取到目标。"""
        full_obj = _mk_file(os.path.join(self.work, "b2.tar.gz"), b"F" * 100)
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "base-full",
            "rp_type": "full",
            "pit_at": _iso(self.base_dt),
            "object_key": full_obj,
        })
        for i in range(10):
            obj = _mk_file(os.path.join(self.work, f"i2-{i:02d}.tar.gz"), b"I" * 32)
            self.jnl.record({
                "task_id": self.task_id,
                "rp_kind": "file-inc",
                "pit_at": _iso(self.base_dt + timedelta(minutes=3 * (i + 1))),
                "object_key": obj,
            })
        # 只取到第 5 个增量
        target = _iso(self.base_dt + timedelta(minutes=3 * 5 + 1))
        chain = self.jnl.find_chain(self.task_id, target)
        self.assertEqual(len(chain), 6)  # base + 5 increments

    def test_db_log_chain(self):
        """db-log 段链：db-full + db-log 段。"""
        full_obj = _mk_file(os.path.join(self.work, "dbfull.sql"), b"D" * 200)
        self.jnl.record({
            "task_id": self.task_id,
            "rp_kind": "db-full",
            "rp_type": "full",
            "pit_at": _iso(self.base_dt),
            "object_key": full_obj,
        })
        for i in range(5):
            obj = _mk_file(os.path.join(self.work, f"binlog-{i}.bin"), b"B" * 100)
            self.jnl.record({
                "task_id": self.task_id,
                "rp_kind": "db-log",
                "rp_type": "log-segment",
                "pit_at": _iso(self.base_dt + timedelta(minutes=5 * (i + 1))),
                "binlog_file": f"mysql-bin.{1000 + i}",
                "binlog_pos": 4 + i * 1000,
                "object_key": obj,
            })
        target = _iso(self.base_dt + timedelta(hours=1))
        chain = self.jnl.find_chain(self.task_id, target)
        # 至少包含 db-full + 5 db-log 段
        self.assertTrue(len(chain) >= 6)
        self.assertEqual(chain[0]["rp_kind"], "db-full")


# ============================ 6. LogRepository ============================
class TestLogRepository(unittest.TestCase):
    """验收 5：init_repo 创建目录、check_quota 正常。"""

    def setUp(self):
        self.task_id = _mk_task("仓库测试任务")
        self.root = os.path.join(_TMP, f"repo_test_{self.task_id}")
        self.repo = LogRepository(self.task_id, repo_root=self.root)

    def test_init_repo_creates_directories(self):
        """init_repo 创建目录结构 + DB 记录。"""
        result = self.repo.init_repo()
        self.assertTrue(os.path.isdir(os.path.join(self.root, "db_logs")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "file_inc")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "file_snapshots", "rt")))
        self.assertIsNotNone(result)
        self.assertEqual(result["repo_root"], os.path.normpath(self.root))

    def test_init_repo_writes_db_record(self):
        """init_repo 后 DB 有 log_repository 记录。"""
        self.repo.init_repo()
        repo_dict = models.get_log_repo(self.task_id)
        self.assertIsNotNone(repo_dict)
        self.assertEqual(repo_dict["task_id"], self.task_id)

    def test_get_repo_from_db(self):
        """get_repo 读取 DB 记录。"""
        self.repo.init_repo()
        repo = self.repo.get_repo()
        self.assertIsNotNone(repo)
        self.assertTrue(repo["repo_root"])

    def test_update_size(self):
        """update_size 扫描目录统计当前体积。"""
        self.repo.init_repo()
        _mk_file(os.path.join(self.root, "file_inc", "a.tar.gz"), b"A" * 4096)
        size = self.repo.update_size()
        self.assertGreaterEqual(size, 4096)
        repo = models.get_log_repo(self.task_id)
        self.assertGreaterEqual(int(repo["current_size_bytes"]), 4096)

    def test_check_quota(self):
        """check_quota 返回正确的配额状态。"""
        self.repo.init_repo()
        result = self.repo.check_quota()
        self.assertIn(result["level"], ("ok", "warn", "full"))
        self.assertEqual(result["quota_bytes"], 214748364800)
        self.assertFalse(result["over_quota"])

    def test_check_quota_with_files(self):
        """有文件时 check_quota 能正确计算。"""
        self.repo.init_repo()
        _mk_file(os.path.join(self.root, "db_logs", "big.bin"), b"X" * 4096)
        self.repo.update_size()
        result = self.repo.check_quota()
        self.assertGreaterEqual(result["current_bytes"], 4096)
        self.assertEqual(result["level"], "ok")

    def test_cleanup_expired(self):
        """cleanup_expired 清理过期恢复点。"""
        self.repo.init_repo()
        jnl = RecoveryJournal()
        # 创建一个过期的恢复点
        old_obj = _mk_file(os.path.join(self.root, "file_inc", "old.tar.gz"), b"O" * 32)
        jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(datetime.now().astimezone() - timedelta(days=10)),
            "object_key": old_obj,
        })
        # 创建一个未过期的恢复点
        new_obj = _mk_file(os.path.join(self.root, "file_inc", "new.tar.gz"), b"N" * 32)
        jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(datetime.now().astimezone() - timedelta(days=1)),
            "object_key": new_obj,
        })
        removed = self.repo.cleanup_expired(7, kind="file_inc")
        self.assertGreaterEqual(removed, 1)

    def test_snapshot_dir(self):
        """snapshot_dir 返回正确的快照目录。"""
        self.repo.init_repo()
        snap_dir = self.repo.snapshot_dir("/path/to/source")
        self.assertTrue(os.path.isdir(snap_dir))
        self.assertIn("file_snapshots", snap_dir)
        self.assertIn("rt", snap_dir)

    def test_init_repo_idempotent(self):
        """重复 init_repo 不报错（更新而非重复插入）。"""
        self.repo.init_repo()
        # 第二次调用应该走 update 而非 insert
        result = self.repo.init_repo()
        self.assertIsNotNone(result)
        # 不应有多条记录
        rows = db.query("SELECT * FROM log_repository WHERE task_id=?", (self.task_id,))
        self.assertEqual(len(rows), 1)


# ============================ 7. 集成：rt_tasks + LogRepository ============================
class TestRtTaskWithLogRepo(unittest.TestCase):
    """rt_tasks 与 LogRepository 联合使用。"""

    def setUp(self):
        self.task_id = _mk_task("联合测试")
        self.root = os.path.join(_TMP, f"combo_{self.task_id}")

    def test_full_workflow(self):
        """创建 rt_task + init_repo → update_status → cleanup。"""
        # 创建 rt_tasks 扩展行
        models.create_rt_task({"task_id": self.task_id})
        rt = models.get_rt_task(self.task_id)
        self.assertEqual(rt["rt_mode"], "file_polling")

        # 初始化日志仓库
        repo = LogRepository(self.task_id, repo_root=self.root)
        repo.init_repo()

        # 写入恢复点
        jnl = RecoveryJournal()
        obj = _mk_file(os.path.join(self.root, "file_inc", "rp1.tar.gz"), b"R" * 128)
        rp_id = jnl.record({
            "task_id": self.task_id,
            "rp_kind": "file-inc",
            "pit_at": _iso(datetime.now().astimezone()),
            "object_key": obj,
        })
        self.assertGreater(rp_id, 0)

        # 更新 rt_tasks 状态
        models.update_rt_task(self.task_id, {
            "is_running": 1,
            "health_status": "healthy",
            "rpo_current_seconds": 5,
        })
        rt = models.get_rt_task(self.task_id)
        self.assertTrue(rt["is_running"])
        self.assertEqual(rt["health_status"], "healthy")

        # 查恢复链
        chain = jnl.find_chain(self.task_id, _iso(datetime.now().astimezone() + timedelta(hours=1)))
        self.assertTrue(len(chain) >= 1)

        # 清理
        jnl.delete_by_task(self.task_id)
        models.delete_rt_task(self.task_id)
        self.assertIsNone(models.get_rt_task(self.task_id))


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
