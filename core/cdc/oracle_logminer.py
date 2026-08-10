# -*- coding: utf-8 -*-
"""
Oracle LogMiner 日志捕获守护（T06）。

实现方式：``DBMS_LOGMNR`` + **SCN 区间拉取**

    V$DATABASE.CURRENT_SCN ──▶ to_scn
        │ to_scn > last_scn ?
        ▼
    V$ARCHIVED_LOG / V$LOG ──▶ DBMS_LOGMNR.ADD_LOGFILE 挂载区间日志
        ▼
    DBMS_LOGMNR.START_LOGMNR(DICT_FROM_ONLINE_CATALOG + COMMITTED_DATA_ONLY)
        ▼
    V$LOGMNR_CONTENTS ──▶ 过滤系统 Schema ──▶ .jsonl 逻辑段
        ▼
    DBMS_LOGMNR.END_LOGMNR（**必须在 finally 中执行**，否则会话残留上下文）

为什么 LogMiner 可以远程使用：``ADD_LOGFILE`` 的路径是**数据库服务端路径**，
由 Oracle 服务进程读取，客户端只需连接后查询 ``V$LOGMNR_CONTENTS`` 视图。
因此平台与数据库不同机也能工作，无需共享文件系统。

选型对比：XStream / GoldenGate 需额外商业授权，归档文件直搬要求平台与库同机
或共享目录，两者在客户环境都不可假设，故 MVP 选 LogMiner。

前置条件：
- 数据库处于 ``ARCHIVELOG`` 模式；
- 账号具备 ``SELECT ANY TRANSACTION`` + ``LOGMINING``（12c+）或
  ``EXECUTE_CATALOG_ROLE``（11g），以及 ``V_$LOGMNR_CONTENTS`` / ``V_$ARCHIVED_LOG``
  的查询权限；
- **强烈建议**开启补充日志（``ALTER DATABASE ADD SUPPLEMENTAL LOG DATA``），
  否则 UPDATE 的 ``SQL_REDO`` 可能缺少主键定位条件（不阻断，只告警）。

拍板项：
- Q2 首启不回溯历史归档，以 ``CURRENT_SCN`` 为起点（避免一次拉爆磁盘，风险 R8）；
- Q5 排除系统 Schema；
- Q7 每 ``RT_LOGMNR_RECONNECT_ROUNDS``（默认 50）轮重连一次。

任何驱动缺失 / 非归档模式 / 权限不足都由 :mod:`core.cdc` 工厂或
:class:`core.rt_backup.db_rt.DbRtCapture` 降级到
:class:`core.cdc.simulated.SimulatedCDCDaemon`，**绝不抛到调用方**（共享知识 #17）。
"""
from __future__ import annotations

import importlib
from typing import Any, List, Tuple

from .polling_base import PollingLogMinerDaemon

# 模块级驱动缓存（共享知识 #7：惰性 import，避免每次 tick 重复导入）
_ORACLE_DRIVER: Any = None
_ORACLE_REASON: str = ""

# 驱动候选：oracledb（thin 模式，无需 Instant Client）优先，cx_Oracle 回落
_DRIVER_CANDIDATES: Tuple[str, ...] = ("oracledb", "cx_Oracle")

_MISSING_DRIVER_REASON = (
    "未安装 oracledb / cx_Oracle 驱动，已降级为仿真日志流"
    "（pip install oracledb 后重启生效）")


def _import_oracledb() -> Tuple[Any, str]:
    """惰性导入 Oracle 驱动，结果模块级缓存。

    Returns:
        ``(module | None, 中文原因)``。**绝不抛异常**。
    """
    global _ORACLE_DRIVER, _ORACLE_REASON
    if _ORACLE_DRIVER is not None:
        return _ORACLE_DRIVER, ""
    if _ORACLE_REASON:
        return None, _ORACLE_REASON
    for name in _DRIVER_CANDIDATES:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        _ORACLE_DRIVER = module
        return module, ""
    _ORACLE_REASON = _MISSING_DRIVER_REASON
    return None, _ORACLE_REASON


def reset_driver_cache() -> None:
    """清空驱动缓存（单元测试注入 mock 驱动时使用）。"""
    global _ORACLE_DRIVER, _ORACLE_REASON
    _ORACLE_DRIVER = None
    _ORACLE_REASON = ""


def probe_oracle_driver() -> dict:
    """探测可选依赖 ``oracledb``（自检面板用）。"""
    module, reason = _import_oracledb()
    return {
        "installed": module is not None,
        "reason": reason,
        "hint": ("" if module is not None
                 else "pip install oracledb 可启用 Oracle LogMiner 真实日志捕获"),
    }


