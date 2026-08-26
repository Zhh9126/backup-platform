# -*- coding: utf-8 -*-
"""
备份依赖插件目录（Plugin Catalog）。

每个数据库/文件备份依赖一组第三方客户端工具（如 xtrabackup、mariabackup、
pgbackrest、mongodump、redis-cli 等）。数据库自带工具（RMAN、mysqldump、pg_dump、
dmrman、sys_dump 等）不在此管理，备份时直接调用数据库安装路径下的可执行文件。

- 每个插件以 JSON manifest 描述自身元数据与安装策略（离线下载 URL + 包管理器备选）。
- 本模块负责从 manifests/ 目录加载全部清单，提供查询接口。
- 不涉及任何 IO/安装副作用：纯读取 + 状态聚合（哪些 binary 已就绪）。
- Linux 优先：最终部署目标为 Linux 服务器。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Dict, List, Optional


# 清单文件根目录（与本文件同级的 manifests/ 子目录）
MANIFESTS_DIR = Path(__file__).parent / "plugins" / "manifests"


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def detect_os() -> str:
    """探测当前操作系统，返回 "linux" / "windows" / "macos" / "unknown"。

    备份服务器多为 Linux/Windows；插件清单只声明这两类，其余视为 unknown。
    """
    sys = platform.system().lower()
    if sys.startswith("win"):
        return "windows"
    if sys.startswith("linux"):
        return "linux"
    if sys.startswith("darwin"):
        return "macos"
    return "unknown"


def detect_package_manager() -> Optional[str]:
    """探测可用的 Linux 包管理器：apt / yum / dnf。

    返回首个存在的包管理器名；都找不到则 None。
    注意：不再支持 Windows 包管理器（choco/scoop），最终部署目标为 Linux。
    """
    candidates = ["apt", "apt-get", "yum", "dnf"]
    for cmd in candidates:
        if shutil.which(cmd):
            return cmd
    return None


def which_any(names: List[str]) -> Optional[str]:
    """从候选 binary 中找出第一个 PATH 内可执行的；找不到返回 None。"""
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _resolve_host(host_id) -> Optional[dict]:
    """把 host_id 解析为 SSH 主机 dict（含解密凭据）。

    host_id 为 None / 0 / "0" 时表示平台本机，返回 None（沿用本机 shutil.which 逻辑）；
    否则按 ssh_hosts.get_host(include_secret=True) 查询。
    """
    if host_id in (None, 0, "0", ""):
        return None
    try:
        from core import ssh_hosts
        return ssh_hosts.get_host(int(host_id), include_secret=True)
    except (TypeError, ValueError):
        return None


def _host_key_of(ssh_host: Optional[dict]) -> str:
    """返回主机唯一键：SSH 主机用 host_key；本机用保留键 'local'。"""
    if not ssh_host:
        return "local"
    return ssh_host.get("host_key") or "local"


# ----------------------------------------------------------------------------
# 清单加载
# ----------------------------------------------------------------------------
def _load_manifest(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 必填字段最小校验
        for key in ("id", "name", "category", "required_clients", "packages"):
            if key not in data:
                print(f"[plugin_catalog] 缺少必填字段 {key}: {path}")
                return None
        return data
    except Exception as e:
        print(f"[plugin_catalog] 加载失败 {path}: {e}")
        return None


def load_all() -> Dict[str, dict]:
    """加载 manifests/ 下所有清单，按 id 索引。"""
    result: Dict[str, dict] = {}
    if not MANIFESTS_DIR.exists():
        return result
    for p in sorted(MANIFESTS_DIR.glob("*.json")):
        m = _load_manifest(p)
        if m:
            result[m["id"]] = m
    return result


# ----------------------------------------------------------------------------
# 状态聚合
# ----------------------------------------------------------------------------
def check_installed(manifest: dict, ssh_host: Optional[dict] = None) -> dict:
    """检查某个插件的所有 required_clients 是否可用。

    - ssh_host 为空（本机）：检查顺序——
      1. 系统 PATH（包管理器安装的通常在 PATH 中）
      2. 离线安装目录（/opt/xxx/bin/）
      3. 安装状态文件记录
    - ssh_host 非空（远端）：走 plugin_runtime.remote_check_clients 在远端探测。

    返回:
        {
          "installed": bool,
          "missing": List[str],
          "found_paths": Dict[name, path]
        }
    """
    import json
    required = manifest.get("required_clients") or []
    found: Dict[str, str] = {}
    missing: List[str] = []

    if ssh_host:
        from core import plugin_runtime
        chk = plugin_runtime.remote_check_clients(ssh_host, required)
        return {
            "installed": chk["installed"],
            "missing": chk["missing"],
            "found_paths": chk["found_paths"],
        }

    # 尝试从状态文件获取安装目录
    install_dirs = []
    try:
        state_dir = Path(__file__).parent / "plugins" / "install_state"
        if state_dir.exists():
            pid = manifest.get("id", "")
            for sf in state_dir.glob("*.json"):
                try:
                    data = json.loads(sf.read_text(encoding="utf-8"))
                    if data.get("plugin_id") == pid and data.get("status") == "ok":
                        ed = data.get("extract_dir")
                        if ed and Path(ed).exists():
                            install_dirs.append(Path(ed))
                except Exception:
                    pass
    except Exception:
        pass

    # 构建搜索路径：系统 PATH + 离线安装目录下的 bin/
    search_paths = os.environ.get("PATH", "").split(os.pathsep)
    for d in install_dirs:
        search_paths.append(str(d / "bin"))
        search_paths.append(str(d))

    for cli in required:
        p = shutil.which(cli, path=os.pathsep.join(search_paths)) if search_paths else shutil.which(cli)
        if p:
            found[cli] = p
        else:
            missing.append(cli)
    return {
        "installed": len(missing) == 0,
        "missing": missing,
        "found_paths": found,
    }


def check_installed_on_host(plugin_id: str, ssh_host: dict) -> dict:
    """在指定远端主机上检查插件是否已安装。

    优先级：
    1. 实时远端探测（plugin_runtime.remote_check_clients，权威判断「现在是否就绪」）；
    2. 合并 plugin_host_state 表的持久化状态（version / method / extract_dir / status）。

    返回:
        {
          "installed": bool,
          "missing": List[str],
          "found_paths": Dict[str, str],
          "version": str|None,
          "status": str,        # plugin_host_state.status 或 'uninstalled'
          "host_key": str,
          "extract_dir": str|None,
        }
    """
    manifest = load_all().get(plugin_id)
    host_key = (ssh_host or {}).get("host_key") or "local"
    base = {
        "installed": False,
        "missing": [],
        "found_paths": {},
        "version": None,
        "status": "uninstalled",
        "host_key": host_key,
        "extract_dir": None,
        "plugin_id": plugin_id,
    }
    if not manifest:
        base["missing"] = [plugin_id]
        return base

    live = check_installed(manifest, ssh_host)
    base["installed"] = live["installed"]
    base["missing"] = live["missing"]
    base["found_paths"] = live["found_paths"]

    # 合并权威落库状态（版本/安装方式/解压目录）
    try:
        import core.db as db
        row = db.get_plugin_host_state(host_key, plugin_id)
        if row:
            base["version"] = row.get("version")
            base["status"] = row.get("status") or "uninstalled"
            base["extract_dir"] = row.get("extract_dir")
            # 落库的 found_paths 若实时探测为空则回填，供前端展示
            if not base["found_paths"] and row.get("found_paths"):
                try:
                    base["found_paths"] = json.loads(row["found_paths"])
                except Exception:
                    pass
    except Exception:
        pass

    # 状态归一：实时探测就绪但库里未落 installed 时，仍以实时为准
    if base["installed"] and base["status"] in ("uninstalled", ""):
        base["status"] = "installed"
    return base


def _list_plugins_remote(filter_category: Optional[str] = None,
                         ssh_host: Optional[dict] = None) -> List[dict]:
    """按远端主机维度渲染插件列表（与 list_plugins 同构，前端可复用）。"""
    from core import plugin_runtime

    host_key = _host_key_of(ssh_host)
    remote_os = plugin_runtime.remote_detect_os(ssh_host)
    remote_pm = plugin_runtime.remote_detect_package_manager(ssh_host)
    catalog = load_all()
    rows: List[dict] = []
    # 延迟导入，避免循环
    try:
        import core.plugin_installer as installer
    except Exception:
        installer = None

    for pid, m in catalog.items():
        if filter_category and m.get("category") != filter_category:
            continue
        st = check_installed_on_host(pid, ssh_host)
        packages = m.get("packages") or {}
        os_supported = remote_os in packages
        os_supported_list = list(packages.keys())
        os_pkg = packages.get(remote_os) or {}
        download_url = (os_pkg.get("fallback") or {}).get("url") or ""
        pms = os_pkg.get("package_managers") or {}
        pm_cmd = ""
        if remote_pm and remote_pm in pms:
            pm_cmd = pms[remote_pm].get("command", "")

        plugin_status = st.get("status") or ("installed" if st.get("installed") else "uninstalled")
        last_message = ""
        if installer:
            sst = installer.get_state(pid, host_key=host_key)
            if sst:
                if sst.get("status") in ("running", "queued"):
                    plugin_status = "installing"
                elif sst.get("status") == "failed":
                    plugin_status = "failed"
                last_message = sst.get("message", "")

        db_types = m.get("supports", []) or []
        strategy_available = (remote_pm and pm_cmd) or bool(download_url)
        recommended = (not st.get("installed")) and os_supported and strategy_available
        rows.append({
            "id": m["id"],
            "name": m["name"],
            "version": st.get("version") or m.get("version", ""),
            "category": m.get("category", ""),
            "description": m.get("description", ""),
            "tags": m.get("tags", []),
            "icon": m.get("icon", "bi-plugin"),
            "homepage": m.get("homepage", ""),
            "required_clients": m.get("required_clients", []),
            "supports": db_types,
            "db_types": db_types,
            # 状态（主机维度）
            "installed": st.get("installed"),
            "missing": st.get("missing"),
            "found_paths": st.get("found_paths"),
            "status": plugin_status,
            "last_message": last_message,
            # 主机维度标识
            "host_key": host_key,
            "host_id": ssh_host.get("id") if ssh_host else None,
            # OS / 安装策略（远端）
            "os_supported": os_supported,
            "os_supported_list": os_supported_list,
            "current_os": remote_os,
            "package_manager": remote_pm,
            "package_manager_command": pm_cmd,
            "download_url": download_url,
            "strategy_available": strategy_available,
            "recommended": recommended,
            "install_paths": [st.get("extract_dir")] if st.get("extract_dir") else [],
        })
    rows.sort(key=lambda r: (r["installed"], not r.get("recommended", False),
                              r["category"], r["name"]))
    return rows


def list_plugins(filter_category: Optional[str] = None,
                 filter_os: Optional[str] = None,
                 host_id=None) -> List[dict]:
    """列出全部插件清单，附带运行时状态。

    filter_category: 按数据库类型过滤，如 "mysql" / "postgresql" 等。
    filter_os: 当前 OS 关键字（"linux" / "windows"），用于筛选可安装的插件。
    host_id: 指定后按「主机维度」返回插件状态（未指定时兼容旧的平台本机维度）。
    """
    ssh_host = _resolve_host(host_id)
    if ssh_host:
        return _list_plugins_remote(filter_category=filter_category,
                                    ssh_host=ssh_host)

    os_name = filter_os or detect_os()
    catalog = load_all()
    rows: List[dict] = []
    # 延迟导入，避免循环
    try:
        import core.plugin_installer as installer
    except Exception:
        installer = None
    for pid, m in catalog.items():
        if filter_category and m.get("category") != filter_category:
            continue
        status = check_installed(m)
        packages = m.get("packages") or {}
        os_supported = os_name in packages
        os_supported_list = list(packages.keys())
        # 提取当前 OS 的下载 URL
        os_pkg = packages.get(os_name) or {}
        download_url = (os_pkg.get("fallback") or {}).get("url") or ""
        # 取当前 OS 的包管理器命令
        pms = os_pkg.get("package_managers") or {}
        pm = detect_package_manager() if os_supported else None
        pm_cmd = ""
        if pm and pm in pms:
            pm_cmd = pms[pm].get("command", "")
        # 状态机：installed / uninstalled / failed / running
        plugin_status = "installed" if status["installed"] else "uninstalled"
        last_message = ""
        if installer:
            st = installer.get_state(pid)
            if st:
                if st.get("status") in ("running", "queued"):
                    plugin_status = "installing"
                elif st.get("status") == "failed":
                    plugin_status = "failed"
                last_message = st.get("message", "")
        # 列出已安装的物理路径
        roots = []
        _install_root = Path(__file__).parent / "plugins" / "installed"
        target = _install_root / pid.replace("/", "_").replace("\\", "_")
        if target.exists():
            roots.append(str(target))
        # 平台级标记：当前 OS 是否有可用的安装策略
        strategy_available = (pm and pm_cmd) or bool(download_url)
        # 关联的数据库类型（与 supports 同源；前端固定读 db_types）
        db_types = m.get("supports", []) or []
        # 是否为本机推荐：未安装 + 当前 OS 适配 + 具备安装策略
        recommended = (not status["installed"]) and os_supported and strategy_available
        rows.append({
            "id": m["id"],
            "name": m["name"],
            "version": m.get("version", ""),
            "category": m.get("category", ""),
            "description": m.get("description", ""),
            "tags": m.get("tags", []),
            "icon": m.get("icon", "bi-plugin"),
            "homepage": m.get("homepage", ""),
            "required_clients": m.get("required_clients", []),
            "supports": db_types,
            "db_types": db_types,
            # 状态
            "installed": status["installed"],
            "missing": status["missing"],
            "found_paths": status["found_paths"],
            "status": plugin_status,
            "last_message": last_message,
            # OS / 安装策略
            "os_supported": os_supported,
            "os_supported_list": os_supported_list,
            "current_os": os_name,
            "package_manager": pm,
            "package_manager_command": pm_cmd,
            "download_url": download_url,
            "strategy_available": strategy_available,
            "recommended": recommended,
            # 安装产物目录
            "install_paths": roots,
        })
    # 排序：未安装的靠前，其次本机推荐优先，最后按 category / name
    rows.sort(key=lambda r: (r["installed"], not r.get("recommended", False),
                              r["category"], r["name"]))
    return rows


def get_plugin(pid: str, host_id=None) -> Optional[dict]:
    """按 id 获取单个插件清单（含状态）。

    host_id: 指定后按主机维度返回远端状态；未指定时返回本机维度（兼容旧调用）。
    """
    m = load_all().get(pid)
    if not m:
        return None
    db_types = m.get("supports", []) or []
    ssh_host = _resolve_host(host_id)
    if ssh_host:
        from core import plugin_runtime
        host_key = _host_key_of(ssh_host)
        st = check_installed_on_host(pid, ssh_host)
        remote_os = plugin_runtime.remote_detect_os(ssh_host)
        os_supported = remote_os in (m.get("packages") or {})
        return {
            "id": m["id"],
            "name": m["name"],
            "version": st.get("version") or m.get("version", ""),
            "category": m.get("category", ""),
            "description": m.get("description", ""),
            "tags": m.get("tags", []),
            "icon": m.get("icon", "bi-plugin"),
            "homepage": m.get("homepage", ""),
            "required_clients": m.get("required_clients", []),
            "supports": db_types,
            "db_types": db_types,
            "installed": st.get("installed"),
            "missing": st.get("missing"),
            "found_paths": st.get("found_paths"),
            "status": st.get("status"),
            "host_key": host_key,
            "host_id": ssh_host.get("id"),
            "current_os": remote_os,
            "os_supported": os_supported,
            "package_manager": plugin_runtime.remote_detect_package_manager(ssh_host),
            "manifest": m,
        }

    status = check_installed(m)
    os_name = detect_os()
    os_supported = os_name in (m.get("packages") or {})
    return {
        "id": m["id"],
        "name": m["name"],
        "version": m.get("version", ""),
        "category": m.get("category", ""),
        "description": m.get("description", ""),
        "tags": m.get("tags", []),
        "icon": m.get("icon", "bi-plugin"),
        "homepage": m.get("homepage", ""),
        "required_clients": m.get("required_clients", []),
        "supports": db_types,
        "db_types": db_types,
        "installed": status["installed"],
        "missing": status["missing"],
        "found_paths": status["found_paths"],
        "current_os": os_name,
        "os_supported": os_supported,
        "package_manager": detect_package_manager(),
        "manifest": m,
    }


def categories() -> List[dict]:
    """返回所有插件类别 + 各类的插件数量，用于前端筛选侧栏。"""
    catalog = load_all()
    bucket: Dict[str, int] = {}
    for m in catalog.values():
        bucket[m["category"]] = bucket.get(m["category"], 0) + 1
    rows = [{"name": k, "count": v} for k, v in bucket.items()]
    rows.sort(key=lambda r: r["name"])
    return rows


def recommended_for_db_type(db_type: str) -> List[dict]:
    """按数据库类型（mysql / postgresql / ...）找出所有相关插件（已装 + 未装）。"""
    db_type = (db_type or "").strip().lower()
    if not db_type:
        return []
    rows = list_plugins()
    return [r for r in rows if db_type in (r.get("db_types") or [])]


def recommend_for_host(db_types: Optional[List[str]] = None,
                       host_id=None) -> List[dict]:
    """根据目标主机操作系统和给定的数据库类型列表，找出推荐安装的插件。

    通常由前端传入目标主机上已配置的备份任务数据库类型（db_types）；
    本函数会筛出：
      - 关联到给定 db_type 的插件
      - 目标 OS 适配（packages 中有对应 key）
      - 还未安装
      - 具备至少一种安装策略（包管理器 / 离线下载）

    返回值与 list_plugins() 单条结构相同，前端可直接渲染。
    """
    rows = list_plugins(host_id=host_id)
    target_types = {t.strip().lower() for t in (db_types or []) if t}
    if not target_types:
        # 全部候选：未安装 + 当前 OS 适配 + 具备安装策略
        return [r for r in rows if r.get("recommended")]
    out = []
    for r in rows:
        if r.get("installed"):
            continue
        if not r.get("os_supported"):
            continue
        if not r.get("strategy_available"):
            continue
        if target_types.intersection(set(r.get("db_types") or [])):
            out.append(r)
    return out


def external_plugins_for_host(host_id: int) -> List[dict]:
    """P2：返回目标主机已配置任务所需的外部插件清单（去重、未安装优先）。

    遍历该主机的备份任务 db_type，收集其对应外部插件，并附带该主机上的
    安装状态（含缺失列表），供「一键补齐」批量下发使用。
    """
    try:
        import core.models as models
        tasks = models.list_tasks() if hasattr(models, "list_tasks") else []
    except Exception:
        tasks = []
    db_types = set()
    for t in tasks:
        dt = (t.get("db_type") or "").strip().lower()
        if dt:
            db_types.add(dt)
    if not db_types:
        return []

    from core import plugin_runtime
    seen: Dict[str, dict] = {}
    ssh_host = _resolve_host(host_id)
    for dt in db_types:
        for m in plugin_runtime.external_plugins_for_db_type(dt):
            pid = m.get("id")
            if not pid or pid in seen:
                continue
            if ssh_host:
                st = check_installed_on_host(pid, ssh_host)
                row = {
                    "id": pid,
                    "name": m.get("name", pid),
                    "category": m.get("category", ""),
                    "db_types": m.get("supports", []) or [],
                    "installed": st.get("installed"),
                    "missing": st.get("missing"),
                    "status": st.get("status"),
                    "host_key": _host_key_of(ssh_host),
                }
            else:
                status = check_installed(m)
                row = {
                    "id": pid,
                    "name": m.get("name", pid),
                    "category": m.get("category", ""),
                    "db_types": m.get("supports", []) or [],
                    "installed": status["installed"],
                    "missing": status["missing"],
                    "status": "installed" if status["installed"] else "uninstalled",
                    "host_key": "local",
                }
            seen[pid] = row
    out = list(seen.values())
    out.sort(key=lambda r: (r.get("installed", False), r.get("id", "")))
    return out