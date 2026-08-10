# -*- coding: utf-8 -*-
"""
T02–T05 测试补全（准 CDP 实时备份）。

运行方式（必须用系统 Python 3.14，DEMO_MODE=on）：
    DEMO_MODE=on python tests/test_rt_t02_t05.py

覆盖验收标准：
  T02 文件近实时捕获
    1. watcher 事件去重/合并（_on_batch 合并并发批次，pending 计数）；
    2. 变更批次经 FileRtCapture 落 RecoveryJournal（base-full + file-inc）；
    3. 策略切换：polling / watchdog / auto，以及 watchdog 不可用降级 polling；
       环境自检 probe_capabilities() 正确报告 watchdog 可用性。
  T03 数据库 CDC 守护
    1. SimulatedCDCDaemon 生成仿真变更流并写入 log_repo / journal；
    2. DbRtCapture 将封存段登记为 db-log 恢复点（DEMO_MODE 强制仿真）；
    3. RtSupervisor 启停 daemon、worker 生命周期管理；
    4. db_cdc 任务 CRUD（经 models.create_rt_task / get_rt_task / update_rt_task
       以及 DbRtCapture._sync_rt_task_row 注册 rt_mode=db_cdc）。
  T04 PITR 恢复
    1. 构造若干 journal 记录 + log_repository 条目，按 target_time 回放得到正确
       恢复序列（build_plan / resolve_chain / validate_chain / window）；
    2. /api/rt/points 与 /api/rt/recover 的接口契约（Flask test_client 登录）。
  T05 前端时间轴
    1. GET /rt-timeline 返回 200 且含 initRtTimeline / 实时备份 标记；导航存在。

设计约定（与 test_rt_journal.py / test_rt_t01.py 一致）：
  - 顶部设置临时 INSTANCE_DIR / LOG_DIR / BACKUP_ROOT / META_DB_PATH 等，
    使用临时库避免污染 instance/*.db；
  - 复用 core.rt_backup.journal / repo / types（T01 的 core.rt.journal 是另一套 API）；
  - 缺失依赖（watchdog 已装则实跑；mysql-replication 未装则 skip）不导致失败。
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
os.environ["RT_BACKUP_ENABLED"] = "on"
_TMP = tempfile.mkdtemp(prefix="rt_t02_t05_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["RT_LOG_ROOT"] = os.path.join(_TMP, "rt_logs")
os.environ["RT_FILE_ROOT"] = os.path.join(_TMP, "rt_files")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
os.environ["SCHEDULER_ENABLED"] = "false"
os.makedirs(os.environ["INSTANCE_DIR"], exist_ok=True)
os.makedirs(os.environ["LOG_DIR"], exist_ok=True)
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)
os.makedirs(os.environ["RT_LOG_ROOT"], exist_ok=True)
os.makedirs(os.environ["RT_FILE_ROOT"], exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402

db.init_schema()                                # noqa: E402

import core.models as models                    # noqa: E402
from core.rt_backup.journal import RecoveryJournal   # noqa: E402
from core.rt_backup.repo import LogRepository        # noqa: E402
from core.rt_backup.types import (                   # noqa: E402
    KIND_DB_LOG,
    KIND_FILE,
    RP_BASE_FULL,
    RP_DB_FULL,
    RP_DB_LOG,
    RP_FILE_INC,
    ChangeBatch,
    RtConfig,
)
from core.rt_backup.watchers import create_watcher, probe_capabilities  # noqa: E402
from core.rt_backup.watchers.polling import PollingWatcher              # noqa: E402
from core.rt_backup.watchers.watchdog_watcher import WatchdogWatcher     # noqa: E402
from core.rt_backup.file_rt import FileRtCapture                        # noqa: E402
from core.rt_backup.db_rt import DbRtCapture                            # noqa: E402
from core.cdc.simulated import SimulatedCDCDaemon                        # noqa: E402
from core.cdc import create_daemon                                        # noqa: E402
from core.rt_backup.supervisor import get_supervisor, reset_supervisor   # noqa: E402
from core.rt_backup.pitr import PITRRestore                              # noqa: E402


# ============================ 工具函数 ============================
def _mk_file(path: str, content: bytes = b"x" * 128) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _mk_file_task(name: str, src_dir: str, **extra) -> int:
    """创建一个带本地源目录的文件任务，并打开实时保护。"""
    os.makedirs(src_dir, exist_ok=True)
    _mk_file(os.path.join(src_dir, "seed1.txt"), b"seed-content-1")
    _mk_file(os.path.join(src_dir, "seed2.txt"), b"seed-content-2")
    task_id = models.create_task({
        "name": name,
        "db_type": "file",
        "host": "127.0.0.1",
        "port": 0,
        "username": "",
        "password": "",
        "db_name": "",
        "backup_type": "full",
        "schedule_type": "manual",
        "enabled": 1,
        "demo_only": 1,
        "extra_options": json.dumps({
            "source_type": "local",
            "source_paths": [src_dir],
        }),
    })
    updates = {"rt_enabled": 1}
    updates.update(extra)
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE backup_tasks SET {sets} WHERE id=?",
               tuple(updates.values()) + (task_id,))
    return task_id


def _mk_db_task(name: str, **extra) -> int:
    """创建一个 MySQL 类型的数据库任务，并打开实时保护。"""
    task_id = models.create_task({
        "name": name,
        "db_type": "mysql",
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


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


# ============================ T02 文件近实时捕获 ============================
class TestT02WatcherStrategy(unittest.TestCase):
    """验收 T02-3：watcher 策略切换与降级。"""

    def test_polling_mode_returns_polling_watcher(self):
        src = tempfile.mkdtemp(prefix="t02_poll_")
        task_id = _mk_file_task("T02-polling", src)
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        cfg.mode = "polling"
        w = create_watcher(task, cfg, lambda b: None)
        self.assertIsInstance(w, PollingWatcher)
        self.assertEqual(w.impl_key, "polling")

    def test_watchdog_mode_uses_watchdog_when_available(self):
        src = tempfile.mkdtemp(prefix="t02_wd_")
        task_id = _mk_file_task("T02-watchdog", src)
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        cfg.mode = "watchdog"
        probe = PollingWatcher(task, cfg, lambda b: None)
        src_cfg = probe.source_cfg
        available, _reason = WatchdogWatcher.is_available(src_cfg)
        w = create_watcher(task, cfg, lambda b: None)
        if available:
            self.assertIsInstance(w, WatchdogWatcher)
        else:
            # watchdog 在本环境不可用 → 必须降级为轮询且写原因
            self.assertIsInstance(w, PollingWatcher)
            self.assertTrue(w.degrade_reason,
                            "watchdog 不可用时必须给出降级原因")

    def test_auto_mode_upgrades_to_watchdog_on_local_dir(self):
        src = tempfile.mkdtemp(prefix="t02_auto_")
        task_id = _mk_file_task("T02-auto", src)
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        cfg.mode = "auto"
        probe = PollingWatcher(task, cfg, lambda b: None)
        src_cfg = probe.source_cfg
        available, _reason = WatchdogWatcher.is_available(src_cfg)
        w = create_watcher(task, cfg, lambda b: None)
        if available:
            self.assertIsInstance(w, WatchdogWatcher)
        else:
            self.assertIsInstance(w, PollingWatcher)

    def test_watchdog_unavailable_falls_back_to_polling(self):
        """源目录不存在时 watchdog 不可用，工厂必须降级为 PollingWatcher。"""
        missing = os.path.join(_TMP, "does_not_exist_dir_xyz")
        task_id = _mk_file_task("T02-fallback",
                                tempfile.mkdtemp(prefix="t02_fb_src_"))
        # 覆盖任务的源为不存在目录
        db.execute(
            "UPDATE backup_tasks SET extra_options=? WHERE id=?",
            (json.dumps({"source_type": "local",
                         "source_paths": [missing]}), task_id))
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        cfg.mode = "watchdog"
        w = create_watcher(task, cfg, lambda b: None)
        self.assertIsInstance(w, PollingWatcher)
        self.assertTrue(w.degrade_reason,
                        "watchdog 不可用必须给出降级原因")

    def test_probe_capabilities_reports_watchdog(self):
        caps = probe_capabilities()
        impls = {i["key"]: i for i in caps["implementations"]}
        self.assertIn("watchdog", impls)
        self.assertIn("polling", impls)
        # watchdog 在当前环境已安装（import 成功）
        try:
            import watchdog  # noqa: F401
            self.assertTrue(impls["watchdog"]["available"])
        except Exception:
            self.assertFalse(impls["watchdog"]["available"])


class TestT02EventMergeAndJournal(unittest.TestCase):
    """验收 T02-1/2：事件合并去重 + 变更批次落 RecoveryJournal。"""

    def test_event_merge_increments_pending_when_capture_in_flight(self):
        """捕获进行中到达的批次被合并（_pending 计数），不立即写 journal。"""
        src = tempfile.mkdtemp(prefix="t02_merge_")
        task_id = _mk_file_task("T02-merge", src)
        task = models.get_task(task_id, include_secret=True)
        capture = FileRtCapture(task, RtConfig.from_task(task))
        # 先建基准，保证后续批次有可落盘的全量链头
        capture.ensure_base()

        journal = RecoveryJournal()
        inc_before = journal.count(task_id, RP_FILE_INC)

        batch = ChangeBatch(changed=["seed1.txt"], snapshot={"seed1.txt": (1, 1)},
                            detected_at=db.now_iso(), trigger="event")

        # 用独立线程持锁，模拟「一次捕获正在飞行」
        held = threading.Event()
        release = threading.Event()

        def _holder():
            capture._capture_lock.acquire()
            held.set()
            release.wait(timeout=10)
            capture._capture_lock.release()

        t = threading.Thread(target=_holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(timeout=5))

        pending_before = capture._pending
        # 锁被 holder 持有 → _on_batch 应走合并分支，_pending+1，不写 journal
        capture._on_batch(batch)
        self.assertEqual(capture._pending, pending_before + 1)
        self.assertEqual(journal.count(task_id, RP_FILE_INC), inc_before)

        release.set()
        t.join(timeout=5)

    def test_change_batch_writes_file_inc_to_journal(self):
        """一次真实变更批次经 FileRtCapture 落成 file-inc 恢复点。"""
        src = tempfile.mkdtemp(prefix="t02_write_")
        task_id = _mk_file_task("T02-write", src)
        task = models.get_task(task_id, include_secret=True)
        capture = FileRtCapture(task, RtConfig.from_task(task))

        # 建立基准全量（落 base-full + 写快照）
        base_point = capture.ensure_base()
        self.assertIsNotNone(base_point)
        journal = RecoveryJournal()
        self.assertGreaterEqual(journal.count(task_id, RP_BASE_FULL), 1)

        # 新增一个文件，构造变更批次
        new_file = os.path.join(src, "added.txt")
        _mk_file(new_file, b"added-content")
        source_files = capture.engine.list_source_files()
        self.assertIn("added.txt", source_files)

        batch = ChangeBatch(changed=["added.txt"], snapshot=source_files,
                            detected_at=db.now_iso(), trigger="manual")
        point = capture._handle_batch(batch)
        self.assertIsNotNone(point)
        self.assertEqual(point.rp_kind, RP_FILE_INC)

        # journal 中应出现 base-full + 至少 1 个 file-inc
        self.assertGreaterEqual(journal.count(task_id, RP_FILE_INC), 1)

        # 恢复链完整：base-full 链头 + 增量，validate_chain 通过
        latest = journal.latest(task_id)
        chain = journal.resolve_chain(task_id, latest.pit_at)
        ok, reason = journal.validate_chain(chain)
        self.assertTrue(ok, f"恢复链校验失败: {reason}")
        self.assertEqual(chain[0].rp_kind, RP_BASE_FULL)


# ============================ T03 数据库 CDC 守护 ============================
class TestT03SimulatedDaemon(unittest.TestCase):
    """验收 T03-1：SimulatedCDCDaemon 生成变更流并写入 journal。"""

    def test_simulated_daemon_generates_and_seals_segments(self):
        src_task_dir = tempfile.mkdtemp(prefix="t03_daemon_")
        task_id = _mk_db_task("T03-daemon")
        task = models.get_task(task_id, include_secret=True)
        cfg = RtConfig.from_task(task)
        repo = LogRepository(task_id, KIND_DB_LOG)
        daemon = SimulatedCDCDaemon(task, cfg, repo)
        self.assertTrue(daemon.is_simulated)
        self.assertTrue(daemon.check_client()[0])

        self.assertTrue(daemon.start())
        self.assertTrue(daemon.is_alive())

        # tick 一次：因 seal_all_immediately，仿真段写入 live 后立即被封存
        result = daemon.tick()
        sealed = result.get("segments") or []
        self.assertGreaterEqual(len(sealed), 1,
                                "一次 tick 应生成一个已封存的仿真段")
        self.assertEqual(sealed[0]["size"], os.path.getsize(sealed[0]["path"]))

        # 显式强制封存剩余段（若有）也应成功
        more = daemon.seal_ready_segments(force=True)
        self.assertIsInstance(more, list)
        daemon.stop()

    def test_dbcapture_registers_db_log_points(self):
        """DbRtCapture 经 tick 把仿真段登记为 db-log 恢复点。"""
        _mk_db_task("T03-worker")  # 占位，避免与下方任务 id 冲突无关
        task_id = _mk_db_task("T03-worker-main")
        task = models.get_task(task_id, include_secret=True)
        worker = DbRtCapture(task, RtConfig.from_task(task))

        # DEMO_MODE=on → 必须走仿真守护
        self.assertTrue(worker.daemon.is_simulated)

        self.assertTrue(worker.start())
        journal = RecoveryJournal()
        # 链头 db-full 已登记
        self.assertGreaterEqual(journal.count(task_id, RP_DB_FULL), 1)

        for _ in range(5):
            worker.tick()

        db_log_count = journal.count(task_id, RP_DB_LOG)
        self.assertGreaterEqual(db_log_count, 3,
                                "多次 tick 应产生多个 db-log 恢复点")

        # 所有 db-log 点均为仿真产物
        latest = journal.latest(task_id, kind=RP_DB_LOG)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.is_simulated, 1)

        # 恢复链完整（db-full + db-log），validate_chain 通过
        chain = journal.resolve_chain(task_id, latest.pit_at)
        ok, reason = journal.validate_chain(chain)
        self.assertTrue(ok, f"DB 恢复链校验失败: {reason}")
        worker.stop()


class TestT03SupervisorLifecycle(unittest.TestCase):
    """验收 T03-2/3：RtSupervisor 生命周期 + db_cdc 任务 CRUD。"""

    def setUp(self):
        reset_supervisor()

    def tearDown(self):
        reset_supervisor()

    def test_supervisor_spawn_and_stop_worker(self):
        task_id = _mk_db_task("T03-sup")
        sup = get_supervisor()
        task = models.get_task(task_id, include_secret=True)
        ok = sup._spawn_worker(task, RtConfig.from_task(task))
        self.assertTrue(ok)
        worker = sup.worker_of(task_id)
        self.assertIsNotNone(worker)
        self.assertTrue(worker.is_alive())
        # 手动停止该 worker（从管理集合移除）
        sup._stop_worker(task_id)
        self.assertIsNone(sup.worker_of(task_id))

    def test_supervisor_reconcile_starts_db_task(self):
        task_id = _mk_db_task("T03-reconcile")
        sup = get_supervisor()
        res = sup.reconcile()
        self.assertIn(task_id, res.get("started", []))
        worker = sup.worker_of(task_id)
        self.assertIsNotNone(worker)
        self.assertTrue(worker.is_alive())
        sup.stop()
        self.assertIsNone(sup.worker_of(task_id))

    def test_db_cdc_task_crud_via_models(self):
        task_id = _mk_db_task("T03-crud")
        rid = models.create_rt_task({
            "task_id": task_id,
            "rt_mode": "db_cdc",
            "capture_interval": 30,
            "db_log_retention_days": 5,
        })
        self.assertGreater(rid, 0)
        row = models.get_rt_task(task_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["rt_mode"], "db_cdc")
        self.assertEqual(row["capture_interval"], 30)

        self.assertTrue(models.update_rt_task(
            task_id, {"rt_mode": "db_cdc", "capture_interval": 60}))
        row2 = models.get_rt_task(task_id)
        self.assertEqual(row2["capture_interval"], 60)

        all_rows = models.list_rt_tasks(only_enabled=False)
        # list_rt_tasks 返回的是 backup_tasks 行（含明文密码），主键为 id
        self.assertTrue(any(r["id"] == task_id for r in all_rows))

    def test_db_cdc_mode_registered_by_worker(self):
        """DbRtCapture 启动后应在 rt_tasks 中把 rt_mode 注册为 db_cdc。"""
        task_id = _mk_db_task("T03-mode")
        task = models.get_task(task_id, include_secret=True)
        worker = DbRtCapture(task, RtConfig.from_task(task))
        try:
            worker.start()
            row = models.get_rt_task(task_id)
            self.assertIsNotNone(row)
            self.assertEqual(row["rt_mode"], "db_cdc")
        finally:
            worker.stop()


class TestT03DaemonFactory(unittest.TestCase):
    """验收 T03：工厂在 DEMO_MODE 下强制仿真；真实客户端缺失则 skip。"""

    def test_create_daemon_forces_simulated_in_demo_mode(self):
        # T06：信创三库已具备真实实现，DEMO_MODE 下同样必须强制仿真
        for engine in ("mysql", "mariadb", "postgresql",
                       "oracle", "kingbase", "dameng"):
            task_id = _mk_db_task(f"T03-factory-{engine}")
            db.execute("UPDATE backup_tasks SET db_type=? WHERE id=?",
                       (engine, task_id))
            task = models.get_task(task_id, include_secret=True)
            cfg = RtConfig.from_task(task)
            repo = LogRepository(task_id, KIND_DB_LOG)
            daemon = create_daemon(task, cfg, repo)
            self.assertTrue(daemon.is_simulated,
                            f"{engine} 在 DEMO_MODE 下应强制仿真")
            self.assertIsInstance(daemon, SimulatedCDCDaemon)

    def test_mysql_binlog_daemon_skips_without_client(self):
        """mysql-replication 未安装时，跳过真实 binlog 守护测试。"""
        try:
            import pymysqlreplication  # noqa: F401
        except Exception:
            self.skipTest("mysql-replication(pymysqlreplication) 未安装，"
                          "跳过真实 binlog 守护测试")
        from core.cdc.mysql_binlog import MySQLBinlogDaemon
        self.assertTrue(hasattr(MySQLBinlogDaemon, "is_available"))

    def test_probe_clients_reports_capabilities(self):
        caps = create_daemon.__module__  # 仅确保导入成功，下方直接调 probe_clients
        from core.cdc import probe_clients
        result = probe_clients()
        self.assertEqual(result["demo_mode"], "on")
        self.assertIn("mysql-replication", result["optional_packages"])
        self.assertIn("psycopg2", result["optional_packages"])
        # 本环境 mysql-replication 确未安装
        self.assertFalse(
            result["optional_packages"]["mysql-replication"]["installed"])
        # T06：Oracle/Kingbase/Dameng 已由"排期后置"转为真实实现，
        # deferred_engines 同步清空（原断言为含三库，此处同步更新）
        self.assertEqual(result["deferred_engines"], [])
        for engine in ("oracle", "kingbase", "dameng"):
            self.assertIn(engine, result["supported_engines"])


# ============================ T04 PITR 恢复 ============================
class TestT04PitrReplay(unittest.TestCase):
    """验收 T04-1：按 target_time 回放得到正确恢复序列。"""

    def _build_file_chain(self, task_id):
        """构造 base-full + 3 个 file-inc，返回 (base_point, points, times)。"""
        repo = LogRepository(task_id, KIND_FILE)
        journal = RecoveryJournal()
        base_file = _mk_file(os.path.join(repo.base_dir(), "base.tar.gz"),
                             b"BASE" * 200)
        now = datetime.now()
        t0 = _iso(now)
        bp = journal.append(task_id, {
            "rp_kind": RP_BASE_FULL, "rp_type": "full",
            "pit_at": t0, "object_key": base_file, "storage_tier": 1,
        })
        prev = bp.id
        points = [bp]
        times = [t0]
        for i in range(1, 4):
            inc_file = _mk_file(
                os.path.join(repo.inc_dir(), f"inc{i}.tar.gz"),
                (f"inc-content-{i}" * 30).encode())
            ti = _iso(now + timedelta(seconds=10 * i))
            times.append(ti)
            p = journal.append(task_id, {
                "rp_kind": RP_FILE_INC, "rp_type": "incremental",
                "pit_at": ti, "parent_rp_id": prev,
                "object_key": inc_file, "storage_tier": 1,
            })
            prev = p.id
            points.append(p)
        return bp, points, times

    def test_build_plan_replays_correct_sequence(self):
        src = tempfile.mkdtemp(prefix="t04_replay_")
        task_id = _mk_file_task("T04-replay", src)
        bp, _points, times = self._build_file_chain(task_id)
        pitr = PITRRestore()

        # 恢复到第 2 个增量（times[2]）应得到 base + inc1 + inc2
        plan = pitr.build_plan(task_id, target_ts=times[2])
        self.assertTrue(plan.complete, plan.gap_reason)
        self.assertEqual(len(plan.chain), 3)
        self.assertEqual(plan.chain[0].id, bp.id)
        # 链按 pit_at 升序
        self.assertEqual(
            plan.chain,
            sorted(plan.chain, key=lambda p: p.pit_at))

    def test_build_plan_before_base_is_incomplete(self):
        src = tempfile.mkdtemp(prefix="t04_incomplete_")
        task_id = _mk_file_task("T04-incomplete", src)
        self._build_file_chain(task_id)
        pitr = PITRRestore()
        early = _iso(datetime.now() - timedelta(hours=1))
        plan = pitr.build_plan(task_id, target_ts=early)
        self.assertFalse(plan.complete)
        self.assertTrue(plan.gap_reason)

    def test_window_returns_correct_bounds(self):
        src = tempfile.mkdtemp(prefix="t04_window_")
        task_id = _mk_file_task("T04-window", src)
        _bp, _points, times = self._build_file_chain(task_id)
        pitr = PITRRestore()
        win = pitr.window(task_id)
        self.assertEqual(win["earliest"], times[0])
        self.assertEqual(win["latest"], times[-1])
        self.assertEqual(win["total"], 4)
        self.assertEqual(win["kind"], KIND_FILE)

    def test_resolve_chain_orders_ascending(self):
        src = tempfile.mkdtemp(prefix="t04_chain_")
        task_id = _mk_file_task("T04-chain", src)
        self._build_file_chain(task_id)
        journal = RecoveryJournal()
        latest = journal.latest(task_id)
        chain = journal.resolve_chain(task_id, latest.pit_at)
        self.assertGreaterEqual(len(chain), 4)
        self.assertEqual(chain[0].rp_kind, RP_BASE_FULL)
        self.assertEqual(
            chain, sorted(chain, key=lambda p: (p.pit_at, p.pit_seq)))


# ============================ T04/T05 接口契约 + 页面 ============================
class TestT04ApiContractAndT05Page(unittest.TestCase):
    """验收 T04-2 接口契约 + T05 页面渲染（Flask test_client）。"""

    def _login(self, client):
        resp = client.post(
            "/login",
            json={"username": config.WEB_USERNAME,
                  "password": config.WEB_PASSWORD},
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_api_points_and_recover_contract(self):
        from app import create_app
        test_app = create_app()
        client = test_app.test_client()
        self._login(client)

        # 准备一个带恢复链的文件任务
        src = tempfile.mkdtemp(prefix="t04_api_")
        task_id = _mk_file_task("T04-api", src)
        repo = LogRepository(task_id, KIND_FILE)
        journal = RecoveryJournal()
        base_file = _mk_file(os.path.join(repo.base_dir(), "base.tar.gz"),
                             b"BASE" * 200)
        now = datetime.now()
        journal.append(task_id, {
            "rp_kind": RP_BASE_FULL, "rp_type": "full",
            "pit_at": _iso(now), "object_key": base_file, "storage_tier": 1,
        })
        inc_file = _mk_file(os.path.join(repo.inc_dir(), "inc1.tar.gz"),
                            b"inc" * 100)
        latest_iso = _iso(now + timedelta(seconds=10))
        journal.append(task_id, {
            "rp_kind": RP_FILE_INC, "rp_type": "incremental",
            "pit_at": latest_iso, "parent_rp_id": journal.latest(task_id).id,
            "object_key": inc_file, "storage_tier": 1,
        })

        # GET /api/rt/points
        r = client.get(f"/api/rt/points?task_id={task_id}")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"], data)
        self.assertGreaterEqual(len(data["items"]), 1)
        self.assertIn("window", data)

        # POST /api/rt/recover（DEMO_MODE 下走仿真恢复）
        r2 = client.post(
            "/api/rt/recover",
            json={"task_id": task_id, "target_ts": latest_iso},
            content_type="application/json")
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        jd = r2.get_json()
        self.assertTrue(jd["ok"], jd)
        self.assertTrue(jd["simulated"])

    def test_rt_timeline_page_renders(self):
        from app import create_app
        test_app = create_app()
        client = test_app.test_client()
        self._login(client)

        r = client.get("/rt-timeline")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        # 页面含「实时备份」标记（导航 + 页面标题）
        self.assertIn("实时备份", html,
                      "导航/页面应包含「实时备份」标记")
        # 初始化脚本由 app.js 提供：页面引用该脚本且脚本内含 initRtTimeline
        self.assertIn("/static/js/app.js", html,
                      "时间轴页面应加载 app.js")
        js = client.get("/static/js/app.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("initRtTimeline", js.get_data(as_text=True),
                      "app.js 应定义 initRtTimeline()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
