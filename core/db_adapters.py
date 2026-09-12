# -*- coding: utf-8 -*-
"""可插拔数据库类型（Database Adapter）。

设计目标
--------
内置引擎（mysql / oracle / postgresql / …）由 ``core.engines`` 静态注册；
本模块承载**用户在界面上新增**的数据库类型——本质是一段 bash 脚本模板
加上若干能力声明。运行时把适配器渲染为 ``BackupEngine`` 子类塞进
``ENGINE_REGISTRY``，让备份/恢复/列库/测试与内置走同一套链路。

字段对齐
--------
表结构参见 ``core/db.py`` 的 ``CREATE TABLE db_adapters(...)``。
写入白名单 ``_FIELDS`` 与 ``db_adapters`` 表列名一一对应；不在其中的字段
会被静默丢弃（与 models.create_task 风格一致）。

脚本模板渲染
------------
脚本里出现的 ``{{KEY}}`` 在运行时统一替换为 ``${PLATFORM_<KEY>}``（shell
变量引用）。所有 ``PLATFORM_*`` 已由平台在执行前注入，脚本里**绝对不要
把敏感值拼成明文**。这样设计的好处：

- 脚本文件落盘不含明文密码；
- 与 base.py 的自定义脚本通道（``run_backup``/``run_restore``）完全兼容，
  ``CustomDBEngine`` 直接走该通道，无需重写 SSH/产物拉取等基础设施。

占位命名约定
- ``{{DB_HOST}} {{DB_PORT}} {{DB_USER}} {{DB_PASSWORD}} {{DB_NAME}}``  → 连接信息
- ``{{BACKUP_TYPE}}``    → full / incremental / snapshot
- ``{{BACKUP_DIR}}``     → 本次运行的远端产物目录（必须把文件写到此处）
- ``{{BACKUP_FILE}}``    → 恢复时平台推送过来的本地文件远端路径
- ``{{RESTORE_DB}}``     → 恢复表单里的目标库名
- ``{{TASK_ID}} {{TASK_NAME}}`` → 任务上下文
- ``{{TOOL_BIN}}``       → ``params.tool_bin`` 的工具绝对路径
- ``{{PARAM_<KEY>}}``    → ``params`` JSON 中的键，注入为 ``PLATFORM_PARAM_<KEY>``
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from typing import Any, Optional

import core.db as db
from core.engines.base import BackupEngine, BackupResult, BackupStatus, BackupType


# --------- 写入白名单（与表结构严格对齐） ---------
_FIELDS = (
    "db_type", "display_name", "category", "default_port", "icon", "description",
    "enabled",
    "backup_modes", "supports_incremental", "supports_full_instance", "supports_sync",
    "client_tools", "skip_client_check",
    "script_backup_full", "script_backup_incremental", "script_backup_full_instance",
    "script_restore", "script_verify", "script_list_dbs", "script_test_conn",
    "artifact_dir", "timeout_sec", "params",
    "builtin",
)
_BOOL_FIELDS = ("enabled", "supports_incremental", "supports_full_instance",
                "supports_sync", "skip_client_check", "builtin")

# 内置 db_type（与 core.engines 注册表一致），不可被自定义适配器占用
BUILTIN_TYPES = (
    "mysql", "mariadb", "postgresql", "oracle", "kingbase", "dameng",
    "sqlserver", "redis", "mongodb", "file",
)


# ============== 行 ↔ dict 转换 ==============
def _row(row: dict) -> dict:
    out = dict(row)
    for k in ("backup_modes", "client_tools", "params"):
        raw = out.get(k)
        try:
            out[k] = json.loads(raw) if isinstance(raw, str) and raw.strip() else (
                [] if k in ("backup_modes", "client_tools") else {})
        except Exception:
            out[k] = [] if k != "params" else {}
    for k in _BOOL_FIELDS:
        out[k] = 1 if out.get(k) else 0
    return out


def _pack(data: dict) -> dict:
    """把 dict 序列化为入库格式（JSON 字段 → str，布尔 → 0/1）。"""
    out = {}
    for k in _FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k in ("backup_modes", "client_tools"):
            if isinstance(v, str):
                v = json.loads(v) if v.strip() else []
            v = v or []
            out[k] = json.dumps(list(v), ensure_ascii=False)
        elif k == "params":
            if isinstance(v, str):
                v = json.loads(v) if v.strip() else {}
            out[k] = json.dumps(v or {}, ensure_ascii=False)
        elif k in _BOOL_FIELDS:
            out[k] = 1 if v not in (0, False, "0", None, "") else 0
        elif k == "default_port":
            try:
                out[k] = int(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                out[k] = None
        elif k == "timeout_sec":
            try:
                out[k] = max(60, int(v or 7200))
            except (TypeError, ValueError):
                out[k] = 7200
        else:
            out[k] = v if v is not None else ""
    return out


# ============== 校验 ==============
_DBTYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
RESERVED_PREFIXES = ("platform", "plugin", "system")  # 防止与未来保留字冲突


def validate(data: dict, existing: Optional[dict] = None) -> list[str]:
    """返回错误列表（空=通过）。existing 为现有记录（更新时使用）。"""
    errs: list[str] = []
    db_type = (data.get("db_type") or "").strip().lower()
    if not db_type:
        errs.append("db_type 不能为空")
    elif not _DBTYPE_RE.match(db_type):
        errs.append(f"db_type 不合法：{db_type!r}（必须小写字母/数字/下划线，2~32 位）")
    elif not existing and db_type in BUILTIN_TYPES:
        errs.append(f"db_type {db_type!r} 是内置类型，无法新增（请在「数据库类型」选已内置的）")
    elif any(db_type.startswith(p + "_") for p in RESERVED_PREFIXES):
        errs.append(f"db_type 不允许以 {RESERVED_PREFIXES} 开头")
    display_name = (data.get("display_name") or "").strip()
    if not display_name:
        errs.append("显示名称不能为空")
    elif len(display_name) > 64:
        errs.append("显示名称过长（≤64 字符）")
    # 脚本模板：备份脚本至少要有一个
    if not existing or any(k in data for k in
                           ("script_backup_full", "script_backup_incremental")):
        if not data.get("script_backup_full") and not data.get("script_backup_incremental"):
            if not existing:
                errs.append("至少需要 script_backup_full 或 script_backup_incremental 之一")
    modes = data.get("backup_modes") or ["logical"]
    if isinstance(modes, str):
        try:
            modes = json.loads(modes)
        except Exception:
            modes = []
    for m in modes or []:
        if m not in ("logical", "physical"):
            errs.append(f"backup_modes 取值非法：{m}")
    return errs


# ============== CRUD ==============
def list_adapters(include_disabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM db_adapters"
    params = ()
    if not include_disabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY builtin DESC, category ASC, id ASC"
    return [_row(r) for r in db.query(sql, params)]


def get_by_dbtype(db_type: str, include_disabled: bool = True) -> Optional[dict]:
    if not db_type:
        return None
    sql = "SELECT * FROM db_adapters WHERE db_type=?"
    if not include_disabled:
        sql += " AND enabled=1"
    row = db.query_one(sql, (db_type,))
    return _row(row) if row else None


def get_by_id(adapter_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM db_adapters WHERE id=?", (adapter_id,))
    return _row(row) if row else None


def create(data: dict, created_by: str = "") -> int:
    errs = validate(data)
    if errs:
        raise ValueError("; ".join(errs))
    packed = _pack(data)
    packed["created_by"] = created_by or ""
    packed["created_at"] = db.now_iso()
    packed["updated_at"] = packed["created_at"]
    cols = list(packed.keys())
    sql = "INSERT INTO db_adapters ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(packed.values()))


def update(adapter_id: int, data: dict) -> bool:
    existing = get_by_id(adapter_id)
    if not existing:
        return False
    # 不允许通过更新绕过 db_type 改名（避免破坏任务引用一致性）
    data = {k: v for k, v in data.items() if k != "db_type"}
    errs = validate({**existing, **data}, existing=existing)
    if errs:
        raise ValueError("; ".join(errs))
    packed = _pack(data)
    packed["updated_at"] = db.now_iso()
    sets = ",".join("{}=?".format(k) for k in packed.keys())
    db.execute(
        "UPDATE db_adapters SET {} WHERE id=?".format(sets),
        tuple(list(packed.values()) + [adapter_id]),
    )
    return True


def set_enabled(adapter_id: int, enabled: bool) -> bool:
    db.execute("UPDATE db_adapters SET enabled=?, updated_at=? WHERE id=?",
               (1 if enabled else 0, db.now_iso(), adapter_id))
    return True


def delete(adapter_id: int) -> tuple[bool, str]:
    """被 backup_tasks 引用时拒绝删除。返回 (ok, msg)。"""
    a = get_by_id(adapter_id)
    if not a:
        return False, "适配器不存在"
    if a.get("builtin"):
        return False, "内置适配器不允许删除"
    n = db.query_one(
        "SELECT COUNT(*) AS c FROM backup_tasks WHERE db_type=?",
        (a["db_type"],))
    cnt = (n or {}).get("c", 0) if isinstance(n, dict) else (n[0] if n else 0)
    # 兼容不同 SQLite 取值方式
    if isinstance(cnt, dict):
        cnt = cnt.get("c", 0)
    if cnt and cnt > 0:
        return False, f"被 {cnt} 个备份任务引用，请先迁移或删除相关任务"
    db.execute("DELETE FROM db_adapters WHERE id=?", (adapter_id,))
    return True, ""


def count_tasks_using(db_type: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM backup_tasks WHERE db_type=?", (db_type,))
    if not row:
        return 0
    return row.get("c", 0) if isinstance(row, dict) else (row[0] if row else 0)


# ============== 脚本模板渲染 ==============
# {{KEY}} → ${PLATFORM_<KEY>}（KEY 自动大写，字母/数字/下划线）
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def render_template(template: str, params: Optional[dict] = None) -> str:
    if not template:
        return ""
    params = params or {}

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        # 透明别名：PARAM_<KEY> 直接走 params
        if key.startswith("PARAM_"):
            return "${PLATFORM_" + key + "}"  # 由平台注入 PLATFORM_PARAM_<KEY>
        return "${PLATFORM_" + key.upper() + "}"
    return _PLACEHOLDER_RE.sub(_sub, template)# ============== 自定义引擎（CustomDBEngine） ==============
class CustomDBEngine(BackupEngine):
    """界面新增的数据库类型：完全由 db_adapters 表中的脚本模板驱动。

    - backup(backup_type)   → 渲染 full/incremental 脚本，走 base._backup_custom_remote
    - restore(backup_path)  → 渲染 restore 脚本，走 base._restore_custom_remote
    - verify_record         → 委托基类（文件存在 + checksum），也可执行 script_verify
    - list_databases        → 走 script_list_dbs（基类默认 []，由 JDBC 兜底）

    类属性 db_type / display_name / required_clients 由 ``build_engine_class``
    在动态构造子类时设置。
    """

    # 适配层分级固定为 custom_api（脚本模板 = API 集成风格）
    adapter_tier = "custom_api"

    def __init__(self, task: dict, storage_root: str, logger=None,
                 spec: Optional[dict] = None):
        super().__init__(task, storage_root, logger)
        self._spec = spec or {}

    # ---------- 工具：渲染并构造 extra ----------
    def _render_for(self, kind: str, backup_type: Optional[str] = None) -> str:
        keymap = {
            "backup_full": "script_backup_full",
            "backup_incremental": "script_backup_incremental",
            "backup_full_instance": "script_backup_full_instance",
            "restore": "script_restore",
            "verify": "script_verify",
            "list_dbs": "script_list_dbs",
            "test_conn": "script_test_conn",
        }
        tmpl = self._spec.get(keymap.get(kind, ""), "")
        if not tmpl:
            return ""
        params = self._spec.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        return render_template(tmpl, params)

    def _build_extra(self, script: str) -> dict:
        extra = {
            "custom_script": script,
            "custom_timeout": int(self._spec.get("timeout_sec") or 7200),
        }
        if self._spec.get("artifact_dir"):
            extra["custom_artifact_dir"] = self._spec["artifact_dir"]
        return extra

    def _build_restore_extra(self, script: str) -> dict:
        return {
            "custom_restore_script": script,
            "custom_timeout": int(self._spec.get("timeout_sec") or 7200),
        }

    # ---------- backup / restore ----------
    def backup(self, backup_type: BackupType) -> BackupResult:
        from core import remote_dump
        bt = backup_type.value if isinstance(backup_type, BackupType) else str(backup_type)
        db_name_blank = not (self.task.get("db_name") or "").strip()
        if bt == "incremental" and self._spec.get("script_backup_incremental"):
            tmpl_kind = "backup_incremental"
        elif (bt in ("full", "snapshot")) and db_name_blank \
                and self._spec.get("script_backup_full_instance"):
            tmpl_kind = "backup_full_instance"
        else:
            tmpl_kind = "backup_full"
        script = self._render_for(tmpl_kind)
        if not script:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"自定义适配器未提供 {tmpl_kind} 脚本模板")
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="自定义适配器需要在数据库服务器上执行脚本：请纳管 SSH 主机或在任务中指定")
        return self._backup_custom_remote(ssh_host, backup_type, self._build_extra(script))

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        from core import remote_dump
        script = self._render_for("restore")
        if not script:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="自定义适配器未提供 restore 脚本模板")
        ssh_host = kwargs.get("target_host_info") or remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="自定义适配器恢复需要在数据库服务器上执行脚本：恢复表单需选择目标 SSH 主机")
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"本地备份文件不存在: {backup_path}")
        return self._restore_custom_remote(
            ssh_host, backup_path, script, self._build_restore_extra(script), **kwargs)

    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """优先执行 script_verify（远端校验），无脚本则走基类文件校验。"""
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        script = self._render_for("verify")
        if not script:
            return super().verify_record(record, options or {})
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return super().verify_record(record, options or {})
        ts = self._timestamp()
        check_script = (
            script
            + f"\necho PLATFORM_BACKUP_FILE={shlex_quote(record.get('backup_path') or '')}\n"
            + f"echo PLATFORM_CHECKSUM={shlex_quote(record.get('checksum') or '')}\n"
        )
        client = remote_dump._connect(ssh_host)
        try:
            inner = remote_dump._wrap_login("bash -c " + shlex.quote(check_script))
            out, _, rc = _ssh_exec_pipe(client, inner, timeout=300)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            if rc == 0:
                return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                    message=f"自定义脚本校验通过: {text.strip()[-200:]}",
                                    verified=True)
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"自定义脚本校验失败(rc={rc}): {text[-500:]}")
        finally:
            try:
                client.close()
            except Exception:
                pass

    def list_databases(self) -> list:
        """走 script_list_dbs（每行一个库名）。"""
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        script = self._render_for("list_dbs")
        if not script:
            return super().list_databases()
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return super().list_databases()
        client = remote_dump._connect(ssh_host)
        try:
            inner = remote_dump._wrap_login("bash -c " + shlex.quote(script))
            out, _, rc = _ssh_exec_pipe(client, inner, timeout=120)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            if rc != 0:
                self.logger.warning("[%s] list_dbs 脚本 rc=%s: %s",
                                    self.task_name, rc, text[-300:])
                return []
            dbs = []
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                dbs.append(s)
            return dbs
        except Exception as e:
            self.logger.warning("[%s] list_dbs 异常: %s", self.task_name, e)
            return []
        finally:
            try:
                client.close()
            except Exception:
                pass

    def check_client(self) -> tuple:
        """脚本适配器一般不需要客户端检查；缺工具时仅警告。"""
        if self._spec.get("skip_client_check"):
            return True, "ok（已跳过客户端检查）"
        tools = self._spec.get("client_tools") or []
        if not tools:
            return True, "ok（脚本模板无需客户端工具）"
        missing = [t for t in tools if not shutil.which(t)]
        if missing:
            return False, ("缺少客户端工具: " + ", ".join(missing)
                           + "（远端可用将自动使用远程执行；可在适配器勾选「跳过客户端检查」）")
        return True, "ok"# ============== 动态注册 ==============
def build_engine_class(spec: dict) -> type:
    """根据 spec 动态构造一个 BackupEngine 子类。

    - 类属性 db_type / display_name / required_clients 由 spec 决定
    - adapter_tier = "custom_api"（已在 CustomDBEngine 上固定）
    - 不复制 CustomDBEngine 之外的字段，避免污染调度链路
    """
    cls_name = "CustomEngine_" + re.sub(r"\W", "_", spec["db_type"])

    class _Cls(CustomDBEngine):
        # 实例属性优先级：spec（动态）→ 类属性（兜底）
        pass

    _Cls.__name__ = cls_name
    _Cls.__qualname__ = cls_name
    _Cls.db_type = spec["db_type"]
    _Cls.display_name = spec.get("display_name") or spec["db_type"]
    _Cls.required_clients = list(spec.get("client_tools") or [])
    # 构造一个接受 spec 的工厂替换默认 __init__
    _orig_init = _Cls.__init__

    def __init__(self, task, storage_root, logger=None):
        _orig_init(self, task, storage_root, logger, spec=spec)

    _Cls.__init__ = __init__
    return _Cls


def engine_meta(spec: dict) -> dict:
    """返回给前端的引擎元信息（/api/meta 中 db_type_meta）。"""
    modes = spec.get("backup_modes") or ["logical"]
    return {
        "db_type": spec["db_type"],
        "display_name": spec.get("display_name") or spec["db_type"],
        "category": spec.get("category") or "custom",
        "icon": spec.get("icon") or "bi-box-seam",
        "default_port": spec.get("default_port"),
        "description": spec.get("description") or "",
        "backup_modes": modes,
        "supports_incremental": bool(spec.get("supports_incremental")),
        "supports_full_instance": bool(spec.get("supports_full_instance")),
        "supports_sync": bool(spec.get("supports_sync")),
        "adapter_tier": "custom_api",
        "builtin": bool(spec.get("builtin")),
        "source": "adapter",
    }


def register_all() -> int:
    """把 db_adapters 表中所有 enabled=1 的适配器注入 core.engines 引擎注册表。

    返回新注册数量（同 db_type 重复注册会覆盖）。失败不抛错（容错：
    表尚未迁移 / 模块循环导入场景）。
    """
    from core import engines
    n = 0
    try:
        specs = list_adapters(include_disabled=False)
    except Exception:
        return 0
    for spec in specs:
        try:
            cls = build_engine_class(spec)
            engines.ENGINE_REGISTRY[spec["db_type"]] = cls
            engines.ENGINE_DISPLAY[spec["db_type"]] = cls.display_name
            n += 1
        except Exception:
            continue
    return n


def reload_one(db_type: str) -> bool:
    """更新单个适配器的注册（创建/更新后调用）。"""
    from core import engines
    spec = get_by_dbtype(db_type, include_disabled=True)
    if not spec:
        engines.ENGINE_REGISTRY.pop(db_type, None)
        engines.ENGINE_DISPLAY.pop(db_type, None)
        return True
    if not spec.get("enabled"):
        engines.ENGINE_REGISTRY.pop(db_type, None)
        engines.ENGINE_DISPLAY.pop(db_type, None)
        return True
    cls = build_engine_class(spec)
    engines.ENGINE_REGISTRY[db_type] = cls
    engines.ENGINE_DISPLAY[db_type] = cls.display_name
    return True


def reload_all() -> int:
    """重载所有适配器（清掉旧的+加新的）。"""
    from core import engines
    # 仅清理此前由适配器动态加入的（db_type 不在 BUILTIN_TYPES）
    for t in list(engines.ENGINE_REGISTRY.keys()):
        if t not in BUILTIN_TYPES:
            engines.ENGINE_REGISTRY.pop(t, None)
            engines.ENGINE_DISPLAY.pop(t, None)
    return register_all()


def ensure_registered(db_type: str) -> bool:
    """惰性注册：某次 get_engine 之前调用，确保已注册。"""
    from core import engines
    if db_type in engines.ENGINE_REGISTRY:
        return True
    if db_type in BUILTIN_TYPES:
        return False
    return reload_one(db_type)


# ============== 测试连接（独立于引擎注册，单独在 API 层调用） ==============
def test_connection(spec: dict, ssh_host: Optional[dict] = None,
                   task: Optional[dict] = None) -> dict:
    """在 SSH 主机（或平台本机）执行 script_test_conn。

    返回 {ok, rc, stdout, stderr, duration_sec, ssh_host}。
    """
    import core.remote_dump as rd
    from core.engines.file import _ssh_exec_pipe

    script = spec.get("script_test_conn") or spec.get("script_list_dbs") or ""
    if not script:
        return {"ok": False, "error": "未配置 script_test_conn（也未配置 script_list_dbs）",
                "rc": -1, "stdout": "", "stderr": "", "duration_sec": 0.0,
                "ssh_host": ""}
    script = render_template(
        script,
        spec.get("params") if isinstance(spec.get("params"), dict) else {})

    if not ssh_host:
        # 尝试按任务解析
        if task:
            ssh_host = rd.resolve_ssh_host(task)
    if ssh_host:
        client = rd._connect(ssh_host)
        try:
            inner = rd._wrap_login("bash -c " + shlex.quote(script))
            start = time.time()
            out, err, rc = _ssh_exec_pipe(client, inner,
                                          timeout=int(spec.get("timeout_sec") or 120))
            duration = round(time.time() - start, 3)
            return {
                "ok": rc == 0,
                "rc": rc,
                "stdout": (out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or ""))[-2000:],
                "stderr": (err or "")[-1000:],
                "duration_sec": duration,
                "ssh_host": ssh_host.get("host_key", ""),
            }
        finally:
            try:
                client.close()
            except Exception:
                pass
    # 无 SSH：本地执行
    try:
        start = time.time()
        env = os.environ.copy()
        if task:
            from core.db import decrypt_secret
            pw = decrypt_secret(task.get("password") or "")
            if pw:
                env["PLATFORM_DB_PASSWORD"] = pw
            env["PLATFORM_DB_HOST"] = task.get("host") or ""
            env["PLATFORM_DB_PORT"] = str(task.get("port") or "")
            env["PLATFORM_DB_USER"] = task.get("username") or ""
            env["PLATFORM_DB_NAME"] = task.get("db_name") or ""
        proc = __import__("subprocess").run(
            ["bash", "-c", script], env=env, capture_output=True,
            text=True, timeout=int(spec.get("timeout_sec") or 120))
        duration = round(time.time() - start, 3)
        return {"ok": proc.returncode == 0, "rc": proc.returncode,
                "stdout": (proc.stdout or "")[-2000:], "stderr": (proc.stderr or "")[-1000:],
                "duration_sec": duration, "ssh_host": "(local)"}
    except Exception as e:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": str(e),
                "duration_sec": 0.0, "ssh_host": "(local)"}

# ============== 便捷查询 ==============
def enabled_types() -> list[str]:
    """返回所有已启用 db_type 列表（用于 engines.supported_types() 合并）。"""
    try:
        return [r["db_type"] for r in db.query(
            "SELECT db_type FROM db_adapters WHERE enabled=1 ORDER BY id")]
    except Exception:
        return []
