# -*- coding: utf-8 -*-
"""Agent 执行通道 API：注册 / 列表 / 探活 / 删除 / 远端安装命令生成。"""
import base64
import io
import os

from flask import jsonify, request

from core import agent_hosts
from . import api_bp


def _operator():
    from flask import session
    return session.get("username") or "system"


@api_bp.route("/agents", methods=["GET"])
def list_agents():
    return jsonify(agent_hosts.list_hosts())


@api_bp.route("/agents", methods=["POST"])
def create_agent():
    data = request.get_json(force=True, silent=True) or {}
    hostname = (data.get("hostname") or "").strip()
    if not hostname:
        return jsonify({"error": "hostname 必填"}), 400
    try:
        aid = agent_hosts.create_host(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        if "UNIQUE" in str(e):
            return jsonify({"error": "该主机已注册 Agent"}), 409
        return jsonify({"error": str(e)[:200]}), 500
    rec = agent_hosts.get_by_id(aid)
    # 注册即探活
    p = agent_hosts.probe(aid)
    return jsonify({
        "id": aid,
        "token": agent_hosts.get_token(rec),  # 仅注册响应返回一次明文
        "probe": p,
    })


@api_bp.route("/agents/<int:aid>", methods=["PUT"])
def update_agent(aid):
    data = request.get_json(force=True, silent=True) or {}
    agent_hosts.update_host(aid, data)
    return jsonify({"ok": True})


@api_bp.route("/agents/<int:aid>", methods=["DELETE"])
def delete_agent(aid):
    agent_hosts.delete_host(aid)
    return jsonify({"ok": True})


@api_bp.route("/agents/<int:aid>/probe", methods=["POST"])
def probe_agent(aid):
    p = agent_hosts.probe(aid)
    if not p.get("ok"):
        p["hint"] = ("目标机 Agent 端口未监听：请检查 systemctl is-active "
                     "aidbm-agent、服务日志与防火墙，或重新一键安装执行器。")
    return jsonify(p), (200 if p.get("ok") else 502)


@api_bp.route("/agents/<int:aid>/capabilities", methods=["GET"])
def agent_capabilities(aid):
    """备份插件能力自报 + 平台侧传输协商结论（只读，供页面展示）。"""
    rec = agent_hosts.get_by_id(aid)
    if not rec:
        return jsonify({"error": "注册记录不存在"}), 404
    from core.agent_client import AgentTransport
    t = AgentTransport(rec["hostname"], rec["port"],
                       agent_hosts.get_token(rec), timeout=8,
                       label="agent:%s" % rec["hostname"])
    try:
        caps = t.capabilities(refresh=True)
        return jsonify({"ok": True, "caps": caps,
                        "negotiation": t.negotiation()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502
    finally:
        t._reset()


@api_bp.route("/agents/<int:aid>/install-cmd", methods=["GET"])
def install_cmd(aid):
    """生成在数据库服务器上执行的一键安装命令（黑盒：loader + 加密载荷，
    明文源码不出平台）。"""
    rec = agent_hosts.get_by_id(aid)
    if not rec:
        return jsonify({"error": "注册记录不存在"}), 404
    token = agent_hosts.get_token(rec)
    from core import agent_payload
    agent_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agent", "backup_agent.py")
    port = int(rec.get("port") or 8466)
    loader_py, payload_bytes = agent_payload.build_payload(
        open(agent_src, encoding="utf-8").read(), token)
    payload_b64 = base64.b64encode(payload_bytes).decode()
    loader_b64 = base64.b64encode(loader_py.encode()).decode()
    remote_sh = (
        "mkdir -p /opt/aidbm-agent && "
        "echo '{LOADER_B64}' | base64 -d > /opt/aidbm-agent/{LOADER_NAME} && "
        "echo '{PAYLOAD_B64}' | base64 -d > /opt/aidbm-agent/{PAYLOAD_NAME} && "
        "rm -f /opt/aidbm-agent/backup_agent.py && "
        "chmod 700 /opt/aidbm-agent/{LOADER_NAME} && "
        "chmod 600 /opt/aidbm-agent/{PAYLOAD_NAME} && "
        "cat > /etc/systemd/system/aidbm-agent.service <<'UNIT_EOF'\n"
        "[Unit]\nDescription=AIDBM Backup Agent\nAfter=network.target\n\n"
        "[Service]\nExecStart=/usr/bin/python3 /opt/aidbm-agent/{LOADER_NAME} "
        " --token {TOKEN} --port {PORT}\nRestart=always\n"
        "RestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n"
        "UNIT_EOF\n"
        "systemctl daemon-reload && systemctl enable aidbm-agent && "
        "systemctl restart aidbm-agent && "
        "sleep 1 && systemctl is-active aidbm-agent\n")
    script = (remote_sh
              .replace("{LOADER_B64}", loader_b64)
              .replace("{PAYLOAD_B64}", payload_b64)
              .replace("{LOADER_NAME}", agent_payload.LOADER_NAME)
              .replace("{PAYLOAD_NAME}", agent_payload.PAYLOAD_NAME)
              .replace("{TOKEN}", token).replace("{PORT}", str(port)))
    return jsonify({"script_file": "/tmp/aidbm_install_agent.sh",
                    "script": script})


@api_bp.route("/agents/remote-install", methods=["POST"])
def remote_install():
    """一键把备份执行器插件安装到目标主机。

    body: {"host_id": 已纳管 SSH 主机 id, "agent_port": 可选，默认 8466}
    流程：取纳管凭据 → 探测目标机 Python/依赖 → SFTP 推送零依赖载荷 →
    启动并校验 systemd 退出码 → 轮询探活 → 成功才启用注册记录。

    返回拆成 install / probe 两段状态；任一失败都回滚注册记录，
    避免留下 enabled=1 却连不上的僵尸执行器。
    """
    import time
    data = request.get_json(force=True, silent=True) or {}
    host_id = int(data.get("host_id") or 0)
    agent_port = int(data.get("agent_port") or 8466)
    from core import ssh_hosts
    hp = ssh_hosts.get_host(host_id, include_secret=True)
    if not hp:
        return jsonify({"error": "纳管主机不存在"}), 404
    hostname = hp.get("hostname") or ""
    if not hostname:
        return jsonify({"error": "纳管主机缺少 hostname"}), 400

    existing = agent_hosts.get_by_host(hostname)
    created = existing is None
    prev = dict(existing) if existing else None
    if existing:
        agent_id = int(existing["id"])
        agent_hosts.update_host(agent_id, {"port": agent_port, "enabled": 0})
    else:
        agent_id = agent_hosts.create_host({
            "name": hp.get("name") or hostname, "hostname": hostname,
            "port": agent_port, "transport": "auto", "enabled": 0})
    rec = agent_hosts.get_by_id(agent_id)
    token = agent_hosts.get_token(rec)

    result = {"ok": False, "agent_id": agent_id,
              "install": {"ok": False}, "probe": {"ok": False}}

    def _rollback():
        """安装未成功：新建记录删除；升级记录恢复原有状态。"""
        try:
            if created:
                agent_hosts.delete_host(agent_id)
                result["rolled_back"] = "新建注册记录已清理"
            elif prev:
                agent_hosts.update_host(agent_id, {
                    "enabled": prev.get("enabled", 1),
                    "port": prev.get("port", 8466)})
                result["rolled_back"] = "已恢复原有注册状态"
        except Exception as e:
            result["rollback_error"] = str(e)[:120]

    def _hint(text=""):
        return ("目标机 %d 端口未就绪：请在目标机执行 "
                "systemctl status aidbm-agent 与 journalctl -u aidbm-agent -n 30 "
                "查看服务日志，并核对防火墙/SELinux。离线环境若曾报缺 cryptography，"
                "重新执行本安装即可下发零第三方依赖载荷。%s"
                % (agent_port, ("（" + text[:120] + "）") if text else ""))

    try:
        import paramiko
        from core.ssh_hosts import load_pkey
        from core import agent_payload
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=hp["hostname"], port=int(hp.get("port") or 22),
                  username=hp.get("username") or "root", timeout=15,
                  allow_agent=False, look_for_keys=False)
        pkey = load_pkey(hp.get("private_key") or "") \
            if hp.get("private_key") else None
        if pkey is not None:
            kw["pkey"] = pkey
        else:
            kw["password"] = crypto_decrypt(hp)
        c.connect(**kw)

        # 目标机 Python 决定 systemd ExecStart；缺 cryptography 时下发
        # v2 纯标准库载荷，保证离线/零安装目标机也能启动。
        _cn, _po, _ = c.exec_command(
            "for v in python3.13 python3.12 python3.11 python3.10 "
            "python3.9 python3.8 python3.7; do command -v $v && break; done; "
            "command -v python3", timeout=20)
        _py = (_po.read().decode(errors="replace").strip().splitlines()
               or ["/usr/bin/python3"])[0].strip() or "/usr/bin/python3"
        _cn2, _po2, _ = c.exec_command(
            "%s -c 'import cryptography' 2>/dev/null && echo AES_OK || echo NO_AES"
            % _py, timeout=20)
        has_aes = "AES_OK" in _po2.read().decode(errors="replace")

        agent_src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "agent", "backup_agent.py")
        with io.open(agent_src_path, encoding="utf-8") as f:
            agent_src = f.read()
        loader_py, payload_bytes = agent_payload.build_payload(
            agent_src, token, prefer_aes=has_aes)

        sftp = c.open_sftp()
        try:
            try:
                sftp.mkdir("/opt/aidbm-agent")
            except IOError:
                pass
            with sftp.open("/opt/aidbm-agent/" + agent_payload.LOADER_NAME,
                           "w") as f:
                f.write(loader_py)
            with sftp.open("/opt/aidbm-agent/" + agent_payload.PAYLOAD_NAME,
                           "wb") as f:
                f.write(payload_bytes)
            try:
                sftp.remove("/opt/aidbm-agent/backup_agent.py")
            except IOError:
                pass
            unit = (
                "[Unit]\nDescription=AIDBM Backup Agent\n"
                "After=network.target\n\n[Service]\n"
                "ExecStart=%s /opt/aidbm-agent/%s --token %s --port %d\n"
                "Restart=always\nRestartSec=3\n\n"
                "[Install]\nWantedBy=multi-user.target\n"
                % (_py, agent_payload.LOADER_NAME, token, agent_port))
            with sftp.open("/etc/systemd/system/aidbm-agent.service", "w") as f:
                f.write(unit)
        finally:
            sftp.close()

        cmd = (
            "chmod 700 /opt/aidbm-agent/%s && chmod 600 /opt/aidbm-agent/%s && "
            "systemctl daemon-reload && systemctl enable aidbm-agent "
            "&& systemctl restart aidbm-agent "
            "&& sleep 1 && systemctl is-active aidbm-agent"
            % (agent_payload.LOADER_NAME, agent_payload.PAYLOAD_NAME))
        _chn, _o, _e = c.exec_command(cmd, timeout=60)
        out = _o.read().decode(errors="replace").strip()
        err = _e.read().decode(errors="replace").strip()
        rc = _chn.channel.recv_exit_status()
        service_state = out.strip().splitlines()[-1] if out.strip() else ""
        diagnostics = ""
        if rc != 0 or service_state != "active":
            _dch, _do, _ = c.exec_command(
                "systemctl status --no-pager -l aidbm-agent 2>&1 | tail -18; "
                "echo ===JOURNAL===; "
                "journalctl -u aidbm-agent -n 20 --no-pager 2>&1 | tail -20",
                timeout=25)
            diagnostics = _do.read().decode(errors="replace").strip()[:2500]
        c.close()

        install_ok = rc == 0 and service_state == "active"
        result["install"] = {
            "ok": install_ok, "rc": rc, "service": service_state,
            "stderr": err[:300], "diagnostics": diagnostics,
            "python": _py,
            "payload_format": "AES-GCM" if has_aes else "HMAC-SHA256",
        }
        # 服务刚起时端口可能尚未就绪：轮询探活，不再一次判定就判死
        probe = {"ok": False, "error": "未执行"}
        for _ in range(8):
            probe = agent_hosts.probe(agent_id)
            if probe.get("ok"):
                break
            time.sleep(1)
        result["probe"] = probe
        result["ok"] = bool(install_ok and probe.get("ok"))
        if result["ok"]:
            agent_hosts.update_host(agent_id, {"enabled": 1})
        else:
            _rollback()
            result["hint"] = _hint(
                "" if install_ok else (diagnostics or err or "服务启动失败"))
            result["message"] = ("执行器服务启动或探活未通过，"
                                 "详见 install / probe 分段结果与 hint")
        return jsonify(result)
    except Exception as e:
        _rollback()
        result["error"] = str(e)[:300]
        result["message"] = "执行器安装异常：" + str(e)[:200]
        result["hint"] = _hint(str(e))
        return jsonify(result), 502


def crypto_decrypt(hp: dict) -> str:
    # get_host(include_secret=True) 已解密（ssh_hosts.py 内部统一处理）
    return hp.get("password") or ""
