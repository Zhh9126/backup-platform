# -*- coding: utf-8 -*-
"""
T06 信创库 CDC 测试（Oracle LogMiner / Kingbase WAL / 达梦 DM_LOGMNR）。

运行方式（必须用系统 Python 3.14，DEMO_MODE=on）::

    cd E:/备份管理平台/backup_platform
    DEMO_MODE=on python -m pytest tests/test_rt_t06_cdc.py -v

验收边界（主理人拍板 Q1）：本机**没有**真实 Oracle / Kingbase / 达梦环境，
因此验收目标是「代码结构正确 + 降级链路完整 + 位点契约一致」，
所有需要真实连库的路径一律用 **Fake 驱动**（假连接 + 假游标）驱动，
不发起任何真实网络连接。

覆盖点
------
T06-1 拉取式抽象层
  1. ``PollingLogMinerDaemon.is_alive()`` 走 ``_running`` 而非 ``self.proc``；
  2. ``tick()`` 抽变更 → 原子写 ``.jsonl`` 段 → 立即封存 → 位点推进；
  3. ``.jsonl`` 首行 ``_meta`` 元信息结构正确，业务行逐行 JSON；
  4. ``FETCH_LIMIT`` / ``MAX_SEGMENT_BYTES`` 上限保护；
  5. 抽取异常只落 ``last_error``，``tick()`` 绝不外抛；
  6. ``resume_from()`` 续传位点；周期重连（Q7）。
T06-2 Kingbase
  7. ``KingbaseWALDaemon`` 继承 PG 实现，6 个类属性差异生效；
  8. PG 行为契约不回归（端口/密码环境变量/LSN 语句/命令行）。
T06-3 Oracle / Dameng
  9. Oracle：非 ARCHIVELOG → 预检失败（降级原因可读）；
 10. Oracle：``_fetch_changes`` 无论成败都执行 ``END_LOGMNR``；
 11. Oracle：系统 Schema 被排除在 SQL 之外（拍板 Q5）；
 12. 达梦：位点 ``dm_lsn``、``V$RLOG.CUR_LSN`` 取数、归档候选 SQL 回退。
T06-4 注册与自检
 13. ``CDC_REGISTRY`` / ``ENGINE_DAEMON_MAP`` / ``supported_engines()``；
 14. DEMO_MODE 下三库强制仿真；
 15. ``probe_clients()`` 的 ``deferred_engines == []`` 与 3 个新 optional_packages；
 16. 驱动缺失 → ``check_client()`` 返回 False 且原因可读 → 工厂降级仿真。
T06-5 位点与安全
 17. ``DbRtCapture._position_label()`` 的 SCN/LSN 前缀（CH-T06-2）；
 18. ``_daemon_position_fields()`` 把 SCN 落到 wal_lsn/wal_end_lsn 列；
 19. R17：日志输出不含 ``SQL_REDO`` 明文；
 20. ``requirements.txt`` 中信创驱动全部为注释行（不破坏 pip install）。
"""

