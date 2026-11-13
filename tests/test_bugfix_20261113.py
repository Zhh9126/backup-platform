# -*- coding: utf-8 -*-
"""2026-11-13 全面 BUG 排查轮的回归锁定（业务层 + 系统层）。

本文件锁定的都是**实测复现过**的真实缺陷，不是风格问题：

1. 任务列表「下次备份时间」对已跑过一次的 cron 任务恒为空——
   APScheduler 的 get_next_fire_time 收到 naive 的 previous_fire_time 与
   aware 的 now 会抛 TypeError，被宽 except 吞掉后返回 None。
2. 「定期清理」界面配置的最少保留份数（keep_min）被静默忽略——
   run_cleanup 读到了 cfg 却只用了 purge_records。
3. 增量/合成链基备被清理误删——backup_sets.parent_set_id 存的是
   **backup_sets.id**，拿 backup_records.id 去比永不命中（GFS 侧同款）。
4. keep_min 按"记录序位"保留而非"文件仍在磁盘"，幽灵记录会让磁盘上
   最后一个真正可用的备份被清掉。
5. Redis 恢复 rdb 文件替换失败仍返回成功（实例活着 ≠ 数据已恢复），
   且恒写 dump.rdb 与实例 dbfilename 不一致。
6. 跨主机恢复把目标库名直拼进远端 shell 命令（命令注入）。
7. 达梦 PITR 把还原目录直拼远端 shell 命令（命令注入）。
8. 真实恢复演练的临时库名精确到秒，撞名后 CREATE 失败而 finally 仍
   无条件 DROP，可能删掉另一场演练正在用的库。
9. 群晖任务创建时扣 file 配额，但 get_counts 的 file 只统计 db_type='file'
   → 配额可无限绕过，且白占数据库任务名额。
10. 同步通道（run_task_now 非 file 分支）完全没有进程内去重，手动连点
    或手动与调度撞车会并发跑同一任务、覆盖同一产物。
"""
import os
import sys
import tempfile

# ---------------- 0. 隔离运行：临时元数据库 + 临时备份根目录 ----------------
_TMP = os.path.join(tempfile.mkdtemp(prefix="bp_bugfix1113_"), "work")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402

config.META_DB_PATH = _MODULE_DB
_BACKUP_ROOT = os.path.join(_TMP, "backups")
os.makedirs(_BACKUP_ROOT, exist_ok=True)
config.BACKUP_ROOT = _BACKUP_ROOT
db.init_schema()                                # noqa: E402

import pytest                                   # noqa: E402
import core.models as models                    # noqa: E402


def _task(**kw):
    data = {"name": "bugfix-1113", "biz_system": "E2E", "db_type": "mysql",
            "host": "10.9.8.7", "db_name": "appdb", "backup_type": "full",
            "schedule_type": "interval", "interval_minutes": 60}
    data.update(kw)
    return models.create_task(data)


@pytest.fixture(autouse=True)
def _no_quota(monkeypatch):
    """配额不是本文件要测的对象（第 9 项单独验证计数口径）。"""
    from core import license as lic
    monkeypatch.setattr(lic, "check_quota", lambda *a, **k: None)
    monkeypatch.setattr(lic, "check_server_quota", lambda *a, **k: None)


# ---------------- 1. 下次备份时间：naive/aware 混比 ----------------
def test_next_backup_time_not_empty_after_last_run():
    from core import scheduler
    task = {"id": 1, "name": "t", "db_type": "mysql",
            "schedule_type": "cron", "schedule_expr": "0 3 * * *",
            "schedule_days": "", "interval_minutes": 60}
    got = scheduler._next_fire_naive(
        task, "schedule_type", "schedule_expr", "interval_minutes",
        "schedule_days", "2026-11-10 03:00:00")
    assert got is not None, "跑过一次的 cron 任务必须能算出下次触发时间"
    assert got.tzinfo is None, "对外展示统一用本地 naive 时间"


