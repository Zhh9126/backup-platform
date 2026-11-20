# -*- coding: utf-8 -*-
"""Agent 执行通道 API：注册 / 列表 / 探活 / 删除 / 远端安装命令生成。"""
from flask import jsonify, request, send_file
from auth import permission_required

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
    if not isinstance(data, dict) or not isinstance(data.get("hostname", ""), str):
        return jsonify({"error": "注册信息需为对象，hostname 需为字符串"}), 400
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
    if not isinstance(data, dict):
        return jsonify({"error": "更新信息需为 JSON 对象"}), 400
    if not agent_hosts.get_by_id(aid):
        return jsonify({"error": "注册记录不存在"}), 404
    try:
        agent_hosts.update_host(aid, data)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@api_bp.route("/agents/<int:aid>", methods=["DELETE"])
def delete_agent(aid):
    agent_hosts.delete_host(aid)
    return jsonify({"ok": True})


@api_bp.route("/agents/<int:aid>/probe", methods=["POST"])
def probe_agent(aid):
    if not agent_hosts.get_by_id(aid):
        return jsonify({"ok": False, "error": "注册记录不存在"}), 404
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


@api_bp.get("/agents/packages")
def agent_packages():
    from core.agent_distribution import artifact
    packages = []
    for arch in ("x86_64", "aarch64"):
        try:
            _, metadata = artifact(arch)
            packages.append(metadata)
        except ValueError:
            continue
    return jsonify({"packages": packages})


@api_bp.get("/agents/package")
@permission_required("agent.use")
def agent_package_download():
    from core.agent_distribution import artifact
    try:
        path, metadata = artifact(request.args.get("arch", "x86_64"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    response = send_file(path, as_attachment=True, download_name="aidbm-agent",
                         mimetype="application/octet-stream", conditional=True)
    response.headers["X-Artifact-SHA256"] = metadata["sha256"]
    return response


@api_bp.get("/agents/<int:aid>/install-cmd")
@permission_required("agent.use")
def install_cmd(aid):
    """手工安装使用同一份二进制，不再下发 Python 源码或 loader。"""
    from core.agent_distribution import artifact, unit_text, BINARY, UNIT, ENVIRONMENT
    rec = agent_hosts.get_by_id(aid)
    if not rec:
        return jsonify({"error": "注册记录不存在"}), 404
    try:
        _, metadata = artifact(request.args.get("arch", "x86_64"))
        token = agent_hosts.get_token(rec)
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            raise ValueError("注册令牌格式非法")
        port = int(rec["port"])
        script = (
            "set -eu\numask 077\n"
            "test \"$(sha256sum /tmp/aidbm-agent | cut -d' ' -f1)\" = '" + metadata["sha256"] + "'\n"
            "install -d -m 750 /opt/aidbm-agent\n"
            "install -d -m 700 /etc/aidbm-agent\n"
            "install -m 700 /tmp/aidbm-agent " + BINARY + ".new\n"
            "printf '%s\\n' 'AIDBM_AGENT_TOKEN=" + token + "' > " + ENVIRONMENT + "\n"
            "cat > " + UNIT + " <<'UNIT_EOF'\n" + unit_text(port) + "UNIT_EOF\n"
            "chmod 600 " + ENVIRONMENT + " " + UNIT + "\n"
            "mv -f " + BINARY + ".new " + BINARY + "\n"
            "systemctl daemon-reload\nsystemctl enable aidbm-agent\n"
            "systemctl restart aidbm-agent\nsystemctl is-active aidbm-agent\n"
            "rm -f /opt/aidbm-agent/backup_agent.py /opt/aidbm-agent/agent_loader.py "
            "/opt/aidbm-agent/agent_payload.enc /tmp/aidbm-agent\n")
        return jsonify({"script": script, "package": metadata,
                        "package_url": "/api/agents/package?arch=" + metadata["arch"],
                        "instructions": "先下载单文件包并复制到目标机 /tmp/aidbm-agent，再执行脚本。升级建议使用远端安装的校验及回滚流程。"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@api_bp.post("/agents/remote-install")
def remote_install():
    """按预检、暂存、安装阶段部署单文件执行器。"""
    from core import ssh_hosts, agent_distribution
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "部署信息需为 JSON 对象"}), 400
    try:
        host_id = int(data.get("host_id") or 0)
        port = int(data.get("agent_port") or 8466)
        phase = data.get("phase") or "install"
        if not 1 <= port <= 65535 or phase not in ("precheck", "stage", "install"):
            raise ValueError("部署端口或阶段非法")
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "host_id、端口或部署阶段非法"}), 400
    host = ssh_hosts.get_host(host_id, include_secret=True)
    if not host:
        return jsonify({"ok": False, "error": "纳管主机不存在"}), 404
    client = None
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        options = dict(hostname=host["hostname"], port=int(host.get("port") or 22),
                       username=host.get("username") or "root", timeout=15,
                       allow_agent=False, look_for_keys=False)
        key = ssh_hosts.load_pkey(host.get("private_key") or "") if host.get("private_key") else None
        options.update({"pkey": key} if key is not None else {"password": host.get("password") or ""})
        client.connect(**options)
        result = agent_distribution.deploy(client, host["hostname"], port, phase)
        db = agent_distribution.db
        db.add_log("INFO" if result["ok"] else "WARNING", "agent-deploy",
                   f"Agent {host['hostname']} 阶段 {phase}，成功={result['ok']}")
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:300],
                        "hint": "请检查部署包、SSH、systemd 和目标版本兼容性"}), 502
    finally:
        if client:
            client.close()
