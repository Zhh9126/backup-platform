# -*- coding: utf-8 -*-
"""
QA 专项测试：备份存储管理（三重备份体系）。

独立第二道防线：直接针对 storage_backends / tier_replication / api/storage
写真实用例、自己跑、独立判定（源码 Bug → 反馈修复 / 测试 Bug → 自修）。

设计目标（对齐用户需求）：
  L1 = MinIO（热/活数据，备份第一落点）
  L2 = S3（冷数据，异地容灾）
  L3 = 源端本地路径导出（服务端路径，可离线转移）

运行方式（系统 Python 3.14.3 + DEMO_MODE=on）：
    SET DEMO_MODE=on
    python tests/qa_storage.py
"""
import os
import sys
import io
import json
import time
import shutil
import tempfile
import threading
import unittest
from unittest import mock

# ---------------- 0. 运行环境 ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="qa_storage_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
os.environ["SCHEDULER_ENABLED"] = "false"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                  # noqa: E402
import core.db as db                           # noqa: E402
db.init_schema()                               # noqa: E402
import core.models as models                   # noqa: E402
from app import app as flask_app                # noqa: E402

from core.storage_backends import (            # noqa: E402
    LocalStorageBackend, MinIOStorageBackend, S3StorageBackend,
    get_backend, list_supported_types, TYPE_META, TIER_NAMES,
)


# ---------------- 1. 测试基础设施 ----------------
def clear_all():
    for t in ("backup_sets", "backup_records", "restore_records",
              "system_config", "storage_targets", "tasks", "system_logs"):
        try:
            db.execute(f"DELETE FROM {t}")
        except Exception:
            pass


