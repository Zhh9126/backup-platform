# -*- coding: utf-8 -*-
"""无备份目录时的底层磁盘 / 文件系统扫描恢复（零安装、纯 SSH 只读）。

对标 zBackup「scan_fs 裸盘恢复 —— 无备份目录也能从底层磁盘恢复数据」，
但实现严格遵循 AIDBM 的硬性约束，差异如实说明：

* zBackup 的 scan_fs 是**外部二进制**（`scan_fs_v3/v4.*_linux`，从
  `/opt/zbackup/bin/`、`/opt/scan_fs/`、`/logmnr/` 查找后 nohup 启动），
  用户提供的 4 个可执行文件里并不包含它，无法移植其内部实现；
* 本模块只通过 **SSH + paramiko 执行远端系统自带的只读命令**
  （`lsblk`/`blkid`/`find`/`stat`/`sha256sum`），不在被管服务器安装任何工具，
  不读裸块设备、不做块级数据雕刻；
* 产出「可恢复对象清单」（卷 + 文件 + Oracle 数据文件/归档识别），
  再按清单经 SFTP 拉回平台暂存目录，交给既有恢复通道处理。

因此本能力覆盖「备份目录丢失但仍能从文件系统/数据文件层找回数据」，
不承诺「已覆盖磁盘块的扇区级雕刻恢复」。
"""
from __future__ import annotations

import json
import os
import re

from core import remote_dump

ORACLE_DATA_RE = re.compile(
    r"(?i)(^|/)(spfile[^/]*|control[^/]*|\w+\.dbf|\w+\.arc|"
    r"archivelog/|flash_recovery_area/|fast_recovery_area/)")
ORACLE_SUFFIX_RE = re.compile(r"(?i)\.(dbf|arc|log|ctl|ora)$")
DEFAULT_ROOTS = ("/u01", "/opt/oracle", "/oradata", "/data", "/var/lib/mysql")


class ScanFSError(ValueError):
    """扫描失败（SSH 不可达 / 参数非法），消息可直接透出。"""


def _client(task: dict):
    ssh = remote_dump.resolve_ssh_host(dict(task or {}))
    if ssh is None:
        raise ScanFSError("未找到可用的 SSH 主机；请纳管主机或在任务高级选项配置 SSH 凭据")
    try:
        return remote_dump._connect_isolated(ssh)
    except Exception as e:
        raise ScanFSError(f"SSH 连接失败: {e}")


def _exec(cli, cmd: str, timeout: int = 60) -> str:
    try:
        _, out, err = cli.exec_command(cmd, timeout=timeout)
        text = out.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
    except Exception as e:
        raise ScanFSError(f"远端命令执行失败: {e}")
    if rc not in (0, 1):  # find/grep 对不存在路径返回 1 属正常
        detail = err.read().decode("utf-8", "replace").strip()
        raise ScanFSError(f"远端命令 rc={rc}: {detail[:300]}")
    return text


def _volumes(cli) -> list:
    text = _exec(cli, "lsblk -J -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT 2>/dev/null || true")
    try:
        data = json.loads(text)
        out = []
        for dev in data.get("blockdevices", []):
            out.append({
                "name": dev.get("name"), "type": dev.get("type"),
                "size": dev.get("size"), "fstype": dev.get("fstype"),
                "mountpoint": dev.get("mountpoint"),
            })
            for child in dev.get("children") or []:
                out.append({
                    "name": child.get("name"), "type": child.get("type"),
                    "size": child.get("size"), "fstype": child.get("fstype"),
                    "mountpoint": child.get("mountpoint"),
                })
        return out
    except Exception:
        # 老系统无 lsblk -J：回退 blkid 纯文本
        return [{"name": line.split(":", 1)[0], "raw": line[:200]}
                for line in _exec(cli, "blkid 2>/dev/null || true").splitlines()
                if ":" in line]


def _kind(path: str) -> str:
    if ORACLE_DATA_RE.search(path):
        return "oracle_artifact"
    # .log 既是 Oracle 归档也可能是 redo/binlog：先按内容命名判日志类
    if re.search(r"(?i)(binlog|mysql-bin\.|redo|wal|0000\d+\.log)", path):
        return "db_log"
    if ORACLE_SUFFIX_RE.search(path):
        return "db_file"
    return "file"


