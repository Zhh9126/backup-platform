# -*- coding: utf-8 -*-
"""
插件运行时分发（Plugin Runtime）：工具分类映射 + 远端探测辅助。

职责边界：
- 「数据库自带物理工具」硬编码映射（引擎直接调用，不在 manifests 中）：
  oracle→rman、postgresql→pg_basebackup、kingbase→sys_basebackup、dameng→dmrman。
- 「外部插件」由 manifests 的 `supports` 字段推导（避免双份维护）。
- 远端探测：复用 core.remote_dump 的 `_connect` / `_wrap_login` /
  `_resolve_remote_bin` / `remote_has_tool`，对给定 ssh_host 返回 OS、包管理器、
  二进制缺失清单与二进制版本。

无 Agent 约束：所有探测都通过 paramiko SSH 在远端数据库服务器上完成。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from core import remote_dump


# ----------------------------------------------------------------------------
# 工具分类映射
# ----------------------------------------------------------------------------
# 数据库自带物理工具（引擎直接调用，故硬编码；不在 manifests 里维护）
BUNDLED_PHYSICAL_TOOLS: Dict[str, List[str]] = {
    "oracle":     ["rman"],
    "postgresql": ["pg_basebackup"],
    "kingbase":   ["sys_basebackup"],
    "dameng":     ["dmrman"],
    "mysql":      [],
    "mariadb":    [],
    "redis":      [],
    "mongodb":    [],
}


def bundled_physical_tools(db_type: str) -> List[str]:
    """返回某数据库类型自带物理工具名列表（无则返回空列表）。"""
    key = (db_type or "").strip().lower()
    return list(BUNDLED_PHYSICAL_TOOLS.get(key, []))


def external_plugins_for_db_type(db_type: str) -> List[dict]:
    """返回某数据库类型对应的「外部插件」清单（由 manifests.supports 推导）。

    例如 mysql → percona-xtrabackup-80 / percona-xtrabackup-24 / mariabackup；
    postgresql → pgbackrest；mongodb → mongodb-database-tools；redis → redis-tools。
    """
    import core.plugin_catalog as catalog  # 延迟导入，避免循环依赖

    key = (db_type or "").strip().lower()
    if not key:
        return []
    out: List[dict] = []
    for manifest in catalog.load_all().values():
        supports = manifest.get("supports") or []
        if key in supports:
            out.append(manifest)
    # 稳定排序：按 id
    out.sort(key=lambda m: m.get("id", ""))
    return out


def external_plugin_ids_for_db_type(db_type: str) -> List[str]:
    """返回某数据库类型对应的外部插件 id 列表。"""
    return [m.get("id") for m in external_plugins_for_db_type(db_type) if m.get("id")]


# ----------------------------------------------------------------------------
# 远端探测辅助
# ----------------------------------------------------------------------------
def _exec(ssh_host: dict, shell_cmd: str, timeout: int = 30):
    """在远端执行一条命令，返回 (stdout_str, stderr_str, returncode)。"""
    from core.engines.file import _ssh_exec_pipe

    client = None
    try:
        client = remote_dump._connect(ssh_host)
        out, err, rc = _ssh_exec_pipe(
            client, remote_dump._wrap_login(shell_cmd), timeout=timeout)
        stdout = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
        return stdout, (err or ""), rc
    except Exception as e:
        return "", str(e), -1
    finally:
        # 连接由 file._get_ssh_client 统一池化，这里不主动 close 以复用。
        pass


def remote_detect_os(ssh_host: dict) -> str:
    """探测远端主机 OS 类型：linux / windows / unknown。"""
    stdout, _err, rc = _exec(ssh_host, "uname -s 2>/dev/null || echo unknown", timeout=20)
    if rc != 0 or not stdout:
        return "unknown"
    sysname = stdout.strip().splitlines()[0].strip().lower()
    if "linux" in sysname:
        return "linux"
    if "mingw" in sysname or "msys" in sysname or "windows" in sysname or "cygwin" in sysname:
        return "windows"
    if "darwin" in sysname:
        return "macos"
    return "unknown"


def remote_detect_package_manager(ssh_host: dict) -> Optional[str]:
    """探测远端可用的 Linux 包管理器：apt / apt-get / yum / dnf（逐个探测）。

    复用 remote_dump.remote_has_tool（command -v / which 双兜底），
    天然规避 paramiko 非交互 shell 的 PATH 缺失。
    """
    for pm in ("apt", "apt-get", "yum", "dnf"):
        try:
            if remote_dump.remote_has_tool(ssh_host, pm):
                return pm
        except Exception:
            continue
    return None


def remote_check_clients(ssh_host: dict, tools: List[str]) -> dict:
    """在远端检查一组二进制是否就绪。

    返回:
        {
          "installed": bool,            # 全部就绪为 True
          "missing":   List[str],       # 缺失的二进制名
          "found_paths": Dict[str, str] # 已发现的二进制 -> 绝对路径
        }
    """
    tools = [t for t in (tools or []) if t]
    client = None
    try:
        client = remote_dump._connect(ssh_host)
    except Exception:
        client = None

    found: Dict[str, str] = {}
    missing: List[str] = []
    for tool in tools:
        path = None
        if client is not None:
            try:
                path = remote_dump._resolve_remote_bin(client, tool)
            except Exception:
                path = None
        if path:
            found[tool] = path
        else:
            missing.append(tool)
    return {
        "installed": len(missing) == 0,
        "missing": missing,
        "found_paths": found,
    }


def remote_bin_version(ssh_host: dict, bin_path: str, args: str = "--version") -> Optional[str]:
    """在远端执行 `<bin_path> <args>`，返回版本输出首行；失败返回 None。"""
    if not bin_path:
        return None
    import shlex
    cmd = f"{shlex.quote(bin_path)} {args}"
    stdout, _err, rc = _exec(ssh_host, cmd, timeout=60)
    if rc != 0:
        # 部分工具把版本打印到 stderr（如 redis-cli 旧版），尝试合并读取
        if not stdout:
            stdout, _err2, rc2 = _exec(ssh_host, f"{shlex.quote(bin_path)} {args} 2>&1", timeout=60)
            if rc2 != 0:
                return None
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            return line
    return None
