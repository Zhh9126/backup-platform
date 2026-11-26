"""统一 Agent 发布与一键部署。

统一发布物（2026-11-26 起）
--------------------------
用户口径：「**只安装一次，所有功能都支持，不区分类型**」。因此发布与安装
不再按 CPU 架构 / 发行版 / 数据库类型分流：

1. **统一发布物 = Python 源码包**（默认）：``agent/`` 目录的纯标准库源码，
   实时打成 tar.gz。**一个包通吃** x86_64 / aarch64 / loongarch64 / mips64 /
   sw_64 与麒麟 / 统信 / 欧拉 / CentOS 等全部系统；能力（exec / 流式执行 /
   文件收发 / 目录操作 / 摘要 / 取消 / 续传 + block-cdp / bmr / app-dr /
   block-carve 四个插件）**总是全量最新**——平台代码一改，重装一次即生效，
   不必为每种架构单独打包。
2. **单文件 ELF 仅作兜底**：目标机**没有 python3** 时使用
   ``dist/agents/linux-<arch>/aidbm-agent``（PyInstaller 产物，免 Python
   环境）。该包是构建时的能力快照，可能不含最新的插件能力——此时安装结果
   会如实提示「建议目标机装 python3 后重装一次以获得全部能力」。

安装过程保持：内容寻址暂存 → SHA256 校验 → systemd 单元原子替换 → 探活 →
失败回滚。2026-11-25 另修三处现场问题：

- **防火墙**：目标机 firewalld/ufw 在跑时自动放行 Agent 端口（原来端口不通
  会被误报成「服务没起来」）；
- **诊断分层**：探活失败时先做目标机**本地**端口自查，区分「服务没起来」与
  「服务起来了但平台到目标机的网络/防火墙不通」，并保留现场（不再删掉
  systemd 单元导致 ``systemctl status aidbm-agent`` 报 unit not found）；
- **保留现场**：全新安装失败时保留已装文件与单元（仅不 enable），并把
  journalctl 输出落盘到 ``/var/log/aidbm-agent-install.log``，便于离线排障。

2026-11-26 追加：**装完自动保持最新**——安装时把平台地址写入
``/etc/aidbm-agent/agent.env``，Agent 端的 ``agent/selfupdate.py`` 线程
定期比对平台清单，版本变化即下载替换并重启（见 :func:`agent_manifest`）。
"""
import gzip
import hashlib
import json
import os
import re
import shlex
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import config
from core import agent_hosts, lease, db

BINARY = "/opt/aidbm-agent/aidbm-agent"
UNIT = "/etc/systemd/system/aidbm-agent.service"
ENVIRONMENT = "/etc/aidbm-agent/agent.env"
LEGACY_FILES = ("backup_agent.py", "agent_loader.py", "agent_payload.enc")
DIAG_LOG = "/var/log/aidbm-agent-install.log"
# 自更新状态（记录当前已安装的源码包摘要/版本，供 Agent 端比对，避免重复下载）
STATE_FILE = "/opt/aidbm-agent/.selfupdate.json"

AGENT_SOURCE_FILES = ("backup_agent.py", "plugins.py", "iso9660.py", "selfupdate.py")
ARCH_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64",
                "armv8l": "aarch64", "loongarch64": "loongarch64",
                "loong64": "loongarch64", "mips64": "mips64",
                "mips64el": "mips64", "sw_64": "sw_64", "sw64": "sw_64"}

# 源码包缓存（按源码指纹失效）：Agent 自更新会周期拉清单，避免重复打包
_BUNDLE_CACHE = {}


