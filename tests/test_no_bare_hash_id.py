# -*- coding: utf-8 -*-
"""UI 裸编号门禁：禁止「#+数字」出现在面向用户的文案里。

历史反复出现同一类问题：下拉选项、弹窗标题、toast、prompt、title 悬停
里拼出 "#135 任务名"。统一口径是「中文标签 + 数字」或直接用名称，
本门禁扫描 templates / static/js / api / core 的拼接型编号，防止复发。

合理豁免（不是编号）：CSS 选择器、颜色、锚点、HTML 实体、Markdown、
注释、序号列头、Oracle 系统列名 TS#、临时 schema 名、AI 澄清候选徽章。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("templates", "static/js", "api", "core")
SCAN_EXT = (".html", ".js", ".py")

# 拼接型编号： "#" + id / "任务 #" + id / "#${id}" / "#%s" / "#{id}"
BARE_HASH = re.compile(
    r'#(?=\$\{[^}]+\}|%[sd]|\{[^}]+\}|["\']\s*\+)|(["\'])#\1\s*\+')

EXEMPT = re.compile(
    r"querySelector|getElementById|CSS\.escape|\$\(\s*[\"']#|"
    r"href=[\"']#|data-bs-target=[\"']#|a\[href=|&#\d|&#x|"
    r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|^\s*#|^\s*//|^\s*\*|"
    r"HexColor|#[=-]{3,}|Aidbm#|#\{1,4\}|ts#=|ag-cand-id|"
    r"<th[^>]*>\s*#\s*</th>|执行 #12|f?[\"']\s*##?\s", re.I)


def _line_has_bare_hash(line: str) -> bool:
    """单行检测：供测试本体与门禁自测共用。"""
    if "#" not in line or EXEMPT.search(line):
        return False
    return bool(BARE_HASH.search(line))


def _violations():
    out = []
    for dirname in SCAN_DIRS:
        base = os.path.join(ROOT, dirname)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs
                       if d not in ("__pycache__", "vendor", "node_modules")]
            for fn in sorted(files):
                if not fn.endswith(SCAN_EXT):
                    continue
                if fn in ("bootstrap.bundle.min.js", "bootstrap.min.js"):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, ROOT)
                try:
                    lines = open(path, encoding="utf-8").read().split("\n")
                except Exception:
                    continue
                for i, line in enumerate(lines, 1):
                    if _line_has_bare_hash(line):
                        out.append("%s:%d: %s" % (rel, i, line.strip()[:130]))
    return out


def test_no_bare_hash_id_in_ui():
    bad = _violations()
    assert not bad, (
        "以下位置又拼出了「#+编号」裸展示，请改用名称或「中文标签 + 数字」：\n"
        + "\n".join(bad[:40]))


def test_gate_actually_blocks_bare_hash_id():
    """门禁自身不能空转：正反例都要验证。"""
    assert _line_has_bare_hash('return "<option>#" + esc(t.id);')
    assert _line_has_bare_hash('toast("备份记录 #" + rid, "success");')
    assert _line_has_bare_hash('notice(`记录 #${id} 检测完成`);')
    assert _line_has_bare_hash('f"任务 #{tid} 不存在"')
    assert _line_has_bare_hash('"<td>#" + esc(l.source_id) + "</td>"')
    # 合理豁免
    assert not _line_has_bare_hash('const el = $("#" + id);')
    assert not _line_has_bare_hash('<th>#</th>')
    assert not _line_has_bare_hash('a.href = "#";')
    assert not _line_has_bare_hash('color: #0D9488;')
    assert not _line_has_bare_hash('&#10;')
    assert not _line_has_bare_hash('title="任务 ID " + id + "：" + name')
    assert not _line_has_bare_hash('"来源备份记录 123（mysql）脱敏导出"')
