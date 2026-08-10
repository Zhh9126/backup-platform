# -*- coding: utf-8 -*-
"""
插件一键安装器（Plugin Installer）。

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
- 安全：记录全部 stdout/stderr，便于排错。
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
# 状态文件持久化（供前端轮询）
# ----------------------------------------------------------------------------
def _state_path(pid: str) -> Path:
    safe = pid.replace("/", "_").replace("\\", "_")
    return STATE_DIR / f"{safe}.json"


def _log_path(pid: str) -> Path:
    safe = pid.replace("/", "_").replace("\\", "_")
    return LOG_DIR / f"{safe}.log"


def _write_state(pid: str, state: dict) -> None:
    state["updated_at"] = datetime.utcnow().isoformat() + "Z"
    with open(_state_path(pid), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _append_log(pid: str, line: str) -> None:
    line = f"[{datetime.utcnow().isoformat()}Z] {line}\n"
    with open(_log_path(pid), "a", encoding="utf-8") as f:
        f.write(line)


def get_state(pid: str) -> Optional[dict]:
    p = _state_path(pid)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_states() -> List[dict]:
    rows = []
    for p in STATE_DIR.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                rows.append(json.load(f))
        except Exception:
            pass
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return rows


# ----------------------------------------------------------------------------
# 安装策略选择
# ----------------------------------------------------------------------------
def _select_strategy(manifest: dict) -> Optional[dict]:
    """根据当前 OS，选择一个最佳安装策略。

    优先级：离线下载 > 包管理器 > 纯手动指引。

    离线下载优先（offline_first）：因为最终部署目标为离线 Linux 服务器，
    离线包可以直接解压使用，不需要联网装包管理器。

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
    pkg = manifest.get("packages") or {}
    os_pkg = pkg.get(os_name) or {}
    pm = catalog.detect_package_manager()

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
# 安装流程
# ----------------------------------------------------------------------------
def _run_command(cmd: str, pid: str, timeout: int = 1800) -> dict:
    """在子进程中执行 shell 命令字符串，捕获 stdout/stderr 与退出码。

    出于安全考虑：仅在受信任的内置清单命令中调用，绝不 shell=True。
    """
    _append_log(pid, f"$ {cmd}")
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
        _append_log(pid, f"exit={ret.returncode}")
        if out:
            _append_log(pid, f"stdout: {out[-2000:]}")
        if err:
            _append_log(pid, f"stderr: {err[-2000:]}")
        return {
            "returncode": ret.returncode,
            "stdout": out,
            "stderr": err,
        }
    except subprocess.TimeoutExpired as e:
        _append_log(pid, f"timeout after {timeout}s")
        return {"returncode": -1, "stdout": "", "stderr": f"timeout: {e}"}
    except Exception as e:
        _append_log(pid, f"error: {e}")
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


