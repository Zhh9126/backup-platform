# -*- coding: utf-8 -*-
"""静态资源缓存戳门禁：改了 JS/CSS 忘了升 ?v= 会直接失败。

历轮用户反复反馈「页面没变化」的根因都是浏览器拿着旧脚本；
同一资源在不同模板里用不同戳还会造成重复下载 + 旧版覆盖新版。
"""
import datetime
import os
import re
from collections import defaultdict

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, "templates")
REF_RE = re.compile(r"(/static/[\w./-]+\.(?:js|css))\?v=(\d{8,10})")


def _refs():
    for tpl in sorted(os.listdir(TPL_DIR)):
        if not tpl.endswith(".html"):
            continue
        src = open(os.path.join(TPL_DIR, tpl), encoding="utf-8").read()
        for m in REF_RE.finditer(src):
            yield tpl, m.group(1), m.group(2)


def _mtime_date(asset):
    f = os.path.join(ROOT, asset.lstrip("/"))
    if not os.path.exists(f):
        return None
    return datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y%m%d")


def test_no_stale_cache_buster():
    """戳日期不得早于资源文件最后修改日期。"""
    if os.environ.get("AIDBM_SKIP_MTIME_CHECK"):
        pytest.skip("干净 checkout 会把 mtime 全部刷新，显式跳过")
    stale = []
    for tpl, asset, stamp in _refs():
        mt = _mtime_date(asset)
        if mt and stamp[:8] < mt:
            stale.append("%s -> %s?v=%s (mtime %s)" % (tpl, asset, stamp, mt))
    assert not stale, (
        "以下静态资源改过但缓存戳没升，用户浏览器会一直拿旧文件：\n"
        + "\n".join(stale[:30]))


def test_same_asset_uses_same_version_across_templates():
    """同一资源全站只能有一个版本号（operations.html 曾引用旧版 bkp-core）。"""
    versions = defaultdict(set)
    for _tpl, asset, stamp in _refs():
        versions[asset].add(stamp)
    conflicts = {k: sorted(v) for k, v in versions.items() if len(v) > 1}
    assert not conflicts, "同一资源存在多个缓存戳: %s" % conflicts


def test_core_scripts_are_referenced():
    """全站底座脚本不能被误删。"""
    base = open(os.path.join(TPL_DIR, "base.html"), encoding="utf-8").read()
    for asset in ("/static/js/i18n.js", "/static/js/bkp-core.js",
                  "/static/js/pager.js", "/static/js/app.js",
                  "/static/js/precheck.js"):
        assert asset + "?v=" in base, "base.html 缺少 %s" % asset


def test_gate_actually_scans_assets():
    """门禁自身不能空转。"""
    refs = list(_refs())
    assert len(refs) >= 15, "只扫到 %d 处静态引用，正则可能失效" % len(refs)
