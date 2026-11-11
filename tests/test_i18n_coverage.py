# -*- coding: utf-8 -*-
"""i18n 门禁：英文模式下不允许出现未翻译的中文 UI 文案。

新增功能时若往模板里写了新中文而忘了在 static/js/i18n.js 补英文词条，
本测试会直接失败并列出漏翻文案，避免「每次切换英文都有中文残留」。

判定口径与 i18n.js 一致：EN 词典精确匹配 / PREFIXES 前缀匹配；
含服务端插值的动态文案由 i18n.js 的 PATTERNS 正则覆盖（此处显式白名单）。
刻意保留中文的项（语言切换标签、示例脚本）也在此白名单。
"""
import json
import os
import re
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N_JS = os.path.join(ROOT, "static", "js", "i18n.js")
TPL_DIR = os.path.join(ROOT, "templates")
CJK = re.compile(r"[\u4e00-\u9fff]")
JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
SKIP_TAGS = {"script", "style", "code", "pre", "textarea", "noscript", "template"}
ATTRS = ("title", "placeholder", "data-i18n", "data-i18n-ph", "aria-label")

# 由 PATTERNS 正则覆盖（渲染后含数字/客户名），Python 不复现 JS 正则
PATTERN_COVERED_PREFIXES = (
    "已激活永久商业授权（客户: ",
    "社区免费版（每类受控功能最多纳管 ",
    "v100 · AIDBM · ",
    "每类限额 ",
    "未激活商业授权 · 各项备份任务与网络设备每类上限为 ",
)
# 刻意保留中文：语言切换标签、textarea 中的示例脚本内容
KEEP_PREFIXES = ("中文 / English", "#!/bin/bash")
# PATTERNS 中数值型动态文案（Python 不复现 JS 正则，用等价 Python 正则声明）
PATTERN_COVERED_RES = (r"^共 \d+ 个会话$",)
_STAR_RE = re.compile(r"^(.*?)\s*\*+$")


def _is_covered(text, keys, prefixes):
    if text in keys:
        return True
    star = _STAR_RE.match(text)       # i18n.lookup 的「必填 *」星号归一
    if star and star.group(1) in keys:
        return True
    if any(text.startswith(p) for p in prefixes):
        return True
    if any(text.startswith(p) for p in PATTERN_COVERED_PREFIXES):
        return True
    if any(text.startswith(p) for p in KEEP_PREFIXES):
        return True
    return any(re.match(rx, text) for rx in PATTERN_COVERED_RES)


def _norm(s):
    return JINJA_EXPR.sub("100", s).strip()


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = set()
        self.skip = 0

    def _add(self, v):
        if not v or not CJK.search(v) or v.strip().startswith("{%"):
            return
        s = _norm(v)
        if s and CJK.search(s):
            self.items.add(s)

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        for k, v in attrs:
            if k in ATTRS:
                self._add(v)

    def handle_startendtag(self, tag, attrs):
        for k, v in attrs:
            if k in ATTRS:
                self._add(v)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip:
            return
        raw = data.strip()
        if raw.startswith("{%"):
            return
        self._add(raw)


def _scan_templates():
    items = set()
    for name in sorted(os.listdir(TPL_DIR)):
        if not name.endswith(".html"):
            continue
        c = _Collector()
        c.feed(open(os.path.join(TPL_DIR, name), encoding="utf-8").read())
        items.update(c.items)
    return items


def _parse_i18n():
    """返回 (EN 键集合, EN 值列表, PREFIXES 列表)。"""
    src = open(I18N_JS, encoding="utf-8").read()
    keys, values, prefixes = set(), [], []
    # 不限定行首：词典既有 var EN = { ... } 多行形式，也有
    # Object.assign(EN, { "k": "v" }); 单行补丁形式
    key_re = re.compile(r'"((?:\\.|[^"\\])*)":\s*"((?:\\.|[^"\\])*)"')
    for m in key_re.finditer(src):
        try:
            k = json.loads('"%s"' % m.group(1))
            v = json.loads('"%s"' % m.group(2))
        except Exception:
            continue
        keys.add(k)
        values.append((k, v))
    pm = re.search(r"var PREFIXES\s*=\s*\[(.*?)\];", src, re.S)
    if pm:
        for m in re.finditer(r'"((?:\\.|[^"\\])*)"', pm.group(1)):
            try:
                prefixes.append(json.loads('"%s"' % m.group(1)))
            except Exception:
                pass
    return keys, values, prefixes


def test_all_visible_chinese_has_english_translation():
    keys, _values, prefixes = _parse_i18n()
    assert len(keys) > 1000, "i18n 词典解析异常，仅解析到 %d 条" % len(keys)
    uncovered = []
    for text in sorted(_scan_templates()):
        if not _is_covered(text, keys, prefixes):
            uncovered.append(text)
    assert not uncovered, (
        "英文模式会残留中文（%d 条）。请在 static/js/i18n.js 的 EN 词典补英文词条，"
        "或为动态文案加 PATTERNS 正则：\n%s" % (len(uncovered), "\n".join(uncovered[:30])))


def test_english_translations_contain_no_chinese():
    """历史缺陷回归：英文译文里混入中文（如 'no partial success包装'）。"""
    _keys, values, _prefixes = _parse_i18n()
    bad = [(k, v) for k, v in values if CJK.search(v)]
    assert not bad, "以下英文译文仍含中文：\n%s" % "\n".join(
        "%r -> %r" % (k[:60], v[:80]) for k, v in bad[:20])


def test_gate_actually_blocks_new_untranslated_text():
    """门禁自身不能空转：已知未翻译串必须被判定为未覆盖。"""
    keys, _values, prefixes = _parse_i18n()
    assert not _is_covered("全新未翻译功能标签ZZZ", keys, prefixes)
    assert _is_covered("仪表盘", keys, prefixes)


def test_i18n_cache_buster_is_current():
    """改了 i18n.js 必须同步升 base.html 的 ?v= 缓存戳，否则用户浏览器拿旧词典。"""
    base = open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8").read()
    m = re.search(r"i18n\.js\?v=(\d+)", base)
    assert m, "base.html 未引用带版本号的 i18n.js"
    assert int(m.group(1)) >= 2026111101, (
        "i18n.js 已更新但缓存戳仍为 %s，请同步升级" % m.group(1))