def _download_and_extract(strategy: dict, pid: str) -> dict:
    """下载离线包并解压到 extract_dir。返回是否成功。"""
    import urllib.request
    import tarfile
    import zipfile

    url = strategy.get("url")
    extract_dir = strategy.get("extract_dir") or str(INSTALL_ROOT / pid)
    extract_path = Path(extract_dir)
    extract_path.mkdir(parents=True, exist_ok=True)
    _append_log(pid, f"download: {url}")
    _append_log(pid, f"extract to: {extract_path}")

    # 从 URL 提取真实扩展名，避免固定 .download 导致解压失败
    real_suffix = _guess_ext_from_url(url)
    tmp_path = INSTALL_ROOT / f"{pid}_{int(time.time())}{real_suffix}"
    _append_log(pid, f"tmp file: {tmp_path.name}")

    try:
        with urllib.request.urlopen(url, timeout=600) as resp, \
                open(tmp_path, "wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        _append_log(pid, f"downloaded: {tmp_path.stat().st_size} bytes")

        # 按扩展名选择解压方式
        if real_suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".gz", ".bz2", ".xz", ".tar"):
            with tarfile.open(tmp_path, "r:*") as tf:
                tf.extractall(extract_path)
        elif real_suffix == ".zip":
            with zipfile.ZipFile(tmp_path, "r") as zf:
                zf.extractall(extract_path)
        else:
            return {"ok": False, "message": f"不支持的压缩格式: {real_suffix} (URL: {url})"}

        _append_log(pid, f"extracted to: {extract_path}")
        return {"ok": True, "extract_dir": str(extract_path)}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _install_thread(pid: str, manifest: dict) -> None:
    """后台线程：执行安装策略，全程写入状态文件。"""
    _write_state(pid, {
        "id": pid,
        "plugin_id": pid,
        "name": manifest.get("name", pid),
        "status": "running",
        "method": None,
        "message": "准备开始安装",
        "progress": 5,
        "started_at": datetime.utcnow().isoformat() + "Z",
    })

    strategy = _select_strategy(manifest)
    if not strategy:
        _write_state(pid, {
            "id": pid,
            "name": manifest.get("name", pid),
            "status": "failed",
            "message": "当前系统暂不支持此插件的自动安装，请参考插件说明手工安装",
        })
        return

    _write_state(pid, {
        "id": pid,
        "name": manifest.get("name", pid),
        "status": "running",
        "method": strategy["method"],
        "message": strategy.get("note", ""),
        "progress": 20,
    })

    method = strategy["method"]
    if method == "manual_only":
        _write_state(pid, {
            "id": pid,
            "plugin_id": pid,
            "name": manifest.get("name", pid),
            "status": "manual",
            "method": method,
            "message": strategy.get("note", "请参考插件文档手工安装"),
            "progress": 100,
        })
        return

    dl = {}  # 离线下载结果，package_manager 分支下为空

    if method == "package_manager":
        cmd = strategy.get("command", "")
        if not cmd:
            _write_state(pid, {
                "id": pid,
                "name": manifest.get("name", pid),
                "status": "failed",
                "message": "清单缺少 package_manager 命令",
            })
            return
        result = _run_command(cmd, pid, timeout=1800)
        if result["returncode"] != 0:
            _write_state(pid, {
                "id": pid,
                "name": manifest.get("name", pid),
                "status": "failed",
                "method": method,
                "message": "包管理器安装失败，请查看日志",
                "stderr_tail": (result.get("stderr") or "")[-800:],
                "progress": 60,
            })
            return
        _write_state(pid, {
            "id": pid,
            "name": manifest.get("name", pid),
            "status": "running",
            "method": method,
            "message": "安装命令执行成功，开始验证",
            "progress": 80,
        })

    elif method == "fallback_download":
        dl = _download_and_extract(strategy, pid)
        if not dl.get("ok"):
            _write_state(pid, {
                "id": pid,
                "plugin_id": pid,
                "name": manifest.get("name", pid),
                "status": "failed",
                "method": method,
                "message": f"下载/解压失败: {dl.get('message')}",
                "progress": 60,
            })
            return
        _append_log(pid, f"extract_dir: {dl['extract_dir']}")

    # 安装后验证：检查 required_clients 是否就绪
    final = catalog.check_installed(manifest)
    if final["installed"]:
        _write_state(pid, {
            "id": pid,
            "plugin_id": pid,
            "name": manifest.get("name", pid),
            "status": "success",
            "method": method,
            "extract_dir": dl.get("extract_dir", "") if method == "fallback_download" else "",
            "message": "安装成功，依赖二进制已就绪",
            "found_paths": final["found_paths"],
            "progress": 100,
        })
    else:
        # 即使未在默认 PATH 中发现，也提示人工加 PATH
        tips = manifest.get("post_install_tips") or []
        msg = "依赖二进制未在 PATH 中发现，请检查并加入 PATH 环境变量。"
        if tips:
            msg += "\n提示：" + "；".join(tips[:2])
        _write_state(pid, {
            "id": pid,
            "plugin_id": pid,
            "name": manifest.get("name", pid),
            "status": "success_with_warn",
            "method": method,
            "extract_dir": dl.get("extract_dir", "") if method == "fallback_download" else "",
            "message": msg,
            "missing": final["missing"],
            "progress": 100,
        })


def install(pid: str) -> dict:
    """异步触发安装，返回初始状态。"""
    manifest = catalog.load_all().get(pid)
    if not manifest:
        return {"ok": False, "message": f"插件不存在: {pid}"}

    # 已安装则无需重复
    if catalog.check_installed(manifest)["installed"]:
        return {
            "ok": True,
            "message": "已安装，无需重复操作",
            "installed": True,
        }

    _append_log(pid, f"=== install requested ===")
    t = threading.Thread(
        target=_install_thread, args=(pid, manifest), daemon=True
    )
    t.start()
    state = get_state(pid) or {
        "id": pid, "name": manifest.get("name", pid),
        "status": "queued", "message": "任务已加入队列"
    }
    return {"ok": True, "state": state}


def uninstall(pid: str) -> dict:
    """卸载：仅清理平台管理的离线下载产物（state/log/installed/）。

    系统包管理器安装的包需要用户自行 `apt remove` 或 `choco uninstall`，
    这里给出明确指引。
    """
    state = get_state(pid)
    msg = []
    # 删除离线下载的安装目录
    target = INSTALL_ROOT / pid.replace("/", "_").replace("\\", "_")
    if target.exists():
        try:
            shutil.rmtree(target)
            msg.append(f"已删除离线安装目录 {target}")
        except Exception as e:
            msg.append(f"删除离线目录失败: {e}")
    # 清理状态/日志
    for p in (_state_path(pid), _log_path(pid)):
        try:
            if p.exists():
                p.unlink()
                msg.append(f"清理 {p}")
        except Exception:
            pass
    return {"ok": True, "message": "；".join(msg) or "无需清理",
            "state": state}