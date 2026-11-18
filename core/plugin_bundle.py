# -*- coding: utf-8 -*-
"""离线插件包（单文件 bundle）：无网现场一次导入。

## 背景

现场经常是纯离线（政务内网、专网），装插件要一个个下载/拷贝、逐个点安装。
这里把"常用组合"打成**一个 tar.gz**：里面带 manifest + 各插件的离线安装包 +
sha256 清单，现场只需「导入一个文件」，平台自动解包、校验、逐个安装。

## 包结构

    aidbm-plugins-20261114.tar.gz
    ├── bundle.json                     # 格式版本 / 生成时间 / 插件清单 / 校验和
    └── packages/
        ├── percona-xtrabackup-80/<原始安装包>
        ├── pgbackrest/<原始安装包>
        └── ...

bundle.json 里每个插件记录：id / name / version（期望版本）/ manifest /
package{file,size,sha256}（无离线包时为 null，导入时按在线通道尝试）。

## 诚实边界

- 构建时若本地没有离线包（`core/plugins/offline_packages/<pid>/`），仍会生成
  bundle，但在 `missing_packages` 里如实列出——这种包导入后只能走联网安装；
- 导入时**逐项校验 sha256**，不一致直接拒绝该插件（不静默安装被篡改的包）；
- 不做签名验签（离线环境通常无 CA 体系），只做完整性校验。
"""
from __future__ import annotations

import hashlib
import json
import os
import tarfile
import time

import core.db as db
from core import plugin_catalog, plugin_installer

BUNDLE_FORMAT = "aidbm-plugin-bundle/1"
BUNDLE_JSON = "bundle.json"
PKG_PREFIX = "packages"


def _sha256(path: str) -> str:
    """统一委托 core.db.sha256_file（此前 7 个模块各写一份，已收敛）。"""
    from core import db as _db
    return _db.sha256_file(path)


def _offline_package(pid: str) -> str:
    """取该插件的离线安装包（offline_packages/<pid>/ 下最新文件），无则空串。"""
    d = os.path.join(str(plugin_installer.OFFLINE_PKG_DIR), pid)
    if not os.path.isdir(d):
        return ""
    files = [os.path.join(d, n) for n in os.listdir(d)
             if os.path.isfile(os.path.join(d, n))]
    if not files:
        return ""
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[0]


def build(plugin_ids: list, out_path: str = "", note: str = "") -> dict:
    """把一组插件打成单文件离线包。"""
    ids = [str(x).strip() for x in (plugin_ids or []) if str(x).strip()]
    if not ids:
        return {"ok": False, "message": "请提供要打包的插件 id（plugins 列表）"}
    manifests = plugin_catalog.load_all()
    items, missing, with_pkg = [], [], 0
    for pid in ids:
        man = manifests.get(pid)
        if not man:
            missing.append({"id": pid, "reason": "插件不存在（manifest 未找到）"})
            continue
        pkg = _offline_package(pid)
        entry = {
            "id": pid, "name": man.get("name") or pid,
            "version": str(man.get("version") or ""),
            "category": man.get("category") or "",
            "manifest": man,
            "package": None,
        }
        if pkg:
            entry["package"] = {"file": os.path.basename(pkg),
                                "size": os.path.getsize(pkg),
                                "sha256": _sha256(pkg)}
            with_pkg += 1
        else:
            missing.append({"id": pid,
                            "reason": "平台侧无离线安装包（导入后将尝试在线安装）"})
        items.append(entry)

    if not items:
        return {"ok": False, "message": "没有可打包的插件", "missing": missing}

    meta = {
        "format": BUNDLE_FORMAT,
        "created_at": db.now_iso(),
        "note": note or "",
        "plugin_count": len(items),
        "package_count": with_pkg,
        "plugins": items,
    }
    out = out_path or os.path.join(
        str(plugin_installer.PLUGIN_DIR), "bundles",
        "aidbm-plugins-%s.tar.gz" % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        raw = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(BUNDLE_JSON)
        info.size = len(raw)
        info.mtime = int(time.time())
        import io
        tar.addfile(info, io.BytesIO(raw))
        for it in items:
            if not it.get("package"):
                continue
            src = _offline_package(it["id"])
            if not src:
                continue
            arc = "%s/%s/%s" % (PKG_PREFIX, it["id"], it["package"]["file"])
            tar.add(src, arcname=arc)
    return {"ok": True, "path": out, "size": os.path.getsize(out),
            "plugin_count": len(items), "with_package": with_pkg,
            "plugins": [{"id": i["id"], "name": i["name"], "version": i["version"],
                         "has_package": bool(i.get("package"))} for i in items],
            "missing_packages": missing}


def inspect(path: str) -> dict:
    """只读查看 bundle 内容（不安装）。"""
    if not path or not os.path.isfile(path):
        return {"ok": False, "message": "bundle 文件不存在: %s" % path}
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = tar.getmember(BUNDLE_JSON)
            meta = json.loads(tar.extractfile(member).read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "message": "无法解析 bundle: %s" % str(e)[:200]}
    if meta.get("format") != BUNDLE_FORMAT:
        return {"ok": False, "message": "bundle 格式不匹配：%s（期望 %s）"
                                       % (meta.get("format"), BUNDLE_FORMAT)}
    return {"ok": True, "path": path, "size": os.path.getsize(path), "meta": meta}


def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    """解包到指定目录并防路径穿越（bundle 可能来自外部）。"""
    root = os.path.realpath(dest)
    for m in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, m.name))
        if not (target == root or target.startswith(root + os.sep)):
            raise RuntimeError("bundle 内含越界路径，已拒绝解包: %s" % m.name)
    tar.extractall(dest)


