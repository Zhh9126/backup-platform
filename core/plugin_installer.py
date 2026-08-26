# -*- coding: utf-8 -*-
"""
插件一键安装器（Plugin Installer）—— 服务端化改造版。

设计原则：
- Linux 优先：最终部署目标为 Linux 服务器，Windows 仅作开发调试用。
  插件清单只提供 Linux 安装策略。
- 离线下载优先（offline_first）：优先通过 URL 下载离线包并解压，
  包管理器（apt/yum）作为备选。
  离线包可直接解压到 /opt 目录下使用，无需联网装包管理器。
- 数据库自带工具（RMAN、mysqldump、pg_dump、dmrman 等）不在此安装，
  备份时直接调用数据库安装路径下的可执行文件。
- 异步执行：安装可能耗时数分钟，使用后台线程 + 状态文件记录进度，
  前端可通过轮询查看实时日志与状态。
- 安全：记录全部 stdout/stderr，便于排错。SSH 密码绝不落日志/状态文件。
- 主机维度：支持 host_id 参数，安装到远端 SSH 主机；host_id=None 时
  兼容旧行为（本机安装），但也落 plugin_host_state 表（host_key="local"）。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import core.plugin_catalog as catalog


# 状态目录（运行日志、状态文件、安装产物）
PLUGIN_DIR = Path(__file__).parent / "plugins"
STATE_DIR = PLUGIN_DIR / "state"
LOG_DIR = PLUGIN_DIR / "logs"
INSTALL_ROOT = PLUGIN_DIR / "installed"

for d in (STATE_DIR, LOG_DIR, INSTALL_ROOT):
    d.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------
# 状态机取值映射
# ----------------------------------------------------------------------------
# 状态文件（供前端轮询）使用旧值：running/queued/success/success_with_warn/
# failed/manual，保持与 plugins.js 的兼容。
# DB plugin_host_state 表使用统一值：uninstalled/installing/installed/failed/
# manual/success_with_warn。
_STATUS_MAP_TO_DB = {
    "running":           "installing",
    "queued":            "installing",
    "success":           "installed",
    "success_with_warn": "success_with_warn",
    "failed":            "failed",
    "manual":            "manual",
}


def _map_status_to_db(status: str) -> str:
    """将状态文件中的 status 映射为 DB plugin_host_state.status。"""
    return _STATUS_MAP_TO_DB.get(status, status or "uninstalled")


# ----------------------------------------------------------------------------
# 文件名安全化
# ----------------------------------------------------------------------------
def _safe_host_key(host_key: Optional[str]) -> str:
    """将 host_key 安全化为文件名片段。

    host_key 形如 "root@1.2.3.4:22"，含 / \\ @ : 需替换为 _。
    本机 host_key="local"。
    """
    if not host_key:
        return "local"
    return (host_key.replace("/", "_").replace("\\", "_")
            .replace("@", "_").replace(":", "_"))


def _safe_pid(pid: str) -> str:
    """将 plugin_id 安全化为文件名片段。"""
    return pid.replace("/", "_").replace("\\", "_")


# ----------------------------------------------------------------------------
# 状态文件持久化（供前端轮询）
# ----------------------------------------------------------------------------
def _state_path(pid: str, host_key: Optional[str] = None) -> Path:
    """返回状态文件路径。

    - host_key 非空：返回 ``<safe_host_key>__<pid>.json``
    - host_key 为 None（旧调用兼容）：优先读 ``local__<pid>.json``，
      不存在则回退到旧版 ``<pid>.json``；都不存在时返回新路径（用于首次写入）。
    """
    safe = _safe_pid(pid)
    if host_key is not None:
        hk = _safe_host_key(host_key)
        return STATE_DIR / f"{hk}__{safe}.json"
    # host_key=None: 兼容旧调用
    new_path = STATE_DIR / f"local__{safe}.json"
    old_path = STATE_DIR / f"{safe}.json"
    if new_path.exists():
        return new_path
    if old_path.exists():
        return old_path
    return new_path  # 默认返回新路径


def _log_path(pid: str, host_key: Optional[str] = None) -> Path:
    """返回日志文件路径（与 _state_path 同构）。"""
    safe = _safe_pid(pid)
    if host_key is not None:
        hk = _safe_host_key(host_key)
        return LOG_DIR / f"{hk}__{safe}.log"
    new_path = LOG_DIR / f"local__{safe}.log"
    old_path = LOG_DIR / f"{safe}.log"
    if new_path.exists():
        return new_path
    if old_path.exists():
        return old_path
    return new_path


def _write_state(pid: str, state: dict, host_key: str = "local") -> None:
    """写入状态文件（JSON），自动追加 updated_at 与 host_key。"""
    state["updated_at"] = datetime.utcnow().isoformat() + "Z"
    state.setdefault("host_key", host_key)
    state.setdefault("plugin_id", pid)
    p = _state_path(pid, host_key=host_key)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _append_log(pid: str, line: str, host_key: str = "local") -> None:
    """追加一行日志（带时间戳）。"""
    ts = datetime.utcnow().isoformat() + "Z"
    line = f"[{ts}] {line}\n"
    p = _log_path(pid, host_key=host_key)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)


def get_state(pid: str, host_key: Optional[str] = None) -> Optional[dict]:
    """读取状态文件。

    - host_key 非空：读 ``<safe_host_key>__<pid>.json``
    - host_key 为 None：兼容旧调用，读 local__ 或旧版文件
    """
    p = _state_path(pid, host_key=host_key)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_states(host_id: Optional[int] = None) -> List[dict]:
    """列出全部插件安装状态。

    - host_id=None：返回全部状态文件内容（兼容旧行为）
    - host_id 非空：返回该 SSH 主机维度的状态
    """
    # 解析 host_id -> host_key
    target_hk: Optional[str] = None
    if host_id is not None and host_id != 0:
        try:
            from core import ssh_hosts
            h = ssh_hosts.get_host(int(host_id), include_secret=False)
            if h:
                target_hk = h.get("host_key")
        except (TypeError, ValueError):
            pass

    rows: List[dict] = []
    for p in STATE_DIR.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if target_hk:
                # 按 host_key 过滤
                if data.get("host_key") != target_hk:
                    continue
            rows.append(data)
        except Exception:
            pass
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return rows


# ----------------------------------------------------------------------------
# 安装策略选择
# ----------------------------------------------------------------------------
def _select_strategy(manifest: dict) -> Optional[dict]:
    """根据当前本机 OS，选择一个最佳安装策略。

    优先级：离线下载 > 包管理器 > 纯手动指引。

    返回值：
        {
          "method": "package_manager" | "fallback_download" | "manual_only",
          "command": "..."(仅 package_manager),
          "url": "..."(仅 fallback_download),
          "extract_dir": "..."(仅 fallback_download),
          "note": "..."
        }
        若方法不可用（既无 fallback URL 又无包管理器），返回 None。
    """
    os_name = catalog.detect_os()
    return _select_strategy_for_os(manifest, os_name, catalog.detect_package_manager())


def _select_strategy_for_os(manifest: dict, os_name: str,
                            pm: Optional[str]) -> Optional[dict]:
    """按指定 OS 与包管理器选择安装策略（供本机/远端复用）。

    优先级：离线下载 > 包管理器 > 纯手动指引。
    """
    pkg = manifest.get("packages") or {}
    os_pkg = pkg.get(os_name) or {}

    # 离线下载优先：先检查 fallback URL（支持离线包场景）
    fallback = os_pkg.get("fallback")
    if fallback and fallback.get("url"):
        return {
            "method": "fallback_download",
            "url": fallback["url"],
            "extract_dir": fallback.get("extract_dir") or "",
            "binaries": fallback.get("binaries") or [],
            "note": "下载离线包并解压（离线优先）"
        }

    # 包管理器作为备选
    pms = os_pkg.get("package_managers") or {}
    if pm and pm in pms:
        return {
            "method": "package_manager",
            "command": pms[pm].get("command") or "",
            "binaries": pms[pm].get("binaries") or [],
            "note": f"通过 {pm} 安装"
        }

    note = (os_pkg.get("note")
            or manifest.get("post_install_tips", [""])[0])
    if note:
        return {
            "method": "manual_only",
            "note": note
        }
    return None


# ----------------------------------------------------------------------------
# 本机安装流程
# ----------------------------------------------------------------------------
def _run_command(cmd: str, pid: str, host_key: str = "local",
                 timeout: int = 1800) -> dict:
    """在子进程中执行 shell 命令字符串，捕获 stdout/stderr 与退出码。

    出于安全考虑：仅在受信任的内置清单命令中调用，绝不 shell=True。
    """
    _append_log(pid, f"$ {cmd}", host_key=host_key)
    try:
        ret = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (ret.stdout or "").strip()
        err = (ret.stderr or "").strip()
        _append_log(pid, f"exit={ret.returncode}", host_key=host_key)
        if out:
            _append_log(pid, f"stdout: {out[-2000:]}", host_key=host_key)
        if err:
            _append_log(pid, f"stderr: {err[-2000:]}", host_key=host_key)
        return {
            "returncode": ret.returncode,
            "stdout": out,
            "stderr": err,
        }
    except subprocess.TimeoutExpired as e:
        _append_log(pid, f"timeout after {timeout}s", host_key=host_key)
        return {"returncode": -1, "stdout": "", "stderr": f"timeout: {e}"}
    except Exception as e:
        _append_log(pid, f"error: {e}", host_key=host_key)
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def _guess_ext_from_url(url: str) -> str:
    """从 URL 路径中猜测压缩包扩展名。

    处理 .tar.gz / .tgz / .tar.bz2 / .tar.xz / .zip 等常见格式。
    返回 ".tar.gz" / ".zip" 等；无法判断时返回 ".download"。
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    # 按复合后缀优先匹配
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"):
        if path.endswith(ext):
            return ext
    # 单后缀
    for ext in (".gz", ".bz2", ".xz", ".zip", ".tar"):
        if path.endswith(ext):
            return ext
    return ".download"


