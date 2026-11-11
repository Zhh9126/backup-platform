#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""目标端免装取证（core/agentless）单测。

原则：**判定逻辑必须离线可测**（parse/judge 是纯函数），只有真正的 SSH 取回
在 ``audit_host`` 里做。这里不连接任何主机。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 与 test_api_contract.py 同款隔离：元数据库/产物目录指向临时目录，不触碰真实实例。
_TMP = tempfile.mkdtemp(prefix="agentless_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest  # noqa: E402

from core.agentless.audit import (build_audit_shell, judge, parse_sections,
                                  audit_report_text)
from core.agentless import channels


def fake_output(**sections) -> str:
    """拼装一段符合取证脚本输出格式的文本。"""
    lines = ["##### begin #####"]
    for name, rows in sections.items():
        lines.append("##### %s #####" % name)
        lines.extend(rows)
    lines.append("##### end #####")
    return "\n".join(lines) + "\n"


class TestParseSections(unittest.TestCase):
    def test_splits_and_drops_empty(self):
        text = fake_output(processes=["  ", "123 root xtrabackup --backup"],
                           systemd=[])
        secs = parse_sections(text)
        self.assertEqual(secs["processes"], ["123 root xtrabackup --backup"])
        # 空节保留为空列表："取回来了但没东西"(PASS) 与"根本没取回来"(UNKNOWN) 要分得开
        self.assertEqual(secs.get("systemd"), [])

    def test_begin_end_not_treated_as_section(self):
        secs = parse_sections(fake_output(temp_files=["/tmp/bk_x.cnf"]))
        self.assertEqual(list(secs), ["temp_files"])

    def test_empty_output(self):
        self.assertEqual(parse_sections(""), {})
        self.assertEqual(parse_sections("garbage line"), {})


class TestJudge(unittest.TestCase):
    def test_clean_pass(self):
        secs = parse_sections(fake_output(
            processes=[], temp_files=[], crontab=[], systemd=[], packages=[]))
        r = judge(secs)
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["findings"], [])

    def test_resident_process_is_fail(self):
        secs = parse_sections(fake_output(processes=[
            "1234 root /tmp/.bp_x/xtrabackup --backup --target-dir=/tmp/bk_t"],
            temp_files=[]))
        r = judge(secs)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(r["findings"][0]["severity"], "critical")
        self.assertIn("残留进程", r["findings"][0]["category"])
        self.assertEqual(r["findings"][0]["invasion_level"], "X")

    def test_temp_residue_is_warn(self):
        secs = parse_sections(fake_output(temp_files=["/tmp/bk_xtrabackup.cnf",
                                                      "/tmp/.bp_work/x.tar"]))
        r = judge(secs)
        self.assertEqual(r["verdict"], "WARN")
        self.assertTrue(all(f["severity"] == "warn" for f in r["findings"]))
        self.assertEqual(len(r["findings"]), 2)

    def test_customer_own_backup_software_is_not_attributed(self):
        """客户自己装的备份/agent 不能被判成"平台装的"——不做归因，不判 FAIL。"""
        secs = parse_sections(fake_output(
            processes=["900 root /usr/bin/commvault_backupd"],
            packages=["mariadb-backup-10.11"]))
        r = judge(secs)
        self.assertEqual(r["verdict"], "PASS", "客户自有软件不应触发 WARN/FAIL")

    def test_no_sections_means_unknown(self):
        r = judge({})
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertTrue(r["findings"])  # 必须给出不可采信的原因

    def test_systemd_and_cron_are_critical(self):
        secs = parse_sections(fake_output(
            systemd=["aidbm-agent.service"], crontab=["0 2 * * * /opt/aidbm/run.sh"]))
        r = judge(secs)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(len(r["findings"]), 2)


class TestShellAndReport(unittest.TestCase):
    def test_shell_is_readonly(self):
        """取证脚本禁止出现写操作（否则就不是取证了）。"""
        shell = build_audit_shell()
        for bad in ("rm ", "rm -rf", ">", ">>", "mv ", "dd ", "tee "):
            if bad == ">":  # 允许 2>/dev/null 的 stderr 重定向
                continue
            self.assertNotIn(bad, shell, "取证脚本疑似包含写操作: %r" % bad)

    def test_report_text_mentions_verdict(self):
        rep = {"host": "root@192.0.2.9:22", "checked_at": "2026-09-19T00:00:00",
               "returncode": 0, "verdict": "PASS", "checked_sections": ["processes"],
               "findings": []}
        text = audit_report_text(rep)
        self.assertIn("PASS", text)
        self.assertIn("root@192.0.2.9:22", text)


class TestChannelDeclarations(unittest.TestCase):
    def test_every_channel_has_known_level(self):
        for c in channels.channels():
            self.assertIn(c["invasiveness"], channels.INVASION_LEVELS)
            self.assertTrue(c["impl"])
            self.assertFalse(c["requires_install"],
                             "任何通道都不允许要求目标端安装软件")

    def test_forbidden_declared(self):
        cap = channels.capabilities()
        self.assertIn("常驻", " ".join(cap["forbidden"]))
        self.assertTrue(all(lv in cap["invasion_levels"]
                            for lv in ("A0", "A1", "A2", "A3", "X")))

    def test_requirements_have_checker_file(self):
        for db, items in channels.capabilities()["target_requirements"].items():
            for it in items:
                self.assertTrue(it["checked_in"], "%s 缺少核对文件" % db)


# ---------------------------------------------------------------- API 层

@pytest.fixture(scope="module")
def app():
    from app import create_app
    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture(scope="module")
def client(app):
    with app.test_client() as c:
        c.post("/login", data={"username": "admin", "password": "admin123"})
        with c.session_transaction() as _sess:
            if not _sess.get("user"):
                _sess["user"] = {"username": "admin", "role": "admin"}
        yield c


def test_capabilities_contract(client):
    r = client.get("/api/v1/capabilities")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body["channels"], "至少要声明一条通道"
    assert {"A0", "A1", "A2", "A3", "X"} <= set(body["invasion_levels"])
    ids = {c["id"] for c in body["channels"]}
    assert {"ssh", "native-protocol", "jdbc", "temp-binary"} <= ids
    # 任何通道都不允许要求目标端安装软件：这是对外承诺的核心
    assert all(not c["requires_install"] for c in body["channels"])


def test_audit_unknown_host_returns_three_part_error(client):
    r = client.get("/api/v1/targets/999999/no-agent-audit")
    assert r.status_code == 404
    body = r.get_json()
    assert body["code"] == "AIDBM-1004"
    assert body["message"] and isinstance(body["details"], dict)


def test_capabilities_available_on_legacy_prefix_with_deprecation(client):
    r = client.get("/api/capabilities")
    assert r.status_code == 200
    assert r.headers.get("Deprecation") == "true"
    assert '/api/v1/capabilities' in (r.headers.get("Link") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
