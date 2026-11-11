# -*- coding: utf-8 -*-
"""单表/逻辑表直接恢复（P0-1，对标 DSG 单表直接恢复 / 鼎甲挂接恢复）：

从「全实例逐库 tar」或「单库 mysqldump 产物」中按表锚点切片，抽出指定表的
建表 + 数据 SQL（不落目标实例、不搭辅助实例），交给既有单文件恢复通道灌入。

支持产物：
- 全实例 tar：tar.gz 内 dbs/<db>.sql（core/logical_full 统一逐库布局）
- 单库 dump：.sql / .sql.gz / .sql.zst / .zst

切片语义（mysqldump 输出格式）：
- 表段起点 = ``-- Table structure for table `name``` 注释行；
- 段内容 = 从该行到下一个表锚点（或 ``-- Dump completed`` 结尾）之间的全部行
  （含 DROP/CREATE/INSERT/LOCK），视图临时段（Temporary table structure）
  只作为边界不入选；
- 文件头（SET 等会话前缀）保留，但剔除 ``USE`` / ``CREATE DATABASE`` 行——
  表级恢复一律显式落目标库（由恢复通道以客户端参数选库），防止误灌回源库。
"""
import gzip
import os
import re
import tarfile
import tempfile

# 表结构锚点：-- Table structure for table `t1`
_RE_TBL_ANCHOR = re.compile(
    r"^--\s+Table structure for table\s+`?(\w+)`?")
# 视图临时段边界（不作为表）
_RE_VIEW_ANCHOR = re.compile(
    r"^--\s+Temporary table structure for view\s+`?(\w+)`?")
# 结尾锚点
_RE_TRAILER = re.compile(r"^--\s+Dump completed")
# 头部剔除行：USE / CREATE DATABASE（表级切片显式选库）
_RE_HEADER_SKIP = re.compile(
    r"^\s*(USE\s+|CREATE\s+DATABASE\b)", re.IGNORECASE)

TAR_SUFFIXES = (".tar.gz", ".tgz")
SQL_SUFFIXES = (".sql", ".sql.gz", ".sql.zst", ".zst", ".gz")


class TableExtractError(ValueError):
    """切片失败（产物不识别 / 指定表不存在等），消息可直接透出给用户。"""


