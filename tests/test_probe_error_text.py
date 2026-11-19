# -*- coding: utf-8 -*-
"""连接探测错误文本不得被硬截断。

现场反馈：任务表单「测试连接」提示显示不全，Oracle 报错被切在
``at host "192.168.204``（IP 中间）。根因是 probe 层用 ``[:160]`` 硬截断，
把尾部关键定位信息（host / port / ORA-xxxxx）丢掉。

这里锁定 ``_clip`` 的语义：短文本原样返回；长文本保留完整；极端超长时
保留头 + 尾并给出省略标记（尾部信息对排障最关键）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import probe  # noqa: E402

# 133 实测原文（oracledb thin 连接不存在服务名时的真实返回）
ORACLE_REAL = (
    'DPY-6005: cannot connect to database (CONNECTION_ID=rAonnLvXdlhwsz4yRRqRBQ==).\n'
    'DPY-6001: Service "ORCL_NOT_EXIST" is not registered with the listener at host '
    '"192.168.220.133" port 1521. (Similar to ORA-12514)'
)


def test_clip_keeps_short_text_untouched():
    s = "连接失败: (1045, \"Access denied for user 'root'@'10.0.0.1'\")"
    assert probe._clip(s) == s


def test_oracle_error_no_longer_cut_at_160():
    out = probe._clip(ORACLE_REAL)
    assert out == ORACLE_REAL.strip()
    # 早期 [:160] 会正好切在 '... at host "192.168.204' 附近
    assert 'port 1521' in out, "尾部端口信息被截断"
    assert 'ORA-12514' in out, "尾部 ORA 提示被截断"
    assert len(out) > 200


def test_clip_keeps_head_and_tail_for_huge_text():
    huge = "H" * 3000 + "TAIL-MARKER"
    out = probe._clip(huge)
    assert len(out) <= probe._ERR_MAX + 40
    assert out.startswith("H")
    assert out.endswith("TAIL-MARKER")
    assert "中间省略" in out


def test_clip_handles_empty_and_none():
    assert probe._clip(None) == ""
    assert probe._clip("   ") == ""


def test_probe_sources_have_no_hard_slice():
    """源码级防回归：probe.py 里不应再出现 [:120]/[:150]/[:160]/[:200] 这类硬截断。"""
    import re
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "probe.py")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    bad = []
    for i, line in enumerate(lines, 1):
        code = line.split("#", 1)[0]
        if re.search(r"\[:\s*(120|150|160|200|220)\]", code):
            bad.append((i, line.strip()))
    assert not bad, "存在硬截断：%s" % bad
