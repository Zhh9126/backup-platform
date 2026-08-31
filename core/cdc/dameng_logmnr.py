# -*- coding: utf-8 -*-
"""
达梦（DM8）DM_LOGMNR 日志捕获守护（T06）。

达梦的日志挖掘包 ``DBMS_LOGMNR`` 在接口形态上高度对齐 Oracle LogMiner，
差异集中在**位点语义**与**系统视图名**两处：

============  ==============================  ==============================
维度           Oracle                          达梦 DM8
============  ==============================  ==============================
位点           V$DATABASE.CURRENT_SCN (SCN)    V$RLOG.CUR_LSN (LSN)
归档开关       V$DATABASE.LOG_MODE             V$DATABASE.ARCH_MODE ('Y'/'N')
归档文件       V$ARCHIVED_LOG.NAME             V$ARCH_FILE.PATH
补充日志       SUPPLEMENTAL_LOG_DATA_MIN       V$DM_INI RLOG_APPEND_LOGIC
挖掘视图       V$LOGMNR_CONTENTS               V$LOGMNR_CONTENTS（同名）
驱动           oracledb / cx_Oracle            dmPython（非 PyPI）
============  ==============================  ==============================

因此本实现复用 :class:`core.cdc.polling_base.PollingLogMinerDaemon` 的全部
生命周期 / tick 骨架 / 段落盘逻辑，只实现 5 个抽象钩子。

位点统一（CH-T06-2）：达梦 LSN 是**单调递增的整数**，直接复用
``recovery_journal.wal_lsn`` / ``wal_end_lsn`` 两列承载，用
``position_kind='dm_lsn'`` 与 PostgreSQL 的 ``'X/Y'`` 形式 LSN 区分，
零 Schema 迁移。

前置条件：
- 数据库处于归档模式（``ARCH_MODE='Y'``）；
- 建议开启逻辑日志 ``RLOG_APPEND_LOGIC``（``SP_SET_PARA_VALUE(1,
  'RLOG_APPEND_LOGIC', 2)``），否则 ``SQL_REDO`` 可能不完整（不阻断，只告警）；
- 账号具备 ``V$RLOG`` / ``V$ARCH_FILE`` / ``V$LOGMNR_CONTENTS`` 查询权限
  与 ``DBMS_LOGMNR`` 执行权限。

``dmPython`` 不在 PyPI 分发（随达梦客户端 ``drivers/python`` 目录提供），
缺失时按共享知识 #17 降级为仿真日志流，**绝不抛到调用方**。
"""
from __future__ import annotations

import importlib
from typing import Any, List, Tuple

from .polling_base import PollingLogMinerDaemon

# 模块级驱动缓存（共享知识 #7）
_DM_DRIVER: Any = None
_DM_REASON: str = ""

#: 驱动候选：dmPython 官方驱动；dmpython 为部分发行版的小写包名
_DRIVER_CANDIDATES: Tuple[str, ...] = ("dmPython", "dmpython")

_MISSING_DRIVER_REASON = (
    "未安装 dmPython 驱动（非 PyPI 包，随达梦客户端提供），已降级为仿真日志流。"
    "安装：cd <DM_HOME>/drivers/python/dmPython && python setup.py install")


def _import_dmpython() -> Tuple[Any, str]:
    """惰性导入达梦驱动，结果模块级缓存。

    Returns:
        ``(module | None, 中文原因)``。**绝不抛异常**。
    """
    global _DM_DRIVER, _DM_REASON
    if _DM_DRIVER is not None:
        return _DM_DRIVER, ""
    if _DM_REASON:
        return None, _DM_REASON
    for name in _DRIVER_CANDIDATES:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        _DM_DRIVER = module
        return module, ""
    _DM_REASON = _MISSING_DRIVER_REASON
    return None, _DM_REASON


def reset_driver_cache() -> None:
    """清空驱动缓存（单元测试注入 mock 驱动时使用）。"""
    global _DM_DRIVER, _DM_REASON
    _DM_DRIVER = None
    _DM_REASON = ""