def _source_fingerprint(source: Path) -> str:
    """源码指纹（文件名 + 大小 + mtime）：变了就重新打包。"""
    parts = []
    for name in AGENT_SOURCE_FILES:
        f = source / name
        try:
            st = f.stat()
            parts.append("%s:%d:%d" % (name, st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append("%s:missing" % name)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def normalize_arch(arch: str) -> str:
    arch = (arch or "").strip().lower()
    return ARCH_ALIASES.get(arch, arch)


# --------------------------------------------------------------------------- #
# 发布包解析
# --------------------------------------------------------------------------- #
def artifact(arch="x86_64"):
    """取 ELF 单文件发布包（不存在则 ValueError）。"""
    arch = normalize_arch(arch)
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


def agent_source_dir() -> Path:
    """定位 agent 源码目录（源码运行 / PyInstaller 冻结 / 安装目录三种形态）。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "agent")
    candidates.append(Path(config.BASE_DIR) / "agent")
    candidates.append(Path(__file__).resolve().parents[1] / "agent")
    for c in candidates:
        if (c / "backup_agent.py").is_file():
            return c
    raise ValueError("平台缺少 agent 源码目录，无法使用免编译安装模式；"
                     "请用 scripts/build_agent.py 生成 ELF 发布包")


def _source_version(source: Path) -> str:
    try:
        text = (source / "backup_agent.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "0.0.0"
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else "0.0.0"


def source_bundle() -> dict:
    """把 agent 源码打成 tar.gz（内存临时文件），返回包描述。

    每次按源码内容重算摘要：**改完 Agent 代码，重新点一次「安装并启动」就是
    升级**——不需要重新 PyInstaller 打包，也不需要目标机做任何事。

    打包结果按源码指纹缓存：Agent 自更新会周期性地拉清单（每台 Agent 一次），
    不缓存的话每次请求都要重新 tar+gzip 一遍源码，纯属浪费。
    """
    source = agent_source_dir()
    fingerprint = _source_fingerprint(source)
    cached = _BUNDLE_CACHE.get("bundle")
    if cached and _BUNDLE_CACHE.get("fingerprint") == fingerprint:
        try:
            if os.path.isfile(cached["path"]):
                return cached
        except Exception:
            pass
    buffer_path = Path(tempfile.mkdtemp(prefix="aidbm-agent-pkg-")) / "agent.tar.gz"
    # 摘要用于目标机内容寻址暂存（staging/<sha256> 已存在就不重传），
    # 因此打包必须**可复现**：gzip 头与 tar 成员的 mtime/属主全部归零、
    # 不写文件名，否则同一份源码每次打包摘要都变（gzip 头默认带当前时间），
    # 暂存缓存永远命中不了。
    with buffer_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=raw) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for name in AGENT_SOURCE_FILES:
                    f = source / name
                    if not f.is_file():
                        continue
                    info = tar.gettarinfo(str(f), arcname=name)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with f.open("rb") as fp:
                        tar.addfile(info, fp)
    digest = hashlib.sha256()
    with buffer_path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    metadata = {
        "version": _source_version(source), "os": "linux", "arch": "any",
        "sha256": digest.hexdigest(), "size": buffer_path.stat().st_size,
        "glibc": "any", "format": "python-source", "mode": "python",
        "files": [n for n in AGENT_SOURCE_FILES if (source / n).is_file()],
    }
    bundle = {"mode": "python", "path": buffer_path, "metadata": metadata}
    _BUNDLE_CACHE["fingerprint"] = fingerprint
    _BUNDLE_CACHE["bundle"] = bundle
    return bundle


def resolve_package(arch: str, glibc: str, python_ver: str, prefer: str = "auto") -> dict:
    """选择发布物：**统一发布物**——一个包通吃所有架构与系统。

    用户口径（2026-11-26）：「只安装一次，所有功能都支持，不区分类型」。
    因此选择规则与旧实现**相反**：

    - **默认（auto）用 Python 源码包**：纯标准库、随平台代码实时更新，
      同一份包覆盖 x86_64 / aarch64 / loongarch64 / mips64 / sw_64 与
      麒麟 / 统信 / 欧拉 / CentOS 等全部系统；能力（exec / 流式执行 /
      文件收发 / 目录操作 / 摘要 / 取消 / 续传 + 四个插件）**始终是全量最新**，
      平台升级后重装一次即生效，不必为每种架构单独打包、不必按类型重装；
    - **ELF 单文件只在目标机没有 python3 时兜底**（并如实提示「该包是能力
      全量之前的快照，建议目标机装 python3 后重装一次以获得全部能力」）；
    - ``prefer="elf" / "python"`` 是运维排查用的显式指定，不是默认路径。

    返回值新增字段：``unified``（是否统一发布物）、``elf_available``
    （该架构是否有可用 ELF 兜底包）、``fallback_reason``（为什么没用另一种形态）。
    """
    arch = normalize_arch(arch)
    prefer = (prefer or "auto").strip().lower()
    python_ok = (bool(python_ver) and python_ver != "none"
                 and _version_tuple(python_ver) >= (3, 6))
    elf_path, elf_meta, elf_error = None, {}, ""
    try:
        elf_path, elf_meta = artifact(arch)
        if not glibc:
            # 没有 getconf glibc 版本：大概率是 musl（Alpine）或精简系统，
            # PyInstaller 单文件是 glibc 链接的，直接用会 "not found"。
            elf_error = "目标机未检测到 glibc（可能为 musl），单文件包不适用"
        else:
            have = _version_tuple(glibc)
            need = _version_tuple(elf_meta.get("glibc") or "")
            if have and need and have < need:
                elf_error = ("目标机 glibc %s 低于单文件包要求 %s"
                             % (glibc, elf_meta["glibc"]))
    except ValueError as exc:
        elf_error = str(exc)

    if prefer == "elf":
        if elf_path and not elf_error:
            return {"mode": "elf", "path": elf_path, "metadata": elf_meta,
                    "note": "单文件 ELF（运维显式指定；能力快照可能较旧）",
                    "unified": False, "elf_available": True,
                    "fallback_reason": "运维显式指定 ELF 形态"}
        raise ValueError("无法使用单文件 ELF 包：%s" % (elf_error or "该架构无发布包"))
    if prefer == "python":
        if not python_ok:
            raise ValueError("目标机没有 python3（≥3.6），无法使用统一源码发布物")
        bundle = source_bundle()
        bundle["note"] = "统一发布物：Python 源码包（运维显式指定）"
        bundle["unified"] = True
        bundle["elf_available"] = bool(elf_path and not elf_error)
        bundle["fallback_reason"] = "运维显式指定源码形态"
        return bundle

    # auto：源码优先（统一发布物），ELF 仅兜底
    if python_ok:
        bundle = source_bundle()
        bundle["note"] = ("统一发布物：Python 源码包（一个包通吃所有架构与系统，"
                          "能力随平台升级自动更新）")
        bundle["unified"] = True
        bundle["elf_available"] = bool(elf_path and not elf_error)
        bundle["fallback_reason"] = (elf_error or
                                     "统一发布物策略：源码包能力随平台代码实时更新，"
                                     "不再按架构分别打包")
        return bundle
    if elf_path and not elf_error:
        return {"mode": "elf", "path": elf_path, "metadata": elf_meta,
                "note": ("目标机未安装 python3，兜底使用单文件 ELF"
                         "（该包是能力全量之前的快照，建议目标机装 python3 后重装一次）"),
                "unified": False, "elf_available": True,
                "fallback_reason": "目标机没有 python3（≥3.6）"}
    raise ValueError(
        "目标机 %s 架构无法安装 Agent：既没有 python3（≥3.6），也没有可用的单文件包。"
        "处理方式：① 在目标机安装 python3（统信 UOS / 麒麟 / 欧拉默认已装，"
        "装完重装一次即可获得全部能力）；② 或在同架构机器上运行 "
        "scripts/build_agent.py 生成发布包并放到 dist/agents/linux-%s/。"
        "原始原因：%s" % (arch, arch, elf_error or "未找到发布包"))


def _version_tuple(text: str):
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) if nums else ()


# --------------------------------------------------------------------------- #
# 自更新清单（Agent 端定期比对，装一次即可长期跟随平台版本）
# --------------------------------------------------------------------------- #
def agent_manifest() -> dict:
    """返回 Agent 自更新清单（平台侧）。

    Agent 端 ``agent/selfupdate.py`` 定期拉取本清单：版本或 sha256 与本地
    记录不一致即下载新包、校验、原子替换并重启服务——用户**只装一次**，
    平台后续升级的 Agent 能力自动到位。
    """
    bundle = source_bundle()
    meta = bundle["metadata"]
    return {
        "version": meta["version"],
        "sha256": meta["sha256"],
        "size": meta["size"],
        "format": meta.get("format", "python-source"),
        "files": list(meta.get("files") or []),
        "platform_version": getattr(config, "PLATFORM_VERSION", ""),
        "updated_at": db.now_iso(),
        "download_path": "/api/v1/agents/download",
        "manifest_path": "/api/v1/agents/manifest",
    }


# --------------------------------------------------------------------------- #
# 远端命令
# --------------------------------------------------------------------------- #
def _command(client, command, timeout=60):
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    output = stdout.read().decode("utf-8", "replace").strip()
    error = stderr.read().decode("utf-8", "replace").strip()
    if stdout.channel.recv_exit_status() != 0:
        raise ValueError(error[-500:] or output[-500:] or "远端操作失败")
    return output


def _facts_command():
    """一次往返取全目标机事实（避免多次握手抖动）。"""
    return r"""echo "UID=$(id -u)"
echo "OS=$(uname -s)"
echo "ARCH=$(uname -m)"
echo "KERNEL=$(uname -r)"
echo "GLIBC=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
echo "PY=$(python3 -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo none)"
echo "SYSTEMD=$(test -d /run/systemd/system && echo yes || echo no)"
echo "SYSTEMCTL=$(command -v systemctl >/dev/null 2>&1 && echo yes || echo no)"
echo "FIREWALLD=$(command -v firewall-cmd >/dev/null 2>&1 && echo yes || echo no)"
echo "UFW=$(command -v ufw >/dev/null 2>&1 && echo yes || echo no)"
echo "IPTABLES=$(command -v iptables >/dev/null 2>&1 && echo yes || echo no)"
echo "RESTORECON=$(command -v restorecon >/dev/null 2>&1 && echo yes || echo no)"
echo "SELINUX=$(getenforce 2>/dev/null || echo none)"
echo "MACHINEID=$(cat /etc/machine-id 2>/dev/null)"
"""


def _parse_facts(output: str) -> dict:
    facts = {}
    for line in (output or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            facts[k.strip()] = v.strip()
    return facts


def precheck(client, prefer="auto"):
    raw = _command(client, _facts_command(), timeout=90)
    facts = _parse_facts(raw)
    if facts.get("UID") != "0":
        raise ValueError("单文件安装需要 root SSH 账号（当前 uid=%s）" % facts.get("UID"))
    if facts.get("OS") != "Linux":
        raise ValueError("一键安装仅支持 Linux 目标机（当前 %s）；"
                         "Windows 目标机请使用 Agent 手动安装包" % facts.get("OS"))
    if facts.get("SYSTEMD") != "yes" or facts.get("SYSTEMCTL") != "yes":
        raise ValueError("目标机没有运行中的 systemd，无法托管 Agent 服务；"
                         "请改用 SSH 通道备份（无需 Agent）")
    package = resolve_package(facts.get("ARCH"), facts.get("GLIBC"), facts.get("PY"),
                               prefer=prefer)
    space = _command(client, "df -Pk /opt | tail -1")
    need = max(int(package["metadata"]["size"]) * 3, 8 * 1024 * 1024)
    if int(space.split()[3]) * 1024 < need:
        raise ValueError("目标 /opt 分区空间不足（暂存+回滚需要约 %d MB）" % (need // (1024 * 1024)))
    identity = (facts.get("MACHINEID") or "").strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", identity):
        raise ValueError("目标 systemd 主机身份无效（/etc/machine-id 缺失或格式不对）")
    facts["machine_id"] = identity.lower()
    facts["mode"] = package["mode"]
    facts["package"] = package["metadata"]
    return package, facts


def unit_text(port, mode="elf"):
    env_lines = ""
    if mode == "python":
        # PYTHONDONTWRITEBYTECODE：避免在 /opt/aidbm-agent 留 __pycache__（安装目录洁净校验）
        env_lines = ("Environment=PYTHONUNBUFFERED=1\n"
                     "Environment=PYTHONIOENCODING=utf-8\n"
                     "Environment=PYTHONDONTWRITEBYTECODE=1\n")
    # 注意：不写 StartLimitIntervalSec/StartLimitBurst —— CentOS7 的 systemd 219
    # 在 [Unit] 段不认识这两项，会在 journalctl 里刷 "Unknown lvalue" 噪音，
    # 干扰现场排障。Restart=on-failure + RestartSec 已足够。
    return (
        "[Unit]\nDescription=AIDBM Backup Agent\nAfter=network.target\n\n"
        "[Service]\nType=simple\nEnvironmentFile=" + ENVIRONMENT + "\n"
        + env_lines +
        f"ExecStart={BINARY} --port {int(port)}\n"
        "Restart=on-failure\nRestartSec=3\nKillMode=control-group\n"
        "TimeoutStopSec=30\nUMask=0077\nLimitNOFILE=65536\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


LAUNCHER = (
    "#!/bin/sh\n"
    "# AIDBM Agent launcher（Python 源码模式：使用目标机自带 python3）\n"
    'PY="${AIDBM_PYTHON:-python3}"\n'
    'exec "$PY" -u /opt/aidbm-agent/backup_agent.py "$@"\n'
)


def _write(client, path, text, mode):
    sftp = client.open_sftp()
    try:
        with sftp.open(path, "w") as fp:
            fp.write(text)
        sftp.chmod(path, mode)
    finally:
        sftp.close()


def _open_firewall(client, port, facts):
    """放行 Agent 端口（firewalld / ufw / iptables 依次尝试，best-effort）。

    现场最常见失败原因就是目标机防火墙挡了 8466，平台侧探活表现为 timed out，
    被误判成「服务没起来」。这里主动放行并把结果如实回报。
    """
    result = {"ok": False, "port": int(port), "actions": [], "warnings": []}
    try:
        if facts.get("FIREWALLD") == "yes":
            active = _command(client, "systemctl is-active firewalld 2>/dev/null || true")
            if active == "active":
                _command(client, f"firewall-cmd --permanent --add-port={int(port)}/tcp", timeout=40)
                _command(client, "firewall-cmd --reload", timeout=40)
                result["actions"].append("firewalld 已放行 %d/tcp 并重载" % int(port))
                result["ok"] = True
                return result
        if facts.get("UFW") == "yes":
            status = _command(client, "ufw status 2>/dev/null | head -1 || true")
            if "active" in status:
                _command(client, f"ufw allow {int(port)}/tcp", timeout=40)
                result["actions"].append("ufw 已放行 %d/tcp" % int(port))
                result["ok"] = True
                return result
        if facts.get("IPTABLES") == "yes":
            # 只有真的在拦才插规则：INPUT 默认 ACCEPT 且无 DROP/REJECT 规则时无需动
            policy = _command(client, "iptables -S INPUT 2>/dev/null | head -1 || true")
            drops = _command(client, "iptables -S INPUT 2>/dev/null | grep -cE -- '-j (DROP|REJECT)' || true")
            if "ACCEPT" in policy and (drops or "0").strip() in ("0", ""):
                result["actions"].append("未检测到生效中的防火墙（INPUT 默认放行），无需放行端口")
                result["ok"] = True
                return result
            check = _command(
                client,
                "iptables -C INPUT -p tcp --dport %d -j ACCEPT 2>/dev/null && echo present || echo absent"
                % int(port))
            if check != "present":
                _command(client, "iptables -I INPUT -p tcp --dport %d -j ACCEPT" % int(port),
                         timeout=40)
                result["actions"].append("iptables 已插入放行规则 %d/tcp（重启后失效，"
                                         "建议改用 firewalld/ufw 持久化）" % int(port))
                result["ok"] = True
                return result
            result["actions"].append("iptables 已有 %d/tcp 放行规则" % int(port))
            result["ok"] = True
            return result
        result["actions"].append("未检测到 firewalld / ufw / iptables，跳过防火墙配置")
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append("防火墙配置失败（不阻断安装）：%s" % str(exc)[:200])
    return result


def _local_listen(client, port):
    """目标机本地端口自查：区分「服务没起来」与「平台到目标机不通」。"""
    cmd = (
        "if command -v python3 >/dev/null 2>&1; then "
        "python3 -c \"import socket;s=socket.socket();s.settimeout(1);"
        "print('LISTEN' if s.connect_ex(('127.0.0.1',%d))==0 else 'NOLISTEN')\"; "
        "else timeout 1 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/%d' "
        "&& echo LISTEN || echo NOLISTEN; fi" % (int(port), int(port)))
    try:
        return "LISTEN" in _command(client, cmd, timeout=20)
    except Exception:  # noqa: BLE001
        return None


def deploy(client, hostname, port=8466, phase="install", platform_url="",
           prefer="auto"):
    if phase not in ("precheck", "stage", "install"):
        raise ValueError("phase 仅支持 precheck、stage、install")
    if not 1 <= port <= 65535:
        raise ValueError("Agent 端口需在 1..65535")
    package, facts = precheck(client, prefer=prefer)
    metadata = package["metadata"]
    result = {"ok": True, "phase": phase, "package": metadata,
              "mode": package["mode"], "mode_note": package.get("note", ""),
              "platform_url": platform_url,
              "target": facts, "install": {"ok": False}, "probe": {"ok": False}}
    if phase == "precheck":
        result["hint"] = ("预检通过：目标 %s，glibc %s，python %s，将使用%s"
                          % (facts.get("ARCH"), facts.get("GLIBC") or "-",
                             facts.get("PY"), package.get("note") or package["mode"]))
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
                sftp.put(str(package["path"]), temporary)
                sftp.chmod(temporary, 0o600)
                _command(client, "test \"$(sha256sum " + temporary + " | cut -d' ' -f1)\" = "
                         + shlex.quote(metadata["sha256"]) + " && mv -f " + temporary + " " + stage)
            finally:
                sftp.close()
        result["stage"] = {"ok": True, "sha256": metadata["sha256"]}
        if phase == "stage":
            return result
        if not lease.renew(name=name, owner=owner, ttl=600)["ok"]:
            raise ValueError("部署锁已过期，请重试部署")
        return _install(client, hostname, port, stage, package, facts, result, owner,
                        platform_url=platform_url)
    finally:
        lease.release(name=name, owner=owner)


def _install(client, hostname, port, stage, package, facts, result, owner,
            platform_url=""):
    metadata = package["metadata"]
    mode = package["mode"]
    # 不能经 enabled-only 查询，否则失败后重装会产生重复注册。
    previous = db.query_one("SELECT * FROM agent_hosts WHERE hostname=? ORDER BY id LIMIT 1",
                            (hostname,))
    created = previous is None
    agent_id = None
    rollback = "/var/lib/aidbm-agent/rollback/" + owner
    paths = {"binary": BINARY, "unit": UNIT, "environment": ENVIRONMENT}
    if mode == "python":
        for name in AGENT_SOURCE_FILES:
            paths["src_" + name] = "/opt/aidbm-agent/" + name
        paths["state"] = STATE_FILE
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
        env_content = "AIDBM_AGENT_TOKEN=" + token + "\n"
        if platform_url and mode == "python":
            # 平台地址写入 env：Agent 端自更新线程据此拉取新版本（装一次即可长期跟随）
            env_content += "AIDBM_PLATFORM_URL=" + platform_url.rstrip("/") + "\n"
            env_content += "AIDBM_AGENT_SELFUPDATE=1\n"
        else:
            # ELF 兜底包是构建时快照，没有源码可替换，关闭自更新（如实不假装）
            env_content += "AIDBM_AGENT_SELFUPDATE=0\n"
        _write(client, ENVIRONMENT + ".new", env_content, 0o600)
        _write(client, UNIT + ".new", unit_text(port, mode), 0o600)
        if mode == "python":
            _deploy_python_files(client, stage, rollback)   # 源码 + 启动器 BINARY.new
            install_step = f"chmod 700 {BINARY}.new && "
        else:
            install_step = f"cp {stage} {BINARY}.new && chmod 700 {BINARY}.new && "
        _command(client, install_step +
                 f"mv -f {BINARY}.new {BINARY} && mv -f {ENVIRONMENT}.new {ENVIRONMENT} && "
                 f"mv -f {UNIT}.new {UNIT} && systemctl daemon-reload && "
                 "systemctl restart aidbm-agent && systemctl is-active --quiet aidbm-agent")
        if facts.get("RESTORECON") == "yes":
            try:
                _command(client, "restorecon -R /opt/aidbm-agent 2>/dev/null || true")
            except Exception:  # noqa: BLE001
                pass
        result["firewall"] = _open_firewall(client, port, facts)
        probe = None
        from core.agent_client import AgentTransport
        for attempt in range(15):
            transport = AgentTransport(hostname, port, token, timeout=5)
            try:
                caps = transport.capabilities(refresh=True)
                if (caps.get("version") != metadata["version"] or
                        caps.get("distribution") not in ("single-executable", "source")):
                    raise ValueError(
                        "Agent 版本/运行形态不匹配（期望 %s%s，实际 %s/%s）" % (
                            metadata["version"],
                            "single-executable" if mode == "elf" else "source",
                            caps.get("version"), caps.get("distribution")))
                probe = {"ok": True, "caps": caps}
                if not caps.get("enc") and mode == "python":
                    # 目标机 python3 缺 cryptography：降级为 HMAC-SHA256 认证 + 完整性
                    # （平台侧 AgentTransport 已按 enc=false 走签名模式，功能不受影响，
                    # 只是传输不加密）。如实告知，不假装加密可用。
                    probe["warning"] = (
                        "目标机 python3 缺少 cryptography 模块：Agent 通道降级为 "
                        "HMAC-SHA256 认证（可用，但传输体不加密）。"
                        "如需加密传输，在目标机安装 python3-cryptography 后重新安装。")
                break
            except Exception as exc:
                local = _local_listen(client, port)
                probe = {"ok": False, "error": str(exc)[:150], "local_listen": local}
                if attempt == 14:
                    break
                time.sleep(1)
            finally:
                transport._reset()
        result["probe"] = probe
        if not probe["ok"]:
            raise AgentProbeError(_probe_message(probe, port), probe)
        _command(client, "systemctl enable aidbm-agent")
        agent_hosts.update_host(agent_id, {"port": port, "enabled": 1})
        agent_hosts.probe(agent_id)
        if mode == "python":
            # 记录已安装的源码包摘要/版本：Agent 端自更新线程据此判断是否需要更新
            state = json.dumps({"sha256": metadata["sha256"],
                                "version": metadata.get("version", ""),
                                "installed_at": db.now_iso(),
                                "platform_url": platform_url or ""},
                               ensure_ascii=False)
            _write(client, STATE_FILE, state + "\n", 0o600)
        # 版本切换已提交，清理故障单独报告，避免删除旧文件后再回滚到旧启动器。
        warnings = []
        obsolete = LEGACY_FILES
        if mode == "python":
            # Python 模式下 backup_agent.py 等就是当前运行文件，绝不能当遗留删掉
            obsolete = tuple(f for f in LEGACY_FILES if f not in AGENT_SOURCE_FILES)
        for filename in obsolete:
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
            expected = ["aidbm-agent"]
            if mode == "python":
                expected += list(AGENT_SOURCE_FILES) + [os.path.basename(STATE_FILE)]
            # __pycache__ 是历史运行痕迹（本版本已用 PYTHONDONTWRITEBYTECODE 规避）
            extra = [c for c in contents if c not in expected and c != "__pycache__"]
            if extra:
                warnings.append("安装目录仍有额外文件，请核对旧安装遗留内容：%s" % ", ".join(extra))
        except Exception as exc:
            warnings.append(str(exc))
        result["cleanup"] = {"ok": not warnings, "warnings": warnings}
        result.update(agent_id=agent_id, probe=probe, ok=True,
                      warning=probe.get("warning", ""),
                      install={"ok": True, "version": metadata["version"],
                               "format": metadata.get("format", "single-executable"),
                               "mode": mode, "path": BINARY,
                               "sha256": metadata["sha256"]})
        return result
    except Exception as exc:
        probe = result.get("probe") or {}
        diagnostics = _collect_diagnostics(client, port)
        result["install"]["diagnostics"] = diagnostics
        result["diagnostics_path"] = DIAG_LOG
        try:
            _write(client, DIAG_LOG, "AIDBM Agent 安装失败诊断 %s\n\n%s\n"
                   % (db.now_iso(), diagnostics), 0o600)
        except Exception:
            pass
        errors = []
        retain = (not snapshots["binary"])     # 全新安装：保留现场便于排障
        for key, path in paths.items():
            try:
                if retain and key != "unit":
                    continue
                if snapshots[key]:
                    _command(client, f"cp -p {rollback}/{key} {path}.rollback && "
                             f"mv -f {path}.rollback {path}")
                else:
                    _command(client, f"rm -f -- {path}")
            except Exception as rollback_exc:
                errors.append(str(rollback_exc))
        try:
            _command(client, "systemctl daemon-reload")
            if retain:
                _command(client, "systemctl stop aidbm-agent 2>/dev/null || true")
                _command(client, "systemctl disable aidbm-agent 2>/dev/null || true")
            elif snapshots["unit"]:
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
                      mode=mode, retained=retain,
                      rolled_back=("安装失败：已保留安装现场（服务单元与文件保留，仅停用），"
                                   "便于在目标机直接排查" if retain
                                   else "部署失败，已尝试恢复原二进制、服务与注册状态"),
                      rollback_errors=errors,
                      hint=_failure_hint(probe, result.get("firewall") or {}, retain))
        return result


class AgentProbeError(Exception):
    def __init__(self, message, probe):
        super().__init__(message)
        self.probe = probe


def _probe_message(probe, port):
    error = probe.get("error") or ""
    if "不匹配" in error:
        # 服务起来了、端口通，但回包身份/版本不对（常见：旧版本 Agent 还在跑）
        return "Agent 已监听但身份校验未通过：%s" % error
    if probe.get("local_listen") is True:
        return ("目标机上 %d 端口已监听，但平台侧连不上：多为防火墙/SELinux/网络策略"
                "拦截（平台→目标机方向）%s" % (port, probe.get("error") or ""))
    if probe.get("local_listen") is False:
        return ("目标机上 %d 端口未监听：Agent 服务没起来（看诊断里的 journalctl 日志）%s"
                % (port, probe.get("error") or ""))
    return "探活失败：%s" % (probe.get("error") or "未知原因")


def _collect_diagnostics(client, port):
    parts = []
    for cmd in ("systemctl status aidbm-agent --no-pager -l 2>&1 | tail -30",
                "journalctl -u aidbm-agent -n 60 --no-pager 2>&1 | tail -60",
                "ls -l /opt/aidbm-agent 2>&1",
                "ss -ltnp 2>/dev/null | grep -E ':%d\\b' || netstat -ltnp 2>/dev/null | grep -E ':%d\\b' || true"
                % (int(port), int(port)),
                "getenforce 2>/dev/null; systemctl is-active firewalld 2>/dev/null || true"):
        try:
            out = _command(client, cmd, timeout=20)
            if out:
                parts.append("$ " + cmd.split("|")[0].strip() + "\n" + out)
        except Exception as exc:  # noqa: BLE001
            parts.append("$ %s\n(执行失败: %s)" % (cmd.split("|")[0].strip(), str(exc)[:120]))
    return "\n\n".join(parts)[-8000:]


def _failure_hint(probe, firewall, retain):
    if probe.get("local_listen") is True:
        return ("服务已在目标机监听，但平台侧不可达：确认目标机防火墙放行该端口"
                "（已尝试自动放行：%s），以及平台到目标机的网络策略。"
                % ("；".join(firewall.get("actions") or []) or "无"))
    if probe.get("local_listen") is False:
        return ("Agent 服务启动后端口没起来：诊断里的 journalctl 会有真实原因。"
                "常见：目标机缺 libc 兼容库（改用 Python 源码模式已自动规避）、"
                "端口被占用、SELinux 拒绝执行。")
    return "查看返回的 diagnostics（journalctl / 端口 / SELinux）定位。"


def _deploy_python_files(client, stage, rollback):
    """解包源码到安装目录（先解到临时目录，再逐个 install 覆盖，原子性更好）。"""
    tmp = "/var/lib/aidbm-agent/unpack/" + os.path.basename(stage)
    _command(client, f"rm -rf {tmp} && mkdir -p {tmp} && tar -xzf {stage} -C {tmp}")
    for name in AGENT_SOURCE_FILES:
        _command(client, f"if test -f {tmp}/{name}; then install -m 600 {tmp}/{name} "
                         f"/opt/aidbm-agent/{name}; fi")
    _command(client, f"rm -rf {tmp}")
    # 启动器：systemd 的 ExecStart 始终指向 /opt/aidbm-agent/aidbm-agent
    _write(client, BINARY + ".new", LAUNCHER, 0o755)
