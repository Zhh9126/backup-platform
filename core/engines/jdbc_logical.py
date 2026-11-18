# -*- coding: utf-8 -*-
"""通用 JDBC 逻辑备份引擎（关系型长尾兜底通道）。

## 为什么需要

平台内置引擎都是"按库型写工具链"（pg_dump/mysqldump/expdp/…）。但长尾库型
（Db2 / SAP HANA / Vertica / Teradata / Informix / YashanDB / TDengine …）在
客户现场常常**没有任何可用 CLI**，只有一张 JDBC 驱动 jar。此时唯一可行的
通用通道就是：JDBC 连接 → 元数据 + SELECT 导出 → 回放 INSERT。

## 产物格式（JSONL，自描述、跨方言可读）

    第一行  {"type":"meta","db_type":"db2","tables":N,"created_at":"..."}
    每张表  {"type":"table","name":"S.T1","ddl":"CREATE TABLE ...","columns":[...],
             "jdbc_types":[...]}
    数据行  {"type":"row","table":"S.T1","values":[1,"abc",null,...]}

二进制/LOB 统一 base64 并带 ``__b64__`` 前缀，避免 JSON 里出现控制字符。

## 诚实边界

- **只保证同方言恢复**（源/目标同库型）。跨方言（如 Db2 → PG）类型映射不承诺，
  需要时请走「数据迁移」通道的类型映射器。
- 大表按 ``fetchSize`` 流式读取并分批写盘，不把整表读进内存；但没有并行分片。
- 不导出存储过程/触发器/权限（JDBC 元数据不统一，强行导出会制造"假完整"）。
- 需要平台侧有 JRE + 对应驱动 jar（``core/jdbc.py`` 的 DRIVER_CONFIG 解析）。
"""
from __future__ import annotations

import base64
import json
import os
import time

import config
from core.engines.base import BackupEngine, BackupResult, BackupStatus, BackupType


def _b64(v) -> str:
    if isinstance(v, memoryview):
        v = v.tobytes()
    if isinstance(v, bytearray):
        v = bytes(v)
    return "__b64__" + base64.b64encode(v).decode("ascii")


def _encode(v):
    """把 JDBC 返回的 Java 值转成 JSON 可序列化值。"""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return _b64(v)
    cls = type(v).__name__
    if "byte" in cls.lower() or "Byte" in cls:
        try:
            return _b64(bytes(v))
        except Exception:
            pass
    if cls in ("BigDecimal", "BigInteger"):
        return str(v)
    if "Timestamp" in cls or "Date" in cls or "Time" in cls:
        return str(v)
    try:
        return str(v)
    except Exception:
        return None


def _decode(v):
    if isinstance(v, str) and v.startswith("__b64__"):
        return base64.b64decode(v[7:])
    return v