import io
import json
import logging
import os
import sys
import tempfile
import unittest

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
os.environ["RT_BACKUP_ENABLED"] = "on"
_TMP = tempfile.mkdtemp(prefix="rt_t06_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["RT_LOG_ROOT"] = os.path.join(_TMP, "rt_logs")
os.environ["RT_FILE_ROOT"] = os.path.join(_TMP, "rt_files")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
os.environ["SCHEDULER_ENABLED"] = "false"
for _d in ("INSTANCE_DIR", "LOG_DIR", "BACKUP_ROOT", "RT_LOG_ROOT",
           "RT_FILE_ROOT"):
    os.makedirs(os.environ[_d], exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                        # noqa: E402
import core.db as db                                 # noqa: E402

db.init_schema()                                     # noqa: E402

import core.models as models                         # noqa: E402
from core.rt_backup.repo import LogRepository        # noqa: E402
from core.rt_backup.types import (                   # noqa: E402
    KIND_DB_LOG,
    POSITION_KIND_LABELS,
    STREAMABLE_ENGINES,
    RtConfig,
)
from core.cdc import (                               # noqa: E402
    CDC_REGISTRY,
    ENGINE_DAEMON_MAP,
    ENGINE_IMPORT_ERRORS,
    create_daemon,
    probe_clients,
    supported_engines,
)
from core.cdc.polling_base import PollingLogMinerDaemon      # noqa: E402
from core.cdc.oracle_logminer import OracleLogMinerDaemon    # noqa: E402
from core.cdc.dameng_logmnr import DamengLogMnrDaemon        # noqa: E402
from core.cdc.kingbase_wal import KingbaseWALDaemon          # noqa: E402
from core.cdc.pg_wal import PostgresWALDaemon                # noqa: E402
from core.cdc.simulated import SimulatedCDCDaemon            # noqa: E402


# ======================================================================
# 测试替身：假驱动 / 假连接 / 假游标（绝不发起真实网络连接）
# ======================================================================
class FakeCursor:
    """按「SQL 关键字 → 结果集」脚本返回数据的假游标。"""

    def __init__(self, owner: "FakeConnection") -> None:
        self.owner = owner
        self._rows: list = []

    def execute(self, sql, params=None):
        self.owner.executed.append((str(sql), params))
        if self.owner.raise_on and self.owner.raise_on in str(sql):
            raise RuntimeError(f"注入的 SQL 故障: {self.owner.raise_on}")
        self._rows = self.owner.resolve(str(sql))
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.owner.closed_cursors += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeConnection:
    """假连接：``script`` 是 (SQL 片段, 结果集) 的有序列表，首个命中即返回。"""

    def __init__(self, script=None, raise_on: str = "") -> None:
        self.script = list(script or [])
        self.raise_on = raise_on
        self.executed: list = []
        self.closed = False
        self.closed_cursors = 0

    def resolve(self, sql: str):
        upper = sql.upper()
        for needle, rows in self.script:
            if needle.upper() in upper:
                return list(rows)
        return []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True

    def rollback(self):
        return None

    def executed_sql(self) -> str:
        return "\n".join(sql for sql, _p in self.executed)


class FakeDriver:
    """假驱动模块：``connect()`` 返回预置的 FakeConnection。"""

    __name__ = "fake_driver"

    def __init__(self, conn: FakeConnection = None) -> None:
        self.conn = conn or FakeConnection()
        self.connect_calls = 0

    def connect(self, *args, **kwargs):
        self.connect_calls += 1
        return self.conn


# ======================================================================
# 工具函数
# ======================================================================
def _mk_task(name: str, db_type: str, port: int = 0) -> int:
    """建一个指定引擎的数据库任务并打开实时保护。"""
    task_id = models.create_task({
        "name": name,
        "db_type": db_type,
        "host": "127.0.0.1",
        "port": port,
        "username": "tester",
        "password": "s3cr3t",
        "db_name": "demo",
        "backup_type": "full",
        "schedule_type": "manual",
        "enabled": 1,
    })
    db.execute("UPDATE backup_tasks SET rt_enabled=1 WHERE id=?", (task_id,))
    return task_id


def _build(daemon_cls, db_type: str, name: str, port: int = 0):
    """构造一个未启动的守护（含 task / RtConfig / LogRepository）。"""
    task_id = _mk_task(name, db_type, port=port)
    task = models.get_task(task_id, include_secret=True)
    cfg = RtConfig.from_task(task)
    repo = LogRepository(task_id, KIND_DB_LOG)
    return daemon_cls(task, cfg, repo), repo, task


def _read_jsonl(path: str):
    """读取 .jsonl 段，返回 (meta, rows)。"""
    with open(path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().split("\n") if ln.strip()]
    meta = json.loads(lines[0])
    rows = [json.loads(ln) for ln in lines[1:]]
    return meta, rows


# ======================================================================
# 一个最小可跑的拉取式实现（只为验证抽象层骨架）
# ======================================================================
class _StubPollingDaemon(PollingLogMinerDaemon):
    """把 5 个抽象钩子换成可控桩，用于验证 tick / 落盘 / 位点骨架。"""

    engine_key = "stub_polling"
    display_name = "Stub 拉取式守护"
    required_clients: list = []
    is_simulated = False
    POSITION_KIND = "scn"
    POSITION_LABEL = "SCN"
    POSITION_ROW_KEY = "scn"
    DEFAULT_PORT = 1521

    driver = FakeDriver()
    fail_fetch = False
    positions = None          # 每次 _current_position_value 依次返回
    batches = None            # 每次 _fetch_changes 依次返回的行

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pos_iter = list(self.positions or ["100", "200"])
        self._batch_iter = list(self.batches or [])
        self.connect_count = 0

    @classmethod
    def _import_driver(cls):
        return cls.driver, ""

    def _connect(self):
        self.connect_count += 1
        return FakeConnection()

    def _probe_source(self, conn):
        return True, ""

    def _current_position_value(self, conn):
        if self._pos_iter:
            return self._pos_iter.pop(0)
        return self._last_pos

    def _fetch_changes(self, conn, from_pos, to_pos):
        if self.fail_fetch:
            raise RuntimeError("注入的抽取故障")
        rows = self._batch_iter.pop(0) if self._batch_iter else []
        return rows, to_pos


# ======================================================================
# T06-1 拉取式抽象层
# ======================================================================
class TestT06PollingBase(unittest.TestCase):
    """验收 T06-1：抽象层生命周期 / tick 骨架 / 段落盘 / 位点。"""

    def _start(self, positions, batches):
        cls = type("_D", (_StubPollingDaemon,),
                   {"positions": positions, "batches": batches})
        daemon, repo, _task = _build(cls, "oracle", "T06-polling", port=1521)
        # start() 在 DEMO_MODE 下会拒绝连库（共享知识 #8），这里直接手动就绪
        daemon._conn = FakeConnection()
        daemon._last_pos = positions[0]
        daemon._start_pos = positions[0]
        daemon._running = True
        daemon.started_at = db.now_iso()
        daemon._pos_iter = list(positions[1:])
        daemon._sync_position()
        return daemon, repo

    def test_is_alive_uses_running_flag_not_proc(self):
        """共享知识 #19：拉取式守护没有子进程，is_alive 只看 _running。"""
        daemon, _repo = self._start(["100", "200"], [[]])
        self.assertIsNone(daemon.proc)
        self.assertTrue(daemon.is_alive())
        daemon._running = False
        self.assertFalse(daemon.is_alive())

    def test_demo_mode_start_refuses_real_connection(self):
        """共享知识 #8：DEMO_MODE=on 时 start() 必须拒绝真实连库。"""
        daemon, _repo, _task = _build(_StubPollingDaemon, "oracle", "T06-demo")
        with self.assertRaises(RuntimeError) as ctx:
            daemon.start()
        self.assertIn("演示模式", str(ctx.exception))
        self.assertFalse(daemon.is_alive())

    def test_tick_writes_jsonl_segment_and_advances_position(self):
        """一轮 tick：抽 2 行 → 写 .jsonl → 立即封存 → 位点推进到 200。"""
        rows = [
            {"scn": "150", "ts": "2026-01-01 10:00:00", "owner": "APP",
             "table": "ORDERS", "op": "INSERT",
             "redo": "insert into APP.ORDERS values (1,'张三')", "undo": ""},
            {"scn": "180", "ts": "2026-01-01 10:00:05", "owner": "APP",
             "table": "ORDERS", "op": "UPDATE",
             "redo": "update APP.ORDERS set amt=9 where id=1", "undo": ""},
        ]
        daemon, repo = self._start(["100", "200"], [rows])
        result = daemon.tick()

        self.assertTrue(result["alive"])
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["segments"]), 1,
                         "seal_all_immediately=True 应立即封存本轮段")
        self.assertEqual(daemon._last_pos, "200")

        pos = daemon.current_position()
        self.assertEqual(pos["wal_end_lsn"], "200")
        self.assertEqual(pos["position_kind"], "scn")
        self.assertEqual(pos["scn"], "200")

        info = result["segments"][0]
        self.assertTrue(info["name"].startswith("stub_polling_"))
        self.assertTrue(info["name"].endswith(".jsonl"))
        meta, body = _read_jsonl(info["path"] if info.get("path")
                                 else info["object_key"])
        self.assertTrue(meta["_meta"])
        self.assertEqual(meta["position_kind"], "scn")
        self.assertEqual(meta["from_scn"], "100")
        self.assertEqual(meta["to_scn"], "200")
        self.assertEqual(meta["rows"], 2)
        self.assertFalse(meta["truncated"])
        self.assertEqual([r["op"] for r in body], ["INSERT", "UPDATE"])
        self.assertEqual(body[0]["table"], "ORDERS")

    def test_tick_without_changes_only_advances_position(self):
        """源端位点前进但无业务变更：不产段，位点照常推进。"""
        daemon, _repo = self._start(["100", "300"], [[]])
        result = daemon.tick()
        self.assertEqual(result["segments"], [])
        self.assertEqual(daemon._last_pos, "300")

    def test_tick_never_raises_on_fetch_failure(self):
        """共享知识 #17：抽取异常只落 last_error，tick 绝不外抛。"""
        daemon, _repo = self._start(["100", "200"], [[]])
        daemon.fail_fetch = True
        result = daemon.tick()          # 不应抛出
        self.assertTrue(result["alive"])
        self.assertIn("注入的抽取故障", daemon.last_error)

    def test_segment_truncated_by_max_bytes(self):
        """共享知识 #21：单段超出 MAX_SEGMENT_BYTES 时本轮截断，余量下轮继续。"""
        rows = [{"scn": str(100 + i), "op": "INSERT",
                 "redo": "x" * 400} for i in range(1, 21)]
        cls = type("_D", (_StubPollingDaemon,), {"MAX_SEGMENT_BYTES": 1200})
        daemon, repo, _task = _build(cls, "oracle", "T06-trunc")
        daemon._running = True
        path, written, effective_to = daemon._write_segment(rows, "100", "120")
        self.assertTrue(path.endswith(".jsonl"))
        self.assertLess(written, len(rows), "应发生截断")
        self.assertNotEqual(effective_to, "120",
                            "截断后结束位点必须回退到实际写入的最后一行")
        meta, body = _read_jsonl(path)
        self.assertTrue(meta["truncated"])
        self.assertEqual(len(body), written)

    def test_resume_from_restores_position(self):
        """续传：从 state.position 恢复位点，优先取 wal_end_lsn。"""
        daemon, _repo, _task = _build(_StubPollingDaemon, "oracle", "T06-resume")
        daemon.resume_from({"position": {"wal_lsn": "100",
                                         "wal_end_lsn": "999"}})
        self.assertEqual(daemon._last_pos, "999")

    def test_position_comparison_prefers_integer(self):
        """位点比较：纯数字按整数比（避免 '9' > '100' 的字符串陷阱）。"""
        self.assertTrue(PollingLogMinerDaemon._pos_gt("100", "9"))
        self.assertFalse(PollingLogMinerDaemon._pos_gt("9", "100"))
        self.assertTrue(PollingLogMinerDaemon._pos_gt("1", ""))
        self.assertFalse(PollingLogMinerDaemon._pos_gt("", ""))

    def test_periodic_reconnect_rounds(self):
        """拍板 Q7：每 N 轮重连一次会话，释放 LogMiner 资源。"""
        daemon, _repo = self._start(["100"] + ["100"] * 6, [[]] * 6)
        daemon._reconnect_rounds = 3
        base = daemon.connect_count
        for _ in range(3):
            daemon._poll_once()
        self.assertEqual(daemon.connect_count, base + 1,
                         "第 3 轮应触发一次重连")

    def test_describe_reports_position_kind(self):
        """describe() 暴露 position_kind / position_label，供 UI 渲染前缀。"""
        daemon, _repo, _task = _build(_StubPollingDaemon, "oracle", "T06-desc")
        info = daemon.describe()
        self.assertEqual(info["position_kind"], "scn")
        self.assertEqual(info["position_label"], "SCN")


