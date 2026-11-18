# -*- coding: utf-8 -*-
"""侧边栏二级菜单结构回归（2026-11-13）。

背景：侧栏一级项过多（尤其「运维」段 10+ 条），改为折叠组：
- 存储管理：存储池 / 对象存储 / 磁带库 / NAS / FTP / NFS / 外挂；
- 运维监控：运营分析 / 巡检 / 任务监控 / 报表 / 告警 / 数据挖掘；
- 系统设置：系统参数 / Agent 管理 / 备份插件 / 用户管理 / 系统日志。

Agent 管理原先挂在「备份管理」一级，现必须收敛到「系统设置」二级里。
"""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "templates" / "base.html"


def _html():
    return BASE.read_text(encoding="utf-8")


def _sub_block(html, sid):
    """取某个二级菜单容器内的 HTML 片段。"""
    start = html.index('id="%s"' % sid)
    seg = html[start:]
    depth, i = 0, seg.index(">")
    # 简化解析：从容器开始标签后按 div 计数截取
    body = seg[i + 1:]
    out, depth = [], 1
    for m in re.finditer(r"<div\b|</div>", body):
        if m.group(0) == "<div":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                out.append(body[:m.start()])
                break
    return out[0] if out else body[:2000]


def test_three_groups_registered():
    html = _html()
    for gid, target in (("storageNavParent", "storageNavSub"),
                        ("opsNavParent", "opsNavSub"),
                        ("sysNavParent", "sysNavSub")):
        assert 'id="%s"' % gid in html, f"缺少折叠组 {gid}"
        assert 'data-target="%s"' % target in html, f"{gid} 未绑定 data-target"
        assert 'id="%s"' % target in html, f"缺少二级容器 {target}"


def test_agent_and_plugins_moved_into_system_settings():
    html = _html()
    sys_sub = _sub_block(html, "sysNavSub")
    assert 'href="/agentless"' in sys_sub, "Agent 管理必须在系统设置二级菜单内"
    assert 'href="/plugins"' in sys_sub, "备份插件必须在系统设置二级菜单内"
    assert 'href="/settings"' in sys_sub
    assert 'href="/users"' in sys_sub
    assert 'href="/logs"' in sys_sub
    # 不能再作为一级项出现在备份管理段
    assert html.count('href="/agentless"') == 1, "Agent 管理不应重复出现在多处"
    assert html.count('href="/plugins"') == 1, "备份插件不应重复出现在多处"


def test_ops_group_contains_monitoring_items():
    html = _html()
    ops_sub = _sub_block(html, "opsNavSub")
    for href in ("/operations", "/inspection", "/monitor", "/reports",
                 "/alert", "/datamining"):
        assert 'href="%s"' % href in ops_sub, f"{href} 应在运维监控组内"


def test_storage_group_still_intact():
    html = _html()
    sub = _sub_block(html, "storageNavSub")
    for href in ("/storage", "/object_storage", "/tape", "/synology",
                 "/storage?kind=ftp", "/storage?kind=nfs", "/storage?kind=mount"):
        assert 'href="%s"' % href in sub, f"{href} 丢失"


def test_logout_remains_top_level():
    html = _html()
    assert 'class="nav-link logout"' in html