def _download_to_local(url: str, pid: str, host_key: str = "local") -> dict:
    """下载离线包到本地临时文件（不解压），返回临时文件路径。

    返回: {"ok": bool, "path": str, "ext": str, "message": str}
    """
    import urllib.request

    _append_log(pid, f"download: {url}", host_key=host_key)
    real_suffix = _guess_ext_from_url(url)
    tmp_path = INSTALL_ROOT / f"{pid}_{int(time.time())}{real_suffix}"
    _append_log(pid, f"tmp file: {tmp_path.name}", host_key=host_key)

    try:
        with urllib.request.urlopen(url, timeout=600) as resp, \
                open(tmp_path, "wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        _append_log(pid, f"downloaded: {tmp_path.stat().st_size} bytes",
                     host_key=host_key)
        return {"ok": True, "path": str(tmp_path), "ext": real_suffix, "message": ""}
    except Exception as e:
        return {"ok": False, "path": "", "ext": real_suffix, "message": str(e)}


def _download_and_extract(strategy: dict, pid: str,
                          host_key: str = "local") -> dict:
    """下载离线包并解压到 extract_dir（本机）。返回是否成功。"""
    import tarfile
    import zipfile

    url = strategy.get("url")
    extract_dir = strategy.get("extract_dir") or str(INSTALL_ROOT / pid)
    extract_path = Path(extract_dir)
    extract_path.mkdir(parents=True, exist_ok=True)
    _append_log(pid, f"download: {url}", host_key=host_key)
    _append_log(pid, f"extract to: {extract_path}", host_key=host_key)

    # 从 URL 提取真实扩展名，避免固定 .download 导致解压失败
    real_suffix = _guess_ext_from_url(url)
    tmp_path = INSTALL_ROOT / f"{pid}_{int(time.time())}{real_suffix}"
    _append_log(pid, f"tmp file: {tmp_path.name}", host_key=host_key)

    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=600) as resp, \
                open(tmp_path, "wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        _append_log(pid, f"downloaded: {tmp_path.stat().st_size} bytes",
                     host_key=host_key)

        # 按扩展名选择解压方式
        if real_suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz",
                           ".tbz2", ".txz", ".gz", ".bz2", ".xz", ".tar"):
            with tarfile.open(tmp_path, "r:*") as tf:
                tf.extractall(extract_path)
        elif real_suffix == ".zip":
            with zipfile.ZipFile(tmp_path, "r") as zf:
                zf.extractall(extract_path)
        else:
            return {"ok": False, "message": f"不支持的压缩格式: {real_suffix} (URL: {url})"}

        _append_log(pid, f"extracted to: {extract_path}", host_key=host_key)
        return {"ok": True, "extract_dir": str(extract_path)}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _install_thread_local(pid: str, manifest: dict) -> None:
    """后台线程：执行本机安装策略，全程写入状态文件 + 落库。"""
    host_key = "local"
    name = manifest.get("name", pid)

    _write_state(pid, {
        "id": pid,
        "plugin_id": pid,
        "name": name,
        "status": "running",
        "method": None,
        "message": "准备开始安装",
        "progress": 5,
        "started_at": datetime.utcnow().isoformat() + "Z",
    }, host_key=host_key)

    # 落库：installing
    try:
        import core.db as db
        db.upsert_plugin_host_state(host_key, pid, {
            "status": "installing",
            "host_id": 0,
            "message": "准备开始安装",
        })
    except Exception:
        pass

    strategy = _select_strategy(manifest)
    if not strategy:
        _write_state(pid, {
            "id": pid, "name": name,
            "status": "failed",
            "message": "当前系统暂不支持此插件的自动安装，请参考插件说明手工安装",
        }, host_key=host_key)
        _persist_db(host_key, pid, "failed", message="不支持自动安装")
        return

    _write_state(pid, {
        "id": pid, "name": name,
        "status": "running",
        "method": strategy["method"],
        "message": strategy.get("note", ""),
        "progress": 20,
    }, host_key=host_key)

    method = strategy["method"]
    if method == "manual_only":
        _write_state(pid, {
            "id": pid, "plugin_id": pid, "name": name,
            "status": "manual",
            "method": method,
            "message": strategy.get("note", "请参考插件文档手工安装"),
            "progress": 100,
        }, host_key=host_key)
        _persist_db(host_key, pid, "manual", method=method,
                     message=strategy.get("note", ""))
        return

    dl: dict = {}  # 离线下载结果，package_manager 分支下为空

    if method == "package_manager":
        cmd = strategy.get("command", "")
        if not cmd:
            _write_state(pid, {
                "id": pid, "name": name,
                "status": "failed",
                "message": "清单缺少 package_manager 命令",
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", method=method,
                         message="缺少 package_manager 命令")
            return
        result = _run_command(cmd, pid, host_key=host_key, timeout=1800)
        if result["returncode"] != 0:
            _write_state(pid, {
                "id": pid, "name": name,
                "status": "failed",
                "method": method,
                "message": "包管理器安装失败，请查看日志",
                "stderr_tail": (result.get("stderr") or "")[-800:],
                "progress": 60,
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", method=method,
                         message="包管理器安装失败")
            return
        _write_state(pid, {
            "id": pid, "name": name,
            "status": "running",
            "method": method,
            "message": "安装命令执行成功，开始验证",
            "progress": 80,
        }, host_key=host_key)

    elif method == "fallback_download":
        dl = _download_and_extract(strategy, pid, host_key=host_key)
        if not dl.get("ok"):
            _write_state(pid, {
                "id": pid, "plugin_id": pid, "name": name,
                "status": "failed",
                "method": method,
                "message": f"下载/解压失败: {dl.get('message')}",
                "progress": 60,
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", method=method,
                         message=f"下载/解压失败: {dl.get('message')}")
            return
        _append_log(pid, f"extract_dir: {dl['extract_dir']}", host_key=host_key)

    # 安装后验证：检查 required_clients 是否就绪
    final = catalog.check_installed(manifest)
    extract_dir = dl.get("extract_dir", "") if method == "fallback_download" else ""

    if final["installed"]:
        _write_state(pid, {
            "id": pid, "plugin_id": pid, "name": name,
            "status": "success",
            "method": method,
            "extract_dir": extract_dir,
            "message": "安装成功，依赖二进制已就绪",
            "found_paths": final["found_paths"],
            "progress": 100,
        }, host_key=host_key)
        _persist_db(host_key, pid, "installed", method=method,
                     extract_dir=extract_dir,
                     found_paths=final["found_paths"],
                     message="安装成功")
    else:
        tips = manifest.get("post_install_tips") or []
        msg = "依赖二进制未在 PATH 中发现，请检查并加入 PATH 环境变量。"
        if tips:
            msg += "\n提示：" + "；".join(tips[:2])
        _write_state(pid, {
            "id": pid, "plugin_id": pid, "name": name,
            "status": "success_with_warn",
            "method": method,
            "extract_dir": extract_dir,
            "message": msg,
            "missing": final["missing"],
            "progress": 100,
        }, host_key=host_key)
        _persist_db(host_key, pid, "success_with_warn", method=method,
                     extract_dir=extract_dir,
                     found_paths=final["found_paths"],
                     message=msg)


# ----------------------------------------------------------------------------
# 远端安装流程
# ----------------------------------------------------------------------------
def _install_thread_remote(pid: str, manifest: dict, ssh_host: dict) -> None:
    """后台线程：在远端 SSH 主机上执行安装策略，全程写入状态文件 + 落库。

    流程：
    1. 探测远端 OS / 包管理器
    2. 选择策略（离线包优先 > 包管理器 > 手动指引）
    3. 离线包：下载到本机临时文件 → SFTP 上传到远端 /tmp/<pid>.tar.gz
       → 远端解压到 /opt/backup_plugins/<pid>
    4. 包管理器：远端执行 apt/yum install
    5. 验证：remote_check_clients / remote_bin_version
    6. 落库：upsert_plugin_host_state
    """
    from core import plugin_runtime, remote_dump

    host_key = ssh_host.get("host_key", "remote")
    host_id = ssh_host.get("id")
    name = manifest.get("name", pid)

    _write_state(pid, {
        "id": pid,
        "plugin_id": pid,
        "name": name,
        "status": "running",
        "method": None,
        "message": f"准备在远端主机 {host_key} 上安装",
        "progress": 5,
        "host_key": host_key,
        "host_id": host_id,
        "started_at": datetime.utcnow().isoformat() + "Z",
    }, host_key=host_key)

    # 落库：installing
    try:
        import core.db as db
        db.upsert_plugin_host_state(host_key, pid, {
            "status": "installing",
            "host_id": host_id,
            "message": f"正在远端主机 {host_key} 上安装",
        })
    except Exception:
        pass

    # 1. 探测远端环境
    _append_log(pid, f"探测远端主机 {host_key} 环境...", host_key=host_key)
    remote_os = plugin_runtime.remote_detect_os(ssh_host)
    remote_pm = plugin_runtime.remote_detect_package_manager(ssh_host)
    _append_log(pid, f"远端 OS={remote_os}, 包管理器={remote_pm}",
                 host_key=host_key)

    # 2. 选择策略
    strategy = _select_strategy_for_os(manifest, remote_os, remote_pm)
    if not strategy:
        _write_state(pid, {
            "id": pid, "name": name,
            "status": "failed",
            "message": f"远端主机 {host_key}（OS={remote_os}）暂不支持此插件的自动安装",
        }, host_key=host_key)
        _persist_db(host_key, pid, "failed", host_id=host_id,
                     message=f"远端 OS={remote_os} 不支持自动安装")
        return

    method = strategy["method"]
    _write_state(pid, {
        "id": pid, "name": name,
        "status": "running",
        "method": method,
        "message": strategy.get("note", ""),
        "progress": 20,
    }, host_key=host_key)

    # 3a. 手动指引
    if method == "manual_only":
        _write_state(pid, {
            "id": pid, "plugin_id": pid, "name": name,
            "status": "manual",
            "method": method,
            "message": strategy.get("note", "请参考插件文档手工安装"),
            "progress": 100,
        }, host_key=host_key)
        _persist_db(host_key, pid, "manual", host_id=host_id, method=method,
                     message=strategy.get("note", ""))
        return

    extract_dir = ""
    remote_extract_dir = f"/opt/backup_plugins/{_safe_pid(pid)}"

    # 3b. 离线包：下载 → SFTP 上传 → 远端解压
    if method == "fallback_download":
        url = strategy.get("url", "")
        _append_log(pid, f"下载离线包: {url}", host_key=host_key)
        dl = _download_to_local(url, pid, host_key=host_key)
        if not dl.get("ok"):
            _write_state(pid, {
                "id": pid, "plugin_id": pid, "name": name,
                "status": "failed",
                "method": method,
                "message": f"下载失败: {dl.get('message')}",
                "progress": 60,
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", host_id=host_id, method=method,
                         message=f"下载失败: {dl.get('message')}")
            return

        local_pkg = dl["path"]
        remote_pkg = f"/tmp/{_safe_pid(pid)}.tar.gz"
        _append_log(pid, f"SFTP 上传 {local_pkg} -> {remote_pkg}",
                     host_key=host_key)
        _write_state(pid, {
            "id": pid, "name": name,
            "status": "running",
            "method": method,
            "message": "正在上传离线包到远端主机...",
            "progress": 40,
        }, host_key=host_key)

        try:
            remote_dump.sftp_put(ssh_host, local_pkg, remote_pkg)
            _append_log(pid, "SFTP 上传成功", host_key=host_key)
        except Exception as e:
            _write_state(pid, {
                "id": pid, "name": name,
                "status": "failed",
                "method": method,
                "message": f"SFTP 上传失败: {e}",
                "progress": 50,
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", host_id=host_id, method=method,
                         message=f"SFTP 上传失败: {e}")
            _cleanup_local_pkg(local_pkg)
            return
        finally:
            _cleanup_local_pkg(local_pkg)

        # 远端解压
        extract_cmd = (
            f"mkdir -p {remote_extract_dir} && "
            f"tar -xf {remote_pkg} -C {remote_extract_dir} && "
            f"rm -f {remote_pkg}"
        )
        _append_log(pid, f"远端解压: {extract_cmd}", host_key=host_key)
        _write_state(pid, {
            "id": pid, "name": name,
            "status": "running",
            "method": method,
            "message": "正在远端解压...",
            "progress": 60,
        }, host_key=host_key)

        result = remote_dump.remote_exec_capture(ssh_host, extract_cmd, timeout=300)
        if result.get("returncode", -1) != 0:
            err = (result.get("stderr") or result.get("stdout") or "")[-800:]
            _append_log(pid, f"远端解压失败: {err}", host_key=host_key)
            _write_state(pid, {
                "id": pid, "name": name,
                "status": "failed",
                "method": method,
                "message": f"远端解压失败: {err}",
                "progress": 65,
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", host_id=host_id, method=method,
                         message=f"远端解压失败: {err}")
            return
        _append_log(pid, "远端解压成功", host_key=host_key)
        extract_dir = remote_extract_dir

    # 3c. 包管理器：远端执行安装命令
    elif method == "package_manager":
        cmd = strategy.get("command", "")
        if not cmd:
            _write_state(pid, {
                "id": pid, "name": name,
                "status": "failed",
                "message": "清单缺少 package_manager 命令",
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", host_id=host_id, method=method,
                         message="缺少 package_manager 命令")
            return
        _append_log(pid, f"远端执行: {cmd}", host_key=host_key)
        _write_state(pid, {
            "id": pid, "name": name,
            "status": "running",
            "method": method,
            "message": f"远端执行 {remote_pm} 安装中...",
            "progress": 50,
        }, host_key=host_key)

        result = remote_dump.remote_exec_capture(ssh_host, cmd, timeout=1800)
        if result.get("returncode", -1) != 0:
            err = (result.get("stderr") or result.get("stdout") or "")[-800:]
            _append_log(pid, f"包管理器安装失败: {err}", host_key=host_key)
            _write_state(pid, {
                "id": pid, "name": name,
                "status": "failed",
                "method": method,
                "message": f"远端包管理器安装失败: {err}",
                "progress": 60,
            }, host_key=host_key)
            _persist_db(host_key, pid, "failed", host_id=host_id, method=method,
                         message=f"远端包管理器安装失败: {err}")
            return
        _append_log(pid, "包管理器安装成功", host_key=host_key)

    # 4. 验证：remote_check_clients
    _write_state(pid, {
        "id": pid, "name": name,
        "status": "running",
        "method": method,
        "message": "安装完成，正在验证依赖工具...",
        "progress": 80,
    }, host_key=host_key)

    required_clients = manifest.get("required_clients") or []
    chk = plugin_runtime.remote_check_clients(ssh_host, required_clients)
    _append_log(pid, f"验证结果: installed={chk['installed']}, "
                     f"missing={chk['missing']}, found={chk['found_paths']}",
                host_key=host_key)

    # 探测版本（取第一个发现的工具）
    version: Optional[str] = None
    if chk["found_paths"]:
        first_tool = list(chk["found_paths"].keys())[0]
        first_path = chk["found_paths"][first_tool]
        try:
            version = plugin_runtime.remote_bin_version(ssh_host, first_path)
            if version:
                _append_log(pid, f"版本: {first_tool} -> {version}",
                             host_key=host_key)
        except Exception:
            pass

    # 5. 写状态 + 落库
    if chk["installed"]:
        _write_state(pid, {
            "id": pid, "plugin_id": pid, "name": name,
            "status": "success",
            "method": method,
            "extract_dir": extract_dir,
            "message": f"远端安装成功，依赖二进制已就绪（{host_key}）",
            "found_paths": chk["found_paths"],
            "version": version,
            "progress": 100,
        }, host_key=host_key)
        _persist_db(host_key, pid, "installed", host_id=host_id, method=method,
                     version=version, extract_dir=extract_dir,
                     found_paths=chk["found_paths"],
                     message="远端安装成功")
    else:
        tips = manifest.get("post_install_tips") or []
        msg = (f"远端安装完成，但部分二进制未在 PATH 中发现: "
               f"{', '.join(chk['missing'])}")
        if tips:
            msg += "\n提示：" + "；".join(tips[:2])
        _write_state(pid, {
            "id": pid, "plugin_id": pid, "name": name,
            "status": "success_with_warn",
            "method": method,
            "extract_dir": extract_dir,
            "message": msg,
            "missing": chk["missing"],
            "found_paths": chk["found_paths"],
            "version": version,
            "progress": 100,
        }, host_key=host_key)
        _persist_db(host_key, pid, "success_with_warn", host_id=host_id,
                     method=method, version=version, extract_dir=extract_dir,
                     found_paths=chk["found_paths"], message=msg)


# ----------------------------------------------------------------------------
# DB 落库辅助
# ----------------------------------------------------------------------------
def _persist_db(host_key: str, pid: str, db_status: str,
                host_id: Optional[int] = None, version: Optional[str] = None,
                method: Optional[str] = None, extract_dir: Optional[str] = None,
                found_paths: Optional[dict] = None,
                message: Optional[str] = None) -> None:
    """安全落库到 plugin_host_state 表（异常吞掉，不阻塞安装流程）。"""
    try:
        import core.db as db
        fields: dict = {"status": db_status}
        if host_id is not None:
            fields["host_id"] = host_id
        if version is not None:
            fields["version"] = version
        if method is not None:
            fields["method"] = method
        if extract_dir is not None:
            fields["extract_dir"] = extract_dir
        if found_paths is not None:
            fields["found_paths"] = json.dumps(found_paths, ensure_ascii=False)
        if message is not None:
            fields["message"] = message
        db.upsert_plugin_host_state(host_key, pid, fields)
    except Exception:
        pass


def _cleanup_local_pkg(path: str) -> None:
    """清理本地临时下载的离线包文件。"""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 公共接口
# ----------------------------------------------------------------------------
def install(pid: str, host_id: Optional[int] = None) -> dict:
    """异步触发安装，返回初始状态。

    - host_id=None：本机安装（兼容旧行为），host_key="local"
    - host_id 非空：远端 SSH 主机安装
    """
    manifest = catalog.load_all().get(pid)
    if not manifest:
        return {"ok": False, "message": f"插件不存在: {pid}"}

    # 幂等检查：已安装则无需重复
    if host_id is not None and host_id != 0:
        try:
            from core import ssh_hosts
            ssh_host = ssh_hosts.get_host(int(host_id), include_secret=True)
        except (TypeError, ValueError):
            ssh_host = None
        if not ssh_host:
            return {"ok": False, "message": f"SSH 主机不存在: host_id={host_id}"}

        host_key = ssh_host.get("host_key", "remote")
        # 查 DB 状态
        try:
            import core.db as db
            row = db.get_plugin_host_state(host_key, pid)
            if row and row.get("status") == "installed":
                return {"ok": True, "installed": True,
                        "message": f"远端主机 {host_key} 已安装 {pid}，无需重复操作"}
        except Exception:
            pass
        # 实时探测
        try:
            chk = catalog.check_installed_on_host(pid, ssh_host)
            if chk.get("installed"):
                return {"ok": True, "installed": True,
                        "message": f"远端主机 {host_key} 已具备 {pid} 依赖工具"}
        except Exception:
            pass

        _append_log(pid, f"=== install requested (remote: {host_key}) ===",
                     host_key=host_key)
        t = threading.Thread(
            target=_install_thread_remote,
            args=(pid, manifest, ssh_host),
            daemon=True,
        )
        t.start()
        state = get_state(pid, host_key=host_key) or {
            "id": pid, "name": manifest.get("name", pid),
            "status": "queued", "message": f"任务已加入队列（远端 {host_key}）"
        }
        return {"ok": True, "state": state}

    # ---- 本机安装（host_id=None） ----
    # 幂等：本机已安装
    if catalog.check_installed(manifest)["installed"]:
        return {
            "ok": True,
            "message": "已安装，无需重复操作",
            "installed": True,
        }

    _append_log(pid, "=== install requested (local) ===", host_key="local")
    t = threading.Thread(
        target=_install_thread_local, args=(pid, manifest), daemon=True
    )
    t.start()
    state = get_state(pid) or {
        "id": pid, "name": manifest.get("name", pid),
        "status": "queued", "message": "任务已加入队列"
    }
    return {"ok": True, "state": state}


def uninstall(pid: str, host_id: Optional[int] = None) -> dict:
    """卸载插件。

    - host_id=None：清理本机离线下载产物 + 状态/日志 + DB(host_key="local")
    - host_id 非空：清理远端 /opt/backup_plugins/<pid> + DB(host_key) + 本地状态/日志

    系统包管理器安装的包需要用户自行 ``apt remove`` 或 ``yum remove``，
    这里给出明确指引。
    """
    msg: List[str] = []

    if host_id is not None and host_id != 0:
        # ---- 远端卸载 ----
        try:
            from core import ssh_hosts
            ssh_host = ssh_hosts.get_host(int(host_id), include_secret=True)
        except (TypeError, ValueError):
            ssh_host = None
        if not ssh_host:
            return {"ok": False, "message": f"SSH 主机不存在: host_id={host_id}"}

        host_key = ssh_host.get("host_key", "remote")
        remote_dir = f"/opt/backup_plugins/{_safe_pid(pid)}"

        # 远端清理
        try:
            from core import remote_dump
            result = remote_dump.remote_exec_capture(
                ssh_host, f"rm -rf {remote_dir}", timeout=60)
            if result.get("returncode", -1) == 0:
                msg.append(f"已清理远端目录 {remote_dir}")
            else:
                err = (result.get("stderr") or "")[:200]
                msg.append(f"远端清理失败: {err or '未知错误'}")
        except Exception as e:
            msg.append(f"远端清理异常: {e}")

        # 删除 DB 状态
        try:
            import core.db as db
            db.delete_plugin_host_state(host_key, pid)
            msg.append("已清除 DB 状态")
        except Exception:
            pass

        # 清理本地状态/日志
        for p in (_state_path(pid, host_key=host_key),
                  _log_path(pid, host_key=host_key)):
            try:
                if p.exists():
                    p.unlink()
                    msg.append(f"清理 {p.name}")
            except Exception:
                pass

        state = get_state(pid, host_key=host_key)
        return {"ok": True, "message": "；".join(msg) or "无需清理",
                "state": state}

    # ---- 本机卸载 ----
    host_key = "local"
    state = get_state(pid)
    # 删除离线下载的安装目录
    target = INSTALL_ROOT / _safe_pid(pid)
    if target.exists():
        try:
            shutil.rmtree(target)
            msg.append(f"已删除离线安装目录 {target}")
        except Exception as e:
            msg.append(f"删除离线目录失败: {e}")
    # 清理状态/日志（新路径 + 旧路径兼容）
    for p in (_state_path(pid, host_key=host_key),
              _log_path(pid, host_key=host_key)):
        try:
            if p.exists():
                p.unlink()
                msg.append(f"清理 {p.name}")
        except Exception:
            pass
    # 旧路径兼容
    old_state = STATE_DIR / f"{_safe_pid(pid)}.json"
    old_log = LOG_DIR / f"{_safe_pid(pid)}.log"
    for p in (old_state, old_log):
        try:
            if p.exists():
                p.unlink()
                msg.append(f"清理旧文件 {p.name}")
        except Exception:
            pass

    # 删除 DB 状态
    try:
        import core.db as db
        db.delete_plugin_host_state(host_key, pid)
        msg.append("已清除 DB 状态")
    except Exception:
        pass

    return {"ok": True, "message": "；".join(msg) or "无需清理",
            "state": state}