# ======================================================================
# T06-3 Oracle LogMiner
# ======================================================================
class TestT06Oracle(unittest.TestCase):
    """验收 T06-3：Oracle 预检 / SCN 取数 / END_LOGMNR / 系统 Schema 过滤。"""

    def _daemon(self):
        daemon, repo, _task = _build(OracleLogMinerDaemon, "oracle",
                                     "T06-oracle", port=1521)
        return daemon

    def test_class_contract(self):
        self.assertEqual(OracleLogMinerDaemon.engine_key, "oracle_logminer")
        self.assertEqual(OracleLogMinerDaemon.POSITION_KIND, "scn")
        self.assertEqual(OracleLogMinerDaemon.POSITION_LABEL, "SCN")
        self.assertEqual(OracleLogMinerDaemon.required_clients, [])
        self.assertFalse(OracleLogMinerDaemon.is_simulated)
        self.assertTrue(OracleLogMinerDaemon.seal_all_immediately)
        self.assertTrue(issubclass(OracleLogMinerDaemon, PollingLogMinerDaemon))

    def test_dsn_uses_easy_connect(self):
        daemon = self._daemon()
        self.assertEqual(daemon._dsn(), "127.0.0.1:1521/demo")

    def test_probe_source_rejects_noarchivelog(self):
        """非 ARCHIVELOG 必须预检失败，且原因对运维可读。"""
        conn = FakeConnection([("LOG_MODE", [("NOARCHIVELOG",)])])
        ok, reason = self._daemon()._probe_source(conn)
        self.assertFalse(ok)
        self.assertIn("ARCHIVELOG", reason)
        self.assertIn("降级为仿真", reason)

    def test_probe_source_passes_and_warns_without_supplemental_log(self):
        """补充日志未开启不阻断，只写 degrade_reason。"""
        conn = FakeConnection([
            ("LOG_MODE", [("ARCHIVELOG",)]),
            ("SUPPLEMENTAL_LOG_DATA_MIN", [("NO",)]),
            ("V$LOGMNR_CONTENTS", []),
        ])
        daemon = self._daemon()
        ok, reason = daemon._probe_source(conn)
        self.assertTrue(ok, reason)
        self.assertIn("补充日志", daemon.degrade_reason)

    def test_current_scn(self):
        conn = FakeConnection([("CURRENT_SCN", [(1234567,)])])
        self.assertEqual(self._daemon()._current_position_value(conn), "1234567")

    def test_contents_sql_excludes_system_schemas(self):
        """拍板 Q5：系统 Schema 必须出现在 NOT IN 列表里。"""
        sql = self._daemon()._contents_sql()
        for owner in ("SYS", "SYSTEM", "SYSAUX", "XDB", "DBSNMP"):
            self.assertIn(f"'{owner}'", sql)
        self.assertIn("NOT IN", sql)
        self.assertIn("ORDER BY SCN", sql)

    def test_fetch_changes_always_ends_logmnr(self):
        """START_LOGMNR 之后无论成败都必须 END_LOGMNR（拍板 Q7 / finally）。"""
        daemon = self._daemon()
        conn = FakeConnection(
            [("V$ARCHIVED_LOG", [("/arch/1_100.arc", 100, 1, 500)]),
             ("V$LOGMNR_CONTENTS", [])],
            raise_on="START_LOGMNR")
        with self.assertRaises(RuntimeError):
            daemon._fetch_changes(conn, "100", "200")
        self.assertIn("END_LOGMNR", conn.executed_sql())

    def test_fetch_changes_returns_rows(self):
        daemon = self._daemon()
        conn = FakeConnection([
            ("V$ARCHIVED_LOG", [("/arch/1_100.arc", 100, 1, 500)]),
            ("V$LOGMNR_CONTENTS", [
                (150, "2026-01-01 10:00:00", "APP", "ORDERS", "INSERT",
                 "insert into APP.ORDERS values (1)", None),
            ]),
        ])
        rows, to_pos = daemon._fetch_changes(conn, "100", "200")
        self.assertEqual(to_pos, "200")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner"], "APP")
        self.assertEqual(rows[0]["op"], "INSERT")
        self.assertEqual(rows[0]["undo"], "")
        self.assertIn("END_LOGMNR", conn.executed_sql())

    def test_fetch_changes_holds_position_when_no_logfile(self):
        """挂不到任何日志时位点原地等待，绝不空推进（否则会丢变更）。"""
        daemon = self._daemon()
        conn = FakeConnection([])
        rows, to_pos = daemon._fetch_changes(conn, "100", "200")
        self.assertEqual(rows, [])
        self.assertEqual(to_pos, "100")

    def test_driver_missing_degrades_not_raises(self):
        """驱动缺失（本机确实没装 oracledb）→ check_client False + 原因可读。"""
        ok, reason = OracleLogMinerDaemon.check_client()
        try:
            import oracledb  # noqa: F401
            self.skipTest("本机已安装 oracledb，跳过缺失降级断言")
        except ImportError:
            pass
        self.assertFalse(ok)
        self.assertIn("oracledb", reason)


