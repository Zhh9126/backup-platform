# -*- coding: utf-8 -*-
"""行级 CDC 内核（对标 Debezium / Canal / Flink CDC / GoldenGate）。

与既有 :mod:`core.cdc` 的区别
------------------------------
既有 CDC 守护做的是**物理日志段搬运**（把 binlog / WAL 文件整段拉回平台落盘，
用于 PITR 回放），粒度是「文件段」；本模块做的是**行级变更捕获**：把事务日志
解析成结构化变更事件（INSERT / UPDATE / DELETE + 前后镜像 + 主键 + 位点），
粒度是「行」。行级事件是业界三类能力的共同底座：

1. **CDC 实时备份（CDP 持续数据保护）**：事件流实时落库，可把任意表回滚到
   任意时间点（生成反向撤销 SQL），也可正向重放到目标库；
2. **增量迁移**（DTS 不停机迁移）：全量迁移完成后用事件流追平增量；
3. **实时同步**（realtime）：事件流持续投递到目标库。

设计约束（共享知识，必须遵守）
------------------------------
- **零新增 PyPI 依赖**：只用平台已内置的 pymysql / psycopg2 / oracledb 与数据库
  自带客户端（mysqlbinlog / pg_recvlogical），离线环境可直接运行；
- **零客户端安装**：所有解析在平台侧完成，源库只需开放日志访问权限；
- **密码不进命令行**：MySQL 走 ``MYSQL_PWD`` + ``--no-defaults``（配置文件里的
  password 优先级高于环境变量，必须屏蔽），PG 走 ``PGPASSWORD``。

事件模型::

    {
      "op": "INSERT" | "UPDATE" | "DELETE" | "DDL",
      "schema": "...", "table": "...",
      "ts": "2026-09-15 10:00:00",        # 源端事务时间
      "position": "mysql-bin.000002:1234" | "LSN" | "SCN",
      "pk": {"id": 1},                     # 主键值（无主键时为空）
      "before": {...}, "after": {...},     # 前后镜像（dict: 列名 -> 值）
      "before_partial": false              # 前镜像不完整（如 PG 未开 FULL 身份）
    }
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import config
import core.db as db

_LOG = db.get_logger("cdc.rowlevel")

# 事件写入批大小（攒批提交，降低高频写入开销）
_BATCH_SIZE = 50
_FLUSH_INTERVAL_SEC = 1.0

# 值序列化：bytes 无法直接 JSON，统一编码为 "hex:<hex>"（SQL 生成时按方言还原）
_HEX_PREFIX = "hex:"


# ======================================================================
# 工具
# ======================================================================
def _b(v) -> str:
    """bytes → 可 JSON 序列化的 hex 串。"""
    return _HEX_PREFIX + v.hex()


def _jsonable(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return _b(bytes(v))
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    # Decimal / datetime / date / time / timedelta
    return str(v)


def _json_dump(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _json_load(s) -> Any:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def _unescape_mysql(s: str) -> str:
    """还原 mysqlbinlog 输出中的转义（\\0 \\n \\r \\\\ \\' \\" \\Z \\b \\t）。"""
    out, i = [], 0
    mapping = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b",
               "Z": "\x1a", "\\": "\\", "'": "'", '"': '"'}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append(mapping.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _unescape_pg(s: str) -> str:
    """还原 test_decoding 输出中的转义（'' → '）。"""
    return s.replace("''", "'")


def _q_mysql(v) -> str:
    """MySQL 目标 SQL 字面量。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s.startswith(_HEX_PREFIX):
        return "X'" + s[len(_HEX_PREFIX):] + "'"
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _q_pg(v) -> str:
    """PostgreSQL 目标 SQL 字面量。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s.startswith(_HEX_PREFIX):
        return "'\\x" + s[len(_HEX_PREFIX):] + "'"
    return "'" + s.replace("'", "''") + "'"


def _q_ident_mysql(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _q_ident_pg(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def quote_literal(v, dialect: str) -> str:
    return _q_mysql(v) if dialect in ("mysql", "mariadb") else _q_pg(v)


def quote_ident(name: str, dialect: str) -> str:
    return (_q_ident_mysql(name) if dialect in ("mysql", "mariadb")
            else _q_ident_pg(name))


# ======================================================================
# 表元数据（列序 + 主键）：binlog / 逻辑解码只给序号或裸名，需要补齐列信息
# ======================================================================
class TableMeta:
    """表元数据：列名顺序 + 主键列。带 TTL 缓存，避免每行查库。"""

    CACHE_TTL = 300

    def __init__(self):
        self._cache: Dict[str, Tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get(self, conn_factory, dbkey: str, schema: str, table: str) -> dict:
        key = f"{dbkey}|{schema}|{table}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.CACHE_TTL:
                return hit[1]
        meta = {"columns": [], "pk": []}
        try:
            meta = conn_factory(schema, table) or meta
        except Exception as exc:  # 元数据失败不阻断捕获，降级为 col_N
            _LOG.debug("[cdc] 表元数据获取失败 %s: %s", key, exc)
        with self._lock:
            self._cache[key] = (now, meta)
        return meta

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()


META = TableMeta()


def _mysql_meta_factory(cfg: dict):
    """返回 callable(schema, table) -> {'columns': [...], 'pk': [...]}。"""
    import pymysql

    def _f(schema: str, table: str) -> dict:
        conn = pymysql.connect(host=cfg["host"], port=int(cfg.get("port") or 3306),
                               user=cfg["user"], password=cfg.get("password") or "",
                               connect_timeout=5, charset="utf8mb4")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema=%s AND table_name=%s"
                    " ORDER BY ordinal_position", (schema, table))
                cols = [r[0] for r in cur.fetchall()]
                cur.execute(
                    "SELECT column_name FROM information_schema.statistics"
                    " WHERE table_schema=%s AND table_name=%s AND index_name='PRIMARY'"
                    " ORDER BY seq_in_index", (schema, table))
                pk = [r[0] for r in cur.fetchall()]
            return {"columns": cols, "pk": pk}
        finally:
            conn.close()

    return _f


def _pg_meta_factory(cfg: dict):
    import psycopg2

    def _f(schema: str, table: str) -> dict:
        conn = psycopg2.connect(host=cfg["host"], port=int(cfg.get("port") or 5432),
                                user=cfg["user"], password=cfg.get("password") or "",
                                dbname=cfg.get("db_name") or "postgres",
                                connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT a.attname FROM pg_attribute a"
                    " JOIN pg_class c ON c.oid=a.attrelid"
                    " JOIN pg_namespace n ON n.oid=c.relnamespace"
                    " WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0"
                    " AND NOT a.attisdropped ORDER BY a.attnum", (schema, table))
                cols = [r[0] for r in cur.fetchall()]
                cur.execute(
                    "SELECT a.attname FROM pg_index i"
                    " JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)"
                    " JOIN pg_class c ON c.oid=i.indrelid"
                    " JOIN pg_namespace n ON n.oid=c.relnamespace"
                    " WHERE n.nspname=%s AND c.relname=%s AND i.indisprimary",
                    (schema, table))
                pk = [r[0] for r in cur.fetchall()]
            return {"columns": cols, "pk": pk}
        finally:
            conn.close()

    return _f


# ======================================================================
# 解析器
# ======================================================================
class BaseRowParser:
    """行级解析器基类：消费日志文本行，产出统一变更事件 dict。"""

    db_type = "base"

    def __init__(self, meta_get=None, include_tables: Iterable[str] = None,
                 exclude_tables: Iterable[str] = None):
        self._meta_get = meta_get or (lambda s, t: {"columns": [], "pk": []})
        self.include = [str(x).lower() for x in (include_tables or []) if x]
        self.exclude = [str(x).lower() for x in (exclude_tables or []) if x]
        self.stats = {"parsed": 0, "filtered": 0}

    # ---------------- 过滤 ----------------
    def _allowed(self, table: str) -> bool:
        t = (table or "").lower()
        if self.include and not any(p in t for p in self.include):
            self.stats["filtered"] += 1
            return False
        if self.exclude and any(p in t for p in self.exclude):
            self.stats["filtered"] += 1
            return False
        return True

    # ---------------- 事件构造 ----------------
    def _event(self, op: str, schema: str, table: str, ts: str, position: str,
               before: dict, after: dict, pk: dict = None,
               before_partial: bool = False, sql_text: str = "") -> dict:
        self.stats["parsed"] += 1
        return {
            "op": op, "schema": schema, "table": table, "ts": ts,
            "position": position, "before": before or {}, "after": after or {},
            "pk": pk or {}, "before_partial": before_partial,
            "sql_text": sql_text or "",
        }

    def feed(self, line: str) -> List[dict]:
        """喂入一行日志，返回本行收口的事件（0~N 个）。

        流式捕获是**逐行**推进的，因此解析状态必须挂在实例上，
        不能放在生成器局部变量里（否则每行都会重置状态机）。
        """
        raise NotImplementedError

    def flush(self) -> Optional[dict]:
        """流结束 / 超时时收口残留事件。"""
        return None

    def parse(self, lines: Iterable[str]) -> Iterator[dict]:
        """一次性解析可迭代的文本行（离线解析 / 单元测试用）。"""
        for line in lines:
            for ev in self.feed(line):
                yield ev
        tail = self.flush()
        if tail:
            yield tail


# ----------------------------------------------------------------------
# MySQL / MariaDB：mysqlbinlog --base64-output=DECODE-ROWS -v
# ----------------------------------------------------------------------
_RE_AT = re.compile(r"^###\s+@(\d+)=(.*)$")
_RE_TS = re.compile(r"^#(\d{6})\s+(\d{1,2}):(\d{2}):(\d{2})")
_RE_END_POS = re.compile(r"end_log_pos\s+(\d+)")
_RE_AT_POS = re.compile(r"^# at (\d+)")
_RE_INSERT = re.compile(r"^###\s+INSERT\s+INTO\s+`?([^`\s.]+)`?\.`?([^`\s]+)`?")
_RE_UPDATE = re.compile(r"^###\s+UPDATE\s+`?([^`\s.]+)`?\.`?([^`\s]+)`?")
_RE_DELETE = re.compile(r"^###\s+DELETE\s+FROM\s+`?([^`\s.]+)`?\.`?([^`\s]+)`?")


class MySQLRowParser(BaseRowParser):
    """解析 ``mysqlbinlog --base64-output=DECODE-ROWS -v`` 的伪 SQL 输出。

    输出样例（MySQL 8.0）::

        # at 1234
        #240915 10:00:00 server id 1  end_log_pos 1300 CRC32 0x... Table_map: `db`.`t` ...
        # at 1300
        #240915 10:00:00 server id 1  end_log_pos 1350 ... Write_rows: table id 90 flags: STMT_END_F
        ### INSERT INTO `db`.`t`
        ###   @1=1 /* INT meta=0 nullable=0 is_null=0 */
        ###   @2='abc' /* VARSTRING(60) meta=60 nullable=1 is_null=0 */
        # at 1350
        #240915 10:00:00 server id 1  end_log_pos 1381 ... Xid = 42

    列名由 ``@N`` 序号映射 information_schema 的列序得到（binlog 只给序号）。
    """

    db_type = "mysql"

    def __init__(self, binlog_file: str = "", meta_get=None, **kw):
        super().__init__(meta_get=meta_get, **kw)
        self.binlog_file = binlog_file
        self._ts = ""
        self._pos = 0
        # 跨行解析状态（流式逐行推进，状态必须挂实例）
        self._op = None
        self._schema = ""
        self._table = ""
        self._before: Dict[int, Any] = {}
        self._after: Dict[int, Any] = {}
        self._mode = "after"
        self._has_before = False

    # ---------------- 值解析 ----------------
    @staticmethod
    def _value(raw: str):
        raw = re.sub(r"/\*.*?\*/\s*$", "", raw).strip()
        if raw == "NULL":
            return None
        if raw.startswith("X'"):
            return bytes.fromhex(raw[2:-1]) if len(raw) > 3 else b""
        if raw.startswith("B'"):
            return raw[2:-1]
        if raw.startswith("'"):
            inner = raw[1:-1] if raw.endswith("'") else raw[1:]
            return _unescape_mysql(inner)
        if re.match(r"^-?\d+$", raw):
            try:
                return int(raw)
            except ValueError:
                return raw
        if re.match(r"^-?\d*\.\d+$", raw):
            try:
                return float(raw)
            except ValueError:
                return raw
        return raw

    def _columns(self, schema: str, table: str) -> Tuple[list, list]:
        meta = self._meta_get(schema, table)
        return meta.get("columns") or [], meta.get("pk") or []

    def _flush_event(self) -> Optional[dict]:
        """把当前累积的行变更收口为一个事件。"""
        if not self._op:
            return None
        cols, pk = self._columns(self._schema, self._table)
        # binlog 只给列序号（@1/@2...），按 information_schema 列序还原列名
        bm = {cols[i - 1] if 0 < i <= len(cols) else f"col_{i}": v
              for i, v in self._before.items()}
        am = {cols[i - 1] if 0 < i <= len(cols) else f"col_{i}": v
              for i, v in self._after.items()}
        # 主键：优先取声明主键的列值，无主键时回退全部镜像
        pk_vals = {}
        src = bm if self._op == "DELETE" else am
        for c in pk:
            if c in src:
                pk_vals[c] = src[c]
        if not pk_vals:
            pk_vals = dict(src)
        ev = self._event(self._op, self._schema, self._table, self._ts,
                         f"{self.binlog_file}:{self._pos}", bm, am, pk_vals,
                         before_partial=(self._op == "UPDATE" and not self._has_before))
        self._op = None
        self._before, self._after = {}, {}
        self._mode, self._has_before = "after", False
        return ev

    def flush(self) -> Optional[dict]:
        ev = self._flush_event()
        return ev if (ev and self._allowed(ev["table"])) else None

    def feed(self, line: str) -> List[dict]:
        line = line.rstrip("\n")
        out: List[dict] = []

        m = _RE_TS.match(line)
        if m:
            yy, hh, mi, ss = m.groups()
            self._ts = f"20{yy[0:2]}-{yy[2:4]}-{yy[4:6]} {int(hh):02d}:{mi}:{ss}"
        m = _RE_AT_POS.match(line)
        if m:
            self._pos = int(m.group(1))
            return out
        m = _RE_END_POS.search(line)
        if m:
            self._pos = int(m.group(1))
            return out

        if line.startswith("### INSERT INTO"):
            ev = self._flush_event()
            if ev and self._allowed(ev["table"]):
                out.append(ev)
            m = _RE_INSERT.match(line)
            self._op = "INSERT"
            self._schema, self._table = (m.group(1), m.group(2)) if m else ("", "")
            self._mode = "after"
            return out
        if line.startswith("### UPDATE"):
            ev = self._flush_event()
            if ev and self._allowed(ev["table"]):
                out.append(ev)
            m = _RE_UPDATE.match(line)
            self._op = "UPDATE"
            self._schema, self._table = (m.group(1), m.group(2)) if m else ("", "")
            self._mode = "before"
            return out
        if line.startswith("### DELETE FROM"):
            ev = self._flush_event()
            if ev and self._allowed(ev["table"]):
                out.append(ev)
            m = _RE_DELETE.match(line)
            self._op = "DELETE"
            self._schema, self._table = (m.group(1), m.group(2)) if m else ("", "")
            self._mode = "before"
            return out
        if line.startswith("### WHERE"):
            self._mode = "before"
            self._has_before = True
            return out
        if line.startswith("### SET"):
            self._mode = "after"
            return out
        m = _RE_AT.match(line)
        if m and self._op:
            idx = int(m.group(1))
            val = self._value(m.group(2))
            if self._mode == "before":
                self._before[idx] = val
            else:
                self._after[idx] = val
            return out
        # 事务提交 / 下一条语句：收口
        if line.startswith("COMMIT") or line.startswith("Xid") or (
                line.startswith("#") and "Query" in line and "thread_id" in line):
            ev = self._flush_event()
            if ev and self._allowed(ev["table"]):
                out.append(ev)
            return out
        return out


# ----------------------------------------------------------------------
# PostgreSQL：pg_recvlogical + test_decoding（contrib 自带，零安装）
# ----------------------------------------------------------------------
_RE_PG_TABLE = re.compile(r"^table\s+([\w.\-\"]+):\s+(INSERT|UPDATE|DELETE):\s?(.*)$")
_RE_PG_COL = re.compile(r"([^\s\[\]:]+)\[([^\]]*)\](?:\([^)]*\))?:")
_RE_PG_COMMIT = re.compile(r"^COMMIT\s+(\S+)(?:\s+\(at\s+([^)]+)\))?")
_RE_PG_BEGIN = re.compile(r"^BEGIN\s+(\S+)")


def _pg_now() -> str:
    """test_decoding 的 COMMIT 行不一定带时间戳，缺失时用平台时间补齐。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


class PgRowParser(BaseRowParser):
    """解析 ``pg_recvlogical --plugin=test_decoding`` 的输出。

    输出样例::

        BEGIN 1/2A3B4C
        table public.t1: INSERT: id[integer]:1 name[character varying](100):'abc'
        table public.t1: UPDATE: id[integer]:1 name[character varying](100):'abc2'
        table public.t1: DELETE: id[integer]:1
        COMMIT 1/2A3B4C (at 2026-09-15 10:00:00.123456+08)

    注意：默认 ``REPLICA IDENTITY`` 下 UPDATE 的旧元组**只有键列**，完整前镜像
    需要对表执行 ``ALTER TABLE ... REPLICA IDENTITY FULL``（Debezium 同样要求）。
    """

    db_type = "postgresql"

    def __init__(self, meta_get=None, **kw):
        super().__init__(meta_get=meta_get, **kw)
        self._lsn = ""
        self._ts = ""
        self._pending: List[dict] = []

    @staticmethod
    def _split_values(payload: str) -> Dict[str, Any]:
        """把 ``col[type]:val col2[type]:val2`` 切成 {col: val}。"""
        out: Dict[str, Any] = {}
        matches = list(_RE_PG_COL.finditer(payload))
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(payload)
            raw = payload[start:end].strip()
            name = m.group(1).strip('"')
            out[name] = PgRowParser._value(raw)
        return out

    @staticmethod
    def _value(raw: str):
        if raw == "null" or raw == "":
            return None
        if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
            return _unescape_pg(raw[1:-1])
        if re.match(r"^-?\d+$", raw):
            try:
                return int(raw)
            except ValueError:
                return raw
        if re.match(r"^-?\d*\.\d+$", raw):
            try:
                return float(raw)
            except ValueError:
                return raw
        if raw in ("true", "false"):
            return raw == "true"
        return raw

    def feed(self, line: str) -> List[dict]:
        line = line.rstrip("\n")
        m = _RE_PG_BEGIN.match(line)
        if m:
            self._lsn = m.group(1)
            self._ts = ""
            return []
        m = _RE_PG_COMMIT.match(line)
        if m:
            self._lsn = m.group(1)
            ts = (m.group(2) or "").strip()
            if ts:
                # 2026-09-15 10:00:00.123456+08 → 秒精度，去掉时区尾巴
                ts = re.sub(r"\.\d+", "", ts)
                ts = re.sub(r"[+-]\d{2}(:?\d{2})?$", "", ts).strip()
                self._ts = ts
            out = []
            for ev in self._pending:
                ev["ts"] = ev.get("ts") or self._ts or _pg_now()
                ev["position"] = self._lsn
                out.append(ev)
            self._pending = []
            return out
        m = _RE_PG_TABLE.match(line)
        if m:
            full, op, payload = m.group(1), m.group(2), m.group(3)
            schema, table = (full.split(".", 1) if "." in full
                             else ("public", full))
            schema, table = schema.strip('"'), table.strip('"')
            if not self._allowed(table):
                return []
            meta = self._meta_get(schema, table)
            pk = meta.get("pk") or []
            # REPLICA IDENTITY FULL 时 UPDATE 输出两段：old-key（前镜像）/
            # new-tuple（后镜像）；默认身份下只有一行（新元组），前镜像缺失。
            if "new-tuple:" in payload:
                idx = payload.index("new-tuple:")
                before = self._split_values(
                    payload[:idx].replace("old-key:", "").strip())
                after = self._split_values(payload[idx + len("new-tuple:"):].strip())
            else:
                row = self._split_values(payload)
                if op == "INSERT":
                    before, after = {}, row
                elif op == "DELETE":
                    before, after = row, {}
                else:
                    before, after = {}, row
            pk_vals = {c: after.get(c) for c in pk if c in after}
            if not pk_vals:
                pk_vals = dict(after) if op != "DELETE" else dict(before)
            self._pending.append(self._event(
                op, schema, table, self._ts, self._lsn, before, after,
                pk_vals, before_partial=(op == "UPDATE" and not before)))
            return []
        return []

    def flush(self) -> Optional[dict]:
        """未提交事务残留（进程退出时）：直接吐出，保证不丢事件。"""
        if not self._pending:
            return None
        out = self._pending
        self._pending = []
        for ev in out:
            ev["ts"] = ev.get("ts") or _pg_now()
        return None

    def drain_pending(self) -> List[dict]:
        """取出尚未提交的事件（进程退出前调用）。"""
        out = self._pending
        self._pending = []
        for ev in out:
            ev["ts"] = ev.get("ts") or _pg_now()
        return out


# ----------------------------------------------------------------------
# Oracle / 达梦：LogMiner 输出的 SQL_REDO 结构化（轻量实现）
# ----------------------------------------------------------------------
class SqlRedoRowParser(BaseRowParser):
    """把 LogMiner 的 ``SQL_REDO`` 文本归一为变更事件。

    LogMiner 提供的是可执行 SQL 文本（非前后镜像），因此这里做**SQL 级**事件：
    ``op`` 由语句首词判定，``sql_text`` 为原始语句，前后镜像留空并在
    :meth:`EventStore.gen_sql` 中直接回放/撤销（撤销用反转语句生成规则）。
    """

    db_type = "oracle"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._ts = ""
        self._scn = ""

    def feed(self, line: str) -> List[dict]:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            sql = (obj.get("sql_redo") or obj.get("SQL_REDO") or "").strip()
            table = obj.get("table_name") or obj.get("TABLE_NAME") or ""
            schema = obj.get("seg_owner") or obj.get("SEG_OWNER") or ""
            self._scn = str(obj.get("scn") or obj.get("SCN") or self._scn or "")
            ts = obj.get("timestamp") or obj.get("TIMESTAMP") or ""
        else:
            sql, table, schema, ts = line, "", "", ""
        if not sql:
            return []
        if ts:
            self._ts = str(ts)[:19]
        verb = sql.split()[0].upper() if sql else ""
        opmap = {"INSERT": "INSERT", "UPDATE": "UPDATE", "DELETE": "DELETE",
                 "ALTER": "DDL", "CREATE": "DDL", "DROP": "DDL", "TRUNCATE": "DDL"}
        op = opmap.get(verb, "SQL")
        if op == "SQL":
            return []
        if table and not self._allowed(table):
            return []
        return [self._event(op, schema, table, self._ts or db.now_iso(),
                            self._scn, {}, {}, {}, sql_text=sql)]


# ======================================================================
# 事件存储
# ======================================================================
class EventStore:
    """行级事件落库 / 查询 / 回放 SQL 生成。"""

    def save(self, stream_id: int, events: List[dict]) -> int:
        if not events:
            return 0
        now = db.now_iso()
        for ev in events:
            db.execute(
                "INSERT INTO cdc_events (stream_id, op, schema_name, table_name,"
                " event_time, position, pk_json, before_json, after_json,"
                " before_partial, sql_text, applied, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (int(stream_id), ev.get("op") or "", ev.get("schema") or "",
                 ev.get("table") or "", ev.get("ts") or now,
                 str(ev.get("position") or ""),
                 _json_dump({k: _jsonable(v) for k, v in (ev.get("pk") or {}).items()}),
                 _json_dump({k: _jsonable(v) for k, v in (ev.get("before") or {}).items()}),
                 _json_dump({k: _jsonable(v) for k, v in (ev.get("after") or {}).items()}),
                 1 if ev.get("before_partial") else 0,
                 (ev.get("sql_text") or "")[:4000], now))
        try:
            db.execute("UPDATE cdc_streams SET events_total=events_total+?,"
                       " last_event_at=?, updated_at=? WHERE id=?",
                       (len(events), now, now, int(stream_id)))
        except Exception:
            pass
        return len(events)

    @staticmethod
    def list_events(stream_id: int, limit: int = 100, op: str = "",
                    table: str = "", since: str = "", until: str = "",
                    offset: int = 0) -> List[dict]:
        sql = ("SELECT * FROM cdc_events WHERE stream_id=?")
        args: List[Any] = [int(stream_id)]
        if op:
            sql += " AND op=?"
            args.append(op.upper())
        if table:
            sql += " AND table_name LIKE ?"
            args.append(f"%{table}%")
        if since:
            sql += " AND event_time >= ?"
            args.append(since)
        if until:
            sql += " AND event_time <= ?"
            args.append(until)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        rows = db.query(sql, tuple(args))
        out = []
        for r in rows:
            d = dict(r)
            d["pk"] = _json_load(d.pop("pk_json", ""))
            d["before"] = _json_load(d.pop("before_json", ""))
            d["after"] = _json_load(d.pop("after_json", ""))
            out.append(d)
        return out

    @staticmethod
    def count(stream_id: int, since: str = "", until: str = "") -> int:
        sql = "SELECT COUNT(*) AS c FROM cdc_events WHERE stream_id=?"
        args: List[Any] = [int(stream_id)]
        if since:
            sql += " AND event_time >= ?"
            args.append(since)
        if until:
            sql += " AND event_time <= ?"
            args.append(until)
        row = db.query_one(sql, tuple(args))
        return int(row["c"]) if row else 0

    @staticmethod
    def fetch_range(stream_id: int, since: str = "", until: str = "",
                    tables: Iterable[str] = None, limit: int = 100000) -> List[dict]:
        sql = "SELECT * FROM cdc_events WHERE stream_id=?"
        args: List[Any] = [int(stream_id)]
        if since:
            sql += " AND event_time >= ?"
            args.append(since)
        if until:
            sql += " AND event_time <= ?"
            args.append(until)
        if tables:
            placeholders = ",".join("?" * len(list(tables)))
            sql += f" AND table_name IN ({placeholders})"
            args += [str(t) for t in tables]
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(int(limit))
        rows = db.query(sql, tuple(args))
        out = []
        for r in rows:
            d = dict(r)
            d["pk"] = _json_load(d.pop("pk_json", ""))
            d["before"] = _json_load(d.pop("before_json", ""))
            d["after"] = _json_load(d.pop("after_json", ""))
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # 回放 / 撤销 SQL 生成（业界 CDP「任意时间点恢复」的核心）
    # ------------------------------------------------------------------
    @staticmethod
    def gen_sql(events: List[dict], dialect: str = "mysql",
                mode: str = "redo", schema_name: str = "") -> List[str]:
        """把事件流翻译成可执行的 SQL 列表。

        Args:
            events: 事件列表（按 id 升序）。
            dialect: ``mysql`` / ``postgresql``（决定标识符与字面量引用）。
            mode: ``redo`` 正向重放（把变更重新做一遍）；
                  ``undo`` 反向撤销（把变更回退，逆序执行，用于误操作回滚）。
            schema_name: 目标库名（为空则用事件自带 schema）。
        """
        seq = events if mode == "redo" else list(reversed(events))
        sqls: List[str] = []
        for ev in seq:
            op = (ev.get("op") or "").upper()
            table = ev.get("table_name") or ev.get("table") or ""
            if not table:
                continue
            schema = schema_name or (ev.get("schema_name") or ev.get("schema") or "")
            tbl = (f"{quote_ident(schema, dialect)}.{quote_ident(table, dialect)}"
                   if schema else quote_ident(table, dialect))
            if op == "DDL":
                if mode == "redo" and ev.get("sql_text"):
                    sqls.append(ev["sql_text"].rstrip(";"))
                continue
            before = ev.get("before") or {}
            after = ev.get("after") or {}
            pk = ev.get("pk") or {}
            if ev.get("sql_text") and not after and not before:
                # LogMiner 等 SQL 级事件：直接回放原语句
                if mode == "redo":
                    sqls.append(ev["sql_text"].rstrip(";"))
                continue
            if mode == "redo":
                if op == "INSERT":
                    if after:
                        sqls.append(EventStore._insert_sql(tbl, after, dialect))
                elif op == "UPDATE":
                    if after:
                        sqls.append(EventStore._update_sql(tbl, after, pk, dialect))
                elif op == "DELETE":
                    cond = pk or before
                    if cond:
                        sqls.append(EventStore._delete_sql(tbl, cond, dialect))
            else:  # undo：逆序执行反向操作
                if op == "INSERT":
                    cond = pk or after
                    if cond:
                        sqls.append(EventStore._delete_sql(tbl, cond, dialect))
                elif op == "DELETE":
                    if before:
                        sqls.append(EventStore._insert_sql(tbl, before, dialect))
                elif op == "UPDATE":
                    target = before or pk
                    if before:
                        sqls.append(EventStore._update_sql(
                            tbl, before, pk or after, dialect))
                    elif target:
                        sqls.append(EventStore._update_sql(tbl, target, pk, dialect))
        return [s for s in sqls if s]

    @staticmethod
    def _where(tbl: str, cond: dict, dialect: str) -> str:
        parts = [f"{quote_ident(k, dialect)} = {quote_literal(v, dialect)}"
                 for k, v in cond.items() if v is not None]
        if not parts:
            return ""
        return f" WHERE " + " AND ".join(parts)

    @staticmethod
    def _insert_sql(tbl: str, row: dict, dialect: str) -> str:
        cols = ", ".join(quote_ident(k, dialect) for k in row.keys())
        vals = ", ".join(quote_literal(v, dialect) for v in row.values())
        return f"INSERT INTO {tbl} ({cols}) VALUES ({vals})"

    @staticmethod
    def _update_sql(tbl: str, row: dict, cond: dict, dialect: str) -> str:
        sets = ", ".join(f"{quote_ident(k, dialect)} = {quote_literal(v, dialect)}"
                         for k, v in row.items())
        where = EventStore._where(tbl, cond or row, dialect)
        return f"UPDATE {tbl} SET {sets}{where}" if where else f"UPDATE {tbl} SET {sets}"

    @staticmethod
    def _delete_sql(tbl: str, cond: dict, dialect: str) -> str:
        where = EventStore._where(tbl, cond, dialect)
        return f"DELETE FROM {tbl}{where}" if where else ""


STORE = EventStore()


# ======================================================================
# 捕获流
# ======================================================================
class RowCDCStream:
    """一条行级 CDC 捕获流：子进程拉日志 → 解析 → 落库 → 位点续传。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg                       # cdc_streams 行（含明文密码）
        self.stream_id = int(cfg["id"])
        self.db_type = (cfg.get("db_type") or "").lower()
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self.last_error = ""
        self.events_captured = 0
        self.started_at = ""
        self._stderr_path = ""

    # ---------------- 命令构造 ----------------
    def _mysql_cmd(self) -> Optional[List[str]]:
        binlog = shutil.which("mysqlbinlog") or "/opt/mysql840b/bin/mysqlbinlog"
        if not os.path.exists(binlog):
            self.last_error = "未找到 mysqlbinlog，无法进行 MySQL 行级 CDC"
            return None
        pos = self.cfg.get("position_json") or {}
        start_file = (pos.get("file") if isinstance(pos, dict) else "") or ""
        start_pos = int((pos.get("pos") if isinstance(pos, dict) else 0) or 0)
        if not start_file:
            file_pos = self._mysql_current_pos()
            if not file_pos:
                return None
            start_file, start_pos = file_pos
        cmd = [binlog, "--no-defaults", "--read-from-remote-server",
               f"--host={self.cfg['host']}",
               f"--port={int(self.cfg.get('port') or 3306)}",
               f"--user={self.cfg.get('username') or 'root'}",
               "--stop-never",
               f"--connection-server-id={900000000 + (self.stream_id % 90000000)}",
               "--base64-output=DECODE-ROWS", "-v",
               f"--start-position={start_pos}", start_file]
        return cmd

    def _mysql_current_pos(self) -> Optional[Tuple[str, int]]:
        try:
            import pymysql
            conn = pymysql.connect(host=self.cfg["host"],
                                   port=int(self.cfg.get("port") or 3306),
                                   user=self.cfg.get("username") or "root",
                                   password=self.cfg.get("password") or "",
                                   connect_timeout=5, charset="utf8mb4")
            try:
                with conn.cursor() as cur:
                    cur.execute("SHOW MASTER STATUS")
                    row = cur.fetchone()
                    if not row:
                        self.last_error = "源库未开启 binlog（SHOW MASTER STATUS 为空）"
                        return None
                    return str(row[0]), int(row[1])
            finally:
                conn.close()
        except Exception as exc:
            self.last_error = f"获取 binlog 位点失败: {exc}"
            return None

    def _pg_slot(self) -> str:
        return f"aidbm_cdc_{self.stream_id}"

    def _pg_cmd(self) -> Optional[List[str]]:
        recv = shutil.which("pg_recvlogical")
        for cand in ("/pgdb/pgsql/bin/pg_recvlogical",
                     "/usr/pgsql-14/bin/pg_recvlogical",
                     "/usr/lib/postgresql/14/bin/pg_recvlogical"):
            if not recv and os.path.exists(cand):
                recv = cand
        if not recv:
            self.last_error = "未找到 pg_recvlogical，无法进行 PostgreSQL 行级 CDC"
            return None
        return [recv, "-h", str(self.cfg["host"]),
                "-p", str(int(self.cfg.get("port") or 5432)),
                "-U", str(self.cfg.get("username") or "postgres"),
                "-d", str(self.cfg.get("db_name") or "postgres"),
                "--slot", self._pg_slot(), "--plugin=test_decoding",
                "--start", "-f", "-"]

    def _pg_ensure_slot(self) -> bool:
        env = os.environ.copy()
        env["PGPASSWORD"] = self.cfg.get("password") or ""
        make = [c for c in (shutil.which("pg_recvlogical"),
                            "/pgdb/pgsql/bin/pg_recvlogical") if c]
        if not make:
            return False
        cmd = [make[0], "-h", str(self.cfg["host"]),
               "-p", str(int(self.cfg.get("port") or 5432)),
               "-U", str(self.cfg.get("username") or "postgres"),
               "-d", str(self.cfg.get("db_name") or "postgres"),
               "--slot", self._pg_slot(), "--plugin=test_decoding",
               "--create-slot"]
        try:
            p = subprocess.run(cmd, env=env, capture_output=True, timeout=30)
            if p.returncode != 0:
                err = (p.stderr or b"").decode("utf-8", "replace").strip()
                if "already exists" not in err:
                    self.last_error = f"创建复制槽失败: {err[:200]}"
                    return False
        except Exception as exc:
            self.last_error = f"创建复制槽异常: {exc}"
            return False
        return True

    @staticmethod
    def _wrap_linebuffered(cmd: List[str]) -> List[str]:
        """强制子进程行缓冲。

        mysqlbinlog / pg_recvlogical 的 stdout 走管道时是 **块缓冲**（4KB），
        不处理的话行级事件会积压在客户端缓冲区里，捕获延迟可达数分钟。
        GNU coreutils 的 ``stdbuf -oL`` 是最轻量的解法（离线环境无需新增依赖）。
        """
        if shutil.which("stdbuf"):
            return ["stdbuf", "-oL", "-eL"] + list(cmd)
        return list(cmd)

    # ---------------- 生命周期 ----------------
    def start(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        self._stop_evt.clear()
        if self.db_type in ("mysql", "mariadb"):
            cmd = self._mysql_cmd()
            env = os.environ.copy()
            env["MYSQL_PWD"] = self.cfg.get("password") or ""
        elif self.db_type == "postgresql":
            if not self._pg_ensure_slot():
                return False
            cmd = self._pg_cmd()
            env = os.environ.copy()
            env["PGPASSWORD"] = self.cfg.get("password") or ""
        else:
            self.last_error = f"暂不支持行级 CDC 的数据库类型: {self.db_type}"
            return False
        if not cmd:
            return False
        cmd = self._wrap_linebuffered(cmd)

        self._stderr_path = os.path.join(
            _log_dir(), f"cdc_stream_{self.stream_id}.log")
        try:
            fh = open(self._stderr_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            fh = subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=fh,
                stdin=subprocess.DEVNULL, env=env,
                text=True, bufsize=1, errors="replace")
        except Exception as exc:
            self.last_error = f"启动 CDC 进程失败: {exc}"
            return False
        self.started_at = db.now_iso()
        self.thread = threading.Thread(target=self._reader, daemon=True,
                                       name=f"cdc-{self.stream_id}")
        self.thread.start()
        db.execute("UPDATE cdc_streams SET status='running', last_error=NULL,"
                   " pid=?, updated_at=? WHERE id=?",
                   (self.proc.pid, db.now_iso(), self.stream_id))
        db.add_log("INFO", "cdc", f"CDC 流 {self.stream_id} 已启动"
                   f"（{self.db_type} @ {self.cfg['host']}）")
        return True

    def stop(self) -> None:
        self._stop_evt.set()
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        db.execute("UPDATE cdc_streams SET status='stopped', pid=NULL,"
                   " updated_at=? WHERE id=?", (db.now_iso(), self.stream_id))
        db.add_log("INFO", "cdc", f"CDC 流 {self.stream_id} 已停止")

    def is_alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # ---------------- 读取与解析 ----------------
    def _meta_get(self, schema: str, table: str) -> dict:
        cfg = {"host": self.cfg["host"], "port": self.cfg.get("port"),
               "user": self.cfg.get("username"), "password": self.cfg.get("password"),
               "db_name": self.cfg.get("db_name")}
        try:
            if self.db_type in ("mysql", "mariadb"):
                factory = _mysql_meta_factory(cfg)
            else:
                factory = _pg_meta_factory(cfg)
            return META.get(factory, f"{self.db_type}:{self.cfg['host']}"
                            f":{self.cfg.get('port')}", schema, table)
        except Exception:
            return {"columns": [], "pk": []}

    def _parser(self, binlog_file: str = ""):
        inc = _split_list(self.cfg.get("include_tables"))
        exc = _split_list(self.cfg.get("exclude_tables"))
        if self.db_type in ("mysql", "mariadb"):
            return MySQLRowParser(binlog_file=binlog_file,
                                  meta_get=self._meta_get,
                                  include_tables=inc, exclude_tables=exc)
        if self.db_type == "postgresql":
            return PgRowParser(meta_get=self._meta_get,
                               include_tables=inc, exclude_tables=exc)
        return SqlRedoRowParser(include_tables=inc, exclude_tables=exc)

    def _reader(self) -> None:
        parser = self._parser()
        buf: List[dict] = []
        last_flush = time.time()
        try:
            for line in iter(self.proc.stdout.readline, ""):
                if self._stop_evt.is_set():
                    break
                if not line:
                    break
                for ev in parser.feed(line):
                    buf.append(ev)
                if len(buf) >= _BATCH_SIZE or (time.time() - last_flush) > _FLUSH_INTERVAL_SEC:
                    if buf:
                        STORE.save(self.stream_id, buf)
                        self.events_captured += len(buf)
                        self._save_position(parser)
                        buf = []
                        last_flush = time.time()
        except Exception as exc:
            self.last_error = f"CDC 读取异常: {exc}"
            _LOG.exception("[cdc] 流 %s 读取异常", self.stream_id)
        finally:
            # 收口：MySQL 收尾一条残留行事件；PG 收尾未提交事务
            try:
                tail = parser.flush()
                if tail:
                    buf.append(tail)
                if hasattr(parser, "drain_pending"):
                    buf.extend(parser.drain_pending())
            except Exception:
                pass
            if buf:
                try:
                    STORE.save(self.stream_id, buf)
                    self.events_captured += len(buf)
                except Exception:
                    pass
            err = ""
            if self.proc:
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
                err = self.last_error or "日志流进程已退出"
            if err:
                db.execute("UPDATE cdc_streams SET status='error', last_error=?,"
                           " updated_at=? WHERE id=?",
                           (err[:500], db.now_iso(), self.stream_id))
                db.add_log("ERROR", "cdc", f"CDC 流 {self.stream_id} 异常退出: {err[:200]}")

    def _save_position(self, parser) -> None:
        pos = {}
        if isinstance(parser, MySQLRowParser):
            pos = {"file": parser.binlog_file, "pos": parser._pos}
        elif isinstance(parser, PgRowParser):
            pos = {"lsn": parser._lsn}
        if pos:
            db.execute("UPDATE cdc_streams SET position_json=?, updated_at=? WHERE id=?",
                       (json.dumps(pos), db.now_iso(), self.stream_id))


def _split_list(v) -> List[str]:
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [x.strip() for x in str(v).replace(";", ",").split(",") if x.strip()]


def _log_dir() -> str:
    d = os.path.join(str(getattr(config, "BACKUP_ROOT", "backups")), "_cdc")
    os.makedirs(d, exist_ok=True)
    return d


def load_stream(stream_id: int, with_secret: bool = True) -> Optional[dict]:
    """读取 CDC 流配置（密码解密，供捕获进程使用）。"""
    row = db.query_one("SELECT * FROM cdc_streams WHERE id=?", (int(stream_id),))
    if not row:
        return None
    cfg = dict(row)
    if with_secret:
        try:
            cfg["password"] = db.decrypt_secret(cfg.get("password") or "")
        except Exception:
            cfg["password"] = ""
    return cfg


# ======================================================================
# 管理器
# ======================================================================
class CDCManager:
    """进程内 CDC 流管理器（启动 / 停止 / 状态）。"""

    def __init__(self):
        self._streams: Dict[int, RowCDCStream] = {}
        self._lock = threading.Lock()

    def get(self, stream_id: int) -> Optional[RowCDCStream]:
        with self._lock:
            return self._streams.get(int(stream_id))

    def start(self, stream_id: int) -> Tuple[bool, str]:
        cfg = load_stream(int(stream_id))
        if not cfg:
            return False, "CDC 流不存在"
        with self._lock:
            st = self._streams.get(int(stream_id))
            if st and st.is_alive():
                return True, "已在运行"
            st = RowCDCStream(cfg)
            self._streams[int(stream_id)] = st
        ok = st.start()
        if not ok:
            with self._lock:
                self._streams.pop(int(stream_id), None)
            return False, st.last_error or "启动失败"
        return True, "已启动"

    def stop(self, stream_id: int) -> Tuple[bool, str]:
        with self._lock:
            st = self._streams.pop(int(stream_id), None)
        if not st:
            db.execute("UPDATE cdc_streams SET status='stopped', pid=NULL,"
                       " updated_at=? WHERE id=?", (db.now_iso(), int(stream_id)))
            return True, "已停止"
        st.stop()
        return True, "已停止"

    def status(self, stream_id: int) -> dict:
        st = self.get(stream_id)
        alive = bool(st and st.is_alive())
        row = db.query_one("SELECT * FROM cdc_streams WHERE id=?", (int(stream_id),))
        info = dict(row) if row else {}
        info.pop("password", None)
        info["alive"] = alive
        info["events_captured"] = st.events_captured if st else 0
        info["error"] = st.last_error if st else ""
        if row:
            try:
                info["position"] = _json_load(row.get("position_json"))
            except Exception:
                info["position"] = {}
        return info

    def running_ids(self) -> List[int]:
        with self._lock:
            return [sid for sid, st in self._streams.items() if st.is_alive()]


manager = CDCManager()


# ======================================================================
# 能力探测（创建流前的前置检查，对标 DTS 预检查）
# ======================================================================
def probe_cdc_capability(db_type: str, host: str, port: int, username: str,
                         password: str, db_name: str = "") -> dict:
    """检查源库是否具备行级 CDC 条件。

    Returns:
        ``{'ok': bool, 'checks': [{'item','ok','message'}], 'message': str}``
    """
    res: Dict[str, Any] = {"ok": False, "checks": [], "db_type": db_type}
    db_type = (db_type or "").lower()
    if db_type in ("mysql", "mariadb"):
        try:
            import pymysql
            conn = pymysql.connect(host=host, port=int(port or 3306), user=username,
                                   password=password or "", connect_timeout=5,
                                   charset="utf8mb4")
        except Exception as exc:
            res["checks"].append({"item": "源库连通性", "ok": False, "message": str(exc)})
            res["message"] = f"源库连接失败: {exc}"
            return res
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW MASTER STATUS")
                row = cur.fetchone()
                res["checks"].append({
                    "item": "binlog 已开启", "ok": bool(row),
                    "message": (f"当前位点 {row[0]}:{row[1]}" if row
                                else "未开启 binlog（log_bin=OFF），无法 CDC")})
                cur.execute("SELECT @@binlog_format, @@binlog_row_image")
                fmt = cur.fetchone() or ("?", "?")
                res["checks"].append({
                    "item": "binlog 格式为 ROW", "ok": str(fmt[0]).upper() == "ROW",
                    "message": f"binlog_format={fmt[0]}, binlog_row_image={fmt[1]}"})
                cur.execute("SHOW GRANTS FOR CURRENT_USER()")
                grants = " ".join(str(g[0]) for g in cur.fetchall())
                ok_priv = ("REPLICATION SLAVE" in grants.upper()
                           or "ALL PRIVILEGES" in grants.upper())
                res["checks"].append({
                    "item": "账号具备 REPLICATION SLAVE 权限", "ok": ok_priv,
                    "message": "权限满足" if ok_priv else "缺少 REPLICATION SLAVE 权限"})
        except Exception as exc:
            res["checks"].append({"item": "能力探测", "ok": False, "message": str(exc)})
        finally:
            conn.close()
        res["checks"].append({
            "item": "平台侧 mysqlbinlog 客户端",
            "ok": bool(shutil.which("mysqlbinlog")
                       or os.path.exists("/opt/mysql840b/bin/mysqlbinlog")),
            "message": "可用" if (shutil.which("mysqlbinlog")
                                  or os.path.exists("/opt/mysql840b/bin/mysqlbinlog"))
                       else "未找到 mysqlbinlog"})
    elif db_type == "postgresql":
        try:
            import psycopg2
            conn = psycopg2.connect(host=host, port=int(port or 5432), user=username,
                                    password=password or "",
                                    dbname=db_name or "postgres", connect_timeout=5)
        except Exception as exc:
            res["checks"].append({"item": "源库连通性", "ok": False, "message": str(exc)})
            res["message"] = f"源库连接失败: {exc}"
            return res
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW wal_level")
                lvl = (cur.fetchone() or ["?"])[0]
                res["checks"].append({
                    "item": "wal_level=logical（逻辑解码前提）",
                    "ok": str(lvl) == "logical",
                    "message": (f"wal_level={lvl}；若为 replica 需改为 logical 并重启"
                                "（逻辑解码必需，物理 WAL 无法解析出行级变更）"
                                if str(lvl) != "logical" else "wal_level=logical")})
                cur.execute("SELECT rolreplication, rolsuper FROM pg_roles"
                            " WHERE rolname=current_user")
                r = cur.fetchone() or (False, False)
                ok_priv = bool(r[0] or r[1])
                res["checks"].append({
                    "item": "账号具备 REPLICATION 属性", "ok": ok_priv,
                    "message": "权限满足" if ok_priv
                               else "需要 ALTER ROLE ... REPLICATION"})
                cur.execute("SELECT COUNT(*) FROM pg_extension"
                            " WHERE extname='test_decoding'")
                res["checks"].append({
                    "item": "test_decoding 插件", "ok": True,
                    "message": "使用内置 test_decoding（contrib 自带，无需安装）"})
        except Exception as exc:
            res["checks"].append({"item": "能力探测", "ok": False, "message": str(exc)})
        finally:
            conn.close()
        res["checks"].append({
            "item": "平台侧 pg_recvlogical 客户端",
            "ok": bool(shutil.which("pg_recvlogical")
                       or os.path.exists("/pgdb/pgsql/bin/pg_recvlogical")),
            "message": "可用" if (shutil.which("pg_recvlogical")
                                  or os.path.exists("/pgdb/pgsql/bin/pg_recvlogical"))
                       else "未找到 pg_recvlogical"})
    else:
        res["checks"].append({
            "item": "数据库类型支持", "ok": False,
            "message": f"{db_type} 行级 CDC 需通过 LogMiner/日志解析通道，"
                       "请在「实时备份」中使用日志段捕获"})
        res["message"] = f"{db_type} 暂不支持行级 CDC"
        return res
    res["ok"] = all(c["ok"] for c in res["checks"])
    if not res["ok"]:
        failed = [c["item"] for c in res["checks"] if not c["ok"]]
        res["message"] = "CDC 前置条件未满足: " + "、".join(failed)
    else:
        res["message"] = "CDC 前置条件全部满足"
    return res


def apply_sql_to_target(tgt: dict, sqls: List[str]) -> dict:
    """把 SQL 列表真实执行到目标库（CDP 回放 / 回滚的真实落地点）。

    Args:
        tgt: ``{'db_type','host','port','username','password','db_name'}``。
    """
    db_type = (tgt.get("db_type") or "").lower()
    done, errors = 0, []
    started = time.time()
    try:
        if db_type in ("mysql", "mariadb"):
            import pymysql
            conn = pymysql.connect(host=tgt["host"], port=int(tgt.get("port") or 3306),
                                   user=tgt.get("username"), password=tgt.get("password") or "",
                                   database=tgt.get("db_name") or "", charset="utf8mb4",
                                   connect_timeout=8, autocommit=False)
        elif db_type == "postgresql":
            import psycopg2
            conn = psycopg2.connect(host=tgt["host"], port=int(tgt.get("port") or 5432),
                                    user=tgt.get("username"), password=tgt.get("password") or "",
                                    dbname=tgt.get("db_name") or "postgres", connect_timeout=8)
            conn.autocommit = False
        else:
            return {"ok": False, "applied": 0, "errors":
                    [f"暂不支持的目标库类型: {db_type}"]}
    except Exception as exc:
        return {"ok": False, "applied": 0, "errors": [f"目标库连接失败: {exc}"]}

    try:
        with conn.cursor() as cur:
            for sql in sqls:
                try:
                    cur.execute(sql)
                    done += 1
                except Exception as exc:
                    errors.append(f"{str(exc)[:160]} | {sql[:100]}")
                    if len(errors) > 50:
                        break
        if errors:
            conn.rollback()
            return {"ok": False, "applied": 0, "errors": errors,
                    "duration_sec": round(time.time() - started, 2),
                    "message": f"执行失败，已回滚（{len(errors)} 条错误）"}
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "applied": done, "errors": [str(exc)],
                "duration_sec": round(time.time() - started, 2)}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"ok": True, "applied": done, "errors": [],
            "duration_sec": round(time.time() - started, 2),
            "message": f"已应用 {done} 条变更"}
