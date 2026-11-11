# -*- coding: utf-8 -*-
"""新增能力的 UI 入口契约：后端有端点就必须有可见入口，防止只交付 API。"""
import os

from api import perm_map

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_scan_fs_page_and_menu():
    assert '@app.route("/scan-fs")' in _src("app.py")
    assert 'render_template("scan_fs.html", page="scan-fs")' in _src("app.py")
    tpl = _src("templates/scan_fs.html")
    assert "scan_fs.js" in tpl
    assert "只读扫描" in tpl and "拉回选中对象" in tpl
    assert "Oracle 实例清单" in tpl
    assert "不读裸块设备、不做扇区级数据雕刻" in tpl
    base = _src("templates/base.html")
    assert 'href="/scan-fs"' in base and "无备份目录扫描" in base


def test_scan_fs_js_covers_all_three_endpoints():
    js = _src("static/js/scan_fs.js")
    assert "/api/scan-fs/scan" in js
    assert "/api/scan-fs/pull" in js
    assert "/oracle-inventory" in js
    assert "sha256" in js and "sf_filePager" in js


def test_task_retry_entry():
    js = _src("static/js/app.js")
    assert "window.retryTask" in js
    assert "/api/tasks/${id}/retry" in js
    assert 'onclick="retryTask(' in js
    assert 't.last_status === "failed"' in js


def test_clone_preflight_entry():
    assert '/clone/preflight"' in _src("api/clone.py")
    html = _src("templates/clone.html")
    assert 'onclick="preflightClone()"' in html
    assert 'id="c_preflight"' in html
    js = _src("static/js/app.js")
    assert "window.preflightClone" in js
    assert '"/api/clone/preflight"' in js
    perm, reason = perm_map.resolve("/api/clone/preflight", "POST")
    assert perm is None and reason == "exempt"


def test_agent_plugin_capability_entry():
    html = _src("templates/agentless.html")
    assert 'id="ac_agent"' in html and 'id="ac_caps"' in html
    assert 'onclick="loadAgentCaps()"' in html
    assert "window.loadAgentCaps" in html
    assert "/capabilities" in html
    assert "端到端 sha256 对账" in html
    assert '/agents/<int:aid>/capabilities' in _src("api/agents.py")


def test_cache_bust_stamp_for_app_js():
    import re
    base = _src("templates/base.html")
    m = re.search(r"app\.js\?v=(\d+)", base)
    assert m and int(m.group(1)) >= 2026111010, "本轮 app.js 改动必须升缓存戳"