def test_next_backup_time_contract_for_interval_and_manual():
    """区间任务能算；manual（无调度）如实返回空串——不伪造时间。"""
    from core import scheduler
    task = {"id": 2, "name": "t2", "db_type": "mysql",
            "schedule_type": "interval", "interval_minutes": 30,
            "cron_expr": "", "last_run_at": "2026-11-10 03:00:00"}
    assert scheduler._next_fire_naive(
        task, "schedule_type", "cron_expr", "interval_minutes",
        "schedule_days", "2026-11-10 03:00:00") is not None
    manual = {"id": 3, "name": "t3", "db_type": "mysql",
              "schedule_type": "manual", "interval_minutes": 0,
              "cron_expr": "", "last_run_at": ""}
    assert scheduler.next_run_display(manual) == ""


# ---------------- 2. 清理必须用界面配置的 keep_min ----------------
def test_run_cleanup_uses_configured_keep_min(monkeypatch):
    from core import backup_cleanup as bc
    seen = {}

    def fake_plan(task_id=None, keep_min=None):
        seen["keep_min"] = keep_min
        return []

    monkeypatch.setattr(bc, "build_plan", fake_plan)
    db.set_system_config(bc.CFG_KEEP_MIN, "7")
    try:
        bc.run_cleanup(dry_run=True)
        assert seen["keep_min"] == 7, "调度/手动清理都必须回落到界面配置的 keep_min"
        # 显式传参时以调用方为准（一键清理预览可自定义）
        bc.run_cleanup(dry_run=True, keep_min=3)
        assert seen["keep_min"] == 3
    finally:
        db.set_system_config(bc.CFG_KEEP_MIN, "1")


