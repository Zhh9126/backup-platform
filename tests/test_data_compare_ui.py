# -*- coding: utf-8 -*-
"""数据对比页（Preact）：每页 10 条分页 + 报告明细弹窗回归。

现场缺陷（2026-11-11）：
1) 任务表 / 报告表全量渲染，几十行连分页条都没有；
2) 「明细」点击无反应 —— showReport 的 BKP.api 异常被吞（Unhandled
   Promise Rejection），且 ReportModal 关闭（ESC/遮罩）后 detail 未清空，
   组件不卸载，useEffect([]) 不会再执行 modal.show()。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = "static/js/data_compare_preact.js"
TPL = "templates/data_compare.html"


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_both_lists_are_paginated_ten_per_page():
    js = _src(JS)
    assert "const PAGE_SIZE = (window.Pager && window.Pager.PAGE_SIZE) || 10;" in js
    assert "function pageSlice(" in js
    assert "const PagerBar" in js
    assert "tasks=${taskPg.list}" in js
    assert "reports=${reportPg.list}" in js
    assert js.count("<${PagerBar} pg=") == 2
    assert "每页 ${pg.pageSize} 条" in js
    # 不再把全量数组直接喂给表格
    assert "tasks=${tasks}" not in js
    assert "reports=${reports}" not in js


def test_pagination_slice_is_bounded_and_converges():
    """纯函数契约：页码越界要收敛到最后一页。"""
    js = _src(JS)
    seg = js[js.index("function pageSlice("):js.index("function pageNums(")]
    assert "Math.min(Math.max(1, Number(page) || 1), pages)" in seg
    assert "rows.slice(start, start + ps)" in seg
    assert "Math.ceil(total / ps)" in seg


def test_report_detail_modal_closes_and_reopens():
    js = _src(JS)
    assert "getOrCreateInstance" in js
    assert 'addEventListener("hidden.bs.modal", onHidden)' in js
    assert "removeEventListener" in js
    # 不再写死 show class，显隐交给 Bootstrap 实例
    assert 'class="modal fade" id="dcReportModal"' in js
    assert 'class="modal fade show"' not in js
    # tables_json 可能是字符串（旧数据），必须兜底
    assert "Array.isArray(report.tables_json)" in js
    assert "Array.isArray(t.diffs)" in js


def test_show_report_errors_are_not_swallowed():
    js = _src(JS)
    seg = js[js.index("const showReport ="):js.index("const showReport =") + 800]
    assert "try {" in seg
    assert "catch (e)" in seg
    assert "加载报告异常" in seg
    assert "toast(" in seg


def test_template_stamp_and_preact_bootstrap():
    tpl = _src(TPL)
    assert 'id="dc-root"' in tpl
    assert '"preact": "/static/vendor/preact/preact.module.js"' in tpl
    m = re.search(r"data_compare_preact\.js\?v=(\d{8,10})", tpl)
    assert m and int(m.group(1)) >= 2026111108
    base = _src("templates/base.html")
    m2 = re.search(r"i18n\.js\?v=(\d{8,10})", base)
    assert m2 and int(m2.group(1)) >= 2026111108


def test_required_api_routes_exist():
    from app import create_app
    rules = {r.rule for r in create_app().url_map.iter_rules()}
    for suffix in ("/api/data-compare-tasks", "/api/data-compare-reports",
                   "/api/data-compare-reports/<int:report_id>",
                   "/api/data-compare-stats"):
        assert suffix in rules, suffix