def scan_host(task: dict, roots: list = None, max_files: int = 5000,
              timeout: int = 60) -> dict:
    """只读扫描远端文件系统，返回可恢复对象清单。"""
    roots = [str(r).strip() for r in (roots or DEFAULT_ROOTS) if str(r).strip()]
    if not roots:
        raise ScanFSError("roots 不能为空")
    for r in roots:
        if (not r.startswith("/") or ".." in r
                or re.search(r"[;&|`$<>\n\r\"'\\]", r)):
            raise ScanFSError(
                f"扫描根目录必须是安全的绝对路径（禁止 .. 与 shell 元字符）: {r}")
    roots = roots[:20]
    max_files = max(1, min(int(max_files), 50000))
    cli = _client(task)
    try:
        volumes = _volumes(cli)
        # 单次 find 列出全部文件（零临时文件、零 agent）
        quoted = " ".join("'" + r.replace("'", "'\\''") + "'" for r in roots)
        cmd = (f"for d in {quoted}; do [ -d \"$d\" ] || continue; "
               f"find \"$d\" -xdev -type f -printf '%p\\t%s\\t%T@\\n' 2>/dev/null; "
               f"done | head -n {max_files}")
        files = []
        for line in _exec(cli, cmd, timeout=timeout).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                size, mtime = int(parts[1]), float(parts[2])
            except ValueError:
                continue
            files.append({"path": parts[0], "size": size, "mtime": mtime,
                          "kind": _kind(parts[0])})
        # Oracle 实例线索（/etc/oratab，只读）
        oratab = _exec(cli, "cat /etc/oratab 2>/dev/null || true", timeout=15)
        oracle_homes = []
        for line in oratab.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split(":")
            if len(cols) >= 2 and cols[1].startswith("/"):
                oracle_homes.append({"sid": cols[0], "home": cols[1]})
        oracle_files = [f for f in files if f["kind"] in ("oracle_artifact", "db_file")]
        return {
            "ok": True,
            "host": str((task or {}).get("host") or ""),
            "roots": roots,
            "volumes": volumes,
            "files": files,
            "file_count": len(files),
            "oracle_homes": oracle_homes,
            "oracle_candidates": oracle_files[:200],
            "truncated": len(files) >= max_files,
            "method": "SSH 只读 find/lsblk/blkid；零安装、不读裸块、不做扇区雕刻",
        }
    finally:
        try:
            cli.close()
        except Exception:
            pass


def pull_objects(task: dict, paths: list, dest_dir: str = "",
                 max_bytes: int = 2 * 1024 ** 3) -> dict:
    """按扫描清单经 SFTP 拉回平台暂存目录，并逐文件计算 sha256。"""
    import hashlib
    import time

    paths = [str(p) for p in (paths or []) if str(p).startswith("/")]
    if not paths:
        raise ScanFSError("paths 不能为空且必须是绝对路径")
    root = dest_dir or os.path.join(
        str(os.environ.get("BACKUP_ROOT") or "/tmp"), "scan_fs_restore")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(root, stamp)
    os.makedirs(dest, exist_ok=True)
    cli = _client(task)
    pulled, failures, total = [], [], 0
    try:
        sftp = cli.open_sftp()
        for src in paths[:500]:
            base = os.path.basename(src.rstrip("/")) or "object"
            local = os.path.join(dest, f"{len(pulled):04d}__{base}")
            try:
                st = sftp.stat(src)
                if total + (st.st_size or 0) > max_bytes:
                    failures.append({"path": src, "error": "超过本次拉取字节上限"})
                    continue
                sftp.get(src, local)
                h = hashlib.sha256()
                with open(local, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                size = os.path.getsize(local)
                total += size
                pulled.append({"source": src, "local": local,
                               "size": size, "sha256": h.hexdigest()})
            except Exception as e:
                failures.append({"path": src, "error": str(e)[:200]})
        sftp.close()
    finally:
        try:
            cli.close()
        except Exception:
            pass
    return {"ok": True, "dest": dest, "pulled": pulled,
            "pulled_count": len(pulled), "bytes": total,
            "failures": failures}