def import_bundle(path: str, host_id: int = None, install: bool = True) -> dict:
    """导入单文件 bundle：校验 → 落离线包目录 → 逐个安装。"""
    info = inspect(path)
    if not info.get("ok"):
        return info
    meta = info["meta"]
    tmp = os.path.join(str(plugin_installer.PLUGIN_DIR), "bundles", ".import_%d"
                       % int(time.time()))
    os.makedirs(tmp, exist_ok=True)
    results, bad = [], []
    try:
        with tarfile.open(path, "r:gz") as tar:
            _safe_extract(tar, tmp)
        for it in meta.get("plugins") or []:
            pid = it.get("id")
            pkg = it.get("package") or None
            row = {"id": pid, "name": it.get("name"), "version": it.get("version"),
                   "installed": False, "message": ""}
            if not pid:
                continue
            if pkg:
                src = os.path.join(tmp, PKG_PREFIX, pid, pkg.get("file") or "")
                if not os.path.isfile(src):
                    bad.append({"id": pid, "reason": "包内缺少文件 %s" % pkg.get("file")})
                    row["message"] = "包内缺少安装文件"
                    results.append(row)
                    continue
                actual = _sha256(src)
                if actual != (pkg.get("sha256") or ""):
                    bad.append({"id": pid, "reason": "sha256 校验失败（文件被修改）"})
                    row["message"] = "sha256 校验失败，已拒绝安装"
                    results.append(row)
                    continue
                dst_dir = os.path.join(str(plugin_installer.OFFLINE_PKG_DIR), pid)
                os.makedirs(dst_dir, exist_ok=True)
                dst = os.path.join(dst_dir, pkg.get("file"))
                with open(src, "rb") as rf, open(dst, "wb") as wf:
                    for chunk in iter(lambda: rf.read(1 << 20), b""):
                        wf.write(chunk)
                row["offline_package"] = dst
            if install:
                try:
                    r = plugin_installer.install(pid, host_id=host_id)
                    row["installed"] = bool(r.get("ok"))
                    row["message"] = (r.get("message") or "")[:300]
                except Exception as e:                              # noqa: BLE001
                    row["message"] = "安装失败: %s" % str(e)[:300]
            results.append(row)
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    ok_n = sum(1 for r in results if r.get("installed"))
    return {"ok": not bad, "bundle": path, "plugin_count": len(results),
            "installed": ok_n, "failed": len(results) - ok_n,
            "integrity_errors": bad, "results": results}
