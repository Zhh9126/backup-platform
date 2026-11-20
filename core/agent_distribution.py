"""单文件 Agent 发布与分阶段部署。"""
import hashlib
import json
import re
import shlex
import time
import uuid
from pathlib import Path

import config
from core import agent_hosts, lease, db

BINARY = "/opt/aidbm-agent/aidbm-agent"
UNIT = "/etc/systemd/system/aidbm-agent.service"
ENVIRONMENT = "/etc/aidbm-agent/agent.env"
LEGACY_FILES = ("backup_agent.py", "agent_loader.py", "agent_payload.enc")


def artifact(arch="x86_64"):
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(arch, arch)
    if arch not in ("x86_64", "aarch64"):
        raise ValueError("当前没有该 CPU 架构的 Agent 发布包")
    directory = Path(config.BASE_DIR) / "dist" / "agents" / ("linux-" + arch)
    try:
        metadata = json.loads((directory / "manifest.json").read_text())
        path = directory / "aidbm-agent"
        with path.open("rb") as fp:
            if fp.read(4) != b"\x7fELF":
                raise ValueError("Agent 发布包必须为 ELF 可执行文件")
            fp.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: fp.read(1 << 20), b""):
                digest.update(chunk)
        if metadata.get("sha256") != digest.hexdigest() or metadata.get("size") != path.stat().st_size:
            raise ValueError("Agent 发布包摘要或大小不一致，请重新构建")
        if metadata.get("os") != "linux" or metadata.get("arch") != arch:
            raise ValueError("Agent 发布清单的平台不匹配")
        return path, metadata
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("该架构尚无有效 Agent 包，请先运行 scripts/build_agent.py") from exc


def _command(client, command, timeout=60):
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    output = stdout.read().decode("utf-8", "replace").strip()
    error = stderr.read().decode("utf-8", "replace").strip()
    if stdout.channel.recv_exit_status() != 0:
        raise ValueError(error[-500:] or output[-500:] or "远端操作失败")
    return output


def precheck(client):
    facts = _command(client, "id -u; uname -s; uname -m; getconf GNU_LIBC_VERSION; "
                     "test -d /run/systemd/system && echo SYSTEMD")
    parts = facts.splitlines()
    if len(parts) != 5 or parts[0] != "0" or parts[1] != "Linux" or parts[4] != "SYSTEMD":
        raise ValueError("单文件安装需要 Linux、root SSH 账号和运行中的 systemd")
    path, metadata = artifact(parts[2])
    have = tuple(int(v) for v in parts[3].split()[-1].split("."))
    need = tuple(int(v) for v in metadata["glibc"].split("."))
    if have < need:
        raise ValueError(f"目标 glibc {parts[3]} 低于发布包要求 {metadata['glibc']}")
    space = _command(client, "df -Pk /opt | tail -1")
    if int(space.split()[3]) * 1024 < metadata["size"] * 3:
        raise ValueError("目标 /opt 分区暂存及回滚空间不足")
    identity = _command(client, "cat /etc/machine-id")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", identity):
        raise ValueError("目标 systemd 主机身份无效")
    return path, metadata, {"os": parts[1], "arch": parts[2], "glibc": parts[3],
                            "machine_id": identity.lower()}