# ======================================================================
# T06-3 达梦 DM_LOGMNR
# ======================================================================
class TestT06Dameng(unittest.TestCase):
    """验收 T06-3：达梦位点语义 / 归档候选回退 / 系统 Schema 过滤。"""

    def _daemon(self):
        daemon, _repo, _task = _build(DamengLogMnrDaemon, "dameng",
                                      "T06-dameng", port=5236)
        return daemon

    def test_class_contract(self):
        self.assertEqual(DamengLogMnrDaemon.engine_key, "dameng_logmnr")
        self.assertEqual(DamengLogMnrDaemon.POSITION_KIND, "dm_lsn")
        self.assertEqual(DamengLogMnrDaemon.POSITION_LABEL, "LSN")
        self.assertEqual(DamengLogMnrDaemon.POSITION_ROW_KEY, "lsn")
        self.assertEqual(DamengLogMnrDaemon.DEFAULT_PORT, 5236)
        self.assertTrue(issubclass(DamengLogMnrDaemon, PollingLogMinerDaemon))

    def test_sysdba_is_not_excluded(self):
        """达梦的 SYSDBA 常被用作业务 Schema，绝不能排除。"""
        self.assertNotIn("SYSDBA", DamengLogMnrDaemon.EXCLUDED_SCHEMAS)
        self.assertIn("SYS", DamengLogMnrDaemon.EXCLUDED_SCHEMAS)

    def test_probe_source_rejects_non_archive_mode(self):
        conn = FakeConnection([("ARCH_MODE", [("N",)])])
        ok, reason = self._daemon()._probe_source(conn)
        self.assertFalse(ok)
        self.assertIn("归档", reason)

    def test_probe_source_warns_without_append_logic(self):
        conn = FakeConnection([
            ("ARCH_MODE", [("Y",)]),
            ("RLOG_APPEND_LOGIC", [("0",)]),
            ("V$LOGMNR_CONTENTS", []),
        ])
        daemon = self._daemon()
        ok, reason = daemon._probe_source(conn)
        self.assertTrue(ok, reason)
        self.assertIn("RLOG_APPEND_LOGIC", daemon.degrade_reason)

    def test_current_lsn_from_vrlog(self):
        conn = FakeConnection([("CUR_LSN", [(98765,)])])
        self.assertEqual(self._daemon()._current_position_value(conn), "98765")

    def test_arch_files_falls_back_between_candidates(self):
        """首个候选 SQL 无结果时应自动回退到下一个候选。"""
        conn = FakeConnection([("V$ARCHIVED_LOG", [("/dmarch/a.log",)])])
        paths = self._daemon()._arch_files(conn, 1, 100)
        self.assertEqual(paths, ["/dmarch/a.log"])

    def test_fetch_changes_maps_lsn_row_key(self):
        conn = FakeConnection([
            ("V$ARCH_FILE", [("/dmarch/a.log",)]),
            ("V$LOGMNR_CONTENTS", [
                (555, "2026-01-01 11:00:00", "SYSDBA", "T1", "DELETE",
                 "delete from SYSDBA.T1 where id=1", "insert ..."),
            ]),
        ])
        rows, to_pos = self._daemon()._fetch_changes(conn, "100", "600")
        self.assertEqual(to_pos, "600")
        self.assertEqual(rows[0]["lsn"], "555")
        self.assertEqual(rows[0]["owner"], "SYSDBA")
        self.assertIn("END_LOGMNR", conn.executed_sql())

    def test_driver_missing_degrades_not_raises(self):
        ok, reason = DamengLogMnrDaemon.check_client()
        try:
            import dmPython  # noqa: F401
            self.skipTest("本机已安装 dmPython，跳过缺失降级断言")
        except ImportError:
            pass
        self.assertFalse(ok)
        self.assertIn("dmPython", reason)


