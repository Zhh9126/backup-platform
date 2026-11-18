# -*- coding: utf-8 -*-
"""可插拔数据库类型：模板目录 / 批量导入 / 参数注入 / 页面可见性 回归测试。

覆盖 2026-11-13 的改动：
1. 模板目录（core/db_adapter_templates.py）条目完整、脚本 bash 语法合法；
2. 内置引擎不再因 BUILTIN_TYPES 手写清单漏登记而在页面整行消失；
3. 适配器 params → PLATFORM_PARAM_<KEY> 环境变量真正注入；
4. 模板 → 适配器创建参数可通过 validate()（批量导入前置条件）。

注意：本文件**不在模块顶层** import core.db_adapters / core.engines.base。
它们会在 import 期建立/缓存元数据库连接，与同目录其它用例
（各自在 fixture 里改 META_DB_PATH / BACKUP_ROOT）互相污染——
混跑时会把别的用例打到真实 instance/meta.db 上。统一在用例内部惰性导入。
"""
import subprocess

import pytest

from core import db_adapter_templates as T


def test_catalog_size_and_groups():
    """目录要有足够覆盖度，且分组齐全。"""
    assert len(T.CATALOG) >= 40, f"模板目录条目过少：{len(T.CATALOG)}"
    groups = {g["group"] for g in T.grouped()}
    for need in ("pg", "mysql", "oracle", "olap", "ts", "nosql", "graph", "other"):
        assert need in groups, f"缺少分组 {need}"
    stats = T.stats()
    assert stats["total"] == len(T.CATALOG)
    assert stats["by_verification"].get("protocol", 0) >= 10


def test_catalog_keys_unique_and_dbtype_valid():
    import re
    keys = [t["key"] for t in T.CATALOG]
    assert len(keys) == len(set(keys)), "模板 key 重复"
    for t in T.CATALOG:
        db_type = (t.get("db_type_hint") or t["key"]).lower()
        assert re.match(r"^[a-z][a-z0-9_]{1,31}$", db_type), f"{t['key']} 的 db_type 非法"
        assert t.get("display_name"), f"{t['key']} 缺 display_name"
        assert len(t.get("display_name") or "") <= 64, f"{t['key']} display_name 超长"


def test_all_scripts_bash_syntax_ok(tmp_path):
    """所有模板脚本必须能通过 bash -n（避免交付即语法错）。"""
    bad = []
    for t in T.CATALOG:
        for kind, script in (t.get("scripts") or {}).items():
            if not script:
                continue
            f = tmp_path / "chk.sh"
            f.write_text(script, encoding="utf-8")
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            if r.returncode != 0:
                bad.append((t["key"], kind, r.stderr.strip()[:200]))
    assert not bad, f"脚本语法错误：{bad}"


def test_scripts_write_into_backup_dir():
    """备份脚本必须把产物写进 PLATFORM_BACKUP_DIR（平台只回收该目录）。"""
    missing = []
    for t in T.CATALOG:
        script = (t.get("scripts") or {}).get("script_backup_full") or ""
        if "PLATFORM_BACKUP_DIR" not in script:
            missing.append(t["key"])
    assert not missing, f"备份脚本未落 PLATFORM_BACKUP_DIR：{missing}"


def test_restore_scripts_use_backup_file():
    # sap_hana 例外：HANA 的 RECOVER DATABASE 直接读服务端备份目录，
    # 不使用平台推送的备份文件（脚本里给出 until_ts 恢复语义）。
    allow = {"sap_hana"}
    missing = []
    for t in T.CATALOG:
        if t["key"] in allow:
            continue
        script = (t.get("scripts") or {}).get("script_restore") or ""
        if "PLATFORM_BACKUP_FILE" not in script:
            missing.append(t["key"])
    assert not missing, f"恢复脚本未引用 PLATFORM_BACKUP_FILE：{missing}"


def test_placeholder_render():
    """{{PARAM_X}} 渲染为 ${PLATFORM_PARAM_X}（配合 params 注入使用）。"""
    from core import db_adapters

    out = db_adapters.render_template(
        'echo "{{PARAM_TOOL_BIN}}" "{{DB_HOST}}"', {"tool_bin": "/opt/x"})
    assert "${PLATFORM_PARAM_TOOL_BIN}" in out
    assert "${PLATFORM_DB_HOST}" in out


def test_param_env_injection():
    """params → PLATFORM_PARAM_<KEY> 环境变量（此前从未注入，模板取值恒为空）。"""
    from core.engines import base as engine_base

    lines = engine_base.BackupEngine._custom_param_env_lines(
        object(), {"custom_params": {"tool_bin": "/opt/mysql840b/bin", "repo": "aidbm_repo"}})
    joined = "\n".join(lines)
    assert "PLATFORM_PARAM_TOOL_BIN=" in joined
    assert "/opt/mysql840b/bin" in joined
    assert "PLATFORM_PARAM_REPO=aidbm_repo" in joined
    # 非法键（中文/横杠）必须被跳过，避免生成坏 export
    bad = engine_base.BackupEngine._custom_param_env_lines(
        object(), {"custom_params": {"非法-键": "x"}})
    assert bad == []


def test_builtin_types_cover_engine_registry():
    """回归：页面曾因 BUILTIN_TYPES 漏登记而隐藏 7 个已注册引擎。"""
    from core import db_adapters
    from core.engines import ENGINE_REGISTRY

    try:
        adapter_types = {a["db_type"] for a in db_adapters.list_adapters(include_disabled=True)}
    except Exception:
        # 与其它用例混跑时元数据库可能已被切走/清理，此时只校验内置集合
        adapter_types = set()
    visible = set(db_adapters.builtin_types()) | adapter_types
    hidden = [t for t in ENGINE_REGISTRY if t not in visible]
    assert not hidden, f"这些已注册引擎在页面上不可见：{hidden}"


def test_template_payload_passes_validate():
    """批量导入用的 payload 必须能通过 validate()。"""
    from api.db_adapters import _payload_from_template
    from core import db_adapters

    builtin = set(db_adapters.builtin_types())
    errors = []
    for t in T.CATALOG:
        payload = _payload_from_template(t)
        if payload["db_type"] in builtin:
            continue
        errs = db_adapters.validate(payload)
        if errs:
            errors.append((t["key"], errs))
    assert not errors, f"模板生成参数校验失败：{errors[:5]}"


def test_task_level_adapter_params_override():
    """任务级 adapter_params 覆盖适配器级 params（db_file 等每任务不同）。"""
    from core.db_adapters import CustomDBEngine

    class _E(CustomDBEngine):
        def __init__(self, spec, task):
            self._spec = spec
            self.task = task

    e = _E({"params": {"db_file": "/a.db", "repo": "r1"}},
           {"extra_options": '{"adapter_params": {"db_file": "/b.db"}}'})
    assert e._params() == {"db_file": "/b.db", "repo": "r1"}


@pytest.mark.parametrize("key", ["db2", "cassandra", "clickhouse", "tdengine", "gaussdb",
                                 "goldendb", "oscar", "sqlite", "etcd", "arangodb"])
def test_high_value_templates_present(key):
    """主流/国产高价值库型必须在目录里。"""
    assert T.by_key(key), f"缺少模板 {key}"