def probe_dameng_driver() -> dict:
    """探测可选依赖 ``dmPython``（自检面板用）。"""
    module, reason = _import_dmpython()
    return {
        "installed": module is not None,
        "reason": reason,
        "hint": ("" if module is not None else
                 "dmPython 非 PyPI 包，请在达梦客户端 drivers/python/dmPython "
                 "目录执行 python setup.py install"),
    }


class DamengLogMnrDaemon(PollingLogMinerDaemon):
    """达梦 DM8 的 DM_LOGMNR 拉取式日志捕获守护。"""

    engine_key = "dameng_logmnr"
    display_name = "达梦 DM_LOGMNR 日志捕获"
    required_clients: List[str] = []       # 纯 Python 驱动，无外部命令依赖
    is_simulated = False
    seal_all_immediately = True

    POSITION_KIND = "dm_lsn"
    POSITION_LABEL = "LSN"
    POSITION_ROW_KEY = "lsn"
    SEGMENT_EXT = ".jsonl"
    DEFAULT_PORT = 5236
    DEFAULT_USER = "SYSDBA"
    DEFAULT_DB = "DAMENG"
    DRIVER_NAMES = _DRIVER_CANDIDATES

    #: 排除的系统 Schema（拍板 Q5）。
    #: 注意：达梦的 SYSDBA 既是管理员又常被用作业务 Schema，**不能排除**，
    #: 否则绝大多数现场会捕获不到任何变更。
    EXCLUDED_SCHEMAS: Tuple[str, ...] = (
        "SYS", "SYSSSO", "SYSAUDITOR", "SYSDBAOPER", "CTISYS", "SYSJOB",
    )
    CAPTURED_OPERATIONS: Tuple[str, ...] = ("INSERT", "UPDATE", "DELETE", "DDL")

    # --- SQL 常量集中在类属性，便于现场按版本差异改配 ---
    SQL_ARCH_MODE = "SELECT ARCH_MODE FROM V$DATABASE"
    SQL_APPEND_LOGIC = (
        "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME='RLOG_APPEND_LOGIC'")
    SQL_CURRENT_LSN = "SELECT CUR_LSN FROM V$RLOG"
    #: 归档文件视图在不同 DM8 小版本上列名有出入，按序回退
    SQL_ARCH_FILES: Tuple[str, ...] = (
        "SELECT PATH FROM V$ARCH_FILE "
        "WHERE CLSN >= ? AND CLSN <= ? ORDER BY CLSN",
        "SELECT PATH FROM V$ARCH_FILE ORDER BY CLSN",
        "SELECT NAME FROM V$ARCHIVED_LOG ORDER BY FIRST_CHANGE#",
    )
    # 注意：DM8 的 PL/SQL 块内不解析 DBMS_LOGMNR.NEW/ADDFILE 包常量
    # （报 [-2007] Syntax error nearby [NEW]），必须使用字面量。
    # DM8 常量与 Oracle 不同（E2E 真机验证）：NEW=1, REMOVE=2, ADDFILE=3
    # （用 2 会报 [-2849] cannot remove unlisted logfile）。
    SQL_ADD_LOGFILE_NEW = (
        "BEGIN DBMS_LOGMNR.ADD_LOGFILE(?, 1); END;")
    SQL_ADD_LOGFILE_MORE = (
        "BEGIN DBMS_LOGMNR.ADD_LOGFILE(?, 3); END;")
    SQL_START_LOGMNR = (
        "BEGIN DBMS_LOGMNR.START_LOGMNR("
        "STARTSCN => ?, ENDSCN => ?, "
        "OPTIONS => 2130); END;")   # DICT_FROM_ONLINE_CATALOG+COMMITTED_DATA_ONLY
    SQL_END_LOGMNR = "BEGIN DBMS_LOGMNR.END_LOGMNR; END;"

    def __init__(self, task: dict, rt_config, repo, logger=None) -> None:
        super().__init__(task, rt_config, repo, logger=logger)
        self.db_name: str = str(self.task.get("db_name") or self.DEFAULT_DB)
        self._append_logic_warned: bool = False

    # ------------------------------------------------------------------
    # 驱动与连接
    # ------------------------------------------------------------------
    @classmethod
    def _import_driver(cls) -> Tuple[Any, str]:
        """dmPython 惰性导入。绝不抛异常。"""
        return _import_dmpython()

    def _connect(self):
        """建立达梦连接。

        ``dmPython.connect`` 的签名在不同版本间略有差异，这里按
        ``(user=, password=, server=, port=)`` → ``(user, password, dsn)``
        两种形态依次尝试。
        """
        module, reason = self._import_driver()
        if module is None:
            raise RuntimeError(reason)
        port = int(self.port or self.DEFAULT_PORT)
        try:
            return module.connect(user=self.username, password=self.password,
                                  server=str(self.host), port=port)
        except TypeError:
            # 旧版签名：connect(user, password, dsn)
            return module.connect(self.username, self.password,
                                  f"{self.host}:{port}")

    # ------------------------------------------------------------------
    # 源端预检
    # ------------------------------------------------------------------
    def _probe_source(self, conn) -> Tuple[bool, str]:
        """校验归档模式与 DM_LOGMNR 权限。

        Returns:
            ``(ok, 中文原因)``。逻辑日志未开启不阻断，只写 ``degrade_reason``。
        """
        try:
            row = self._query_one(conn, self.SQL_ARCH_MODE)
        except Exception as exc:
            return False, (f"查询达梦归档模式失败（{exc}），"
                           f"当前账号可能缺少 V$DATABASE 查询权限，已降级为仿真")
        arch_mode = str((row or [""])[0] or "").strip().upper()
        if arch_mode not in ("Y", "1", "TRUE", "ARCHIVELOG"):
            return False, (f"达梦未开启归档模式（ARCH_MODE={arch_mode or '未知'}），"
                           f"无法捕获日志，已降级为仿真")

        # 逻辑日志（RLOG_APPEND_LOGIC）：不阻断，只提示
        try:
            row = self._query_one(conn, self.SQL_APPEND_LOGIC)
            append_logic = str((row or [""])[0] or "0").strip()
        except Exception:
            append_logic = "0"
        if append_logic in ("0", "", "0.0000"):
            self.degrade_reason = (
                "达梦未开启逻辑日志（RLOG_APPEND_LOGIC=0），DM_LOGMNR 可能拿不到"
                "完整的 SQL_REDO。建议执行 SP_SET_PARA_VALUE(1,"
                "'RLOG_APPEND_LOGIC',2) 后重启实例")
            if not self._append_logic_warned:
                self._append_logic_warned = True
                self.logger.warning("[rt.cdc] task=%s %s", self.task_id,
                                    self.degrade_reason)

        # 挖掘视图权限：试查一次（空结果也算通过）
        try:
            self._query(conn, "SELECT 1 FROM V$LOGMNR_CONTENTS WHERE 1=0")
        except Exception as exc:
            return False, (f"当前账号缺少 V$LOGMNR_CONTENTS 查询权限或 DBMS_LOGMNR "
                           f"包未安装（{exc}），已降级为仿真")
        return True, ""

    # ------------------------------------------------------------------
    # 位点
    # ------------------------------------------------------------------
    def _current_position_value(self, conn) -> str:
        """查询 ``V$RLOG.CUR_LSN``。"""
        row = self._query_one(conn, self.SQL_CURRENT_LSN)
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
        """挂载归档 → START_LOGMNR → 查 ``V$LOGMNR_CONTENTS`` → END_LOGMNR。

        Args:
            conn: 达梦连接。
            from_pos: 起始 LSN（不含）。
            to_pos: 结束 LSN（含）。

        Returns:
            ``(变更行列表, 实际结束 LSN)``。挂不到任何日志时返回 ``([], from_pos)``，
            位点原地等待下一轮，避免丢变更。
        """
        from_lsn = self._pos_value(from_pos) or 0
        to_lsn = self._pos_value(to_pos) or 0
        if to_lsn <= from_lsn:
            return [], str(from_pos)

        mounted = self._add_logfiles(conn, from_lsn, to_lsn)
        if mounted <= 0:
            self.logger.debug("[rt.cdc] task=%s LSN 区间 %s→%s 无可挂载归档文件",
                              self.task_id, from_lsn, to_lsn)
            return [], str(from_pos)

        rows: List[dict] = []
        try:
            self._start_logmnr(conn, from_lsn, to_lsn)
            rows = self._read_contents(conn, from_lsn, to_lsn)
        finally:
            # 与 Oracle 同策略：无论成败都释放挖掘上下文
            self._end_logmnr(conn)
        return rows, str(to_lsn)

    def _arch_files(self, conn, from_lsn: int, to_lsn: int) -> List[str]:
        """按候选 SQL 依次查询归档文件路径；全部失败返回空列表。"""
        for index, sql in enumerate(self.SQL_ARCH_FILES):
            params = (from_lsn, to_lsn) if "?" in sql else None
            try:
                rows = self._query(conn, sql, params)
            except Exception as exc:
                self.logger.debug("[rt.cdc] task=%s 归档文件查询候选#%s 失败: %s",
                                  self.task_id, index, exc)
                continue
            paths = [str(row[0]).strip() for row in rows
                     if row and row[0] not in (None, "")]
            if paths:
                return paths
        return []

    def _add_logfiles(self, conn, from_lsn: int, to_lsn: int) -> int:
        """挂载区间内的归档日志文件。

        Returns:
            成功挂载的文件数。
        """
        paths: List[str] = []
        for path in self._arch_files(conn, from_lsn, to_lsn):
            if path and path not in paths:
                paths.append(path)
        if not paths:
            return 0

        mounted = 0
        for path in paths:
            sql = (self.SQL_ADD_LOGFILE_NEW if mounted == 0
                   else self.SQL_ADD_LOGFILE_MORE)
            try:
                self._execute(conn, sql, (path,))
                mounted += 1
            except Exception as exc:
                self.logger.debug("[rt.cdc] task=%s 挂载归档失败 %s: %s",
                                  self.task_id, path, exc)
        return mounted

    def _start_logmnr(self, conn, from_lsn: int, to_lsn: int) -> None:
        """启动 DM_LOGMNR 会话。"""
        self._execute(conn, self.SQL_START_LOGMNR, (from_lsn, to_lsn))

    def _end_logmnr(self, conn) -> None:
        """结束 DM_LOGMNR 会话。失败只记 debug，绝不外抛。"""
        try:
            self._execute(conn, self.SQL_END_LOGMNR)
        except Exception as exc:
            self.logger.debug("[rt.cdc] task=%s END_LOGMNR 异常（已忽略）: %s",
                              self.task_id, exc)

    def _contents_sql(self) -> str:
        """构造 ``V$LOGMNR_CONTENTS`` 查询（排除系统 Schema，拍板 Q5）。

        注意：DM8 没有Oracle 的 ``START_TIME`` 列，对应列为 ``START_TIMESTAMP``
        （E2E 真机验证：用 START_TIME 会报 [-2111] Invalid column name）。
        """
        ops = ", ".join(f"'{op}'" for op in self.CAPTURED_OPERATIONS)
        owners = ", ".join(f"'{name}'" for name in self.EXCLUDED_SCHEMAS)
        return (
            "SELECT SCN, START_TIMESTAMP, SEG_OWNER, TABLE_NAME, OPERATION, "
            "SQL_REDO, SQL_UNDO FROM V$LOGMNR_CONTENTS "
            f"WHERE OPERATION IN ({ops}) "
            f"AND (SEG_OWNER IS NULL OR SEG_OWNER NOT IN ({owners})) "
            "AND SCN > ? AND SCN <= ? "
            "ORDER BY SCN")

    def _read_contents(self, conn, from_lsn: int, to_lsn: int) -> List[dict]:
        """读取变更行并转成段文件的 JSON 结构。

        Note:
            R17：``redo`` / ``undo`` 含业务敏感数据，只能落进段文件，
            **绝不允许写入日志**。
        """
        raw = self._query(conn, self._contents_sql(), (from_lsn, to_lsn))
        rows: List[dict] = []
        for item in raw[:self.FETCH_LIMIT]:
            rows.append({
                "lsn": self._cell_str(item, 0),
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
        if callable(reader):
            try:
                value = reader()
            except Exception:
                return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    # ------------------------------------------------------------------
    def probe_driver(self) -> dict:
        """探测达梦驱动（自检面板用）。"""
        return probe_dameng_driver()