# ======================================================================
# T06-2 Kingbase / PG 钩子化不回归
# ======================================================================
class TestT06Kingbase(unittest.TestCase):
    """验收 T06-2：Kingbase 复用 PG 实现，且 PG 行为契约逐字不变。"""

    def test_inherits_postgres_implementation(self):
        self.assertTrue(issubclass(KingbaseWALDaemon, PostgresWALDaemon))
        self.assertEqual(KingbaseWALDaemon.engine_key, "kingbase_wal")

    def test_six_class_attribute_overrides(self):
        """CH-T06-1 的 6 个类属性差异全部生效。"""
        self.assertEqual(KingbaseWALDaemon.DEFAULT_PORT, 54321)
        self.assertEqual(KingbaseWALDaemon.DEFAULT_USER, "system")
        self.assertEqual(KingbaseWALDaemon.DEFAULT_DB, "test")
        self.assertIn("sys_receivewal",
                      KingbaseWALDaemon.RECEIVE_CLIENT_CANDIDATES)
        self.assertIn("ksql", KingbaseWALDaemon.QUERY_CLIENT_CANDIDATES)
        self.assertIn("KINGBASE_PASSWORD", KingbaseWALDaemon.PASSWORD_ENV)
        self.assertIn("SELECT sys_current_wal_lsn()",
                      KingbaseWALDaemon.CURRENT_LSN_SQL)

    def test_postgres_contract_unchanged(self):
        """PG 侧不得回归：端口/用户/库/客户端/密码环境变量/LSN 语句。"""
        self.assertEqual(PostgresWALDaemon.DEFAULT_PORT, 5432)
        self.assertEqual(PostgresWALDaemon.DEFAULT_USER, "postgres")
        self.assertEqual(PostgresWALDaemon.DEFAULT_DB, "postgres")
        self.assertEqual(PostgresWALDaemon.RECEIVE_CLIENT_CANDIDATES,
                         ("pg_receivewal",))
        self.assertEqual(PostgresWALDaemon.PASSWORD_ENV, ("PGPASSWORD",))
        self.assertEqual(PostgresWALDaemon.CURRENT_LSN_SQL,
                         ("SELECT pg_current_wal_lsn()",))
        self.assertEqual(PostgresWALDaemon.required_clients, ["pg_receivewal"])
        self.assertFalse(PostgresWALDaemon.seal_all_immediately)

    def test_kingbase_auth_env_injects_both_variables(self):
        daemon, _repo, _task = _build(KingbaseWALDaemon, "kingbase",
                                      "T06-kb-env", port=54321)
        env = daemon._auth_env()
        self.assertEqual(env["KINGBASE_PASSWORD"], "s3cr3t")
        self.assertEqual(env["PGPASSWORD"], "s3cr3t")

    def test_pg_auth_env_only_pgpassword(self):
        daemon, _repo, _task = _build(PostgresWALDaemon, "postgresql",
                                      "T06-pg-env", port=5432)
        env = daemon._auth_env()
        self.assertEqual(env["PGPASSWORD"], "s3cr3t")
        self.assertNotIn("KINGBASE_PASSWORD", env)

    def test_kingbase_defaults_applied_when_task_omits_them(self):
        task_id = _mk_task("T06-kb-default", "kingbase", port=0)
        db.execute("UPDATE backup_tasks SET username='', db_name='' WHERE id=?",
                   (task_id,))
        task = models.get_task(task_id, include_secret=True)
        daemon = KingbaseWALDaemon(task, RtConfig.from_task(task),
                                   LogRepository(task_id, KIND_DB_LOG))
        self.assertEqual(daemon.port, 54321)
        self.assertEqual(daemon.username, "system")
        self.assertEqual(daemon.db_name, "test")
        self.assertTrue(daemon.slot_name.startswith("rt_kb_slot_"))

    def test_receive_cmd_uses_resolved_client(self):
        """客户端缺失时 _receive_cmd 返回空列表，start() 据此优雅失败。"""
        daemon, _repo, _task = _build(KingbaseWALDaemon, "kingbase",
                                      "T06-kb-cmd", port=54321)
        cmd = daemon._receive_cmd()
        ok, _reason = KingbaseWALDaemon.check_client()
        if ok:
            self.assertIn("--slot" if config.RT_PG_CREATE_SLOT else "--no-loop",
                          cmd)
        else:
            self.assertEqual(cmd, [])

    def test_check_client_reason_is_actionable(self):
        ok, reason = KingbaseWALDaemon.check_client()
        if ok:
            self.skipTest("本机存在金仓流复制客户端，跳过缺失文案断言")
        self.assertIn("KingbaseES", reason)
        self.assertIn("PATH", reason)


