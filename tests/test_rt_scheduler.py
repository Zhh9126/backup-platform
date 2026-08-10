# -*- coding: utf-8 -*-
"""T03-S3 调度生命周期集成端到端验证：scheduler 拉起 RtSupervisor。

验证点：
1. ``_register_rt_backup`` 在 RT 总开关开启时，于共享 APScheduler 上注册
   Supervisor 主循环 tick + 3 个周期任务（rt_health / rt_prune / rt_watchdog）；
2. 3 个周期任务均 ``max_instances=1 + coalesce=True``；
3. RT 总开关关闭时 ``_register_rt_backup`` 为 no-op，不抢锁、不注册任何 job；
4. ``reload_scheduler`` 路径下 Supervisor 主循环 tick 不被删除（不重启守护）；
5. Supervisor 复用调度器传入的 APScheduler 实例（driver=apscheduler）。
"""
import os
import sys
import tempfile
import unittest

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
os.environ["RT_BACKUP_ENABLED"] = "true"
os.environ["SCHEDULER_ENABLED"] = "true"
_TMP = tempfile.mkdtemp(prefix="rt_sched_test_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["RT_LOG_ROOT"] = os.path.join(_TMP, "rt_logs")
os.environ["RT_FILE_ROOT"] = os.path.join(_TMP, "rt_files")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
for _d in (os.environ["INSTANCE_DIR"], os.environ["LOG_DIR"], os.environ["BACKUP_ROOT"],
           os.environ["RT_LOG_ROOT"], os.environ["RT_FILE_ROOT"]):
    os.makedirs(_d, exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
db.init_schema()                                # noqa: E402

import core.scheduler as scheduler_mod          # noqa: E402
from core.rt_backup.supervisor import (_JOB_ID,  # noqa: E402
                                        reset_supervisor)


def _fresh_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    return BackgroundScheduler()


class RtSchedulerIntegrationTest(unittest.TestCase):
    def tearDown(self):
        # 干净回收：先停守护（释放单实例锁），再销毁单例、清理锁文件
        try:
            from core import rt_backup
            rt_backup.stop()
        except Exception:
            pass
        reset_supervisor()
        try:
            os.unlink(config.RT_LOCK_FILE)
        except OSError:
            pass

    def test_register_rt_backup_creates_four_jobs(self):
        sched = _fresh_scheduler()
        scheduler_mod._register_rt_backup(sched)
        sched.start()
        ids = {j.id for j in sched.get_jobs()}
        self.assertTrue(
            {"rt_supervisor_tick", "rt_health", "rt_prune", "rt_watchdog"}
            .issubset(ids),
            f"RT 任务未全部注册: {ids}")
        sched.shutdown(wait=False)

    def test_periodic_jobs_are_single_instance_coalesced(self):
        sched = _fresh_scheduler()
        scheduler_mod._register_rt_backup(sched)
        sched.start()
        for jid in ("rt_health", "rt_prune", "rt_watchdog"):
            job = sched.get_job(jid)
            self.assertIsNotNone(job, f"{jid} 未注册")
            self.assertEqual(job.max_instances, 1,
                             f"{jid} 必须为 max_instances=1")
            self.assertTrue(job.coalesce, f"{jid} 必须为 coalesce=True")
        sched.shutdown(wait=False)

    def test_register_rt_backup_noop_when_disabled(self):
        saved = config.RT_BACKUP_ENABLED
        config.RT_BACKUP_ENABLED = False
        try:
            sched = _fresh_scheduler()
            scheduler_mod._register_rt_backup(sched)
            ids = {j.id for j in sched.get_jobs()}
            self.assertFalse(
                ids & {"rt_supervisor_tick", "rt_health", "rt_prune",
                       "rt_watchdog"},
                "RT 关闭时不应注册任何 RT job")
        finally:
            config.RT_BACKUP_ENABLED = saved

    def test_reload_preserves_supervisor_tick(self):
        sched = _fresh_scheduler()
        scheduler_mod._register_rt_backup(sched)
        sched.start()
        # 复刻 reload_scheduler 的 RT 处理：跳过 tick job，重注册 3 个周期任务
        for j in list(sched.get_jobs()):
            if j.id == _JOB_ID:
                continue
            sched.remove_job(j.id)
        scheduler_mod._register_rt_periodic_jobs(sched)
        self.assertIsNotNone(
            sched.get_job(_JOB_ID),
            "reload 必须保留 Supervisor 主循环 tick（不重启守护）")
        sched.shutdown(wait=False)

    def test_supervisor_reuses_shared_scheduler(self):
        sched = _fresh_scheduler()
        scheduler_mod._register_rt_backup(sched)
        from core import rt_backup
        status = rt_backup.status()
        self.assertTrue(status["running"], "Supervisor 应在本进程启动")
        self.assertEqual(status["driver"], "apscheduler",
                         "Supervisor 应复用调度器传入的 APScheduler，而非自建")
        sched.start()
        sched.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