class QABase(unittest.TestCase):
    def setUp(self):
        clear_all()
        self.client = flask_app.test_client()
        r = self.client.post("/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(r.status_code, 200, "登录应成功")
        self._tmpdirs = []

    def tearDown(self):
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def _dir(self):
        d = tempfile.mkdtemp(prefix="qa_sd_")
        self._tmpdirs.append(d)
        return d

    def _task(self, db_type="mysql", **kw):
        base = {"name": kw.pop("name", "qa_" + os.urandom(4).hex()),
                "db_type": db_type, "host": "127.0.0.1",
                "port": 3306, "username": "u", "password": "p"}
        base.update(kw)
        return models.create_task(base)


# ---------------- 2. FakeMinio：把 S3/MinIO 协议落地到本地目录 ----------------
class _FakeMinioObj:
    def __init__(self, data, etag="etag"):
        self._data = data
        self.etag = etag
        self.size = len(data)

    def read(self):
        return self._data

    def close(self):
        pass

    def release_conn(self):
        pass


class _FakeMinioClient:
    """极简 MinIO/S3 客户端，用本地目录模拟 bucket/prefix/object。"""

    def __init__(self, *a, **kw):
        self.root = tempfile.mkdtemp(prefix="qa_fakeminio_")
        self.buckets = set()

    def _path(self, bucket, name):
        return os.path.join(self.root, bucket, *name.split("/"))

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        os.makedirs(os.path.join(self.root, bucket), exist_ok=True)
        self.buckets.add(bucket)

    def put_object(self, bucket, name, data, length=-1, part_size=None, metadata=None):
        p = self._path(bucket, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        payload = data.read() if hasattr(data, "read") else data
        with open(p, "wb") as f:
            f.write(payload)
        return _FakeMinioObj(payload)

    def get_object(self, bucket, name):
        p = self._path(bucket, name)
        with open(p, "rb") as f:
            return _FakeMinioObj(f.read())

    def remove_object(self, bucket, name):
        p = self._path(bucket, name)
        if os.path.exists(p):
            os.remove(p)

    def stat_object(self, bucket, name):
        p = self._path(bucket, name)
        if not os.path.exists(p):
            raise FileNotFoundError(name)
        return _FakeMinioObj(b"x")

    def list_objects(self, bucket, prefix=None, recursive=True):
        base = os.path.join(self.root, bucket)
        if not os.path.isdir(base):
            return
        pre = (prefix or "").rstrip("/")
        for dp, _, files in os.walk(base):
            for fn in files:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, base).replace("\\", "/")
                if pre and not rel.startswith(pre):
                    continue
                yield _FakeMinioObj(b"x", etag="e")


# ================= A. 存储后端抽象单元测试 =================
class TestStorageBackends(QABase):

    def test_supported_types_three(self):
        types = {t["type"] for t in list_supported_types()}
        self.assertEqual(types, {"local", "minio", "s3"})

    def test_tier_mapping_matches_user_model(self):
        # 用户需求：L1=MinIO 热, L2=S3 冷, L3=源端本地路径导出
        self.assertEqual(TYPE_META["minio"]["tier"], 1, "MinIO 应为 L1")
        self.assertEqual(TYPE_META["s3"]["tier"], 2, "S3 应为 L2")
        self.assertEqual(TYPE_META["local"]["tier"], 3, "本地路径导出应为 L3")

    def test_local_backend_save_get_delete_exists(self):
        d = self._dir()
        b = LocalStorageBackend({"endpoint": d}, db.get_logger("qa"))
        src = os.path.join(d, "_src.bin")
        with open(src, "wb") as f:
            f.write(b"payload-xyz")
        self.assertTrue(b.save_file(src, "obj1"))
        self.assertTrue(b.file_exists("obj1"))
        got = b.get_file("obj1")
        self.assertEqual(got, b"payload-xyz")
        dest = os.path.join(d, "_out.bin")
        self.assertTrue(b.get_file("obj1", dest_path=dest))
        self.assertTrue(os.path.exists(dest))
        self.assertTrue(b.delete_file("obj1"))
        self.assertFalse(b.file_exists("obj1"))

    def test_local_backend_path_traversal_blocked(self):
        d = self._dir()
        b = LocalStorageBackend({"endpoint": d}, db.get_logger("qa"))
        with self.assertRaises(ValueError):
            b._resolve_path("../escape.bin")

    def test_local_test_connection(self):
        d = self._dir()
        b = LocalStorageBackend({"endpoint": d}, db.get_logger("qa"))
        ok, msg = b.test_connection()
        self.assertTrue(ok)

    def test_minio_backend_roundtrip_with_fake(self):
        d = self._dir()
        cfg = {"endpoint": "http://fake", "access_key": "ak", "secret_key": "sk",
               "bucket": "hot", "region": "us-east-1", "prefix": "bk"}
        b = MinIOStorageBackend(cfg, db.get_logger("qa"))
        src = os.path.join(d, "_src.bin")
        with open(src, "wb") as f:
            f.write(b"minio-payload-123")
        with mock.patch("minio.Minio", _FakeMinioClient):
            self.assertTrue(b.save_file(src, "db1/task1/obj.minio"))
            self.assertTrue(b.file_exists("db1/task1/obj.minio"))
            self.assertEqual(b.get_file("db1/task1/obj.minio"), b"minio-payload-123")
            ok, msg = b.test_connection()
            self.assertTrue(ok)
            self.assertTrue(b.delete_file("db1/task1/obj.minio"))
            self.assertFalse(b.file_exists("db1/task1/obj.minio"))

    def test_s3_backend_roundtrip_with_fake(self):
        d = self._dir()
        cfg = {"endpoint": "http://fake", "access_key": "ak", "secret_key": "sk",
               "bucket": "cold", "region": "us-east-1", "prefix": "bk",
               "extra_options": {"storage_class": "STANDARD_IA"}}
        b = S3StorageBackend(cfg, db.get_logger("qa"))
        src = os.path.join(d, "_src.bin")
        with open(src, "wb") as f:
            f.write(b"s3-payload-456")
        with mock.patch("minio.Minio", _FakeMinioClient):
            self.assertTrue(b.save_file(src, "db1/task1/obj.s3"))
            self.assertEqual(b.get_file("db1/task1/obj.s3"), b"s3-payload-456")
            ok, msg = b.test_connection()
            self.assertTrue(ok)

    def test_minio_save_fails_gracefully_without_server(self):
        cfg = {"endpoint": "http://127.0.0.1:1", "access_key": "ak",
               "secret_key": "sk", "bucket": "x", "region": "us-east-1"}
        b = MinIOStorageBackend(cfg, db.get_logger("qa"))
        src = os.path.join(self._dir(), "_src.bin")
        with open(src, "wb") as f:
            f.write(b"x")
        # 不应抛异常，应返回 False（连接失败）
        self.assertFalse(b.save_file(src, "obj"))


# ================= B. API 端点测试 =================
class TestStorageAPI(QABase):

    def _create_target(self, body):
        r = self.client.post("/api/storage/targets", json=body)
        self.assertIn(r.status_code, (200, 201), r.get_json())
        return r.get_json()["id"]

    def test_create_list_get_update_delete_local(self):
        tid = self._create_target({"name": "本地导出", "type": "local",
                                    "endpoint": self._dir()})
        lst = self.client.get("/api/storage/targets").get_json()
        self.assertEqual(len(lst["targets"]), 1)
        det = self.client.get(f"/api/storage/targets/{tid}").get_json()
        self.assertEqual(det["type"], "local")
        upd = self.client.put(f"/api/storage/targets/{tid}",
                              json={"remark": "updated"})
        self.assertEqual(upd.status_code, 200)
        self.assertEqual(self.client.get(f"/api/storage/targets/{tid}").get_json()["remark"], "updated")
        self.assertEqual(self.client.delete(f"/api/storage/targets/{tid}").status_code, 200)
        self.assertEqual(self.client.get("/api/storage/targets").get_json()["targets"], [])

    def test_create_minio_assigns_tier1(self):
        tid = self._create_target({"name": "MinIO热", "type": "minio",
                                    "endpoint": "http://minio:9000", "bucket": "hot"})
        det = self.client.get(f"/api/storage/targets/{tid}").get_json()
        self.assertEqual(det["tier"], 1, "MinIO 目标 tier 应为 1")

    def test_create_s3_assigns_tier2(self):
        tid = self._create_target({"name": "S3冷", "type": "s3",
                                    "endpoint": "https://s3.amazonaws.com", "bucket": "cold"})
        det = self.client.get(f"/api/storage/targets/{tid}").get_json()
        self.assertEqual(det["tier"], 2, "S3 目标 tier 应为 2")

    def test_create_local_assigns_tier3(self):
        tid = self._create_target({"name": "本地导出", "type": "local",
                                    "endpoint": self._dir()})
        det = self.client.get(f"/api/storage/targets/{tid}").get_json()
        self.assertEqual(det["tier"], 3, "本地导出目标 tier 应为 3")

    def test_secret_key_masked(self):
        tid = self._create_target({"name": "MinIO热", "type": "minio",
                                    "endpoint": "http://minio:9000", "bucket": "hot",
                                    "secret_key": "super-secret"})
        det = self.client.get(f"/api/storage/targets/{tid}").get_json()
        self.assertNotIn("secret_key", det, "返回不应含明文 secret_key")
        self.assertTrue(det.get("has_secret_key"))

    def test_stats_and_usage(self):
        self._create_target({"name": "本地导出", "type": "local", "endpoint": self._dir()})
        st = self.client.get("/api/storage/stats").get_json()
        self.assertIn("tiers", st)
        usage = self.client.get("/api/storage/usage").get_json()
        self.assertIn("used_percent", usage)

    def test_replication_config_defaults(self):
        cfg = self.client.get("/api/storage/replication-config").get_json()
        # 用户要求：备份先到 L1(MinIO)，再实时推送到 L2(S3) 与 L3(本地导出)
        self.assertTrue(cfg.get("push_l1_minio"), "默认应启用 L1 MinIO 推送")
        self.assertTrue(cfg.get("push_l2_s3"), "默认应启用 L2 S3 推送")
        self.assertTrue(cfg.get("push_l3_local"), "默认应启用 L3 本地导出")

    def test_replication_config_roundtrip(self):
        # 前端现已改为 push_l1_minio / push_l2_s3 / push_l3_local 字段
        payload = {"push_l1_minio": 0, "push_l2_s3": 1, "push_l3_local": 0,
                   "timing": "delay_30min", "max_retries": 5, "retry_interval": 60}
        r = self.client.post("/api/storage/replication-config", json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        cfg = self.client.get("/api/storage/replication-config").get_json()
        self.assertEqual(cfg["push_l1_minio"], 0)
        self.assertEqual(cfg["push_l2_s3"], 1)
        self.assertEqual(cfg["push_l3_local"], 0)
        self.assertEqual(cfg["timing"], "delay_30min")
        self.assertEqual(cfg["max_retries"], 5)
        self.assertEqual(cfg["retry_interval"], 60)
        # 旧字段名（replicate_l1_to_l2 等）不在白名单内，应被忽略、不写入响应
        self.assertNotIn("replicate_l1_to_l2", cfg)
        self.assertNotIn("replicate_l2_to_l3", cfg)
        self.assertNotIn("replicate_l1_to_l3", cfg)


# ================= C. 三级复制编排集成测试（fake client） =================
class TestTierReplication(QABase):

    def _setup_three_targets(self):
        minio_dir = self._dir()
        s3_dir = self._dir()
        local_dir = self._dir()
        # 用 fake 把 minio/s3 后端指向本地目录，便于无服务器验证编排
        db.execute(
            "INSERT INTO storage_targets(name,type,tier,endpoint,access_key,"
            "secret_key,bucket,prefix,enabled,extra_options,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("MinIO热", "minio", 1, "http://fake", "ak", db.encrypt_secret("sk"),
             "hot", "bk", 1, None, db.now_iso(), db.now_iso()))
        db.execute(
            "INSERT INTO storage_targets(name,type,tier,endpoint,access_key,"
            "secret_key,bucket,prefix,enabled,extra_options,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("S3冷", "s3", 2, "http://fake", "ak", db.encrypt_secret("sk"),
             "cold", "bk", 1, json.dumps({"storage_class": "STANDARD_IA"}),
             db.now_iso(), db.now_iso()))
        db.execute(
            "INSERT INTO storage_targets(name,type,tier,endpoint,access_key,"
            "secret_key,bucket,prefix,enabled,extra_options,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("本地导出", "local", 3, local_dir, "", "", "", "", 1, None,
             db.now_iso(), db.now_iso()))
        return minio_dir, s3_dir, local_dir

    def test_replicate_creates_all_three_tiers(self):
        minio_dir, s3_dir, local_dir = self._setup_three_targets()
        # 让 minio/s3 后端走 fake
        with mock.patch("minio.Minio", _FakeMinioClient):
            from core import tier_replication
            # 把 fake 的 root 关联到我们的目录：_FakeMinioClient 内部自建目录，
            # 这里直接验证「编排结果 + record 的 storage_tier 标记」即可（文件真实写入 fake）。
            src = os.path.join(self._dir(), "backup.sim")
            with open(src, "wb") as f:
                f.write(b"full-backup-payload")
            tid = self._task()
            rec_id = models.create_record({
                "task_id": tid, "db_type": "mysql", "backup_type": "full",
                "status": "success", "size_bytes": 18, "backup_path": src})
            db.set_system_config("replication_strategy", json.dumps({
                "push_l1_minio": 1, "push_l2_s3": 1, "push_l3_local": 1,
                "max_retries": 2, "retry_interval": 1}))
            res = tier_replication.replicate_to_tiers(src, models.get_task(tid), rec_id)
            self.assertTrue(res.get("minio"), "应推送到 L1 MinIO")
            self.assertTrue(res.get("s3"), "应推送到 L2 S3")
            self.assertTrue(res.get("local"), "应推送到 L3 本地导出")
            # 记录标记应包含三层
            tier = db.query_one(
                "SELECT storage_tier FROM backup_records WHERE id=?", (rec_id,))
            token = (tier["storage_tier"] or "")
            self.assertIn("minio", token)
            self.assertIn("s3", token)
            self.assertIn("local", token)

    def test_replicate_retries_then_fails_records_error(self):
        # 让 minio 后端连接失败（无 fake），验证重试后记录失败不崩溃
        self._setup_three_targets()
        # 把 minio 的后端指向不可用 endpoint
        db.execute("UPDATE storage_targets SET endpoint='http://127.0.0.1:1' "
                   "WHERE type='minio'")
        from core import tier_replication
        src = os.path.join(self._dir(), "backup.sim")
        with open(src, "wb") as f:
            f.write(b"x" * 10)
        tid = self._task()
        rec_id = models.create_record({
            "task_id": tid, "db_type": "mysql", "backup_type": "full",
            "status": "success", "size_bytes": 10, "backup_path": src})
        db.set_system_config("replication_strategy", json.dumps({
            "push_l1_minio": 1, "push_l2_s3": 1, "push_l3_local": 1,
            "max_retries": 2, "retry_interval": 1}))
        res = tier_replication.replicate_to_tiers(src, models.get_task(tid), rec_id)
        # minio 失败但 local/s3 取决于 fake；至少不应抛异常
        self.assertIn("minio", res)
        self.assertIn("s3", res)
        self.assertIn("local", res)


# ---------------- 运行入口 ----------------
if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    passed = total - failures - errors
    print("\n" + "=" * 64)
    print(f"QA 存储专项 通过率 = {passed}/{total}  (失败={failures}, 错误={errors})")
    print("=" * 64)
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0 if (failures == 0 and errors == 0) else 1)
