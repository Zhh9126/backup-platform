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
    """备份插件能力自报 + 平台侧传输协商结论 + 能力矩阵（只读，供页面展示）。"""
    rec = agent_hosts.get_by_id(aid)
    if not rec:
        return jsonify({"error": "注册记录不存在"}), 404
    from core.agent_client import AgentTransport
    t = AgentTransport(rec["hostname"], rec["port"],
                       agent_hosts.get_token(rec), timeout=8,
                       label="agent:%s" % rec["hostname"])
    try:
        caps = t.capabilities(refresh=True)
        matrix = agent_hosts.save_capabilities(aid, caps)
        return jsonify({"ok": True, "caps": caps, "matrix": matrix,
                        "negotiation": t.negotiation()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502
    finally:
        t._reset()


# --------------------------------------------------------------------------- #
# Agent 自更新：装一次即可长期跟随平台版本
# --------------------------------------------------------------------------- #
def _detect_outbound_ip() -> str:
    """探测平台自身出口 IP。

    只做 UDP 路由选路（connect 不发送任何报文），因此在完全离线的内网环境
    同样有效，不会引入任何外网依赖。
    """
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.3)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def _platform_url_for_agent(request, override=""):
    """Agent 回连平台的地址（自更新用）。

    优先级：

    1. 调用方显式传入（install/remote-install 的 platform_url 参数）；
    2. 系统设置里的「Agent 回连平台地址」（system_config: agent_platform_url）；
    3. 本次请求的来源主机（非回环时最准确）；
    4. 平台自身出口 IP + Web 端口。

    踩坑：浏览器可能就在平台机上打开页面，此时 request.host 是 127.0.0.1，
    目标机上的 Agent 回连 127.0.0.1 会连到**它自己**——必须换成目标机可达的
    地址，否则自更新必然「Connection refused」。
    """
    if override:
        return str(override).strip().rstrip("/")
    try:
        from core import db as _db
        cfg = (_db.get_system_config("agent_platform_url", "") or "").strip()
    except Exception:
        cfg = ""
    if cfg:
        return cfg.rstrip("/")
    host = (request.host or "").split(":")[0]
    if host and host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return request.host_url.rstrip("/")
    ip = _detect_outbound_ip()
    if ip:
        import config as _cfg
        return "http://%s:%d" % (ip, int(_cfg.WEB_PORT))
    return request.host_url.rstrip("/")


def _agent_token_from_request() -> str:
    """取 Agent 自更新用的注册令牌（查询参数或请求头均可）。"""
    token = (request.args.get("token") or "").strip()
    if not token:
        token = (request.headers.get("X-Aidbm-Agent-Token") or "").strip()
    return token


def _find_agent_by_token(token: str) -> dict:
    """按注册令牌反查 Agent 记录（令牌加密存储，逐条解密比对）。"""
    if not token:
        return {}
    from core import db as _db
    for row in _db.query("SELECT * FROM agent_hosts ORDER BY id") or []:
        try:
            plain = agent_hosts.get_token(row)
        except Exception:
            plain = ""
        if plain and plain == token:
            return dict(row)
    return {}


@api_bp.route("/agents/platform-url", methods=["GET", "PUT"])
def agent_platform_url():
    """Agent 回连平台的地址（自更新用，留空=自动探测平台出口 IP）。

    浏览器在平台机上打开时请求来源是 127.0.0.1，目标机上的 Agent 回连
    127.0.0.1 会连到自己——因此这里允许显式指定一个目标机可达的地址。
    """
    from core import db as _db
    try:
        if request.method == "PUT":
            data = request.get_json(force=True, silent=True) or {}
            value = str((data.get("platform_url") or "")).strip().rstrip("/")
            if value and not value.startswith(("http://", "https://")):
                return jsonify({"error": "平台地址需以 http:// 或 https:// 开头"}), 400
            _db.set_system_config("agent_platform_url", value)
            return jsonify({"ok": True, "platform_url": value})
        return jsonify({"platform_url": _db.get_system_config("agent_platform_url", "") or "",
                        "detected": _detect_outbound_ip()})
    except Exception as exc:
        return jsonify({"error": str(exc)[:200]}), 500


@api_bp.route("/agents/manifest", methods=["GET"])
def agent_manifest():
    """Agent 自更新清单（令牌鉴权）：Agent 端据此判断是否需要更新。"""
    token = _agent_token_from_request()
    rec = _find_agent_by_token(token)
    if not rec:
        return jsonify({"error": "Agent 令牌无效或已注销"}), 401
    from core.agent_distribution import agent_manifest as _manifest
    data = _manifest()
    data["agent_id"] = rec["id"]
    data["hostname"] = rec["hostname"]
    return jsonify(data)


@api_bp.route("/agents/download", methods=["GET"])
def agent_download():
    """下载统一发布物（Python 源码包，令牌鉴权）：Agent 自更新用。"""
    token = _agent_token_from_request()
    rec = _find_agent_by_token(token)
    if not rec:
        return jsonify({"error": "Agent 令牌无效或已注销"}), 401
    from core.agent_distribution import source_bundle
    try:
        bundle = source_bundle()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    path, metadata = bundle["path"], bundle["metadata"]
    response = send_file(path, as_attachment=True,
                         download_name="aidbm-agent-src.tar.gz",
                         mimetype="application/octet-stream", conditional=True)
    response.headers["X-Artifact-SHA256"] = metadata["sha256"]
    response.headers["X-Artifact-Version"] = metadata.get("version", "")
    return response


@api_bp.get("/agents/packages")
def agent_packages():
    """发布物清单：**统一发布物**（一个包通吃所有架构与系统）。

    - **Python 源码包（默认/首选）**：由 agent/ 源码实时生成（纯标准库），
      一个包覆盖全部架构与国产系统，能力随平台升级自动更新——用户只装一次；
    - **ELF 单文件包（兜底）**：仅在目标机没有 python3 时使用；它是构建时
      的能力快照，可能不含最新插件能力，页面会如实标注。

    没跑过 scripts/build_agent.py 也能一键安装（源码包始终可用）。
    """
    from core.agent_distribution import artifact, source_bundle, agent_source_dir
    packages = []
    unified = None
    try:
        agent_source_dir()
        bundle = source_bundle()
        unified = dict(bundle["metadata"], mode="python", arch="any",
                       unified=True,
                       note="统一发布物：一个包通吃所有架构与系统，能力随平台升级自动更新")
        packages.append(unified)
    except ValueError:
        unified = None
    for arch in ("x86_64", "aarch64", "loongarch64", "mips64", "sw_64"):
        try:
            _, metadata = artifact(arch)
            metadata = dict(metadata, mode="elf", unified=False,
                            note="兜底包（目标机无 python3 时使用；能力为构建时快照）")
            packages.append(metadata)
        except ValueError:
            continue
    return jsonify({"packages": packages,
                    "unified": bool(unified),
                    "default_mode": "python" if unified else (
                        packages[0]["mode"] if packages else ""),
                    "note": ("统一发布物：源码包一个包通吃所有架构与系统；"
                             "ELF 仅在目标机无 python3 时兜底。")})


@api_bp.get("/agents/package")
@permission_required("agent.use")
def agent_package_download():
    """下载 Agent 发布包：arch=<CPU 架构> 取 ELF；arch=source 取 Python 源码包。"""
    from core.agent_distribution import artifact, source_bundle
    arch = (request.args.get("arch") or "x86_64").strip().lower()
    try:
        if arch in ("source", "any", "python"):
            bundle = source_bundle()
            path, metadata = bundle["path"], bundle["metadata"]
            name = "aidbm-agent-src.tar.gz"
        else:
            path, metadata = artifact(arch)
            name = "aidbm-agent"
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    response = send_file(path, as_attachment=True, download_name=name,
                         mimetype="application/octet-stream", conditional=True)
    response.headers["X-Artifact-SHA256"] = metadata["sha256"]
    response.headers["X-Artifact-Mode"] = metadata.get("format", "")
    return response


@api_bp.get("/agents/<int:aid>/install-cmd")
@permission_required("agent.use")
def install_cmd(aid):
    """手工安装脚本：默认发**统一发布物**（源码包），可选 ELF 兜底。

    统一发布物口径：一次安装、全部能力、不按架构/类型分流。脚本自带
    SHA256 校验、systemd 单元与令牌写入；升级时重复执行即可刷新到最新。
    """
    from core.agent_distribution import (artifact, source_bundle, unit_text,
                                         LAUNCHER, BINARY, UNIT, ENVIRONMENT,
                                         AGENT_SOURCE_FILES, STATE_FILE)
    rec = agent_hosts.get_by_id(aid)
    if not rec:
        return jsonify({"error": "注册记录不存在"}), 404
    mode = (request.args.get("mode") or "python").strip().lower()
    try:
        token = agent_hosts.get_token(rec)
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            raise ValueError("注册令牌格式非法")
        port = int(rec["port"])
        platform_url = _platform_url_for_agent(request,
                                              request.args.get("platform_url") or "")
        if mode == "elf":
            _, metadata = artifact(request.args.get("arch", "x86_64"))
            script = (
                "set -eu\numask 077\n"
                "test \"$(sha256sum /tmp/aidbm-agent | cut -d' ' -f1)\" = '" + metadata["sha256"] + "'\n"
                "install -d -m 750 /opt/aidbm-agent\n"
                "install -d -m 700 /etc/aidbm-agent\n"
                "install -m 700 /tmp/aidbm-agent " + BINARY + ".new\n"
                "printf '%s\\n' 'AIDBM_AGENT_TOKEN=" + token + "' > " + ENVIRONMENT + "\n"
                "cat > " + UNIT + " <<'UNIT_EOF'\n" + unit_text(port, "elf") + "UNIT_EOF\n"
                "chmod 600 " + ENVIRONMENT + " " + UNIT + "\n"
                "mv -f " + BINARY + ".new " + BINARY + "\n"
                "systemctl daemon-reload\nsystemctl enable aidbm-agent\n"
                "systemctl restart aidbm-agent\nsystemctl is-active aidbm-agent\n"
                "rm -f /opt/aidbm-agent/backup_agent.py /opt/aidbm-agent/agent_loader.py "
                "/opt/aidbm-agent/agent_payload.enc /tmp/aidbm-agent\n")
            return jsonify({"script": script, "package": metadata, "mode": "elf",
                            "package_url": "/api/agents/package?arch=" + metadata["arch"],
                            "instructions": "先下载单文件包并复制到目标机 /tmp/aidbm-agent，再执行脚本。"})
        # 默认：统一发布物（源码包）
        bundle = source_bundle()
        metadata = bundle["metadata"]
        extract = "\n".join(
            "tar -xzf /tmp/aidbm-agent-src.tar.gz -C /opt/aidbm-agent " + name
            for name in AGENT_SOURCE_FILES)
        script = (
            "set -eu\numask 077\n"
            "test \"$(sha256sum /tmp/aidbm-agent-src.tar.gz | cut -d' ' -f1)\" = '"
            + metadata["sha256"] + "'\n"
            "install -d -m 750 /opt/aidbm-agent\n"
            "install -d -m 700 /etc/aidbm-agent\n"
            + extract + "\n"
            "chmod 600 /opt/aidbm-agent/" + " /opt/aidbm-agent/".join(AGENT_SOURCE_FILES) + "\n"
            "cat > " + BINARY + " <<'LAUNCHER_EOF'\n" + LAUNCHER + "LAUNCHER_EOF\n"
            "chmod 700 " + BINARY + "\n"
            "printf '%s\\n' 'AIDBM_AGENT_TOKEN=" + token + "' 'AIDBM_PLATFORM_URL="
            + platform_url + "' 'AIDBM_AGENT_SELFUPDATE=1' > " + ENVIRONMENT + "\n"
            "printf '%s\\n' '{\"sha256\": \"" + metadata["sha256"] + "\", \"version\": \""
            + metadata.get("version", "") + "\"}' > " + STATE_FILE + "\n"
            "chmod 600 " + ENVIRONMENT + " " + STATE_FILE + "\n"
            "cat > " + UNIT + " <<'UNIT_EOF'\n" + unit_text(port, "python") + "UNIT_EOF\n"
            "chmod 600 " + UNIT + "\n"
            "systemctl daemon-reload\nsystemctl enable aidbm-agent\n"
            "systemctl restart aidbm-agent\nsystemctl is-active aidbm-agent\n")
        return jsonify({"script": script, "package": metadata, "mode": "python",
                        "package_url": "/api/agents/package?arch=source",
                        "instructions": "先下载统一发布物（源码包）并复制到目标机 "
                                       "/tmp/aidbm-agent-src.tar.gz，再以 root 执行脚本；"
                                       "重复执行即为升级（能力随平台更新自动到位）。"})
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
        prefer = (data.get("prefer") or "auto").strip().lower()
        platform_url = _platform_url_for_agent(request, data.get("platform_url") or "")
        if not 1 <= port <= 65535 or phase not in ("precheck", "stage", "install"):
            raise ValueError("部署端口或阶段非法")
        if prefer not in ("auto", "python", "elf"):
            raise ValueError("发布物形态仅支持 auto / python / elf")
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
        result = agent_distribution.deploy(client, host["hostname"], port, phase,
                                           platform_url=platform_url, prefer=prefer)
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