# ======================================================================
# T06-4 注册表 / 工厂 / 自检
# ======================================================================
class TestT06Registry(unittest.TestCase):
    """验收 T06-4：注册表、工厂分派、自检面板。"""

    def test_registry_contains_three_new_engines(self):
        for key in ("oracle_logminer", "kingbase_wal", "dameng_logmnr"):
            self.assertIn(key, CDC_REGISTRY, f"{key} 未注册进 CDC_REGISTRY")

    def test_engine_daemon_map_routes_correctly(self):
        self.assertIs(ENGINE_DAEMON_MAP["oracle"], OracleLogMinerDaemon)
        self.assertIs(ENGINE_DAEMON_MAP["kingbase"], KingbaseWALDaemon)
        self.assertIs(ENGINE_DAEMON_MAP["dameng"], DamengLogMnrDaemon)

    def test_supported_engines_has_six(self):
        engines = supported_engines()
        for name in ("mysql", "mariadb", "postgresql",
                     "oracle", "kingbase", "dameng"):
            self.assertIn(name, engines)

    def test_streamable_engines_matches_registry(self):
        """types.STREAMABLE_ENGINES 与 CDC 工厂支持范围必须一致。"""
        self.assertEqual(set(STREAMABLE_ENGINES), set(supported_engines()))

    def test_no_import_errors_in_healthy_tree(self):
        """健康代码树下三个实现都应导入成功（故障隔离字典为空）。"""
        self.assertEqual(ENGINE_IMPORT_ERRORS, {},
                         f"存在实现加载失败: {ENGINE_IMPORT_ERRORS}")

    def test_demo_mode_forces_simulated_for_all_three(self):
        """共享知识 #8：DEMO_MODE=on 时三库一律仿真，绝不连真实库。"""
        for engine in ("oracle", "kingbase", "dameng"):
            task_id = _mk_task(f"T06-demo-{engine}", engine)
            task = models.get_task(task_id, include_secret=True)
            daemon = create_daemon(task, RtConfig.from_task(task),
                                   LogRepository(task_id, KIND_DB_LOG))
            self.assertIsInstance(daemon, SimulatedCDCDaemon)
            self.assertTrue(daemon.is_simulated)
            self.assertIn("DEMO_MODE", daemon.degrade_reason)

    def test_create_daemon_never_raises_for_unknown_engine(self):
        task_id = _mk_task("T06-unknown", "mysql")
        db.execute("UPDATE backup_tasks SET db_type='nosuchdb' WHERE id=?",
                   (task_id,))
        task = models.get_task(task_id, include_secret=True)
        daemon = create_daemon(task, RtConfig.from_task(task),
                               LogRepository(task_id, KIND_DB_LOG))
        self.assertIsInstance(daemon, SimulatedCDCDaemon)

    def test_probe_clients_reports_t06(self):
        result = probe_clients()
        self.assertEqual(result["deferred_engines"], [],
                         "T06 之后不应再有排期后置的引擎")
        for pkg in ("oracledb", "ksycopg2", "dmPython"):
            self.assertIn(pkg, result["optional_packages"])
            entry = result["optional_packages"][pkg]
            self.assertIn("installed", entry)
            self.assertIn("reason", entry)
            self.assertIn("hint", entry)
            if not entry["installed"]:
                self.assertTrue(entry["hint"], f"{pkg} 缺失时必须给安装指引（Q6）")
        keys = {impl["key"] for impl in result["implementations"]}
        for key in ("oracle_logminer", "kingbase_wal", "dameng_logmnr"):
            self.assertIn(key, keys)
        self.assertIn("engine_import_errors", result)

    def test_probe_clients_never_raises(self):
        """自检面板绝不允许因为某个驱动异常而整体崩掉。"""
        try:
            probe_clients()
        except Exception as exc:      # pragma: no cover
            self.fail(f"probe_clients() 抛出异常: {exc}")


