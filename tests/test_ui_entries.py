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
    # 2026-11-13 页面改为「无备份救援（特殊恢复）」定位：能做什么/不能做什么
    # 用两栏卡片表达，原单句「不读裸块设备、不做扇区级数据雕刻」已并入列表项，
    # 断言随之对齐（能力描述本身没有放宽）。
    assert "只读扫描" in tpl and "拉回选中对象" in tpl
    # 2026-11-24：页面按钮为「实例清单」（全库型，不只 Oracle）
    assert "实例清单" in tpl
    assert "这是「特殊恢复」，不属于常规恢复链路" in tpl
    assert "不读裸块设备" in tpl and "不自动修复数据库一致性" in tpl
    base = _src("templates/base.html")
    # 2026-11-13 菜单改名：明确"特殊恢复"定位（原来的"无备份目录扫描"易被误读为常规恢复）
    assert 'href="/scan-fs"' in base
    # 2026-11-24 口径统一：菜单与页面标题均为「特殊恢复」（另一个
    # test_scan_fs_classify.py::test_page_and_menu_renamed 明确要求不含
    # 「无备份救援（特殊恢复）」，此前本断言与之冲突导致门禁恒失败）
    assert "特殊恢复" in base
    assert "无备份救援（特殊恢复）" not in base


def test_scan_fs_js_covers_all_three_endpoints():
    js = _src("static/js/scan_fs.js")
    assert "/api/scan-fs/scan" in js
    assert "/api/scan-fs/pull" in js
    # 实例清单走全库型 /api/scan-fs/instances（原 Oracle 专用 inventory 端点
    # 保留兼容，但前端统一用全库型接口）
    assert "/api/scan-fs/instances" in js
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
