# -*- coding: utf-8 -*-
"""rsync 增量拉取（DB 远端单文件通道的去重传输）。

为什么不用固定块分块：tar.gz/gzip 流中任一字节变化会使其后全部固定块
偏移（实测命中，见 docs/benchmark_dedup_20261026.md），跨轮命中率趋近 0。
rsync 的滚动校验 delta 算法天然免疫移位：接收端（平台）持有上一份产物作
basis，把校验码发给远端，远端只回传差量——"重复块不走网络"在任意偏移下
都成立，且 rsync 是 Linux 服务器事实标配（零 agent 原则不破坏：不安装
任何东西，只调用系统自带 rsync）。

诚实边界：
- 仅当 SSH 主机为**密钥认证**时可用（rsync 无法交互输密码，sshpass 不可
  依赖）；密码认证主机自动回退原 sftp 通道。
- 远端需已存在产物文件（物理备份远端 tar 形态天然满足）。
- 平台侧保留上一份产物作 basis（最近一次物理备份产物，任何旧产物都可用，
  越近省得越多）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import subprocess
import tempfile

logger = logging.getLogger("core.rsync_delta")


def rsync_available() -> bool:
    found = None
    try:
        import shutil as _sh
        found = _sh.which("rsync")
    except Exception:  # noqa: BLE001
        return False
    return bool(found)


def _ssh_cmd(ssh_host: dict) -> tuple:
    """从纳管主机信息构造非交互 ssh 命令。返回 (cmd_prefix, cleanup_files)。

    仅支持密钥认证；密码认证主机返回 (None, [])（调用方回退 sftp）。
    """
    auth = (ssh_host.get("auth_type") or "").lower()
    key = (ssh_host.get("private_key") or "").strip()
    key_path = (ssh_host.get("private_key_path") or "").strip()
    hostname = ssh_host.get("hostname") or ssh_host.get("host_key", "")
    port = int(ssh_host.get("port") or 22)
    user = ssh_host.get("username") or "root"
    if not key and not key_path and auth not in ("key", "privatekey", "key_auth"):
        # host_key 里可能带密钥路径语义？当前 schema 无 → 密码主机不可用
        return None, []
    cleanup = []
    if key and not key_path:
        fd, key_path = tempfile.mkstemp(prefix="rsync_key_", suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(key if key.endswith("\n") else key + "\n")
        os.chmod(key_path, 0o600)
        cleanup.append(key_path)
    if not key_path:
        return None, []
    if "@" in hostname and not hostname.startswith("/"):
        pass
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
           "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-i", key_path, "-p", str(port),
           f"{user}@{hostname}"]
    return cmd, cleanup


def pull_delta(ssh_host: dict, remote_path: str, local_path: str,
               basis_path: str, timeout: int = 7200) -> dict:
    """rsync 增量拉取：以 basis_path（上一份产物）内容为 basis。

    流程：把 basis 复制（硬链接优先）到 local_path 目标位 → rsync 远端
    current → local_path（rsync 对已存在目标自动做 delta）→ sha256 与远端
    对账。

    Returns:
        {"ok": True, "size": 本地文件大小, "sha256": 本地哈希,
         "delta_bytes": rsync 实际传输字节数, "logical_bytes": 远端文件大小}
    失败抛异常（调用方回退 sftp 整包通道）。
    """
    if not rsync_available():
        raise RuntimeError("平台侧未安装 rsync")
    cmd_prefix, cleanup = _ssh_cmd(ssh_host)
    try:
        if not cmd_prefix:
            raise RuntimeError("SSH 主机非密钥认证，rsync 增量拉取不可用")
        if not os.path.isfile(basis_path):
            raise RuntimeError(f"basis 不存在: {basis_path}")

        # 1) basis 预置到目标位（硬链接优先，零拷贝；跨设备回退复制）
        os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".",
                    exist_ok=True)
        if os.path.exists(local_path):
            os.remove(local_path)
        try:
            os.link(basis_path, local_path)
        except OSError:
            import shutil as _sh
            _sh.copyfile(basis_path, local_path)

        # 2) rsync 拉取（对既有目标做 delta）
        # -e 只能带 ssh 选项：cmd_prefix 最后一个参数是 user@host（ssh 的
        # 目标），必须剥离——rsync 会把 remote_spec 的 host 交给 -e 命令，
        # 多一个 host 会导致远端执行 "ssh root@host root@host rsync..." → 127
        ssh_opts = cmd_prefix[:-1] if cmd_prefix and "@" in cmd_prefix[-1] \
            else cmd_prefix
        remote_spec = (
            f"{ssh_host.get('username') or 'root'}@"
            f"{ssh_host.get('hostname') or ''}:{remote_path}")
        # 不用 --inplace：目标位预置了 basis 的**硬链接**，inplace 会直接改写
        # 该 inode → basis（上一份备份产物）被破坏性覆盖（两份备份变同一内容，
        # 实测产物互为硬链接且内容相同）。rsync 默认写临时文件+rename 替换，
        # delta 仍基于目标旧内容，basis 原件安全。
        cmd = (["rsync", "--stats", "-z",
                "-e", " ".join(shlex.quote(x) for x in ssh_opts),
                remote_spec, local_path])
        logger.info("[rsync-delta] %s", " ".join(cmd[:2] + [remote_spec,
                                                            local_path]))
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        stats = r.stdout or ""
        if r.returncode != 0:
            raise RuntimeError(f"rsync 拉取失败 rc={r.returncode}: "
                               f"{(r.stderr or stats)[-300:]}")
        # 3) 传输量统计：rsync --stats 的 "Total transferred file size" 包含
        # **匹配数据**（非网络量），真正走网络的是 Literal data / 收发字节数。
        # 优先 "Total bytes received"（receiver 收到的字面量+元数据）。
        delta_bytes = 0
        m = (re.search(r"Total bytes received:\s*([\d,]+(?:\.\d+)?[KMG]?)", stats)
             or re.search(r"Literal data:\s*([\d,]+(?:\.\d+)?[KMG]?)", stats)
             or re.search(r"Total transferred file size:\s*([\d,]+)", stats))
        if m:
            delta_bytes = _parse_rsync_bytes(m.group(1))
        # 4) 对账：本地 sha256 == 远端 sha256
        local_sha = _file_sha256(local_path)
        _, r_sha = _remote_sha256(cmd_prefix, remote_path)
        if local_sha != r_sha:
            raise RuntimeError(
                f"rsync 拉取后校验不符: local={local_sha[:12]}… "
                f"remote={r_sha[:12]}…")
        size = os.path.getsize(local_path)
        logger.info("[rsync-delta] 完成: 逻辑 %d 字节 / 实传 %d 字节",
                    size, delta_bytes)
        return {"ok": True, "size": size, "sha256": local_sha,
                "delta_bytes": delta_bytes, "logical_bytes": size}
    finally:
        for f in cleanup:
            try:
                os.remove(f)
            except OSError:
                pass


def _parse_rsync_bytes(s: str) -> int:
    """解析 rsync 统计数值：'1,234,567' / '12.34M' / '1.2G' → 字节数。"""
    s = (s or "").strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KMG]?)$", s)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return 0
    n = float(m.group(1))
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}.get(m.group(2), 1)
    return int(n * mult)


def _remote_sha256(cmd_prefix, remote_path: str) -> tuple:
    cmd = list(cmd_prefix) + [f"sha256sum {shlex.quote(remote_path)}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"远端 sha256 计算失败: {(r.stderr or '')[:200]}")
    m = re.search(r"^([0-9a-f]{64})\b", r.stdout or "", re.M)
    if not m:
        raise RuntimeError("远端 sha256 输出解析失败")
    return True, m.group(1)


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