# ---------------- 3/4. 增量链基备 + 幽灵记录护栏 ----------------
def _mk_record(task_id, path, status="success"):
    db.execute(
        "INSERT INTO backup_records (task_id, status, backup_path, size_bytes, "
        "started_at, finished_at) VALUES (?,?,?,?,?,?)",
        (task_id, status, path, os.path.getsize(path) if os.path.isfile(path) else 0,
         "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
    return db.query_one("SELECT id FROM backup_records ORDER BY id DESC LIMIT 1")["id"]


def test_chain_base_is_protected_from_cleanup():
    """基备（被增量引用）不得进入清理清单，否则整条链无法恢复。"""
    from core import backup_cleanup as bc
    tid = _task(name="chain-task", retention_count=1, interval_minutes=60)
    p_base = os.path.join(_BACKUP_ROOT, "chain_base.sql")
    p_inc = os.path.join(_BACKUP_ROOT, "chain_inc.sql")
    for p in (p_base, p_inc):
        with open(p, "wb") as f:
            f.write(b"x" * 16)
    base_rid = _mk_record(tid, p_base)
    inc_rid = _mk_record(tid, p_inc)
    base_sid = models.create_backup_set({
        "task_id": tid, "record_id": base_rid, "set_type": "full",
        "object_key": p_base, "size_bytes": 16})
    models.create_backup_set({
        "task_id": tid, "record_id": inc_rid, "set_type": "incremental",
        "object_key": p_inc, "parent_set_id": base_sid, "size_bytes": 16})

    assert bc._is_chain_base(base_rid) is True
    assert bc._is_chain_base(inc_rid) is False

    plan = bc.build_plan(task_id=tid, keep_min=0)
    ids = [i["record_id"] for p in plan for i in p["items"]]
    assert base_rid not in ids, "链基备绝不能被清理"


def test_gfs_chain_guard_matches_set_id_not_record_id():
    """GFS 侧护栏同样必须按 set id 判定（否则护栏永不命中）。"""
    src = open(os.path.join(PROJECT_ROOT, "core", "retention_gfs.py"),
               encoding="utf-8").read()
    assert "SELECT id FROM backup_sets WHERE record_id=?" in src
    assert "parent_set_id IN (%s)" in src


def test_keep_min_counts_records_with_existing_file():
    """最新几条是幽灵记录时，磁盘上最后一个可用备份仍要保留。"""
    from core import backup_cleanup as bc
    tid = _task(name="ghost-task", retention_days=1, interval_minutes=60)
    p_alive = os.path.join(_BACKUP_ROOT, "alive.sql")
    with open(p_alive, "wb") as f:
        f.write(b"y" * 16)
    alive_rid = _mk_record(tid, p_alive)
    # 两条更新的幽灵记录（文件不存在）
    _mk_record(tid, os.path.join(_BACKUP_ROOT, "ghost1.sql"))
    _mk_record(tid, os.path.join(_BACKUP_ROOT, "ghost2.sql"))
    # 让全部记录都被保留策略判为过期（3 天前完成）
    db.execute("UPDATE backup_records SET finished_at='2026-01-01 00:00:00', "
               "started_at='2026-01-01 00:00:00' WHERE task_id=?", (tid,))
    plan = bc.build_plan(task_id=tid, keep_min=1)
    ids = [i["record_id"] for p in plan for i in p["items"]]
    assert alive_rid not in ids, "唯一还有文件的备份不能因幽灵记录占据保留位而被清"


# ---------------- 5. Redis 恢复不得假成功 ----------------
def test_redis_restore_fails_when_rdb_replace_fails():
    src = open(os.path.join(PROJECT_ROOT, "core", "engines", "redis.py"),
               encoding="utf-8").read()
    i_fail = src.index("rdb 文件替换未成功")
    i_ping = src.index('ping_ok = "PONG" in out')
    assert i_fail < i_ping, "替换失败必须在 PING 判定之前如实返回失败"
    # 写盘文件名必须跟随实例 dbfilename
    assert "data_dir + '/' + rdb_name" in src


# ---------------- 6/7. 命令注入 ----------------
def test_cross_host_rejects_shell_injection_in_target_db():
    from core import cross_host
    for bad in ("db'; rm -rf /;'", "db`id`", "db$(id)", "db|x", "a b"):
        with pytest.raises(ValueError):
            cross_host._build_restore_cmd("mysql", "/tmp/x.sql", bad, {}, {})
    cmd = cross_host._build_restore_cmd(
        "mysql", "/tmp/x.sql", "appdb", {"source_username": "root"}, {})
    assert "appdb" in cmd and "CREATE DATABASE" in cmd


def test_cross_host_file_target_allows_spaces_but_is_quoted():
    from core import cross_host
    cmd = cross_host._build_restore_cmd("file", "/tmp/p.tar.gz",
                                        "/data/my restore", {}, {})
    assert "/data/my restore" in cmd
    assert "'/data/my restore'" in cmd, "含空格的目录必须被 shell quote"


def test_dameng_pitr_rejects_unsafe_restore_dir():
    src = open(os.path.join(PROJECT_ROOT, "core", "engines", "dameng.py"),
               encoding="utf-8").read()
    assert "非法的 PITR 还原目录" in src
    assert "_rd_q = shlex.quote(restore_dir)" in src


# ---------------- 8. 演练临时库命名与销毁 ----------------
def test_drill_db_name_unique_and_cleanup_guarded():
    src = open(os.path.join(PROJECT_ROOT, "core", "drill.py"),
               encoding="utf-8").read()
    assert "uuid4().hex[:8]" in src, "演练库名必须带随机后缀，避免同秒撞名"
    assert "created_ok" in src, "只有本次建成功的库才允许销毁"
    assert "if created_ok else True" in src


# ---------------- 9. 群晖计入 file 配额 ----------------
def test_synology_task_counts_against_file_quota():
    from core import license as lic
    before = lic.get_counts()
    _task(name="syn-quota", db_type="synology", host="127.0.0.1", port=5000,
          db_name="/share")
    after = lic.get_counts()
    assert after["file"] == before["file"] + 1, "群晖任务必须计入 file 配额"
    assert after["database"] == before["database"], "不得白占数据库任务名额"


# ---------------- 10. 同步通道并发去重 ----------------
def test_run_task_now_rejects_concurrent_trigger(monkeypatch):
    from core import scheduler
    tid = _task(name="concurrent-task", interval_minutes=60)
    monkeypatch.setattr(scheduler.models, "get_task",
                        lambda *a, **k: {"id": tid, "db_type": "mysql",
                                         "name": "concurrent-task"})
    scheduler._RUNNING_TASKS.add(tid)
    try:
        got = scheduler.run_task_now(tid)
        assert got and got.get("duplicate") is True
        assert got.get("accepted") is False
    finally:
        scheduler._RUNNING_TASKS.discard(tid)


def test_run_task_now_marks_advisory_duplicate_in_source():
    """API 层必须把 duplicate 映射成 409（否则前端看到 200）。"""
    src = open(os.path.join(PROJECT_ROOT, "api", "tasks.py"),
               encoding="utf-8").read()
    assert 'if record.get("duplicate"):' in src
    assert "return jsonify(record), 409" in src


# ---------------- 11. 文件恢复的增量删除清单不得越界 ----------------
def test_file_restore_apply_deleted_rejects_traversal():
    """被篡改的 .deleted.txt（含 .. 或绝对路径）不能删到恢复目录之外。"""
    import types
    from core.engines.file import FileBackupEngine

    stub = types.SimpleNamespace(
        task_name="t",
        logger=types.SimpleNamespace(
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None))

    root = os.path.join(_TMP, "restore_root")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    victim = os.path.join(_TMP, "victim.txt")
    with open(victim, "wb") as f:
        f.write(b"keep me")
    inside = os.path.join(root, "sub", "inside.txt")
    with open(inside, "wb") as f:
        f.write(b"delete me")

    FileBackupEngine._apply_deleted(
        stub, root, ["../victim.txt", "/etc/passwd", "sub/inside.txt"])

    assert os.path.isfile(victim), "越界删除必须被拦截"
    assert not os.path.exists(inside), "目录内的正常删除仍要生效"


# ---------------- 12. 保留份数按「备份份」而不是文件个数 ----------------
def test_retention_count_groups_multi_file_batches():
    from core.storage import group_backup_batches

    d = os.path.join(_TMP, "batch_root")
    os.makedirs(d, exist_ok=True)
    # 最新一份：两个文件（同次备份的多片）；第二份：一个；最老一份：两个
    order = [
        ("new_a.tar.gz", 3_000_000), ("new_b.tar.gz", 2_999_990),
        ("mid.tar.gz", 2_000_000),
        ("old_a.tar.gz", 1_000_000), ("old_b.tar.gz", 999_990),
    ]
    paths = []
    for name, ts in order:
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"z")
        os.utime(p, (ts, ts))
        paths.append(p)

    batches = group_backup_batches(paths)
    assert len(batches) == 3, "多文件同批必须聚成一份，而不是按文件个数算 5 份"
    assert batches[0] == [paths[0], paths[1]] or set(batches[0]) == {paths[0], paths[1]}
    # retention_count=2 → 只有最老那一份（两个文件）整体进入删除清单
    to_delete = []
    for batch in batches[2:]:
        to_delete.extend(batch)
    assert set(to_delete) == {paths[3], paths[4]}


# ---------------- 13. 多 worker 启动自愈：只修已消亡进程的记录 ----------------
def test_runtime_registry_alive_workers_and_stale_parse():
    import json
    import time as _t
    from core import runtime_registry as rr

    rr.register()
    alive = rr.alive_workers()
    assert rr.RUNNER_ID in alive, "本进程必须被视为存活"

    # 伪造一个 10 分钟没心跳的 worker：不得出现在存活集合里
    data = rr._load()
    data["dead-host:1:1"] = {"first_seen": _t.time() - 600,
                             "last_beat": _t.time() - 600}
    db.set_system_config(rr._KEY, json.dumps(data))
    assert "dead-host:1:1" not in rr.alive_workers(ttl=90)
    assert rr.RUNNER_ID in rr.alive_workers(ttl=90)

    # 时间解析：naive / ISO / 带时区都要能转成 epoch
    assert rr.parse_started("2026-11-13 10:00:00") > 0
    assert rr.parse_started("2026-11-13T10:00:00+08:00") > 0
    assert rr.parse_started("") == 0


def test_startup_heal_respects_alive_workers():
    src = open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8").read()
    assert "runtime_registry" in src
    assert "_alive_since" in src, "自愈必须按存活 worker 的登记时间过滤"
    assert "存活 worker=%s" in src


# ---------------- 14. 演练不得清生产实例 binlog/GTID ----------------
def test_drill_restore_skips_reset_master():
    mysql_src = open(os.path.join(PROJECT_ROOT, "core", "engines", "mysql.py"),
                     encoding="utf-8").read()
    drill_src = open(os.path.join(PROJECT_ROOT, "core", "drill.py"),
                     encoding="utf-8").read()
    assert 'bool(kwargs.get("drill_mode"))' in mysql_src
    assert "self._drill_mode" in mysql_src
    # 两处 RESET MASTER 调用点都必须被演练开关守卫
    assert mysql_src.count("if not getattr(self, \"_drill_mode\", False):") >= 2
    assert "drill_mode=True" in drill_src


# ---------------- 15. 目标库留空必须确认（原位覆盖源库） ----------------
def test_restore_requires_same_db_confirm():
    sch = open(os.path.join(PROJECT_ROOT, "core", "scheduler.py"),
               encoding="utf-8").read()
    assert "confirm_same_db" in sch
    assert "按源库名原位覆盖" in sch
    api_src = open(os.path.join(PROJECT_ROOT, "api", "restore.py"),
                   encoding="utf-8").read()
    assert "confirm_same_db=" in api_src
    app_js = open(os.path.join(PROJECT_ROOT, "static", "js", "app.js"),
                  encoding="utf-8").read()
    assert "body.confirm_same_db = true" in app_js
    assert "原位覆盖" in app_js


# ---------------- 16. 口令脱敏 ----------------
def test_redact_covers_all_remote_command_forms():
    from core.redact import redact, redact_cmd

    assert "Ceshi@133" not in redact("mysqldump -uroot -p'Ceshi@133' app")
    assert "Ttest@133" not in redact("export PGPASSWORD='Ttest@133'; psql -h x")
    assert "Kingbase@123" not in redact("ksql -U system -W 'Kingbase@123' -d t")
    assert "Redis@Test6389" not in redact("redis-cli -p 6389 -a 'Redis@Test6389'")
    assert "Tiger@123" not in redact('sqlcmd -U sa -P "Tiger@123"')
    assert "Dameng@123" not in redact("USERID='SYSDBA/Dameng@123@127.0.0.1:5236'")
    assert "Dameng@123" not in redact('disql \'SYSDBA/"Dameng@123"@localhost\'')
    assert "Secret123" not in redact("mysql://root:Secret123@10.0.0.1:3306/db")
    # 先脱敏再截断：口令即使落在截断边界也不能泄漏
    cut = redact_cmd("mysqldump -uroot -p'SECRETVALUE' appdb " + "x" * 40, 30)
    assert "SECRETVALUE" not in cut
    # 无敏感内容时原样返回（不做无谓改动）
    assert redact("echo hello") == "echo hello"


def test_add_log_redacts_password(monkeypatch):
    captured = {}

    def fake_execute(sql, params=None):
        captured["params"] = params

    monkeypatch.setattr(db, "execute", fake_execute)
    db.add_log("INFO", "test", "restore cmd: mysqldump -p'P@ssw0rd!' db",
               detail="export PGPASSWORD='P@ssw0rd!'")
    assert "P@ssw0rd!" not in str(captured.get("params"))


# ---------------- 17. SSH 主机指纹严格模式可开启 ----------------
def test_cross_host_strict_host_key_switch(monkeypatch):
    import paramiko
    from core import cross_host

    monkeypatch.setattr(db, "get_system_config", lambda k: "1")
    policy, strict = cross_host._host_key_policy()
    assert strict is True
    assert isinstance(policy, paramiko.RejectPolicy)

    monkeypatch.setattr(db, "get_system_config", lambda k: "")
    policy2, strict2 = cross_host._host_key_policy()
    assert strict2 is False
    assert isinstance(policy2, paramiko.AutoAddPolicy)
