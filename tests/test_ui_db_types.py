# -*- coding: utf-8 -*-
"""数据库类型下拉的回归测试（数据迁移页 / 数据同步页）。

真实缺陷背景：
1. 数据同步页「新建同步任务」的源/目标类型下拉**永远是空的**——META 由
   app.js 在 DOMContentLoaded 里 ``await /api/meta`` 之后才填充，而 sync.js
   在同一个事件里同步调用 ``BKP.fillDbTypeSelect``，永远抢在前面拿到空数组；
2. 数据迁移页「新建迁移计划」的类型下拉只有 MySQL/PostgreSQL 两项——是
   migration.html 里硬编码的 option，MariaDB/金仓/达梦/Oracle/SQL Server
   等**已支持迁移**的类型用户根本选不到。

本文件从三个层面锁定修复，全部可离线运行：
- ``dukpy`` 真实执行 ``static/js/bkp-core.js`` 里的 ``ensureMeta`` /
  ``fillDbTypeSelect`` 源码（不是复刻逻辑），覆盖"竞态（META 未就绪）"
  与"首屏已注入"两条路径；
- Flask test_client 抓真实渲染产物，断言首屏注入的 META 与空 select；
- 源码级断言，防止有人再把 sync.js 的 await 或迁移页的动态填充改回去。

注：``dukpy`` 是测试期可选依赖（离线交付包不含它），缺失时仅跳过 JS 执行部分。
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BKP_CORE = os.path.join(ROOT, "static", "js", "bkp-core.js")
SYNC_JS = os.path.join(ROOT, "static", "js", "sync.js")
APP_JS = os.path.join(ROOT, "static", "js", "app.js")
MIGRATION_HTML = os.path.join(ROOT, "templates", "migration.html")

META = {
    "db_types": ["mysql", "mariadb", "postgresql", "oracle", "kingbase",
                 "dameng", "sqlserver", "redis", "file"],
    # 与 core.sync.plugins.registry.available() 的当前注册集一致：协议兼容层
    # 接入的 highgo / uxdb / tidb / oceanbase 也已注册同步插件与迁移通道，
    # 前端类型下拉应当可见（旧基线只写了 7 项已过时）。
    "sync_types": ["dameng", "highgo", "kingbase", "mariadb", "mysql",
                   "oceanbase", "oracle", "postgresql", "sqlserver",
                   "tidb", "uxdb"],
    "display_names": {"mysql": "MySQL", "mariadb": "MariaDB",
                      "postgresql": "PostgreSQL", "oracle": "Oracle",
                      "kingbase": "kingbase", "dameng": "DM 达梦",
                      "sqlserver": "SQL Server", "redis": "Redis"},
    "default_ports": {"mysql": 3306, "mariadb": 3306, "postgresql": 5432,
                      "oracle": 1521, "kingbase": 54321, "dameng": 5236,
                      "sqlserver": 1433},
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_assign(src: str, marker: str) -> str:
    """从源码里抽出 ``marker`` 开始的整段代码（按花括号配平，函数赋值补分号）。"""
    i = src.index(marker)
    k = src.index("{", i)
    depth = 0
    for p in range(k, len(src)):
        if src[p] == "{":
            depth += 1
        elif src[p] == "}":
            depth -= 1
            if depth == 0:
                code = src[i:p + 1]
                return code + ";" if "= function" in marker else code
    raise AssertionError(f"未能抽取代码块: {marker}")


# --------------------------------------------------------------- JS 真实执行
def _run_js(meta_in_page: dict, call: str) -> dict:
    """在最小 DOM 环境里真实执行 bkp-core.js 的相关源码并返回执行结果。

    执行的三段代码（首屏 META 合并、ensureMeta、fillDbTypeSelect）都是从
    真实文件里按括号配平抽出来的，避免测试里出现"复刻版逻辑"而失去意义。
    """
    dukpy = pytest.importorskip("dukpy", reason="需要 dukpy 才能真实执行前端源码")
    src = _read(BKP_CORE)
    merge = _extract_assign(src, 'if (typeof window !== "undefined" && window.__BKP_META__)')
    ensure = _extract_assign(src, "BKP.ensureMeta = function")
    fill = _extract_assign(src, "BKP.fillDbTypeSelect = function")
    js = f"""
    var window = {{ __BKP_META__: {json.dumps(meta_in_page)} }};
    var BKP = {{
      META: {{ db_types: [], display_names: {{}}, default_ports: {{}} }},
      esc: function (s) {{ return String(s); }},
      fetchCalls: 0
    }};
    // 真实执行"首屏注入 META 合并"那段源码
    {merge}
    var Promise = {{ resolve: function (v) {{ return ST(v); }} }};
    // 极简 Object.assign / 同步 thenable：让被真实执行的异步链同步跑完
    Object.assign = function (t) {{
      for (var i = 1; i < arguments.length; i++) {{
        var s = arguments[i] || {{}};
        for (var k in s) {{ t[k] = s[k]; }}
      }}
      return t;
    }};
    function ST(v) {{
      return {{
        then: function (cb) {{
          var r = cb(v);
          return (r && typeof r.then === "function") ? r : ST(r);
        }},
        catch: function () {{ return ST(v); }}
      }};
    }}
    function fetch() {{
      BKP.fetchCalls++;
      return ST({{ ok: true, json: function () {{ return ST(META_PAYLOAD); }} }});
    }}
    {ensure}
    {fill}
    {call}
    """
    js = js.replace("META_PAYLOAD", json.dumps(META))
    return dukpy.evaljs(js)


def test_fill_select_recovers_when_meta_not_ready_yet():
    """竞态路径：META 尚未就绪（refresh 回落到 /api/meta）也必须填满下拉。"""
    out = _run_js({}, """
    var el = { innerHTML: "" };
    BKP.fillDbTypeSelect(el, [], { typesKey: "sync_types" });
    __result = {
      html: el.innerHTML,
      fetchCalls: BKP.fetchCalls,
      count: (el.innerHTML.match(/<option /g) || []).length
    };
    """)
    assert out["count"] == len(META["sync_types"]), out["html"]
    assert out["fetchCalls"] >= 1          # 确实回落到 /api/meta 取数
    assert "MySQL" in out["html"] and "DM 达梦" in out["html"]


def test_fill_select_uses_injected_meta_without_extra_request():
    """首屏注入路径：模板已注入 META 时应直接填充，不再发请求。"""
    out = _run_js(META, """
    var el = { innerHTML: "" };
    BKP.fillDbTypeSelect(el, [], { typesKey: "sync_types" });
    __result = {
      html: el.innerHTML,
      fetchCalls: BKP.fetchCalls,
      ids: (el.innerHTML.match(/value=\\"([a-z]+)\\"/g) || []).map(function (s) {
        return s.replace('value=\\"', '').replace('\\"', '');
      })
    };
    """)
    assert out["fetchCalls"] == 0
    assert out["ids"] == META["sync_types"]
    assert "file" not in out["ids"] and "redis" not in out["ids"]


def test_fill_select_defaults_to_db_types_and_honours_exclude():
    out = _run_js(META, """
    var el = { innerHTML: "" };
    BKP.fillDbTypeSelect(el, ["file", "redis"]);
    __result = { html: el.innerHTML };
    """)
    assert "file" not in out["html"] and "redis" not in out["html"]
    assert "MySQL" in out["html"]


# ------------------------------------------------------- 渲染产物 / 源码断言
def _client():
    from app import app
    c = app.test_client()
    r = c.post("/login", json={"username": "admin", "password": "admin123"})
    with c.session_transaction() as _sess:
        if not _sess.get("user"):
            _sess["user"] = {"username": "admin", "role": "admin"}
    assert r.status_code == 200, f"登录失败: {r.status_code}"
    return c


def test_pages_inject_meta_on_first_paint():
    c = _client()
    for path in ("/migration", "/sync"):
        html = c.get(path).get_data(as_text=True)
        m = re.search(r"window\.__BKP_META__ = (\{.*?\});", html, re.S)
        assert m, f"{path} 未注入首屏 META（这正是空下拉的根因）"
        meta = json.loads(m.group(1))
        assert meta.get("sync_types") == META["sync_types"]


def test_migration_selects_are_filled_dynamically():
    html = _read(MIGRATION_HTML)
    for sid in ("dm_src_db_type", "dm_tgt_db_type"):
        m = re.search(r'id="%s"' % sid, html)
        assert m, f"{sid} 不存在"
        tail = html[m.end():m.end() + 200]
        assert "<option" not in tail.split("</select>")[0], \
            f"{sid} 又变回硬编码 option 了"
    assert "dm_tgt_db_name_label" in html        # 目标库提示随类型联动


def test_sync_js_waits_for_meta_before_filling():
    src = _read(SYNC_JS)
    i_ensure = src.index("await BKP.ensureMeta()")
    i_fill = src.index('fillDbTypeSelect($("srcDbType")')
    assert i_ensure < i_fill, "必须先 await ensureMeta 再填充类型下拉"
    assert 'typesKey: "sync_types"' in src


def test_migration_js_uses_sync_types_and_port_linkage():
    src = _read(APP_JS)
    assert 'fillDbTypeSelect(sel, [], { typesKey: "sync_types", onReady: afterFill })' in src
    assert "META.default_ports" in src
    i = src.index("async function dmFillTypeSelects")
    assert "await BKP.ensureMeta()" in src[i:i + 400]


# ------------------------------------------------------------------ 后端契约
def test_migratable_types_cover_all_sync_plugins():
    from core.db_migrate import _migratable_types
    types = _migratable_types()
    assert types == META["sync_types"], types


def test_unknown_source_type_returns_note_not_crash():
    from core.db_migrate import engine
    st = engine._source_stats({"src_db_type": "redis", "src_db_name": "x"})
    assert st["tables"] == 0 and "redis" in st["note"]


@pytest.mark.parametrize("db_type,expect_sql", [
    ("mysql", "table_schema"),
    ("mariadb", "table_schema"),
    ("postgresql", "pg_stat_user_tables"),
    ("kingbase", "pg_stat_user_tables"),
    ("oracle", "all_tables"),
    ("dameng", "all_tables"),
    ("sqlserver", "sys.partitions"),
])
def test_source_stats_sql_per_db_type(monkeypatch, db_type, expect_sql):
    """每种库型都要走自己的统计 SQL（含 Oracle/达梦/SQL Server）。"""
    from core.db_migrate import engine

    seen = []

    class _Cur:
        def execute(self, sql):
            seen.append(sql)

        def fetchone(self):
            return (3,)

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr("core.db_migrate._stats_connect",
                        lambda *a, **kw: _Conn())
    st = engine._source_stats({
        "src_db_type": db_type, "src_host": "127.0.0.1", "src_port": 1,
        "src_username": "u", "src_password": "p", "src_db_name": "d"})
    assert st == {"tables": 3, "rows": 3}, st
    assert any(expect_sql in s for s in seen), seen


def test_connect_error_becomes_note_not_exception(monkeypatch):
    from core.db_migrate import engine

    def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("core.db_migrate._stats_connect", _boom)
    st = engine._source_stats({"src_db_type": "mysql", "src_host": "h",
                               "src_db_name": "d", "src_username": "u",
                               "src_password": "p"})
    assert st["tables"] == 0 and "源库连接失败" in st["note"]


def test_create_plan_rejects_unsupported_type():
    from core.db_migrate import engine
    with pytest.raises(ValueError, match="不支持迁移"):
        engine.create_plan({"name": "bad", "src_db_type": "redis",
                            "src_host": "h", "src_db_name": "d",
                            "tgt_db_type": "mysql", "tgt_host": "h",
                            "tgt_db_name": "d"})


def test_sync_api_rejects_unsupported_type():
    c = _client()
    r = c.post("/api/sync-tasks", json={
        "name": "bad-sync", "src_db_type": "redis", "src_host": "h",
        "src_username": "u", "src_password": "p", "src_db_name": "d",
        "tgt_db_type": "mysql", "tgt_host": "h", "tgt_username": "u",
        "tgt_password": "p", "tgt_db_name": "d"})
    assert r.status_code == 400
    body = r.get_json() or {}
    assert "不支持同步" in json.dumps(body, ensure_ascii=False)
