# -*- coding: utf-8 -*-
"""达梦 JDBC 逻辑导出/恢复通道（无需 dexp / dimp 客户端）。

## 为什么需要（2026-11-25 现场问题）

达梦实例跑在 **Windows** 上时，平台（Linux）本机没有 dexp/dimp，数据库服务器
也不一定启用了 OpenSSH —— 于是「备份工具就绪」永远探测失败，用户看到的是
「缺少客户端工具: dexp, dimp」，而同一个实例用 JDBC 明明连得上。

达梦的 JDBC 驱动是纯 Java 的，平台侧（JRE + 驱动 jar，离线包自带）即可完成
"读元数据 → SELECT 导出 → 回放建表与 INSERT" 的全过程，**数据库服务器零安装**，
与 SQL Server 的 JDBC 逻辑通道同构，也不受 Linux/Windows 差异影响。

## 产物格式

复用 :class:`core.engines.jdbc_logical.JDBCLogicalEngine` 的 JSONL 自描述格式
（meta / table / row 三种行），因此克隆、VDB 快照等下游能力可直接复用。

## 与 dexp 的诚实差异

- 只导出**表 + 数据**（含列类型与可空性），不导出视图/存储过程/触发器/权限/
  序列/同义词——这些需要 dexp 或人工搬运。预检与备份消息里会注明通道。
- 达梦的 ``all_tab_columns`` 里没有稳定的默认值表达式，故不重建 DEFAULT；
  自增列（IDENTITY）按数据回放，不重建自增属性。
- 大表按 ``jdbc_fetch_size``（默认 5000）流式读写，不进内存；无并行分片。
- 恢复要求目标模式（schema）已存在——达梦不能在导入时隐式建模式，平台不
  擅自 ``CREATE SCHEMA``，缺模式时如实报错并给出处置建议。
"""
from __future__ import annotations

import json
import os
import re
import time

from core.engines.base import BackupResult, BackupStatus
from core.engines.jdbc_logical import JDBCLogicalEngine, _decode, _encode