# ======================================================================
# T06-5 位点契约 / 安全 / 依赖兜底
# ======================================================================
class TestT06PositionAndSafety(unittest.TestCase):
    """验收 CH-T06-2 位点列复用、R17 日志脱敏、依赖兜底。"""

    def _capture(self, engine: str):
        from core.rt_backup.db_rt import DbRtCapture
        task_id = _mk_task(f"T06-cap-{engine}", engine)
        task = models.get_task(task_id, include_secret=True)
        return DbRtCapture(task, RtConfig.from_task(task))

    def test_position_label_prefixes_scn(self):
        cap = self._capture("oracle")
        label = cap._position_label({"wal_lsn": "100", "wal_end_lsn": "1234567",
                                     "position_kind": "scn"})
        self.assertEqual(label, "SCN: 1234567")

    def test_position_label_prefixes_dameng_lsn(self):
        cap = self._capture("dameng")
        label = cap._position_label({"wal_end_lsn": "98765",
                                     "position_kind": "dm_lsn"})
        self.assertEqual(label, "LSN: 98765")

    def test_position_label_unchanged_for_mysql_and_pg(self):
        """未上报 position_kind 的 MySQL / PG 展示文案逐字不变（防回归）。"""
        cap = self._capture("mysql")
        self.assertEqual(
            cap._position_label({"binlog_file": "mysql-bin.000001",
                                 "binlog_pos": 154}),
            "mysql-bin.000001:154")
        self.assertEqual(
            cap._position_label({"binlog_end_file": "mysql-bin.000009",
                                 "binlog_end_pos": 4}),
            "mysql-bin.000009:4")
        self.assertEqual(cap._position_label({"wal_lsn": "0/1A2B3C48"}),
                         "0/1A2B3C48")
        self.assertEqual(cap._position_label({}), "-")

    def test_daemon_position_fields_reuses_wal_columns(self):
        """CH-T06-2：SCN 复用 wal_lsn / wal_end_lsn 列，零 Schema 迁移。"""
        cap = self._capture("oracle")
        fields = cap._daemon_position_fields(
            {"wal_lsn": "100", "wal_end_lsn": "200",
             "scn": "200", "position_kind": "scn"})
        self.assertEqual(fields["wal_lsn"], "100")
        self.assertEqual(fields["wal_end_lsn"], "200")
        self.assertNotIn("position_kind", fields,
                         "position_kind 不是 journal 列，不得写入")

    def test_daemon_position_fields_alias_fallback(self):
        """守护只填了 scn 别名时也要能落到 wal_* 列。"""
        cap = self._capture("dameng")
        fields = cap._daemon_position_fields({"dm_lsn": "5555"})
        self.assertEqual(fields["wal_lsn"], "5555")
        self.assertEqual(fields["wal_end_lsn"], "5555")

    def test_position_kind_labels_cover_all_kinds(self):
        for kind in ("lsn", "scn", "dm_lsn", "binlog"):
            self.assertIn(kind, POSITION_KIND_LABELS)

    def test_logs_never_contain_sql_redo(self):
        """R17：抽取日志只打印行数与位点区间，绝不打印 SQL_REDO 明文。"""
        secret = "insert into APP.CARD values ('6222021234567890')"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("t06.redo.guard")
        logger.setLevel(logging.DEBUG)
        logger.handlers = [handler]
        logger.propagate = False

        cls = type("_D", (_StubPollingDaemon,), {})
        task_id = _mk_task("T06-redo", "oracle")
        task = models.get_task(task_id, include_secret=True)
        daemon = cls(task, RtConfig.from_task(task),
                     LogRepository(task_id, KIND_DB_LOG), logger=logger)
        daemon._conn = FakeConnection()
        daemon._running = True
        daemon._last_pos = "100"
        daemon._start_pos = "100"
        daemon._pos_iter = ["200"]
        daemon._batch_iter = [[{"scn": "150", "op": "INSERT", "redo": secret}]]
        daemon.tick()

        handler.flush()
        text = stream.getvalue()
        self.assertNotIn(secret, text, "日志中出现了 SQL_REDO 明文（违反 R17）")
        self.assertNotIn("6222021234567890", text)
        self.assertIn("抽取 1 行", text)

    def test_password_never_in_command_line(self):
        """共享知识 #16：密码只走环境变量，绝不进 argv。"""
        daemon, _repo, _task = _build(KingbaseWALDaemon, "kingbase",
                                      "T06-kb-secret", port=54321)
        for cmd in (daemon._receive_cmd(), daemon._create_slot_cmd()):
            self.assertNotIn("s3cr3t", " ".join(cmd))

    def test_requirements_keeps_xc_drivers_commented(self):
        """依赖兜底：信创驱动必须全部是注释行，否则 pip install -r 会整体失败。"""
        path = os.path.join(PROJECT_ROOT, "requirements.txt")
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        active = [ln.strip() for ln in lines
                  if ln.strip() and not ln.strip().startswith("#")]
        for token in ("oracledb", "cx_Oracle", "dmPython", "ksycopg2"):
            for line in active:
                self.assertNotIn(token, line,
                                 f"{token} 不得作为非注释依赖出现: {line}")

    def test_pitr_reports_unsupported_for_xc_engines(self):
        """拍板 Q3/Q4：信创库段回放本期不支持，必须给明确文案而非静默失败。"""
        from core.rt_backup import pitr as pitr_mod
        for engine in ("oracle", "kingbase", "dameng"):
            self.assertIn(engine, pitr_mod._T06_CDC_ENGINES)
        self.assertNotIn("kingbase", pitr_mod._PG_ENGINES)


