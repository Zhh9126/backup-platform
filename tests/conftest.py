# -*- coding: utf-8 -*-
"""测试基础设施：元数据库隔离自愈。

## 为什么需要

平台代码里 ``config.META_DB_PATH`` 在 import 时固定，而相当多测试模块会在
**模块顶层**把 ``os.environ["META_DB_PATH"]`` 指到临时库做隔离（这是对的），
但它们不会在文件结束时切回。于是同一进程里全量跑测试时：

    模块 A 切到 /tmp/xxx/meta.db 并建表
      → 模块 B 没切库，但 config 仍指向 A 的临时库
      → B 的用例报 `sqlite3.OperationalError: no such table: ...`

2026-11-13 全量跑：258 failed / 44 errors，其中 ``test_rt_journal.py``
**单跑 48 passed、全量跑 44 errors** —— 典型的跨模块污染，与业务代码无关，
却会把真正的缺陷淹没在噪声里（"跑全量必挂"）。

## 这里的约定

1. 会话开始记录"原始元数据库"，会话结束恢复环境变量；
2. 每个用例结束后**只做自愈**：当前指向的库若缺核心表，就把路径切回原始库
   （原始库也不可用时才 init_schema 重建），不影响正常依赖临时库的用例；
3. 不主动删数据、不改业务逻辑，尽量无侵入。
"""
import os
import sqlite3

import pytest

import config as app_config

_CORE_TABLES = ("backup_tasks", "storage_targets", "system_config",
                "backup_records", "restore_records")
_ORIG_PATH = None
_REAL_PATH = ""


def _usable(path: str) -> bool:
    """该 sqlite 文件是否含核心表（不存在/无表都算不可用）。"""
    if not path or not os.path.isfile(path):
        return False
    try:
        conn = sqlite3.connect(path, timeout=3.0)
        try:
            have = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            return set(_CORE_TABLES) <= have
        finally:
            conn.close()
    except Exception:
        return False


# 会话开始时记录 db 模块的原始函数引用（用于识别"猴补丁没还原"）
_ORIG_FUNCS = {}


def pytest_configure(config):
    """记录会话开始时的元数据库路径与 db 模块原始函数。

    **默认把元数据库切到临时库**（2026-11-14 实测事故）：
    以前跑 pytest 时 ``app_config.META_DB_PATH`` 仍指向生产 ``instance/meta.db``，
    于是 ``test_goals`` 这类"造任务"的用例把 21 个 g-* 测试任务写进了生产库
    （host=127.0.0.1 db=demo，其中 rt_enabled=1 的三个还让实时守护每 10 秒重试
    刷 ERROR，直到手动删除）。测试绝不能写生产库，这里默认隔离；
    确实需要真实库的用例请显式设 ``AIDBM_TEST_USE_REAL_DB=1``。
    """
    global _ORIG_PATH, _REAL_PATH
    _REAL_PATH = getattr(app_config, "META_DB_PATH", None) or \
        os.environ.get("META_DB_PATH", "")
    if os.environ.get("AIDBM_TEST_USE_REAL_DB") != "1":
        import tempfile
        _dir = tempfile.mkdtemp(prefix="aidbm_test_meta_")
        _path = os.path.join(_dir, "meta.db")
        app_config.META_DB_PATH = _path
        os.environ["META_DB_PATH"] = _path
        try:
            import core.db as _db0
            _db0.init_schema()
        except Exception:
            pass
        # 自愈也只回到隔离库（绝不回到生产库）
        _ORIG_PATH = _path
    else:
        _ORIG_PATH = _REAL_PATH
    try:
        import core.db as _db
        for _name in ("execute", "query", "query_one", "get_conn",
                      "init_schema", "now_iso", "executemany"):
            _fn = getattr(_db, _name, None)
            if _fn is not None:
                _ORIG_FUNCS[_name] = _fn
    except Exception:
        pass


def pytest_unconfigure(config):
    """会话结束：把元数据库路径还原到会话开始前的真实值（不是隔离库）。"""
    _back = _REAL_PATH or _ORIG_PATH
    if _back:
        try:
            app_config.META_DB_PATH = _back
            os.environ["META_DB_PATH"] = _back
        except Exception:
            pass


def _restore_db_funcs() -> None:
    """把被测试猴补丁替换掉的 db 模块函数还原。

    2026-11-13 全量跑暴露：``test_plugin_serverside.py`` 为了隔离，直接把
    ``db.execute/query/query_one`` 替换成写自建临时库的版本，且**不还原**。
    之后所有模块的 db 调用都落到那个只有 3 张表的临时库 →
    ``no such table: storage_targets`` 之类错误批量出现（44 errors / 256 failed），
    把真正的缺陷完全淹没。这里在每个用例 setup 前统一还原。
    """
    try:
        import core.db as _db
        for _name, _fn in _ORIG_FUNCS.items():
            if getattr(_db, _name, None) is not _fn:
                setattr(_db, _name, _fn)
    except Exception:
        pass


def _heal() -> None:
    """当前元数据库不可用（缺表/文件被切走）时自愈回原始库。"""
    cur = getattr(app_config, "META_DB_PATH", "") or ""
    if _usable(cur):
        return
    # ① 优先切回原始库（真实业务库，schema 最完整）
    if _usable(_ORIG_PATH):
        app_config.META_DB_PATH = _ORIG_PATH
        os.environ["META_DB_PATH"] = _ORIG_PATH
        return
    # ② 原始库也不可用 → 按当前路径重建 schema（几秒，避免后续全挂）
    try:
        import core.db as db
        db.init_schema()
    except Exception:
        pass


@pytest.fixture(autouse=True, scope="module")
def _restore_db_funcs_per_module():
    """**模块切换时**还原被猴补丁替换的 db 函数。

    只在模块边界还原，是为了兼顾两类测试：
    - 有些模块（如 test_plugin_serverside）在 setUpClass 里替换 db.execute 指向
      自建临时库并在**同一模块内跨类复用** → 模块内不能还原；
    - 但它结束后必须还原，否则后续所有模块的 db 调用都落到那个只有 3 张表的
      临时库（真机全量跑：44 errors / 256 failed，全是 no such table）。
    """
    _restore_db_funcs()
    yield


def pytest_runtest_setup(item):
    """**每个用例 setup 之前**自愈。

    必须用 hook 而不是 autouse fixture：报错出现在 ``setUpClass``（类级 setup）
    里（``_mk_task`` 一执行就 ``no such table: backup_tasks``），此时用例级
    fixture 还没机会运行，靠"上一个用例结束后修复"来不及。
    """
    _heal()


@pytest.fixture(autouse=True)
def _heal_meta_db():
    """用例结束后再自愈一次，覆盖在用例体内切库的模块。"""
    yield
    _heal()
