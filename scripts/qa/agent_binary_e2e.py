"""一次性 Linux 客户端验收，目标必须尚未安装 aidbm-agent。"""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import paramiko
from core import db, agent_distribution as distribution, agent_hosts
from core.agent_client import AgentTransport


def main():
    db.init_schema()
    host = os.environ.get("AIDBM_TEST_SSH_HOST", "127.0.0.1")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username="root", password=os.environ["AIDBM_TEST_SSH_PASSWORD"],
                   allow_agent=False, look_for_keys=False, timeout=10)
    # 检查在清理 finally 之外，已有客户端时绝不进入验收或清理路径。
    clean = distribution._command(client, "test ! -e /opt/aidbm-agent && "
        "test ! -e /etc/aidbm-agent && test ! -e /etc/systemd/system/aidbm-agent.service && echo CLEAN")
    assert clean == "CLEAN"
    results = {}
    rollback_directory = None
    try:
        results["precheck"] = distribution.deploy(client, host, 18466, "precheck")
        results["stage"] = distribution.deploy(client, host, 18466, "stage")
        results["install"] = distribution.deploy(client, host, 18466, "install")
        assert results["install"]["ok"], results["install"]
        contents = distribution._command(client,
            "find /opt/aidbm-agent -mindepth 1 -maxdepth 1 -printf '%f\\n'").splitlines()
        assert contents == ["aidbm-agent"], contents
        record = agent_hosts.get_by_id(results["install"]["agent_id"])
        transport = AgentTransport(host, 18466, agent_hosts.get_token(record), timeout=10)
        try:
            caps = transport.capabilities(refresh=True)
            assert caps["distribution"] == "single-executable" and caps["enc"]
            command = transport.exec("printf 'BINARY_AGENT_OK'")
            assert command["rc"] == 0 and command["out"] == b"BINARY_AGENT_OK"
            local = Path(os.environ["TMPDIR"]) / "upload.bin"
            local.write_bytes(os.urandom(2 * 1024 * 1024 + 9))
            remote = "/tmp/aidbm-agent-binary-e2e.bin"
            down = local.with_name("download.bin")
            transport.file_put(str(local), remote)
            transport.file_get(remote, str(down))
            assert local.read_bytes() == down.read_bytes()
            transport.fs("remove", remote)
            results["binary_sdk"] = {
                "ok": True, "version": caps["version"], "aes": caps["enc"],
                "single_file": contents, "roundtrip_bytes": local.stat().st_size}
        finally:
            transport._reset()
        upgraded = distribution.deploy(client, host, 18466, "install")
        assert upgraded["ok"] and upgraded["agent_id"] == record["id"], upgraded
        results["upgrade"] = {"ok": True, "same_registration": True}

        import core.agent_client
        original = core.agent_client.AgentTransport
        class RefuseProbe:
            def __init__(self, *args, **kwargs):
                pass
            def capabilities(self, **kwargs):
                raise ValueError("injected probe failure")
            def _reset(self):
                pass
        core.agent_client.AgentTransport = RefuseProbe
        original_sleep = distribution.time.sleep
        distribution.time.sleep = lambda _: None
        try:
            rejected = distribution.deploy(client, host, 18467, "install")
            rollback_directory = rejected.get("rollback_directory")
            assert not rejected["ok"] and not rejected["rollback_errors"], rejected
        finally:
            core.agent_client.AgentTransport = original
            distribution.time.sleep = original_sleep
        assert agent_hosts.get_by_id(record["id"])["port"] == 18466
        restored = original(host, 18466, agent_hosts.get_token(record), timeout=10)
        try:
            for attempt in range(15):
                try:
                    assert restored.health()["version"] == caps["version"]
                    break
                except Exception:
                    if attempt == 14:
                        raise
                    time.sleep(.2)
        finally:
            restored._reset()
        results["rollback"] = {"ok": True, "old_port": 18466}
        (ROOT / "build/agent-test/live/result.json").write_text(json.dumps(results, indent=2))
        print(json.dumps({"single_file": contents, "binary_sdk": "passed",
                          "upgrade": "passed", "rollback": "passed"}))
    finally:
        distribution._command(client,
            "systemctl stop aidbm-agent 2>/dev/null || true; "
            "systemctl disable aidbm-agent 2>/dev/null || true; "
            "rm -f /etc/systemd/system/aidbm-agent.service /etc/aidbm-agent/agent.env "
            "/opt/aidbm-agent/aidbm-agent; systemctl daemon-reload; "
            "rmdir /opt/aidbm-agent /etc/aidbm-agent 2>/dev/null || true")
        if rollback_directory:
            for filename in ("binary", "unit", "environment"):
                distribution._command(client, f"rm -f -- {rollback_directory}/{filename}")
            distribution._command(client, f"rmdir -- {rollback_directory}")
        if results.get("precheck"):
            digest = results["precheck"]["package"]["sha256"]
            distribution._command(client, f"rm -f -- /var/lib/aidbm-agent/staging/{digest}")
        client.close()


if __name__ == "__main__":
    main()
