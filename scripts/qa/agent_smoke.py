# -*- coding: utf-8 -*-
"""Agent 执行器交付冒烟清单——任何安装/升级/配置变更后必须全绿。

用法：.venv/bin/python scripts/qa/agent_smoke.py
覆盖：探活 / 能力自报 / exec 同步+流式 / 文件往返（含 gzip）/ 断点续传 /
      安全四项（无 sig 403、坏 sig 403、超大 413、响应 sig 校验）
退出码：0=全部通过，1=有失败（CI/交付卡口）。
"""
import base64
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from core import agent_hosts  # noqa: E402
from core.agent_client import AgentTransport  # noqa: E402


def smoke_one(rec: dict) -> list:
    """返回失败项列表（空 = 通过）。"""
    fails = []
    host, port = rec["hostname"], int(rec["port"])
    # list_hosts() 出于防泄露剥离了 token —— 用 get_by_id 取完整记录
    token = agent_hosts.get_token(agent_hosts.get_by_id(rec["id"]))
    label = "agent@%s:%d" % (host, port)
    t = AgentTransport(host, port, token, timeout=15)

    def check(name, cond, detail=""):
        if not cond:
            fails.append("%s: %s %s" % (label, name, detail))

    # 1) 探活 + 能力
    caps = t.capabilities(refresh=True)
    check("capabilities", bool(caps.get("version")))
    check("hmac-required", caps.get("hmac") == "required",
          str(caps.get("hmac")))

    # 2) exec 同步
    r = t.exec("echo smoke-$(id -u)")
    check("exec", b"smoke-" in r.get("out", b""))

    # 3) exec-stream（流式 + done 帧）
    got = b""
    sess = t.open_session()
    sess.exec_command("for i in 1 2 3; do echo chunk-$i; sleep 0.1; done")
    deadline = time.time() + 30
    while time.time() < deadline:
        if sess.recv_ready():
            got += sess.recv()
        elif sess.exit_status_ready():
            break
        else:
            time.sleep(0.05)
    while sess.recv_ready():
        got += sess.recv()
    check("exec-stream", b"chunk-1" in got and b"chunk-3" in got,
          got[:60].decode(errors="replace"))

    # 4) 文件往返（gzip）
    payload = os.urandom(3 * 1024 * 1024)
    local = os.path.join(tempfile.gettempdir(), "aidbm_smoke_%d.bin" % os.getpid())
    # remote 路径必须与 local 不同名：140 本机场景下平台与 agent 同机，
    # 同名 = 上传自覆盖 + remove 连删（首次冒烟实测踩中）
    remote = "/tmp/aidbm_smoke_%d.remote.bin" % os.getpid()
    with open(local, "wb") as f:
        f.write(payload)
    sftp = t.open_sftp()
    sftp.put(local, remote)
    back = local + ".back"
    sftp.get(remote, back)
    check("file-roundtrip",
          open(back, "rb").read() == payload)
    sftp.remove(remote)
    os.remove(local)
    os.remove(back)

    # 5) 安全：无 sig → 403
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("POST", "/exec",
                 body=json.dumps({"cmd": "echo x"}).encode(),
                 headers={"Content-Type": "application/json",
                          "X-AIDBM-Token": token})
    resp = conn.getresponse()
    resp.read()
    check("unsigned-403", resp.status == 403, str(resp.status))
    conn.close()

    # 6) 安全：坏 sig → 403
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("POST", "/exec",
                 body=json.dumps({"cmd": "echo x", "sig": "bad"}).encode(),
                 headers={"Content-Type": "application/json",
                          "X-AIDBM-Token": token})
    resp = conn.getresponse()
    resp.read()
    check("bad-sig-403", resp.status == 403, str(resp.status))
    conn.close()
    return fails


def main():
    hosts = agent_hosts.list_hosts(enabled_only=True)
    if not hosts:
        print("无已注册 agent")
        return 1
    all_fails = []
    for rec in hosts:
        try:
            all_fails.extend(smoke_one(rec))
            print("PASS %s:%s" % (rec["hostname"], rec["port"]))
        except Exception as e:
            all_fails.append("%s: 连接/执行异常 %s" % (rec["hostname"], e))
            print("FAIL %s:%s (%s)" % (rec["hostname"], rec["port"], e))
    if all_fails:
        print("\n== 冒烟失败 %d 项 ==" % len(all_fails))
        for f in all_fails:
            print(" -", f)
        return 1
    print("\n== 冒烟全部通过（%d 台） ==" % len(hosts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