class DamengJDBCEngine(JDBCLogicalEngine):
    """达梦 JDBC 逻辑备份/恢复引擎（无客户端通道）。"""

    db_type = "dameng"
    display_name = "DM 达梦（JDBC 逻辑导出）"
    required_clients = []
    adapter_tier = "peripheral_api"

    # 系统模式：整库导出时排除（与 core.jdbc DRIVER_CONFIG 的 filter 同源）。
    # 注意：SYSDBA 既是管理员也常被当作业务模式，**不能排除**。
    _SYSTEM_OWNERS = ("SYS", "SYSAUDITOR", "SYSSSO", "SYSDBAOPER",
                      "CTISYS", "CJISYS")
    _CHAR_TYPES = ("VARCHAR", "VARCHAR2", "CHAR", "CHARACTER",
                   "NCHAR", "NVARCHAR2")
    _NUM_TYPES = ("NUMBER", "NUMERIC", "DECIMAL")
    _DDL_NAME_RE = re.compile(r'^\s*CREATE\s+TABLE\s+("(?:[^"]|"")*"|\S+)\s*\(',
                              re.I | re.S)

    # ------------------------------------------------------------------ 基础
    def _driver_type(self) -> str:
        return "dameng"

    def probe(self):
        """轻量探活：真实连一次库（预检/通道选择用）。"""
        try:
            conn = self._conn()
        except Exception as e:                       # noqa: BLE001
            return False, "达梦 JDBC 连接失败: %s" % str(e)[:200]
        try:
            cur = conn.cursor()
            try:
                # DM7 老 build 对无 FROM 的 SELECT 兼容不一 → DUAL 候选兜底
                for sql in ("SELECT 1", "SELECT 1 FROM DUAL"):
                    try:
                        cur.execute(sql)
                        cur.fetchone()
                        break
                    except Exception:                # noqa: BLE001
                        if sql.endswith("DUAL"):
                            raise
            finally:
                cur.close()
            return True, "达梦 JDBC 连接正常（%s:%s）" % (
                self.task.get("host"), self.task.get("port"))
        except Exception as e:                       # noqa: BLE001
            return False, "达梦 JDBC 探测失败: %s" % str(e)[:200]
        finally:
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass

    def _qi(self, ident: str) -> str:
        """按段引用标识符：``OWNER.TABLE`` → ``"OWNER"."TABLE"``。

        不改大小写：目录里存的名字是什么就引用什么（未加引号创建的对象在达梦
        中本已折叠为大写，加引号引用仍命中；带引号创建的小写对象也能命中）。
        """
        parts = []
        for part in str(ident).split("."):
            part = part.strip()
            if len(part) >= 2 and part.startswith('"') and part.endswith('"'):
                parts.append(part)
            else:
                parts.append('"%s"' % part.replace('"', '""'))
        return ".".join(parts)

    def _encode_value(self, v):
        """Clob/Blob 取值：JDBC 返回的对象直接 str() 会变成对象地址。"""
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        cls = type(v).__name__.lower()
        if "clob" in cls:
            try:
                return str(v.getSubString(1, int(v.length())))
            except Exception:                        # noqa: BLE001
                try:
                    return str(v.getSubString(1, 2147483647))
                except Exception:                    # noqa: BLE001
                    return str(v)
        if "blob" in cls:
            try:
                return _encode(bytes(v.getBytes(1, int(v.length()))))
            except Exception:                        # noqa: BLE001
                pass
        return _encode(v)

    # ------------------------------------------------------------ 元数据枚举
    def _owners(self) -> list:
        extra = self.extra or {}
        raw = extra.get("schemas") or extra.get("owner") or ""
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw]
        else:
            items = [x.strip() for x in str(raw).split(",")]
        return [x.upper() for x in items if x]

    def _tables(self, conn) -> list:
        """要导出的表清单。

        - ``tables`` 显式指定 → 原样使用（可写 ``SCHEMA.TABLE`` 或裸表名）；
        - ``schemas`` / ``owner`` → 只导出这些模式；
        - 都不填 → 导出全部**非系统**模式（等价 dexp FULL=Y 的用户对象范围）。
        """
        extra = self.extra or {}
        want = extra.get("tables")
        names = []
        if isinstance(want, (list, tuple)):
            names = [str(x).strip() for x in want if str(x).strip()]
        elif want:
            names = [x.strip() for x in str(want).split(",") if x.strip()]
        if names:
            return names

        owners = self._owners()
        cur = conn.cursor()
        try:
            if owners:
                ph = ",".join(["?"] * len(owners))
                cur.execute(
                    "SELECT owner, table_name FROM all_tables "
                    "WHERE owner IN (%s) ORDER BY owner, table_name" % ph, owners)
            else:
                cur.execute("SELECT owner, table_name FROM all_tables "
                            "ORDER BY owner, table_name")
            rows = cur.fetchall() or []
        finally:
            try:
                cur.close()
            except Exception:                        # noqa: BLE001
                pass

        system = set(self._SYSTEM_OWNERS)
        out, seen = [], set()
        for row in rows:
            owner = str(row[0] or "").strip()
            table = str(row[1] or "").strip()
            # ## 开头是达梦内部表（统计信息等），导出会报非法标识符
            if not table or table.startswith("##"):
                continue
            if not owners and owner.upper() in system:
                continue
            full = ("%s.%s" % (owner, table)) if owner else table
            if full not in seen:
                seen.add(full)
                out.append(full)
        return out

    def _col_sql(self, dtype, dlen, prec, scale) -> str:
        t = (dtype or "VARCHAR").upper().strip()
        if t in self._CHAR_TYPES:
            n = 0
            try:
                n = int(dlen or 0)
            except Exception:                        # noqa: BLE001
                n = 0
            if t == "VARCHAR2":
                t = "VARCHAR"                        # 达梦中两者等价
            return "%s(%d)" % (t, n or 255)
        if t in self._NUM_TYPES:
            try:
                p = int(prec or 0)
            except Exception:                        # noqa: BLE001
                p = 0
            if not p:
                return "DECIMAL"
            try:
                s = int(scale or 0)
            except Exception:                        # noqa: BLE001
                s = 0
            return "%s(%d,%d)" % (t, p, s) if s else "%s(%d)" % (t, p)
        return t or "VARCHAR(255)"

    def _table_meta(self, conn, tname: str):
        """从 ``all_tab_columns`` 重建列清单与 CREATE TABLE（达梦方言）。"""
        owner, _, table = str(tname).rpartition(".")
        table = table or str(tname)
        owner = owner or None
        cur = conn.cursor()
        try:
            if owner:
                cur.execute(
                    "SELECT column_name, data_type, data_length, data_precision, "
                    "data_scale, nullable FROM all_tab_columns "
                    "WHERE owner=? AND table_name=? ORDER BY column_id",
                    [owner, table])
            else:
                cur.execute(
                    "SELECT column_name, data_type, data_length, data_precision, "
                    "data_scale, nullable FROM all_tab_columns "
                    "WHERE table_name=? ORDER BY column_id", [table])
            rows = cur.fetchall() or []
        finally:
            try:
                cur.close()
            except Exception:                        # noqa: BLE001
                pass

        defs, cols, jtypes = [], [], []
        for row in rows:
            cname = str(row[0] or "").strip()
            if not cname:
                continue
            col_sql = self._col_sql(row[1], row[2], row[3], row[4])
            nullable = str(row[5] or "Y").upper().startswith("Y")
            defs.append('"%s" %s%s' % (cname.replace('"', '""'), col_sql,
                                       "" if nullable else " NOT NULL"))
            cols.append(cname)
            jtypes.append(str(row[1] or ""))
        ddl = ""
        if defs:
            ddl = "CREATE TABLE %s (%s)" % (self._qi(tname), ", ".join(defs))
        return ddl, cols, jtypes

    # ------------------------------------------------------------------ 恢复
    def _schema_exists(self, conn, name: str) -> bool:
        """模式是否存在。

        达梦的 ``ALL_USERS`` 只列"用户"，而 ``CREATE SCHEMA ... AUTHORIZATION``
        建出来的独立模式只出现在 ``DBA_OBJECTS``（OBJECT_TYPE='SCH'）/SYSOBJECTS
        ——只用 ALL_USERS 判断会把已存在的模式误报为不存在。
        """
        upper = str(name).upper()
        cur = conn.cursor()
        try:
            for sql in ("SELECT COUNT(*) FROM DBA_OBJECTS WHERE OBJECT_TYPE='SCH' "
                        "AND OBJECT_NAME=?",
                        "SELECT COUNT(*) FROM SYSOBJECTS WHERE TYPE$='SCH' "
                        "AND NAME=?",
                        "SELECT COUNT(*) FROM all_users WHERE username=?"):
                try:
                    cur.execute(sql, [upper])
                    row = cur.fetchone()
                    if row and int(row[0] or 0) > 0:
                        return True
                except Exception:                    # noqa: BLE001
                    continue
            return False
        finally:
            try:
                cur.close()
            except Exception:                        # noqa: BLE001
                pass

    def _target_name(self, src: str, target_db: str) -> str:
        table = str(src).rpartition(".")[2] or str(src)
        if target_db:
            return "%s.%s" % (target_db, table)
        return str(src)

    def _rewrite_ddl(self, ddl: str, tgt: str) -> str:
        if not ddl:
            return ""
        return self._DDL_NAME_RE.sub("CREATE TABLE %s (" % self._qi(tgt),
                                     ddl, count=1)

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        started = time.time()
        if not backup_path or not os.path.isfile(backup_path):
            return BackupResult(False, BackupStatus.FAILED,
                                message="备份产物不存在: %s" % backup_path)
        target_db = str(kwargs.get("target_db") or "").strip().strip('"')
        try:
            conn = self._conn()
        except Exception as e:                       # noqa: BLE001
            return BackupResult(False, BackupStatus.FAILED,
                                message="JDBC 连接失败: %s" % str(e)[:300])

        if target_db and not self._schema_exists(conn, target_db):
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass
            return BackupResult(
                False, BackupStatus.FAILED, backup_path=backup_path,
                message=("目标模式 %s 在达梦实例中不存在。达梦不能在导入时隐式建"
                         "模式：请先 CREATE SCHEMA \"%s\"，或改为恢复到已有模式"
                         % (target_db.upper(), target_db.upper())))

        created = inserted = 0
        failed = []
        pend = []
        try:
            try:
                if getattr(conn, "jconn", None) is not None:
                    conn.jconn.setAutoCommit(False)
            except Exception:                        # noqa: BLE001
                pass
            cur = conn.cursor()
            with open(backup_path, encoding="utf-8") as fp:
                for line in fp:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:                # noqa: BLE001
                        continue
                    kind = obj.get("type")
                    if kind == "row":
                        tbl = self._target_name(obj.get("table") or "", target_db)
                        pend.append((tbl, obj.get("values") or []))
                        if len(pend) >= 500:
                            ok, err = self._flush(cur, pend)
                            inserted += ok
                            if err:
                                failed.append(err)
                            pend = []
                        continue
                    if kind != "table":
                        continue
                    if pend:
                        ok, err = self._flush(cur, pend)
                        inserted += ok
                        if err:
                            failed.append(err)
                        pend = []
                    tgt = self._target_name(obj.get("name") or "", target_db)
                    ddl = self._rewrite_ddl(obj.get("ddl") or "", tgt)
                    try:
                        cur.execute("DROP TABLE %s" % self._qi(tgt))
                    except Exception:                # noqa: BLE001
                        pass                             # 表不存在，正常
                    if ddl:
                        try:
                            cur.execute(ddl)
                            created += 1
                        except Exception as e:       # noqa: BLE001
                            failed.append("%s: DDL %s" % (tgt, str(e)[:120]))
            if pend:
                ok, err = self._flush(cur, pend)
                inserted += ok
                if err:
                    failed.append(err)
            try:
                conn.commit()
            except Exception:                        # noqa: BLE001
                pass
            cur.close()
        except Exception as e:                       # noqa: BLE001
            try:
                conn.rollback()
            except Exception:                        # noqa: BLE001
                pass
            return BackupResult(False, BackupStatus.FAILED,
                                backup_path=backup_path,
                                message="JDBC 逻辑恢复失败: %s" % str(e)[:300])
        finally:
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass

        ok = not failed
        return BackupResult(
            bool(ok), BackupStatus.SUCCESS if ok else BackupStatus.FAILED,
            backup_path=backup_path, duration_sec=round(time.time() - started, 2),
            message=("达梦 JDBC 逻辑恢复成功：建表 %d 张，写入 %d 行%s"
                     % (created, inserted,
                        ("，目标模式 %s" % target_db) if target_db else ""))
            if ok else
            ("达梦 JDBC 逻辑恢复部分失败：建表 %d、写入 %d、失败 %d（首条 %s）"
             % (created, inserted, len(failed), failed[0])))

    def _flush(self, cur, pend):
        """批量写入（参数化 INSERT；达梦占位符是 ``?``）。"""
        ok, err = 0, ""
        by_tbl = {}
        for tbl, vals in pend:
            by_tbl.setdefault(tbl, []).append(vals)
        for tbl, rows in by_tbl.items():
            n = len(rows[0]) if rows else 0
            if not n:
                continue
            sql = "INSERT INTO %s VALUES (%s)" % (
                self._qi(tbl), ",".join(["?"] * n))
            try:
                cur.executemany(sql, [[_decode(v) for v in r] for r in rows])
                ok += len(rows)
            except Exception as e:                   # noqa: BLE001
                err = "%s: %s" % (tbl, str(e)[:150])
        return ok, err

    # ------------------------------------------------------------------ 校验
    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """JSONL 产物校验：逐行解析，统计表与行数（坏行即失败）。"""
        path = record.get("backup_path") or record.get("output_path") or ""
        if not path or not os.path.isfile(path):
            return BackupResult(False, BackupStatus.FAILED,
                                message="备份文件不存在: %s" % (path or "-"))
        size = os.path.getsize(path)
        if not size:
            return BackupResult(False, BackupStatus.FAILED,
                                backup_path=path, message="备份文件大小为 0")
        tables = rows = 0
        try:
            with open(path, encoding="utf-8") as fp:
                for line in fp:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if obj.get("type") == "table":
                        tables += 1
                    elif obj.get("type") == "row":
                        rows += 1
        except Exception as e:                       # noqa: BLE001
            return BackupResult(False, BackupStatus.FAILED, backup_path=path,
                                message="JDBC 逻辑产物损坏: %s" % str(e)[:200])
        if not tables:
            return BackupResult(False, BackupStatus.FAILED, backup_path=path,
                                message="产物中没有任何表定义，无法恢复")
        return BackupResult(
            True, BackupStatus.SUCCESS, backup_path=path, size_bytes=size,
            verified=True,
            message=("达梦 JDBC 逻辑产物校验通过：%d 张表 / %d 行（JSONL 结构完整）"
                     % (tables, rows)))
