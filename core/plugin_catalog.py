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
def check_installed(manifest: dict) -> dict:
    """检查某个插件的所有 required_clients 是否可用。

    检查顺序：
    1. 系统 PATH（包管理器安装的通常在 PATH 中）
    2. 离线安装目录（/opt/xxx/bin/）
    3. 安装状态文件记录

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


def list_plugins(filter_category: Optional[str] = None,
                 filter_os: Optional[str] = None) -> List[dict]:
    """列出全部插件清单，附带运行时状态。

    filter_category: 按数据库类型过滤，如 "mysql" / "postgresql" 等。
    filter_os: 当前 OS 关键字（"linux" / "windows"），用于筛选可安装的插件。
    """
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


def get_plugin(pid: str) -> Optional[dict]:
    """按 id 获取单个插件清单（含状态）。"""
    m = load_all().get(pid)
    if not m:
        return None
    status = check_installed(m)
    os_name = detect_os()
    os_supported = os_name in (m.get("packages") or {})
    db_types = m.get("supports", []) or []
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


def recommend_for_host(db_types: Optional[List[str]] = None) -> List[dict]:
    """根据本机操作系统和给定的数据库类型列表，找出推荐安装的插件。

    通常由前端传入当前服务器上已配置的备份任务数据库类型（db_types）；
    本函数会筛出：
      - 关联到给定 db_type 的插件
      - 当前 OS 适配（packages 中有对应 key）
      - 还未安装
      - 具备至少一种安装策略（包管理器 / 离线下载）

    返回值与 list_plugins() 单条结构相同，前端可直接渲染。
    """
    target_types = {t.strip().lower() for t in (db_types or []) if t}
    if not target_types:
        # 全部候选：未安装 + 当前 OS 适配 + 具备安装策略
        return [r for r in list_plugins() if r.get("recommended")]
    out = []
    for r in list_plugins():
        if r.get("installed"):
            continue
        if not r.get("os_supported"):
            continue
        if not r.get("strategy_available"):
            continue
        if target_types.intersection(set(r.get("db_types") or [])):
            out.append(r)
    return out