class OracleLogMinerDaemon(PollingLogMinerDaemon):
    """Oracle 的 LogMiner 拉取式日志捕获守护。"""

    engine_key = "oracle_logminer"
    display_name = "Oracle LogMiner 日志捕获"
    required_clients: List[str] = []       # 纯 Python 驱动，无外部命令依赖
    is_simulated = False
    seal_all_immediately = True            # 拉取式：每轮产物天然完整

    POSITION_KIND = "scn"
    POSITION_LABEL = "SCN"
    POSITION_ROW_KEY = "scn"
    SEGMENT_EXT = ".jsonl"
    DEFAULT_PORT = 1521
    DEFAULT_USER = "system"
    DEFAULT_SERVICE = "ORCL"
    DRIVER_NAMES = _DRIVER_CANDIDATES

    # 排除的系统 Schema（拍板 Q5）
    EXCLUDED_SCHEMAS: Tuple[str, ...] = (
        "SYS", "SYSTEM", "SYSAUX", "XDB", "DBSNMP", "OUTLN", "WMSYS",
        "CTXSYS", "MDSYS", "ORDSYS", "ORDDATA", "OLAPSYS", "APPQOSSYS",
        "AUDSYS", "LBACSYS", "DVSYS", "OJVMSYS", "GSMADMIN_INTERNAL",
        "REMOTE_SCHEDULER_AGENT", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC",
    )
    # 捕获的操作类型
    CAPTURED_OPERATIONS: Tuple[str, ...] = ("INSERT", "UPDATE", "DELETE", "DDL")

    # --- SQL 常量集中在类属性，便于现场按版本差异改配（风险 R13 同款策略）---
    SQL_LOG_MODE = "SELECT LOG_MODE FROM V$DATABASE"
    SQL_SUPPLEMENTAL = "SELECT SUPPLEMENTAL_LOG_DATA_MIN FROM V$DATABASE"
    SQL_CURRENT_SCN = "SELECT CURRENT_SCN FROM V$DATABASE"
    SQL_ARCHIVED_LOGS = (
        "SELECT NAME, SEQUENCE#, FIRST_CHANGE#, NEXT_CHANGE# "
        "FROM V$ARCHIVED_LOG "
        "WHERE STANDBY_DEST='NO' AND DELETED='NO' AND NAME IS NOT NULL "
        "AND NEXT_CHANGE# > :from_scn AND FIRST_CHANGE# <= :to_scn "
        "ORDER BY SEQUENCE#")
    SQL_ONLINE_LOGS = (
        "SELECT L.GROUP#, F.MEMBER, L.FIRST_CHANGE#, L.NEXT_CHANGE# "
        "FROM V$LOG L JOIN V$LOGFILE F ON L.GROUP#=F.GROUP# "
        "WHERE L.NEXT_CHANGE# > :from_scn OR L.STATUS='CURRENT'")
    SQL_ADD_LOGFILE_NEW = (
        "BEGIN DBMS_LOGMNR.ADD_LOGFILE("
        "LOGFILENAME => :logfile, OPTIONS => DBMS_LOGMNR.NEW); END;")
    SQL_ADD_LOGFILE_MORE = (
        "BEGIN DBMS_LOGMNR.ADD_LOGFILE("
        "LOGFILENAME => :logfile, OPTIONS => DBMS_LOGMNR.ADDFILE); END;")
    SQL_START_LOGMNR = (
        "BEGIN DBMS_LOGMNR.START_LOGMNR("
        "STARTSCN => :from_scn, ENDSCN => :to_scn, "
        "OPTIONS => DBMS_LOGMNR.DICT_FROM_ONLINE_CATALOG "
        "+ DBMS_LOGMNR.COMMITTED_DATA_ONLY "
        "+ DBMS_LOGMNR.NO_ROWID_IN_STMT); END;")
    SQL_END_LOGMNR = "BEGIN DBMS_LOGMNR.END_LOGMNR; END;"

    def __init__(self, task: dict, rt_config, repo, logger=None) -> None:
        super().__init__(task, rt_config, repo, logger=logger)
        self.service_name: str = str(
            self.task.get("service_name") or self.task.get("db_name")
            or self.DEFAULT_SERVICE)
        self._supplemental_warned: bool = False

    # ------------------------------------------------------------------
    # 驱动与连接
    # ------------------------------------------------------------------
    @classmethod
    def _import_driver(cls) -> Tuple[Any, str]:
        """oracledb → cx_Oracle 回落。绝不抛异常。"""
        return _import_oracledb()

    def _dsn(self) -> str:
        """Easy Connect 串：``host:port/service``。"""
        return f"{self.host}:{self.port or self.DEFAULT_PORT}/{self.service_name}"

    def _connect(self):
        """建立 Oracle 连接（oracledb thin 模式无需 Instant Client）。"""
        module, reason = self._import_driver()
        if module is None:
            raise RuntimeError(reason)
        return module.connect(user=self.username, password=self.password,
                              dsn=self._dsn())

    # ------------------------------------------------------------------
    # 源端预检
    # ------------------------------------------------------------------
    def _probe_source(self, conn) -> Tuple[bool, str]:
        """校验归档模式与 LogMiner 权限。

        Returns:
            ``(ok, 中文原因)``。补充日志未开启不阻断，只写 ``degrade_reason`` 附注。
        """
        try:
            row = self._query_one(conn, self.SQL_LOG_MODE)
        except Exception as exc:
            return False, (f"查询 Oracle 归档模式失败（{exc}），"
                           f"当前账号可能缺少 SELECT ON V_$DATABASE 权限，已降级为仿真")
        log_mode = str((row or [""])[0] or "").strip().upper()
        if log_mode != "ARCHIVELOG":
            return False, (f"Oracle 未开启 ARCHIVELOG 模式（当前 {log_mode or '未知'}），"
                           f"无法捕获日志，已降级为仿真")

        # 补充日志：不阻断，只提示（否则 UPDATE 的 SQL_REDO 可能缺主键条件）
        try:
            row = self._query_one(conn, self.SQL_SUPPLEMENTAL)
            supplemental = str((row or [""])[0] or "").strip().upper()
        except Exception:
            supplemental = ""
        if supplemental not in ("YES", "IMPLICIT"):
            self.degrade_reason = (
                "Oracle 未开启补充日志（ALTER DATABASE ADD SUPPLEMENTAL LOG DATA），"
                "UPDATE/DELETE 的重做 SQL 可能缺少主键定位条件")
            self.logger.warning("[rt.cdc] task=%s %s", self.task_id,
                                self.degrade_reason)

        # LogMiner 权限：直接试查一次视图（空结果也算通过）
        try:
            self._query(conn, "SELECT 1 FROM V$LOGMNR_CONTENTS WHERE 1=0")
        except Exception as exc:
            return False, (f"当前账号缺少 LOGMINING / SELECT ANY TRANSACTION 权限"
                           f"（{exc}），已降级为仿真")
        return True, ""

    # ------------------------------------------------------------------
    # 位点
    # ------------------------------------------------------------------
    def _current_position_value(self, conn) -> str:
        """查询 ``V$DATABASE.CURRENT_SCN``。"""
        row = self._query_one(conn, self.SQL_CURRENT_SCN)
        if not row:
            return ""
        value = row[0]
        if value in (None, ""):
            return ""
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------
    def _fetch_changes(self, conn, from_pos: str,
                       to_pos: str) -> Tuple[List[dict], str]:
        """挂载日志 → START_LOGMNR → 查 ``V$LOGMNR_CONTENTS`` → END_LOGMNR。

        Args:
            conn: Oracle 连接。
            from_pos: 起始 SCN（不含）。
            to_pos: 结束 SCN（含）。

        Returns:
            ``(变更行列表, 实际结束 SCN)``。挂不到任何日志时返回 ``([], from_pos)``，
            让位点原地等待下一轮（不能贸然推进，否则会丢变更）。
        """
        from_scn = self._pos_value(from_pos) or 0
        to_scn = self._pos_value(to_pos) or 0
        if to_scn <= from_scn:
            return [], str(from_pos)

        mounted = self._add_logfiles(conn, from_scn, to_scn)
        if mounted <= 0:
            self.logger.debug("[rt.cdc] task=%s SCN 区间 %s→%s 无可挂载日志文件",
                              self.task_id, from_scn, to_scn)
            return [], str(from_pos)

        rows: List[dict] = []
        try:
            self._start_logmnr(conn, from_scn, to_scn)
            rows = self._read_contents(conn, from_scn, to_scn)
        finally:
            # 拍板 Q7：无论成功失败都必须释放 LogMiner 上下文
            self._end_logmnr(conn)
        return rows, str(to_scn)

    def _add_logfiles(self, conn, from_scn: int, to_scn: int) -> int:
        """挂载区间内的归档日志 + 尚未归档的在线重做日志。

        Returns:
            成功挂载的文件数。
        """
        paths: List[str] = []
        try:
            for row in self._query(conn, self.SQL_ARCHIVED_LOGS,
                                   {"from_scn": from_scn, "to_scn": to_scn}):
                name = str(row[0] or "").strip()
                if name and name not in paths:
                    paths.append(name)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 查询归档日志失败: %s",
                                self.task_id, exc)
        try:
            for row in self._query(conn, self.SQL_ONLINE_LOGS,
                                   {"from_scn": from_scn}):
                member = str(row[1] or "").strip()
                if member and member not in paths:
                    paths.append(member)
        except Exception as exc:
            self.logger.debug("[rt.cdc] task=%s 查询在线重做日志失败: %s",
                              self.task_id, exc)
        if not paths:
            return 0

        mounted = 0
        for path in paths:
            sql = (self.SQL_ADD_LOGFILE_NEW if mounted == 0
                   else self.SQL_ADD_LOGFILE_MORE)
            try:
                self._execute(conn, sql, {"logfile": path})
                mounted += 1
            except Exception as exc:
                # 单个文件挂载失败（已删除 / 无读权限）不影响其余文件
                self.logger.debug("[rt.cdc] task=%s 挂载日志失败 %s: %s",
                                  self.task_id, path, exc)
        return mounted

    def _start_logmnr(self, conn, from_scn: int, to_scn: int) -> None:
        """启动 LogMiner 会话（在线数据字典 + 只读已提交事务）。"""
        self._execute(conn, self.SQL_START_LOGMNR,
                      {"from_scn": from_scn, "to_scn": to_scn})

    def _end_logmnr(self, conn) -> None:
        """结束 LogMiner 会话。失败只记 debug，绝不外抛。"""
        try:
            self._execute(conn, self.SQL_END_LOGMNR)
        except Exception as exc:
            self.logger.debug("[rt.cdc] task=%s END_LOGMNR 异常（已忽略）: %s",
                              self.task_id, exc)

    def _contents_sql(self) -> str:
        """构造 ``V$LOGMNR_CONTENTS`` 查询（排除系统 Schema，拍板 Q5）。"""
        ops = ", ".join(f"'{op}'" for op in self.CAPTURED_OPERATIONS)
        owners = ", ".join(f"'{name}'" for name in self.EXCLUDED_SCHEMAS)
        return (
            "SELECT SCN, TIMESTAMP, SEG_OWNER, TABLE_NAME, OPERATION, "
            "SQL_REDO, SQL_UNDO FROM V$LOGMNR_CONTENTS "
            f"WHERE OPERATION IN ({ops}) "
            f"AND (SEG_OWNER IS NULL OR SEG_OWNER NOT IN ({owners})) "
            "AND SCN > :from_scn AND SCN <= :to_scn "
            "ORDER BY SCN")

    def _read_contents(self, conn, from_scn: int, to_scn: int) -> List[dict]:
        """读取变更行并转成段文件的 JSON 结构。

        Note:
            R17：本方法返回的 ``redo`` / ``undo`` 含业务敏感数据，
            只能落进段文件，**绝不允许写入日志**。
        """
        raw = self._query(conn, self._contents_sql(),
                          {"from_scn": from_scn, "to_scn": to_scn})
        rows: List[dict] = []
        for item in raw[:self.FETCH_LIMIT]:
            rows.append({
                "scn": self._cell_str(item, 0),
                "ts": self._cell_str(item, 1),
                "owner": self._cell_str(item, 2),
                "table": self._cell_str(item, 3),
                "op": self._cell_str(item, 4),
                "redo": self._cell_str(item, 5),
                "undo": self._cell_str(item, 6),
            })
        if len(raw) > self.FETCH_LIMIT:
            self.logger.warning(
                "[rt.cdc] task=%s 本轮变更 %s 行超过上限 %s，已截断，余量下轮继续",
                self.task_id, len(raw), self.FETCH_LIMIT)
        return rows

    @staticmethod
    def _cell_str(row, index: int) -> str:
        """安全取列并转字符串（LOB / datetime / None 全兼容）。"""
        try:
            value = row[index]
        except (IndexError, TypeError):
            return ""
        if value is None:
            return ""
        reader = getattr(value, "read", None)
        if callable(reader):          # cx_Oracle LOB
            try:
                value = reader()
            except Exception:
                return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    # ------------------------------------------------------------------
    def probe_driver(self) -> dict:
        """探测 Oracle 驱动（自检面板用）。"""
        return probe_oracle_driver()