def unit_text(port):
    return (
        "[Unit]\nDescription=AIDBM Backup Agent\nAfter=network.target\n"
        "StartLimitIntervalSec=60\nStartLimitBurst=5\n\n"
        "[Service]\nType=simple\nEnvironmentFile=" + ENVIRONMENT + "\n"
        f"ExecStart={BINARY} --port {int(port)}\n"
        "Restart=on-failure\nRestartSec=3\nKillMode=control-group\n"
        "TimeoutStopSec=30\nUMask=0077\nLimitNOFILE=65536\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


def _write(client, path, text, mode):
    sftp = client.open_sftp()
    try:
        with sftp.open(path, "w") as fp:
            fp.write(text)
        sftp.chmod(path, mode)
    finally:
        sftp.close()


def deploy(client, hostname, port=8466, phase="install"):
    if phase not in ("precheck", "stage", "install"):
        raise ValueError("phase 仅支持 precheck、stage、install")
    if not 1 <= port <= 65535:
        raise ValueError("Agent 端口需在 1..65535")
    path, metadata, facts = precheck(client)
    result = {"ok": True, "phase": phase, "package": metadata, "target": facts,
              "install": {"ok": False}, "probe": {"ok": False}}
    if phase == "precheck":
        return result
    name = "agent-deploy:" + facts["machine_id"]
    owner = uuid.uuid4().hex
    if not lease.acquire(name=name, owner=owner, ttl=600)["acquired"]:
        raise ValueError("该主机已有部署正在执行，请等待完成")
    try:
        stage = "/var/lib/aidbm-agent/staging/" + metadata["sha256"]
        _command(client, "install -d -m 700 /var/lib/aidbm-agent/staging")
        # 暂存文件按内容寻址。仅复用摘要正确的完整包。
        existing_digest = _command(client,
            f"if test -f {stage}; then sha256sum {stage}; else echo missing; fi").split()[0]
        if existing_digest != metadata["sha256"]:
            sftp = client.open_sftp()
            temporary = stage + "." + owner
            try:
                sftp.put(str(path), temporary)
                sftp.chmod(temporary, 0o700)
                _command(client, "test \"$(sha256sum " + temporary + " | cut -d' ' -f1)\" = "
                         + shlex.quote(metadata["sha256"]) + " && mv -f " + temporary + " " + stage)
            finally:
                sftp.close()
        result["stage"] = {"ok": True, "sha256": metadata["sha256"]}
        if phase == "stage":
            return result
        if not lease.renew(name=name, owner=owner, ttl=600)["ok"]:
            raise ValueError("部署锁已过期，请重试部署")
        return _install(client, hostname, port, stage, metadata, result, owner)
    finally:
        lease.release(name=name, owner=owner)


def _install(client, hostname, port, stage, metadata, result, owner):
    # 不能经 enabled-only 查询，否则失败后重装会产生重复注册。
    previous = db.query_one("SELECT * FROM agent_hosts WHERE hostname=? ORDER BY id LIMIT 1",
                            (hostname,))
    created = previous is None
    agent_id = None
    rollback = "/var/lib/aidbm-agent/rollback/" + owner
    paths = {"binary": BINARY, "unit": UNIT, "environment": ENVIRONMENT}
    snapshots = {}
    for key, path in paths.items():
        snapshots[key] = _command(client, f"if test -f {path}; then echo yes; else echo no; fi") == "yes"
    enabled_before = _command(client, "systemctl is-enabled aidbm-agent 2>/dev/null || true") == "enabled"
    active_before = _command(client, "systemctl is-active aidbm-agent 2>/dev/null || true") == "active"
    _command(client, f"install -d -m 700 {rollback}")
    for key, path in paths.items():
        if snapshots[key]:
            _command(client, f"cp -p {path} {rollback}/{key}")
    try:
        if previous:
            agent_id = int(previous["id"])
            token = agent_hosts.get_token(previous)
        else:
            agent_id = agent_hosts.create_host({"hostname": hostname, "port": port,
                                                "transport": "auto", "enabled": 0})
            token = agent_hosts.get_token(agent_hosts.get_by_id(agent_id))
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            raise ValueError("已有注册令牌格式无效")
        _command(client, "install -d -m 750 /opt/aidbm-agent; install -d -m 700 /etc/aidbm-agent")
        _write(client, ENVIRONMENT + ".new", "AIDBM_AGENT_TOKEN=" + token + "\n", 0o600)
        _write(client, UNIT + ".new", unit_text(port), 0o600)
        _command(client, f"cp {stage} {BINARY}.new && chmod 700 {BINARY}.new && "
                 f"mv -f {BINARY}.new {BINARY} && mv -f {ENVIRONMENT}.new {ENVIRONMENT} && "
                 f"mv -f {UNIT}.new {UNIT} && systemctl daemon-reload && "
                 "systemctl restart aidbm-agent && systemctl is-active --quiet aidbm-agent")
        from core.agent_client import AgentTransport
        probe = None
        for _ in range(15):
            transport = AgentTransport(hostname, port, token, timeout=3)
            try:
                caps = transport.capabilities(refresh=True)
                if (caps.get("version") != metadata["version"] or
                        caps.get("distribution") != "single-executable" or not caps.get("enc")):
                    raise ValueError("Agent 身份、版本、单文件形态或 AES 能力不匹配")
                probe = {"ok": True, "caps": caps}
                break
            except Exception as exc:
                probe = {"ok": False, "error": str(exc)[:150]}
                time.sleep(1)
            finally:
                transport._reset()
        result["probe"] = probe
        if not probe["ok"]:
            raise ValueError(probe["error"])
        _command(client, "systemctl enable aidbm-agent")
        agent_hosts.update_host(agent_id, {"port": port, "enabled": 1})
        agent_hosts.probe(agent_id)
        # 版本切换已提交，清理故障单独报告，避免删除旧文件后再回滚到旧启动器。
        warnings = []
        for filename in LEGACY_FILES:
            try:
                _command(client, "rm -f -- /opt/aidbm-agent/" + filename)
            except Exception as exc:
                warnings.append(str(exc))
        try:
            _command(client, "rm -f -- " + stage)
            _command(client, "rm -f -- " + " ".join(f"{rollback}/{key}" for key in paths))
            _command(client, "rmdir -- " + rollback)
        except Exception as exc:
            warnings.append(str(exc))
        try:
            contents = _command(client, "find /opt/aidbm-agent -mindepth 1 -maxdepth 1 -printf '%f\\n'").splitlines()
            if contents != ["aidbm-agent"]:
                warnings.append("安装目录仍有额外文件，请核对旧安装遗留内容")
        except Exception as exc:
            warnings.append(str(exc))
        result["cleanup"] = {"ok": not warnings, "warnings": warnings}
        result.update(agent_id=agent_id, probe=probe, ok=True,
                      install={"ok": True, "version": metadata["version"],
                               "format": "single-executable", "path": BINARY,
                               "sha256": metadata["sha256"]})
        return result
    except Exception as exc:
        try:
            diagnostics = _command(client,
                "journalctl -u aidbm-agent -n 20 --no-pager 2>&1 | tail -20", timeout=15)
            result["install"]["diagnostics"] = diagnostics[-2500:]
        except Exception:
            pass
        errors = []
        for key, path in paths.items():
            try:
                if snapshots[key]:
                    _command(client, f"cp -p {rollback}/{key} {path}.rollback && "
                             f"mv -f {path}.rollback {path}")
                else:
                    _command(client, f"rm -f -- {path}")
            except Exception as rollback_exc:
                errors.append(str(rollback_exc))
        try:
            _command(client, "systemctl daemon-reload")
            if snapshots["unit"]:
                _command(client, "systemctl " + ("enable" if enabled_before else "disable") + " aidbm-agent")
                _command(client, "systemctl " + ("restart" if active_before else "stop") + " aidbm-agent")
            else:
                _command(client, "systemctl stop aidbm-agent 2>/dev/null || true")
        except Exception as rollback_exc:
            errors.append(str(rollback_exc))
        if agent_id:
            if created:
                agent_hosts.delete_host(agent_id)
            else:
                agent_hosts.update_host(agent_id, {"port": previous["port"], "enabled": previous["enabled"]})
        result.update(ok=False, agent_id=agent_id, error=str(exc)[:300],
                      rollback_directory=rollback,
                      rolled_back="部署失败，已尝试恢复原二进制、服务与注册状态",
                      rollback_errors=errors,
                      hint="检查目标机 journalctl -u aidbm-agent -n 30 与网络访问")
        return result
