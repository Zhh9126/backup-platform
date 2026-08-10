# -*- coding: utf-8 -*-
"""全库备份/恢复端到端验证（DEMO_MODE=on 仿真模式）。

覆盖 9 种 db_type 的：
  - 全量备份（full）+ 恢复
  - 增量备份（incremental）：验证平台能正常编排"全量→增量"两条链路并生成记录

说明：
  - 无需安装任何数据库客户端：DEMO_MODE=on 强制引擎走 _simulate_backup/_simulate_restore，
    重点验证"任务 → 调度执行 → 记录落库 → 恢复链路"的业务逻辑正确性。
  - 真实增量/回退（物理 vs 逻辑）的差异由引擎在真实模式下处理，本测试只验证编排层不报错。
  - 引擎在仿真模式下会忠实保留 backup_type（full/incremental/snapshot），可用于断言。

运行：python tests/test_backup_restore_all.py
"""
import os
import sys
import tempfile
import unittest

# ------------- 0. 运行环境（必须在导入 config 之前设置） -------------
os.environ["DEMO_MODE"] = "on"          # 强制仿真，无需真实客户端
os.environ["RT_BACKUP_ENABLED"] = "false"
os.environ["SCHEDULER_ENABLED"] = "false"
_TMP = tempfile.mkdtemp(prefix="bkr_test_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
for _d in (os.environ["INSTANCE_DIR"], os.environ["LOG_DIR"], os.environ["BACKUP_ROOT"]):
    os.makedirs(_d, exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
db.init_schema()                                # noqa: E402
import core.models as models                    # noqa: E402
import core.scheduler as scheduler_mod          # noqa: E402

# 每个 db_type 的演示任务配置
TASKS = {
    "mysql":      {"name": "T-MySQL",      "host": "127.0.0.1", "db_name": "app", "username": "root", "password": "x"},
    "mariadb":    {"name": "T-MariaDB",    "host": "127.0.0.1", "db_name": "app", "username": "root", "password": "x"},
    "postgresql": {"name": "T-PostgreSQL", "host": "127.0.0.1", "db_name": "appdb", "username": "postgres", "password": "x"},
    "oracle":     {"name": "T-Oracle",     "host": "127.0.0.1", "db_name": "ORCL", "username": "sys", "password": "x"},
    "kingbase":   {"name": "T-Kingbase",   "host": "127.0.0.1", "db_name": "TEST", "username": "system", "password": "x"},
    "dameng":     {"name": "T-Dameng",     "host": "127.0.0.1", "db_name": "TEST", "username": "SYSDBA", "password": "x"},
    "redis":      {"name": "T-Redis",      "host": "127.0.0.1", "db_name": "0", "username": "", "password": "x"},
    "mongodb":    {"name": "T-MongoDB",    "host": "127.0.0.1", "db_name": "app", "username": "root", "password": "x"},
    "file":       {"name": "T-File",       "host": "/data", "db_name": "/data/docs", "username": "", "password": ""},
}

# 支持真增量的类型（物理备份）；其余逻辑层无真增量
REAL_INCREMENTAL = {"mysql", "mariadb", "oracle", "dameng"}


def _create_task(db_type: str) -> int:
    cfg = TASKS[db_type]
    data = {
        "name": cfg["name"],
        "db_type": db_type,
        "host": cfg["host"],
        "port": config.DEFAULT_PORTS.get(db_type, 0),
        "username": cfg["username"],
        "password": cfg["password"],
        "db_name": cfg["db_name"],
        "backup_type": "full",
        "backup_mode": "logical",
        "schedule_type": "none",
        "enabled": 1,
        "demo_only": 1,            # 任务级强制仿真
    }
    return models.create_task(data)


class BackupRestoreAllTest(unittest.TestCase):

    def _run_backup(self, task_id, backup_type):
        ret = scheduler_mod.run_task_now(task_id, backup_type=backup_type, operator="test")
        self.assertIsNotNone(ret, f"{backup_type} 备份未返回结果")
        # file 备份是异步的：run_task_now 返回 {"accepted": True, "status": "running"}
        # 其他类型同步返回完整 record
        if ret.get("accepted"):
            rec = self._wait_file_record(task_id)
        else:
            rec = ret
        self.assertIn(rec["status"], ("success", "simulated"),
                      f"{backup_type} 备份状态异常: {rec.get('message')}")
        self.assertGreater(rec["size_bytes"], 0, f"{backup_type} size_bytes 应 > 0")
        # differential 可能被引擎回退为 full（如 file 仿真），仅 full/incremental 强校验
        if backup_type not in ("differential",):
            self.assertEqual(rec["backup_type"], backup_type,
                             f"{backup_type} record.backup_type 应为 {backup_type}, got {rec['backup_type']}")
        return rec

    def _wait_file_record(self, task_id, timeout=30):
        """轮询等待异步文件备份记录完成。"""
        import time
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            rows = models.list_records(task_id=task_id, limit=1)
            if rows:
                last = rows[0]
                if last["status"] != "running":
                    return last
            time.sleep(0.3)
        return last

    def _run_restore(self, record_id, label=""):
        rr = scheduler_mod.run_restore_now(record_id, operator="test")
        self.assertIsNotNone(rr, "恢复未返回记录")
        # file 恢复也可能异步：run_restore_now 返回 {"accepted": True}
        if isinstance(rr, dict) and rr.get("accepted"):
            rr = self._wait_restore_record(record_id)
        # demo 模式下引擎返回 simulated，真实模式返回 success，两者均为"正常执行"
        self.assertIn(rr["status"], ("success", "simulated"), f"恢复失败: {rr.get('message')}")
        self.assertIsNotNone(rr.get("message"), "恢复记录应有 message")
        return rr

    def _wait_restore_record(self, record_id, timeout=30):
        import time
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            rows = models.list_restores(record_id=record_id, limit=1)
            if rows:
                last = rows[0]
                if last["status"] != "running":
                    return last
            time.sleep(0.3)
        return last

    # ---------- 每种 db_type：全量 + 恢复 ----------
    def test_mysql_full_and_restore(self):
        self._test_full_restore("mysql")

    def test_mariadb_full_and_restore(self):
        self._test_full_restore("mariadb")

    def test_postgresql_full_and_restore(self):
        self._test_full_restore("postgresql")

    def test_oracle_full_and_restore(self):
        self._test_full_restore("oracle")

    def test_kingbase_full_and_restore(self):
        self._test_full_restore("kingbase")

    def test_dameng_full_and_restore(self):
        self._test_full_restore("dameng")

    def test_redis_full_and_restore(self):
        self._test_full_restore("redis")

    def test_mongodb_full_and_restore(self):
        self._test_full_restore("mongodb")

    def test_file_full_and_restore(self):
        self._test_full_restore("file")

    def _test_full_restore(self, db_type):
        tid = _create_task(db_type)
        rec = self._run_backup(tid, "full")
        # 校验落库
        stored = models.get_record(rec["id"])
        self.assertIsNotNone(stored, "备份记录未落库")
        self.assertEqual(stored["db_type"], db_type)
        # 恢复
        self._run_restore(rec["id"])

    # ---------- 增量备份（编排层验证） ----------
    def test_mysql_incremental(self):
        self._test_incremental("mysql")

    def test_mariadb_incremental(self):
        self._test_incremental("mariadb")

    def test_oracle_incremental(self):
        self._test_incremental("oracle")

    def test_dameng_incremental(self):
        self._test_incremental("dameng")

    def test_postgresql_incremental(self):
        self._test_incremental("postgresql")

    def test_kingbase_incremental(self):
        self._test_incremental("kingbase")

    def test_mongodb_incremental(self):
        self._test_incremental("mongodb")

    def test_redis_incremental(self):
        self._test_incremental("redis")

    def test_file_differential(self):
        tid = _create_task("file")
        full = self._run_backup(tid, "full")
        diff = self._run_backup(tid, "differential")
        # file 引擎在仿真模式下把 differential 回退为 full（占位备份不分差异类型）
        self.assertIn(diff["backup_type"], ("full", "differential"),
                      "file differential 应记为 full 或 differential")
        self._run_restore(full["id"])

    def _test_incremental(self, db_type):
        tid = _create_task(db_type)
        # 先全量（增量需要基）
        full = self._run_backup(tid, "full")
        # 再增量
        inc = self._run_backup(tid, "incremental")
        self._run_restore(inc["id"])
        # 真增量类型：实测模式应记录 backup_type=incremental（物理引擎支持）
        if db_type in REAL_INCREMENTAL:
            self.assertEqual(inc["backup_type"], "incremental",
                             f"{db_type} 物理增量应保留 incremental 类型")


if __name__ == "__main__":
    unittest.main(verbosity=2)