# ======================================================================
# 故障隔离（人为损坏单个实现文件不得影响平台与其他引擎）
# ======================================================================
class TestT06FaultIsolation(unittest.TestCase):
    """验收「零侵入 + 故障隔离」：单实现损坏只影响该引擎。"""

    def test_import_errors_dict_exists_and_is_consulted(self):
        """ENGINE_IMPORT_ERRORS 必须存在，并被 create_daemon 用于降级文案。"""
        import core.cdc as cdc_pkg
        self.assertTrue(hasattr(cdc_pkg, "ENGINE_IMPORT_ERRORS"))
        self.assertIsInstance(cdc_pkg.ENGINE_IMPORT_ERRORS, dict)

    def test_broken_engine_degrades_to_simulated(self):
        """模拟某引擎加载失败：工厂降级仿真，MySQL/PG 完全不受影响。"""
        import core.cdc as cdc_pkg
        saved_map = dict(cdc_pkg.ENGINE_DAEMON_MAP)
        saved_err = dict(cdc_pkg.ENGINE_IMPORT_ERRORS)
        try:
            cdc_pkg.ENGINE_DAEMON_MAP.pop("oracle", None)
            cdc_pkg.ENGINE_IMPORT_ERRORS["oracle"] = (
                "Oracle LogMiner 实现加载失败: SyntaxError(模拟)")
            os.environ["DEMO_MODE"] = "off"
            config.DEMO_MODE = "off"

            task_id = _mk_task("T06-broken-oracle", "oracle")
            task = models.get_task(task_id, include_secret=True)
            daemon = cdc_pkg.create_daemon(
                task, RtConfig.from_task(task),
                LogRepository(task_id, KIND_DB_LOG))
            self.assertIsInstance(daemon, SimulatedCDCDaemon)
            self.assertIn("加载失败", daemon.degrade_reason)

            # MySQL 能力不受影响：仍然命中 MySQLBinlogDaemon 分支
            from core.cdc.mysql_binlog import MySQLBinlogDaemon
            self.assertIs(cdc_pkg.ENGINE_DAEMON_MAP["mysql"],
                          MySQLBinlogDaemon)
            self.assertIs(cdc_pkg.ENGINE_DAEMON_MAP["postgresql"],
                          PostgresWALDaemon)
            # 自检面板仍然可用，并如实报出损坏原因
            caps = cdc_pkg.probe_clients()
            self.assertIn("oracle", caps["engine_import_errors"])
        finally:
            cdc_pkg.ENGINE_DAEMON_MAP.clear()
            cdc_pkg.ENGINE_DAEMON_MAP.update(saved_map)
            cdc_pkg.ENGINE_IMPORT_ERRORS.clear()
            cdc_pkg.ENGINE_IMPORT_ERRORS.update(saved_err)
            os.environ["DEMO_MODE"] = "on"
            config.DEMO_MODE = "on"


if __name__ == "__main__":
    unittest.main(verbosity=2)