def _open_text(path: str):
    """按扩展名打开文本流（gzip/zstd 透明解压），返回可迭代文本行。"""
    lower = path.lower()
    if lower.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    if lower.endswith(".zst"):
        import subprocess
        proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
        import io
        return io.TextIOWrapper(proc.stdout, encoding="utf-8",
                                errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _is_tar(path: str) -> bool:
    return path.lower().endswith(TAR_SUFFIXES)


def _tar_db_member(tf, db_name: str):
    """在 multi-db tar 中定位 dbs/<db>.sql 成员（兼容前导 ./）。"""
    want = f"dbs/{db_name}.sql"
    for m in tf.getmembers():
        name = m.name.lstrip("./")
        if name == want:
            return m
    return None


def _tar_member_text(tf, member):
    """tar 成员的文本行流（extractfile 是字节流，需包 TextIO）。"""
    import io
    fh = tf.extractfile(member)
    if fh is None:
        return None
    return io.TextIOWrapper(fh, encoding="utf-8", errors="replace")


def _tar_list_dbs(path: str) -> list:
    with tarfile.open(path, "r:*") as tf:
        return sorted(
            os.path.splitext(os.path.basename(m.name))[0]
            for m in tf.getmembers()
            if m.isfile() and m.name.lstrip("./").startswith("dbs/")
            and m.name.endswith(".sql"))


def _parse_stream(lines, want_tables=None, db_label=""):
    """流式解析 mysqldump 文本。

    返回 (header_lines, {table: [lines]}, order)。
    want_tables 非空时只收集指定表（小写匹配）。
    """
    header, sections, order = [], {}, []
    current = None
    seen_anchor = False   # 首个锚点后不再累积头部（防跳过表的 DDL 混入）
    want = {t.lower() for t in (want_tables or [])} or None
    for line in lines:
        m_tbl = _RE_TBL_ANCHOR.match(line)
        m_view = _RE_VIEW_ANCHOR.match(line)
        if m_tbl or m_view or _RE_TRAILER.match(line):
            current = None  # 任何锚点都关闭当前段
            seen_anchor = True
        if m_tbl:
            name = m_tbl.group(1)
            if not want or name.lower() in want:
                current = name
                if name not in sections:
                    sections[name] = [line]
                    order.append(name)
                    continue
        if current:
            sections[current].append(line)
            continue
        if not seen_anchor and not _RE_HEADER_SKIP.match(line):
            header.append(line)
    return header, sections, order


def list_tables(backup_path: str) -> list:
    """列出产物内全部表：[{"schema": db, "obj_name": table}]。

    tar 产物按 dbs/<db>.sql 逐成员解析；单库 dump schema 为空串。
    """
    if not backup_path or not os.path.exists(backup_path):
        raise TableExtractError(f"备份产物不存在: {backup_path}")
    out = []
    if _is_tar(backup_path):
        for db_name in _tar_list_dbs(backup_path):
            with tarfile.open(backup_path, "r:*") as tf:
                member = _tar_db_member(tf, db_name)
                if member is None:
                    continue
                fh = _tar_member_text(tf, member)
                if fh is None:
                    continue
                _, sections, order = _parse_stream(fh)
                for t in order:
                    out.append({"schema": db_name, "obj_name": t})
        return out
    if not backup_path.lower().endswith(SQL_SUFFIXES):
        raise TableExtractError(
            "不支持的产物格式（表级恢复支持全实例 tar.gz 与单库 "
            ".sql/.sql.gz/.sql.zst）: " + os.path.basename(backup_path))
    _, sections, order = _parse_stream(_open_text(backup_path))
    for t in order:
        out.append({"schema": "", "obj_name": t})
    return out


def _normalize_renames(renames) -> dict:
    """规范化表改名映射 ``{源表: 新表名}`` → ``{小写源表名: 新表名}``。

    源表键支持 ``db.table``；新表名只允许常规 MySQL 标识符；
    多个源表映射到同一新表名时拒绝（会互相覆盖）。
    """
    if not renames:
        return {}
    if not isinstance(renames, dict):
        raise TableExtractError("table_renames 必须是 {源表名: 新表名} 对象")
    out = {}
    for src, dst in renames.items():
        s = str(src).strip().strip("`\"'")
        d = str(dst).strip().strip("`\"'")
        if not s or not d:
            raise TableExtractError("表改名映射的源表名 / 新表名不能为空")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", d):
            raise TableExtractError(
                f"新表名不合法（只允许字母/数字/下划线/$ 且不以数字开头）: {d}")
        out[s.split(".")[-1].lower()] = d
    lowered = [v.lower() for v in out.values()]
    if len(set(lowered)) != len(lowered):
        raise TableExtractError("表改名映射存在多个源表指向同一个新表名")
    return out


# 只替换 SQL 中的表标识符位置，不动 INSERT ... VALUES 的字符串数据。
_RE_RENAME_POS = re.compile(
    r"(\b(?:DROP\s+TABLE(?:\s+IF\s+EXISTS)?|CREATE\s+TABLE|"
    r"INSERT\s+INTO|REPLACE\s+INTO|LOCK\s+TABLES|ALTER\s+TABLE|"
    r"Table\s+structure\s+for\s+table|Dumping\s+data\s+for\s+table)\s+)"
    r"`([^`]+)`", re.IGNORECASE)


def _rename_lines(lines, old: str, new: str):
    """把 mysqldump 表段中 ``old`` 的表标识符改成 ``new``。"""
    if not new or new.lower() == old.lower():
        return lines

    def sub(m):
        if m.group(2).lower() != old.lower():
            return m.group(0)
        return f"{m.group(1)}`{new}`"

    return [_RE_RENAME_POS.sub(sub, line) for line in lines]


def slice_tables(backup_path: str, tables: list, renames=None) -> tuple:
    """从产物切片指定表，返回 (tmp_sql_path, found, notes)。

    tables 元素支持 ``db.table``（tar 必须带前缀或单库）与 ``table``。
    renames 为可选的 ``{源表名: 新表名}``，在切片输出时改写表标识符，
    使「只恢复指定表 + 换成另一个表名」一次完成；found 里保留源表名并
    以 ``renamed_to`` 记录新名，不破坏既有调用方的字段契约。
    指定表不存在时抛 TableExtractError（消息含可用表清单，诚实失败）。
    调用方负责删除返回的临时文件。
    """
    if not tables:
        raise TableExtractError("未指定要恢复的表（tables 为空）")
    if not backup_path or not os.path.exists(backup_path):
        raise TableExtractError(f"备份产物不存在: {backup_path}")
    rename_map = _normalize_renames(renames)

    # 解析 db.table 前缀 → {db_lower: set(tables)}；"" 表示不限库/单库
    want_map = {}
    for spec in tables:
        spec = str(spec).strip().strip("`\"'")
        if not spec:
            continue
        if "." in spec:
            d, t = spec.split(".", 1)
            want_map.setdefault(d.lower(), set()).add(t.lower())
        else:
            want_map.setdefault("", set()).add(spec.lower())

    notes = []
    if rename_map:
        notes.append("表改名: " + ", ".join(
            f"{k}→{v}" for k, v in rename_map.items()))
    tmp = tempfile.NamedTemporaryFile(
        prefix="table_slice_", suffix=".sql", delete=False)
    found = []          # [{"schema","obj_name","renamed_to"}]
    try:
        with open(tmp.name, "w", encoding="utf-8") as out:
            header_written = False

            def _emit(header, sections, order, schema):
                nonlocal header_written
                # 库过滤：带前缀的按库匹配；无前缀仅单库/唯一匹配生效
                picked = []
                for t in order:
                    if "" in want_map and t.lower() in want_map[""]:
                        picked.append(t)
                    elif schema and schema.lower() in want_map \
                            and t.lower() in want_map[schema.lower()]:
                        picked.append(t)
                if not picked:
                    return
                if not header_written:
                    out.writelines(header)
                    header_written = True
                for t in picked:
                    new_name = rename_map.get(t.lower()) or ""
                    out.writelines(
                        _rename_lines(sections[t], t, new_name)
                        if new_name else sections[t])
                    found.append({"schema": schema, "obj_name": t,
                                  "renamed_to": new_name})

            if _is_tar(backup_path):
                dbs = _tar_list_dbs(backup_path)
                asked_dbs = [d for d in want_map if d]
                if "" not in want_map:
                    missing = [d for d in asked_dbs if d not in
                               [x.lower() for x in dbs]]
                    if missing:
                        raise TableExtractError(
                            f"备份集中不存在库: {','.join(missing)}；"
                            f"包含的库: {','.join(dbs) or '(无)'}")
                with tarfile.open(backup_path, "r:*") as tf:
                    for db_name in dbs:
                        if "" not in want_map and \
                                db_name.lower() not in want_map:
                            continue
                        member = _tar_db_member(tf, db_name)
                        if member is None:
                            continue
                        fh = _tar_member_text(tf, member)
                        if fh is None:
                            continue
                        header, sections, order = _parse_stream(fh)
                        _emit(header, sections, order, db_name)
            else:
                header, sections, order = _parse_stream(
                    _open_text(backup_path))
                _emit(header, sections, order, "")

        missing = []
        for spec in tables:
            d, _, t = str(spec).partition(".")
            key_t = (t or d).lower()
            key_d = (d.lower() if t else "")
            if not any(f["obj_name"].lower() == key_t and
                       (not key_d or f["schema"].lower() == key_d)
                       for f in found):
                missing.append(str(spec))
        if not found:
            raise TableExtractError(
                f"在备份产物中未找到指定表: {','.join(map(str, tables))}。"
                f"请用「表清单」确认产物内实际包含的表。")
        if missing:
            notes.append("部分表未在产物中找到（未切片）: "
                         + ",".join(missing))
        return tmp.name, found, notes
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def slice_to_temp(backup_path: str, tables: list, renames=None) -> tuple:
    """slice_tables 的同义词（语义更直观：返回 (tmp_path, found, notes)）。"""
    return slice_tables(backup_path, tables, renames=renames)