class JDBCLogicalEngine(BackupEngine):
    """通用 JDBC 逻辑备份/恢复引擎。

    实际驱动由 ``task.db_type`` 经 :mod:`core.jdbc` 的 DRIVER_CONFIG 解析，
    因此同一个引擎类可以服务 Db2 / ClickHouse / YashanDB / TDengine 等长尾类型。
    """

    db_type = "jdbc_generic"
    display_name = "通用 JDBC 逻辑备份"
    required_clients = []              # 只需要 JRE + jar，无需客户端二进制
    adapter_tier = "peripheral_api"

    # ------------------------------------------------------------------ 基础
    def _driver_type(self) -> str:
        """真实驱动类型：任务自身 db_type 优先（jdbc_generic 时退回 extra）。"""
        t = str(self.task.get("db_type") or "").strip().lower()
        if t and t != "jdbc_generic":
            return t
        return str(self.extra.get("jdbc_db_type") or self.task.get("db_name_type")
                   or "mysql").strip().lower()

    def _conn(self, database: str = None):
        import jaydebeapi
        from core import jdbc
        dt = self._driver_type()
        jdbc._ensure_jvm()
        jar, drv_cls, _, _, _, _ = jdbc._resolve_config(dt)
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port")
        dbname = database if database is not None else (self.task.get("db_name") or "")
        url = jdbc.build_url(dt, host, port, dbname)
        user = self.task.get("username") or ""
        pw = self.task.get("password") or ""
        return jaydebeapi.connect(drv_cls, url, [user, pw], jars=str(jar))

    def _tables(self, conn) -> list:
        """用户指定 tables 优先；否则列出当前库可见表（schema.table）。"""
        want = self.extra.get("jdbc_tables") or []
        if isinstance(want, str):
            want = [x.strip() for x in want.split(",") if x.strip()]
        if want:
            return [str(x) for x in want]
        cur = conn.cursor()
        try:
            md = getattr(conn, "jconn", None) or conn._connection
            meta = md.getMetaData()
            out = []
            rs = meta.getTables(None, None, "%", ["TABLE", "BASE TABLE"])
            while rs.next():
                schema = rs.getString("TABLE_SCHEM") or ""
                name = rs.getString("TABLE_NAME") or ""
                if not name:
                    continue
                out.append(("%s.%s" % (schema, name)) if schema else name)
            rs.close()
            return out
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ 备份
    def backup(self, backup_type: BackupType = BackupType.FULL) -> BackupResult:
        started = time.time()
        try:
            conn = self._conn()
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="JDBC 连接失败: %s" % str(e)[:300])
        out_path = self._new_dump_path(BackupType.FULL, ".jsonl")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        fetch = int(self.extra.get("jdbc_fetch_size") or 5000)
        tables = []
        rows_total = 0
        try:
            tables = self._tables(conn)
            if not tables:
                return BackupResult(False, BackupStatus.FAILED,
                                    message="源库没有可见表（或 jdbc_tables 未配置）")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "meta", "db_type": self._driver_type(),
                                    "task_id": self.task_id,
                                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                    "tables": len(tables)}, ensure_ascii=False) + "\n")
                cur = conn.cursor()
                try:
                    for tname in tables:
                        ddl, cols, jtypes = self._table_ddl(conn, tname), *self._table_meta(conn, tname)[1:]
                        f.write(json.dumps({"type": "table", "name": tname,
                                            "ddl": ddl, "columns": cols,
                                            "jdbc_types": jtypes},
                                           ensure_ascii=False) + "\n")
                        try:
                            cur.execute("SELECT * FROM %s" % tname)
                        except Exception as e:
                            return BackupResult(
                                False, BackupStatus.FAILED,
                                backup_path=out_path,
                                message="读取表 %s 失败: %s" % (tname, str(e)[:200]))
                        while True:
                            batch = cur.fetchmany(fetch)
                            if not batch:
                                break
                            for row in batch:
                                vals = [_encode(v) for v in row]
                                f.write(json.dumps({"type": "row", "table": tname,
                                                    "values": vals},
                                                   ensure_ascii=False) + "\n")
                                rows_total += 1
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED, backup_path=out_path,
                                message="JDBC 逻辑导出失败: %s" % str(e)[:300])
        finally:
            try:
                conn.close()
            except Exception:
                pass
        size = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
        return BackupResult(
            True, BackupStatus.SUCCESS, backup_path=out_path, size_bytes=size,
            original_size_bytes=size, duration_sec=round(time.time() - started, 2),
            message="JDBC 逻辑备份成功：%d 张表 / %d 行 / %.1f KB（%s 驱动）"
                    % (len(tables), rows_total, size / 1024.0, self._driver_type()))

    _MYSQL_FAMILY = ("mysql", "mariadb", "greatdb", "goldendb", "tidb", "oceanbase")

    def _qi(self, ident: str) -> str:
        """按方言引用标识符：MySQL 系用反引号，其余用双引号。

        2026-11-13 真机暴露：统一用双引号拼 DDL，在 MySQL 上恢复直接
        ``You have an error in your SQL syntax``（MySQL 默认不认双引号标识符）。
        """
        name = str(ident)
        if self._driver_type() in self._MYSQL_FAMILY:
            return "`%s`" % name.replace("`", "``")
        return '"%s"' % name.replace('"', '""')

    def _table_ddl(self, conn, tname: str) -> str:
        """优先取源库**原生 DDL**（方言最准），取不到再退回元数据拼接。"""
        dt = self._driver_type()
        if dt in self._MYSQL_FAMILY:
            try:
                cur = conn.cursor()
                cur.execute("SHOW CREATE TABLE %s" % tname)
                row = cur.fetchone()
                cur.close()
                if row and len(row) > 1 and row[1]:
                    return str(row[1])
            except Exception:
                pass
        return self._table_meta(conn, tname)[0]

    def _table_meta(self, conn, tname: str):
        """取列名、JDBC 类型与一个可用的 CREATE TABLE 语句（尽力而为）。"""
        cols, jtypes = [], []
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM %s WHERE 1=0" % tname)
            desc = cur.description or []
            cols = [d[0] for d in desc]
            jtypes = [str(getattr(d, "type_code", d[1]) if len(d) > 1 else "") for d in desc]
            cur.close()
        except Exception:
            pass
        ddl = ""
        try:
            md = getattr(conn, "jconn", None) or conn._connection
            meta = md.getMetaData()
            schema, _, table = tname.rpartition(".")
            rs = meta.getColumns(None, schema or None, table or tname, "%")
            defs = []
            while rs.next():
                cname = rs.getString("COLUMN_NAME")
                ctype = rs.getString("TYPE_NAME")
                size = rs.getInt("COLUMN_SIZE")
                if not cname:
                    continue
                if size and int(size) > 0 and ctype and any(
                        k in ctype.upper() for k in ("CHAR", "VARCHAR", "DECIMAL", "NUMERIC")):
                    defs.append('%s %s(%d)' % (self._qi(cname), ctype, int(size)))
                else:
                    defs.append('%s %s' % (self._qi(cname), ctype or "VARCHAR(255)"))
            rs.close()
            if defs:
                ddl = 'CREATE TABLE %s (%s)' % (self._qi(table or tname),
                                                ", ".join(defs))
        except Exception:
            ddl = ""
        return ddl, cols, jtypes

    # ------------------------------------------------------------------ 恢复
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        started = time.time()
        if not backup_path or not os.path.isfile(backup_path):
            return BackupResult(False, BackupStatus.FAILED,
                                message="备份产物不存在: %s" % backup_path)
        table_map = kwargs.get("table_renames") or {}
        target_db = kwargs.get("target_db") or ""
        try:
            conn = self._conn(database=target_db or None)
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="JDBC 连接失败: %s" % str(e)[:300])
        created, inserted, failed = 0, 0, []
        cur_tbl, pend = None, []
        try:
            conn.jconn.setAutoCommit(False) if hasattr(conn, "jconn") else None
            cur = conn.cursor()
            with open(backup_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    kind = obj.get("type")
                    if kind == "row":
                        tbl = obj.get("table")
                        tbl = table_map.get(tbl, tbl)
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
                    name = obj.get("name")
                    tgt = table_map.get(name, name)
                    ddl = obj.get("ddl") or ""
                    try:
                        cur.execute("DROP TABLE IF EXISTS %s" % self._qi(tgt))
                    except Exception:
                        pass
                    if ddl:
                        ddl_t = ddl
                        src_tbl = name.rpartition(".")[2] or name
                        for pat in ('"%s"' % src_tbl, "`%s`" % src_tbl, src_tbl):
                            if pat in ddl_t:
                                ddl_t = ddl_t.replace(pat, self._qi(tgt), 1)
                                break
                        try:
                            cur.execute(ddl_t)
                            created += 1
                        except Exception as e:
                            failed.append("%s: DDL %s" % (tgt, str(e)[:120]))
                    cur_tbl = (obj, tgt)
            if pend:
                ok, err = self._flush(cur, pend)
                inserted += ok
                if err:
                    failed.append(err)
            conn.commit()
            cur.close()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return BackupResult(False, BackupStatus.FAILED, backup_path=backup_path,
                                message="JDBC 恢复失败: %s" % str(e)[:300])
        finally:
            try:
                conn.close()
            except Exception:
                pass
        ok = not failed
        return BackupResult(
            bool(ok), BackupStatus.SUCCESS if ok else BackupStatus.FAILED,
            backup_path=backup_path, duration_sec=round(time.time() - started, 2),
            message=("JDBC 逻辑恢复成功：建表 %d 张，写入 %d 行" % (created, inserted))
                    if ok else
                    ("JDBC 逻辑恢复部分失败：建表 %d、写入 %d、失败 %d（首条 %s）"
                     % (created, inserted, len(failed), failed[0])))

    def _flush(self, cur, pend):
        """批量写入（按表分组，参数化 INSERT，避免拼接注入与转义问题）。"""
        ok, err = 0, ""
        by_tbl = {}
        for tbl, vals in pend:
            by_tbl.setdefault(tbl, []).append(vals)
        for tbl, rows in by_tbl.items():
            n = len(rows[0])
            ph = ",".join(["?"] * n)
            sql = "INSERT INTO %s VALUES (%s)" % (
                ("`%s`" % tbl.replace("`", "``")) if self._driver_type() in self._MYSQL_FAMILY
                else ('"%s"' % tbl.replace('"', '""')), ph)
            try:
                cur.executemany(sql, [[_decode(v) for v in r] for r in rows])
                ok += len(rows)
            except Exception as e:
                err = "%s: %s" % (tbl, str(e)[:150])
        return ok, err
