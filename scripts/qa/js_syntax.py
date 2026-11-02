# -*- coding: utf-8 -*-
"""前端 JS 语法卡口：任何 .js 改动后交付前必须全绿。

esprima 只到 ES2018，代码中的可选链(?. / ?.[)与空值合并(??)在校验前
做等价归一（仅为通过解析，不影响交付产物）。
用法：.venv/bin/python scripts/qa/js_syntax.py
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))  # venv/esprima
os.chdir(os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

try:
    import esprima
except ImportError:
    print("pip install esprima 后运行")
    sys.exit(2)

bad = []
files = sorted(glob.glob("static/js/**/*.js", recursive=True))
for f in files:
    if "bootstrap.bundle" in f or "preact" in f or ".min.js" in f:
        continue
    src = open(f, encoding="utf-8").read()
    test = re.sub(r"\?\.\[", "[", src)
    test = re.sub(r"\?\.", " .", test)
    test = re.sub(r"\?\?", "||", test)
    test = re.sub(r"\?\(", " (", test)
    try:
        esprima.parseScript(test)
        print("OK  ", f)
    except Exception as e:
        bad.append((f, str(e)[:120]))
        print("FAIL", f, str(e)[:120])
if bad:
    print("\n== %d 个文件语法错误，禁止交付 ==" % len(bad))
    sys.exit(1)
print("\n== 前端语法全部通过（%d 个文件） ==" % len(files))
