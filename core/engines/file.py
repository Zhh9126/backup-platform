# -*- coding: utf-8 -*-
"""
文件/目录备份引擎 — 支持本地与远程(SSH, 无Agent)两种源。

特性：
- 全量备份：tar.gz 打包源路径下全部文件
- 增量备份：对比源/目标文件列表（size + mtime，容差 5s），仅传输变化/新增文件
- 远程主机：通过 paramiko SSH 执行 find/tar，无需在被备份机器上安装任何 Agent
- 恢复：将 tar.gz 归档解包到指定目标目录

extra_options JSON 结构示例：
{
  "source_type": "local" | "remote",
  "source_paths": ["/data/app", "/etc/nginx"],
  "source_host": "root@192.168.1.100",   // source_type=remote 时必填，指向 ssh_hosts 中的 host_key
  "target_type": "local" | "remote",
  "target_host": "root@192.168.1.200",   // target_type=remote 时必填
  "target_path": "/backup/file_tasks",      // 本地目标目录或远程目标目录
  "exclude_patterns": ["*.tmp", "*.log", "__pycache__"],
  "follow_symlinks": false
}
"""

import io
import os
import json
import shlex
import time
import tarfile
import tempfile
import threading
import subprocess
import shutil
import hashlib
from typing import List, Dict, Tuple, Optional

import config
import core.db as db


def _safe_write_bytes(archive_path: str, data: bytes, logger=None, retries: int = 3) -> None:
    """安全写入字节：先删旧文件防 Windows 文件锁，重试 3 次应对偶发占用。
    Windows 上将路径归一化为正斜杠以避免反斜杠转义歧义。"""
    # 路径归一化：Windows 反斜杠 → 正斜杠（避免 \t \r 等被误解析为转义符）
    norm_path = archive_path.replace("\\", "/")
    parent = os.path.dirname(norm_path)
    if logger:
        logger.info("[safe_write] 写入路径: %r (norm=%r) 数据大小: %d bytes", archive_path, norm_path, len(data))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(norm_path):
        try:
            os.unlink(norm_path)
            if logger: logger.info("[safe_write] 已删除旧文件")
        except Exception as e:
            if logger:
                logger.warning("[safe_write] 删除旧文件失败（忽略）: %s", e)
    last_err = None
    for attempt in range(retries):
        # 同时尝试正斜杠和反斜杠两种写法（应对 Windows 路径解析差异）
        for path_try in [norm_path, archive_path]:
            try:
                with open(path_try, "wb") as f:
                    f.write(data)
                if logger: logger.info("[safe_write] 写入成功 (path=%s)", path_try)
                return
            except (PermissionError, OSError) as e:
                last_err = e
                if logger:
                    logger.warning("[safe_write] 第 %d 次写入失败 path=%r err=%r", attempt + 1, path_try, e)
        if attempt < retries - 1:
            time.sleep(0.5)
    raise RuntimeError(f"写入归档失败(已重试{retries}次): {last_err}")


def _safe_write_via_temp(archive_path: str, data: bytes, logger=None) -> bool:
    """终极方案：写入临时文件后 os.replace 原子重命名（避开 Windows 句柄问题）。"""
    try:
        import tempfile
        # 在同一目录下创建临时文件
        norm_path = archive_path.replace("\\", "/")
        parent = os.path.dirname(norm_path) or "."
        # 使用 NamedTemporaryFile 写完后用 os.replace 原子改名
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if os.path.exists(archive_path):
                os.unlink(archive_path)
            # 原子替换（Windows 友好）
            os.replace(tmp_path, archive_path)
            if logger: logger.info("[safe_write_via_temp] 写入成功 via temp")
            return True
        except Exception as e:
            try: os.unlink(tmp_path)
            except: pass
            if logger: logger.error("[safe_write_via_temp] failed: %r", e)
            return False
    except Exception as e:
        if logger: logger.error("[safe_write_via_temp] outer failed: %r", e)
        return False
from core.engines.base import (
    BackupEngine, BackupType, BackupStatus, BackupResult,
)

# mtime 容差秒数（避免文件系统精度差异导致误判）
MTIME_TOLERANCE = 5

# 全局 SSH 连接池（线程安全）
_ssh_pool: Dict[str, object] = {}
_ssh_lock = threading.Lock()


def _get_extra(task: dict) -> dict:
    """从 task['extra_options'] 解析 JSON 配置。"""
    raw = task.get("extra_options") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _get_ssh_client(host_key: str, password: str = None):
    """
    获取或创建 SSH 连接（带连接池复用 + 活性探测）。
    host_key 格式: "user@hostname:port" 或 "user@hostname"
    密码优先从参数取，其次从 ssh_hosts 表查询。

    背景：长时间空闲后，中间防火墙/NAT 会静默断开 TCP，但 paramiko 的
    transport.is_active() 仍可能返回 True，导致复用死连接时 exec_command
    抛出 WinError 10060 等连接超时。本函数在复用前通过轻量 heartbeat
    探测连接是否真正可用，不可用则丢弃重建。
    """
    import paramiko

    with _ssh_lock:
        if host_key in _ssh_pool:
            client = _ssh_pool[host_key]
            t = None
            try:
                t = client.get_transport()
            except Exception:
                pass
            alive = False
            if t is not None and t.is_active():
                try:
                    # 轻量心跳：发一个 IGNORE 包，若底层连接已死会立即抛异常
                    t.send_ignore()
                    alive = True
                except Exception:
                    alive = False
            if alive:
                return client
            # 不可用则移出连接池，后续重建
            try:
                if t is not None:
                    t.close()
                client.close()
            except Exception:
                pass
            _ssh_pool.pop(host_key, None)

    # 解析 host_key
    if ":" in host_key and not host_key.startswith("["):
        parts = host_key.rsplit(":", 1)
        addr = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            port = 22
    else:
        addr = host_key
        port = 22

    if "@" in addr:
        user, hostname = addr.split("@", 1)
    else:
        user, hostname = "root", addr

    # 未传入密码则从 DB 查询
    if not password:
        row = db.query_one(
            "SELECT password FROM ssh_hosts WHERE host_key=? LIMIT 1",
            (host_key,),
        )
        if row:
            password = db.decrypt_secret(row["password"] or "")

    with _ssh_lock:
        existing = _ssh_pool.get(host_key)
        # 已有连接但已断开的，丢弃以避免复用死连接
        if existing:
            try:
                t = existing.get_transport()
                if t is None or not t.is_active():
                    _ssh_pool.pop(host_key, None)
            except Exception:
                _ssh_pool.pop(host_key, None)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname, port=port, username=user,
        password=password, timeout=60,
        allow_agent=False, look_for_keys=False,
    )
    # 启用 TCP keepalive + SSH keepalive，降低中间 NAT/防火墙静默断连概率
    try:
        t = client.get_transport()
        if t is not None:
            t.set_keepalive(30)
    except Exception:
        pass
    with _ssh_lock:
        _ssh_pool[host_key] = client
    return client


def _ssh_exec(client, cmd: str, timeout: int = 30) -> Tuple[str, str, int]:
    """在已连接的 SSH 客户端上执行命令。"""
    t = client.get_transport()
    if t is None or not t.is_active():
        raise RuntimeError("SSH transport dead")
    _, sout, serr = client.exec_command(cmd, timeout=timeout)
    out = sout.read().decode("utf-8", errors="replace")
    err = serr.read().decode("utf-8", errors="replace")
    rc = sout.channel.recv_exit_status()
    return out, err, rc


def _ssh_exec_pipe(client, cmd: str, input_data: bytes = None, timeout: int = 600):
    """流式管道执行（用于 tar 数据传输）。stdout 保持原始 bytes 以保真二进制。

    timeout: 最大等待秒数，超时抛 RuntimeError。默认 600 秒（10 分钟）。
    """
    import time as _time
    t = client.get_transport()
    if not t or not t.is_active():
        raise RuntimeError("SSH transport dead")
    sess = t.open_session()
    sess.exec_command(cmd)
    if input_data:
        if isinstance(input_data, str):
            input_data = input_data.encode("utf-8")
        sess.sendall(input_data)
        sess.shutdown_write()
    out, err = b"", b""
    start = _time.time()
    last_heartbeat = start
    while not sess.exit_status_ready():
        if _time.time() - start > timeout:
            sess.close()
            raise RuntimeError(f"SSH 命令超时({timeout}s): {cmd[:80]}")
        # 每 30s 输出一次心跳（仅在确实没有数据流动时）
        now = _time.time()
        if now - last_heartbeat >= 30:
            import logging as _lg
            _lg.getLogger("engine.file").info(
                "SSH 心跳: 已等待 %.0fs, 已收 %d bytes (err %d) cmd=%s",
                now-start, len(out), len(err), cmd[:60])
            last_heartbeat = now
        if sess.recv_ready():
            out += sess.recv(65536)
        elif sess.recv_stderr_ready():
            err += sess.recv_stderr(4096)
        else:
            _time.sleep(0.05)  # 无数据时短暂 sleep 避免 CPU 空转
    # 排空剩余缓冲区
    while sess.recv_ready():
        out += sess.recv(65536)
    while sess.recv_stderr_ready():
        err += sess.recv_stderr(4096)
    rc = sess.recv_exit_status()
    sess.close()
    # stdout 返回原始 bytes（tar.gz 为二进制，绝不能按文本编解码）
    return out, err.decode("utf-8", errors="replace"), rc


# ---------- 文件列表获取 ----------

def _get_local_file_list(base_path: str) -> Dict[str, Tuple[int, int]]:
    """获取本地目录下所有文件的 {相对路径: (大小, mtime)} 映射。"""
    result = {}
    if not os.path.isdir(base_path):
        return result
    for dirpath, _dirs, files in os.walk(base_path):
        for f in files:
            fpath = os.path.join(dirpath, f)
            rel = os.path.relpath(fpath, base_path)
            try:
                st = os.stat(fpath)
                result[rel] = (st.st_size, int(st.st_mtime))
            except OSError:
                pass
    return result


def _get_remote_file_list(client, remote_path: str) -> Optional[Dict[str, Tuple[int, int]]]:
    """通过 SSH find 获取远程目录的文件列表。"""
    cmd = (
        f'cd {shlex.quote(remote_path)} 2>/dev/null && '
        f'find . -type f -printf "%p\\t%s\\t%T@\\n" 2>/dev/null || true'
    )
    out, err, rc = _ssh_exec(client, cmd, timeout=30)
    if rc not in (0, 1):
        return None
    result = {}
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        rel, sz, mt = parts
        rel = rel.lstrip("./")
        try:
            result[rel] = (int(sz), int(float(mt)))
        except (ValueError, TypeError):
            pass
    return result


class FileBackupEngine(BackupEngine):
    db_type = "file"
    display_name = "文件/目录备份"
    required_clients = []  # 标准库 + 可选 paramiko

    def __init__(self, task: dict, storage_root: str, logger=None):
        super().__init__(task, storage_root, logger)
        # 快照命名空间（设计文档 R4）：
        #   ""   —— 普通调度任务，沿用历史布局 file_snapshots/<md5>/
        #   "rt" —— 准 CDP 实时任务，落在 file_snapshots/rt/<md5>/
        # 实时捕获频率远高于普通增量，两者共用基准会互相污染，必须隔离。
        self.snapshot_namespace: str = ""

    @staticmethod
    def _clean_path(path: str) -> str:
        """清理前端可能带上的 '本地 : ' / '远程 : ' 显示前缀，返回干净路径。"""
        if not path:
            return path
        path = path.strip()
        for prefix in ("本地 :", "本地:", "远程 :", "远程:"):
            if path.startswith(prefix):
                path = path[len(prefix):].strip()
                break
        return path

    def _source_paths(self) -> List[str]:
        """返回源路径列表（自动清理显示前缀）。"""
        raw = self.extra.get("source_paths", []) or []
        return [self._clean_path(p) for p in raw if p and self._clean_path(p)]

    def _excludes(self) -> List[str]:
        return self.extra.get("exclude_patterns", [])

    def _parse_target(self) -> dict:
        """解析目标信息。返回 {"type": "local|remote", "path": "...", "host": "..."}"""
        return {
            "type": self.extra.get("target_type", "local"),
            "path": self._clean_path(self.extra.get("target_path", "")),
            "host": self.extra.get("target_host", ""),
        }

    def _parse_source(self) -> dict:
        """解析源信息。"""
        return {
            "type": self.extra.get("source_type", "local"),
            "paths": self._source_paths(),
            "host": self.extra.get("source_host", ""),
        }

    # ---------------- 备份主流程 ----------------

    def backup(self, backup_type: BackupType) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        src = self._parse_source()
        dst = self._parse_target()

        t0 = time.time()

        if backup_type == BackupType.INCREMENTAL:
            result = self._incremental_transfer(src, dst)
        else:
            result = self._full_transfer(src, dst)

        duration = round(time.time() - t0, 2)
        result.duration_sec = duration
        # 落盘后统一后处理：存储池加密(§2.6) + 全局重删(§2.4)，均失败安全
        self._post_process(result)
        return result

    def _post_process(self, result: BackupResult) -> None:
        """落盘后统一后处理：存储池加密 → 全局重删。任一环节失败不影响主流程。"""
        self._apply_pool_encryption(result)
        self._apply_global_dedup(result)

    def _apply_pool_encryption(self, result: BackupResult) -> None:
        """存储池加密（鼎甲迪备 §2.6 备份数据加密 / 防泄露）。

        仅当任务开启 encrypt_pool 且配置了主密钥(BACKUP_POOL_KEY)时加密；
        缺密钥或缺库则跳过（明文落盘并告警），绝不阻断备份。
        """
        try:
            if not self.extra.get("encrypt_pool"):
                return
            from core import crypto_pool as cp
            path = getattr(result, "backup_path", None)
            if not path or not isinstance(path, str) or not os.path.isfile(path):
                return
            if cp.is_encrypted(path):
                return  # 已加密，避免重复
            if not os.path.abspath(path).startswith(os.path.abspath(self.storage_root)):
                return
            r = cp.encrypt_file(path)
            if r.get("encrypted"):
                self.logger.info("[%s] 存储池加密完成: %s",
                                 self.task_name, db.human_size(r["encrypted_bytes"]))
                if result.message:
                    result.message += " | 已加密存储"
            else:
                self.logger.warning("[%s] 存储池加密跳过: %s",
                                    self.task_name, r.get("reason"))
        except Exception as e:  # 加密失败不影响备份主流程
            self.logger.warning("[%s] 存储池加密跳过: %s", self.task_name, e)

    def _apply_global_dedup(self, result: BackupResult) -> None:
        """对本次备份产物做全局切片重删（非阻塞、失败安全）。"""
        try:
            from core import global_dedup as gd
            path = getattr(result, "backup_path", None)
            if not path or not isinstance(path, str) or not os.path.isfile(path):
                return
            # 仅对本地产物重删；远端产物不在本机落盘，跳过
            if not os.path.abspath(path).startswith(os.path.abspath(self.storage_root)):
                return
            res = gd.dedup_file(path, task_id=self.task.get("id"), set_id=None)
            saved = int(res.get("saved_bytes") or 0)
            if saved > 0:
                # 把重删节省追加到结果提示（BackupResult 无 extra 字段，安全附加）
                self.logger.info("[%s] 全局重删节省 %s 字节",
                                 self.task_name, db.human_size(saved))
                if result.message:
                    result.message += f" | 全局重删节省 {db.human_size(saved)}"
        except Exception as e:  # 重删失败不影响备份主流程
            self.logger.warning("[%s] 全局重删跳过: %s", self.task_name, e)

    def _full_transfer(self, src: dict, dst: dict) -> BackupResult:
        """全量打包传输（原子写入，避免 Windows 防病毒/句柄锁导致空文件）。"""
        self.logger.info("[%s] 文件全量备份: %s -> %s", self.task_name, src, dst)

        src_type = src["type"]
        dst_type = dst["type"]
        paths = src["paths"]

        if not paths:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="未配置源路径(source_paths)",
            )

        # 目标为本地且指定了路径时，归档直接落到用户配置的本地目录，方便查找
        if dst_type == "local" and dst.get("path"):
            out_dir = dst["path"]
            self.logger.info("[%s] 使用用户指定本地目标目录: %s", self.task_name, out_dir)
        else:
            out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        archive_name = f"{ts}__{self.task_name}__full.tar.gz"
        archive_path = os.path.join(out_dir, archive_name)

        # 获取源文件列表用于快照（供下次增量对比）
        base = paths[0] if paths else "/"
        sf = {}
        if src_type == "local":
            sf = _get_local_file_list(base)
        else:
            try:
                client = _get_ssh_client(src["host"])
                sf = _get_remote_file_list(client, base) or {}
            except Exception as e:
                self.logger.warning("[%s] 获取远程源文件列表失败: %s", self.task_name, e)

        # 根据源/目标组合选择打包器。本地源→本地目标走自管理(先进压缩)的
        # _tar_local；其余组合走原有原子写入包装（系统 tar 压缩）。
        if src_type == "local" and dst_type == "local":
            self._tar_local(paths, archive_path)
        else:
            def _writer(tmp_path: str):
                if src_type == "local" and dst_type == "remote":
                    self._tar_local_to_remote(paths, dst, tmp_path)
                elif src_type == "remote" and dst_type == "local":
                    self._tar_remote_to_local(src, tmp_path)
                elif src_type == "remote" and dst_type == "remote":
                    self._tar_remote_to_remote(src, dst, tmp_path)
                else:
                    raise ValueError(f"不支持的源/目标组合: {src_type}->{dst_type}")
            self._atomic_write_archive(_writer, archive_path)

        final = self._final_archive_path(archive_path)
        size = os.path.getsize(final) if os.path.exists(final) else 0
        original = self._read_original_size(final)
        ratio = round(size / original, 6) if (original and size) else 0.0
        algo = self._resolve_compress_algo()
        checksum = db.sha256_file(final) if size > 0 else ""
        # 保存快照作为下次增量基准，并记录本次全量归档路径供恢复链使用
        self._save_snapshot(sf, full_path=final)

        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=final, size_bytes=size,
            original_size_bytes=original, compress_algo=algo, compress_ratio=ratio,
            checksum=checksum,
            message=f"全量备份完成 | {len(paths)} 个源 | {db.human_size(size)}",
        )

    # ---------- 增量快照 ----------

    def _snapshot_path(self, namespace: str = None) -> str:
        """返回该任务文件状态快照的存储路径（按源配置哈希，供同一源的多任务共享基准）。

        Args:
            namespace: 快照命名空间。``None`` 表示取实例默认
                :attr:`snapshot_namespace`；空串沿用历史布局
                ``file_snapshots/<md5>/``；非空（如 ``"rt"``）则落到
                ``file_snapshots/rt/<md5>/``，实现实时任务与普通任务的基准隔离。

        Returns:
            snapshot.json 的绝对路径（父目录已创建）。
        """
        ns = self.snapshot_namespace if namespace is None else (namespace or "")
        src_cfg = self._parse_source()
        key = json.dumps(src_cfg, sort_keys=True, ensure_ascii=False)
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        parts = [self.storage_root, "file_snapshots"]
        if ns:
            parts.append(str(ns))
        parts.append(h)
        base = os.path.join(*parts)
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "snapshot.json")

    def _load_snapshot(self, namespace: str = None) -> Optional[Dict[str, Tuple[int, int]]]:
        """加载上次成功备份后保存的文件快照（兼容旧格式）。"""
        path = self._snapshot_path(namespace)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            # 新格式 {"files": {...}, "last_full_path": "..."}
            files = data.get("files") if isinstance(data.get("files"), dict) else data
            if not isinstance(files, dict):
                return None
            return {k: (int(v[0]), int(v[1])) for k, v in files.items()}
        except Exception as e:
            self.logger.warning("[%s] 加载快照失败: %s", self.task_name, e)
            return None

    def _load_snapshot_meta(self, namespace: str = None) -> dict:
        """加载快照完整元数据，包括 last_full_path。"""
        path = self._snapshot_path(namespace)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            if "files" in data:
                return data
            # 旧格式：只有 files，没有 meta
            return {"files": data}
        except Exception as e:
            self.logger.warning("[%s] 加载快照元数据失败: %s", self.task_name, e)
            return {}

    def _save_snapshot(self, snapshot: Dict[str, Tuple[int, int]], full_path: str = None,
                       namespace: str = None) -> None:
        """保存当前源文件状态快照，并在全量备份时记录对应的全量归档路径。"""
        path = self._snapshot_path(namespace)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            meta = self._load_snapshot_meta(namespace)
            meta["files"] = snapshot
            if full_path:
                meta["last_full_path"] = full_path
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception as e:
            self.logger.warning("[%s] 保存快照失败: %s", self.task_name, e)

    def _diff_against_snapshot(self, sf: Dict[str, Tuple[int, int]], snapshot: Dict[str, Tuple[int, int]]) -> Tuple[List[str], List[str]]:
        """对比当前源文件列表与快照，返回 (changed, deleted)。"""
        changed, deleted = [], []
        for rel, (sz, mt) in sf.items():
            if rel not in snapshot:
                changed.append(rel)
            else:
                ssz, smt = snapshot[rel]
                if ssz != sz or abs(mt - smt) > MTIME_TOLERANCE:
                    changed.append(rel)
        for rel in snapshot:
            if rel not in sf:
                deleted.append(rel)
        return changed, deleted

    # ---------- 增量备份主流程 ----------

    def _incremental_transfer(self, src: dict, dst: dict) -> BackupResult:
        """增量备份：基于上次快照仅打包变化文件，不污染用户目标目录。"""
        self.logger.info("[%s] 文件增量备份: %s -> %s", self.task_name, src, dst)

        src_type = src["type"]
        dst_type = dst["type"]
        base = src["paths"][0] if src["paths"] else "/"

        # 1) 获取源文件列表
        if src_type == "local":
            sf = _get_local_file_list(base)
        else:
            client = _get_ssh_client(src["host"])
            sf = _get_remote_file_list(client, base)
            if sf is None:
                self.logger.warning("[%s] 远程源列表获取失败，回退全量", self.task_name)
                return self._full_transfer(src, dst)

        # 2) 加载上次快照作为基准（无快照则回退全量）
        snapshot = self._load_snapshot()
        if not snapshot:
            self.logger.info("[%s] 无历史快照，回退到全量备份", self.task_name)
            return self._full_transfer(src, dst)

        # 3) 计算差异
        changed, deleted = self._diff_against_snapshot(sf, snapshot)
        self.logger.info(
            "[%s] 增量对比: 总计=%d 变化=%d 删除=%d",
            self.task_name, len(sf), len(changed), len(deleted),
        )

        if not changed and not deleted:
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                message="无变化文件，跳过传输",
            )

        # 4) 确定归档目录：本地目标与全量保持一致，直接放到用户配置的目标目录根下
        if dst_type == "local" and dst.get("path"):
            out_dir = dst["path"]
            self.logger.info("[%s] 使用用户指定本地目标目录: %s", self.task_name, out_dir)
        else:
            out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        archive_name = f"{ts}__{self.task_name}__inc.tar.gz"
        archive_path = os.path.join(out_dir, archive_name)

        # 5) 生成仅含变化文件的增量归档（原子写入）
        try:
            if src_type == "local":
                self._tar_files(base, changed, archive_path)
            else:
                self._tar_remote_files(changed, src, archive_path)

            # 远程目标时，把生成的归档也推送过去
            if dst_type == "remote":
                self._upload_file_to_remote(self._final_archive_path(archive_path), dst)
        except Exception as e:
            self.logger.error("[%s] 增量传输异常: %s", self.task_name, e)
            return BackupResult(success=False, status=BackupStatus.FAILED, message=str(e))

        # 6) 保存新快照（作为下次增量基准）
        self._save_snapshot(sf)

        final = self._final_archive_path(archive_path)
        size = os.path.getsize(final) if os.path.exists(final) else 0
        original = self._read_original_size(final)
        ratio = round(size / original, 6) if (original and size) else 0.0
        algo = self._resolve_compress_algo()
        checksum = db.sha256_file(final) if size > 0 else ""
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=final, size_bytes=size,
            original_size_bytes=original, compress_algo=algo, compress_ratio=ratio,
            checksum=checksum,
            message=f"增量备份完成 | 变化={len(changed)} 删除={len(deleted)} | {db.human_size(size)}",
        )

    # ---------- 准 CDP 实时捕获复用入口（core/rt_backup/file_rt.py 调用） ----------

    def list_source_files(self) -> Optional[Dict[str, Tuple[int, int]]]:
        """列出源根目录当前文件状态 ``{相对路径: (大小, mtime)}``。

        本地源恒返回字典（目录不存在时为空字典）；远程源在 SSH 失败时返回
        ``None``，由调用方判定为「本轮扫描不可信」而不是「文件全被删了」。
        """
        src = self._parse_source()
        paths = src.get("paths") or []
        base = paths[0] if paths else ""
        if not base:
            return {}
        if src.get("type") == "local":
            return _get_local_file_list(base)
        try:
            client = _get_ssh_client(src.get("host", ""))
        except Exception as e:
            self.logger.warning("[%s] 连接远程源失败: %s", self.task_name, e)
            return None
        try:
            return _get_remote_file_list(client, base)
        except Exception as e:
            self.logger.warning("[%s] 获取远程源文件列表失败: %s", self.task_name, e)
            return None

    def has_base_snapshot(self) -> bool:
        """当前命名空间下是否已存在可用的增量基准（快照 + 全量归档）。"""
        meta = self._load_snapshot_meta()
        if not isinstance(meta.get("files"), dict) or not meta["files"]:
            # 空目录也算有效基准，只要 last_full_path 归档还在
            if "files" not in meta:
                return False
        full_path = meta.get("last_full_path") or ""
        return bool(full_path) and os.path.exists(full_path)

    def ensure_base_full(self, out_dir: str = "", force: bool = False) -> BackupResult:
        """确保存在基准全量：无快照/无全量归档时立即做一次全量。

        Args:
            out_dir: 基准归档落盘目录。实时任务传 ``LogRepository.base_dir()``，
                使基准与增量同处实时仓库、便于统一 prune 与上云；
                留空则沿用任务自身的目标目录（与普通全量一致）。
            force: 忽略已有基准强制重做（用于「重建基准」运维动作）。

        Returns:
            BackupResult。已存在基准且未 force 时返回 ``success=True`` 且
            ``message`` 标注「复用」，``backup_path`` 指向既有全量归档。
        """
        meta = self._load_snapshot_meta()
        existing_full = meta.get("last_full_path") or ""
        if not force and existing_full and os.path.exists(existing_full) \
                and isinstance(meta.get("files"), dict):
            size = os.path.getsize(existing_full)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=existing_full, size_bytes=size,
                checksum=meta.get("last_full_checksum", ""),
                message=f"复用既有基准全量 | {db.human_size(size)}",
            )

        src = self._parse_source()
        if not src.get("paths"):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="未配置源路径(source_paths)",
            )
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            dst = {"type": "local", "path": out_dir, "host": ""}
        else:
            dst = self._parse_target()

        t0 = time.time()
        result = self._full_transfer(src, dst)
        result.duration_sec = round(time.time() - t0, 2)
        # _full_transfer 内部已 _save_snapshot(sf, full_path=...)，此处补记校验和
        if result.success and result.checksum:
            try:
                snap_meta = self._load_snapshot_meta()
                snap_meta["last_full_checksum"] = result.checksum
                path = self._snapshot_path()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(snap_meta, f, ensure_ascii=False)
            except Exception as e:
                self.logger.warning("[%s] 记录基准校验和失败（已忽略）: %s", self.task_name, e)
        return result

    def capture_increment(self, out_dir: str = "", tag: str = "",
                          changed: List[str] = None, deleted: List[str] = None,
                          source_files: Dict[str, Tuple[int, int]] = None) -> BackupResult:
        """执行一次增量捕获：仅打包变化文件到 ``out_dir``，并提交新基准快照。

        与 :meth:`_incremental_transfer` 共用同一套 diff / 打包 / 快照语义，
        差别仅在于：**不写用户目标目录、不做远程推送**——实时增量一律先落
        本地实时仓库，再由三级存储异步上云。

        Args:
            out_dir: 增量归档输出目录，缺省 ``self._output_dir()``。
            tag: 归档名时间戳前缀，缺省当前时刻。
            changed: 预先算好的变更相对路径列表（来自 Watcher，避免二次扫描）。
            deleted: 预先算好的删除相对路径列表。
            source_files: 预先扫描好的源文件状态，用于提交新快照。

        Returns:
            BackupResult。无变化时 ``success=True`` 且 ``backup_path=""``。
        """
        src = self._parse_source()
        paths = src.get("paths") or []
        if not paths:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="未配置源路径(source_paths)")
        base = paths[0]

        # 1) 源文件状态：优先用 Watcher 传入的扫描结果
        sf = source_files
        if sf is None:
            sf = self.list_source_files()
        if sf is None:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="源文件列表获取失败（远程不可达），本轮跳过")

        # 2) 基准快照
        snapshot = self._load_snapshot()
        if snapshot is None:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="无基准快照，需先执行 ensure_base_full()")

        # 3) 差异（Watcher 已算过则直接采用，保证与其上报的批次一致）
        if changed is None or deleted is None:
            changed, deleted = self._diff_against_snapshot(sf, snapshot)
        changed = list(changed or [])
        deleted = list(deleted or [])

        if not changed and not deleted:
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                backup_path="", size_bytes=0,
                                message="无变化文件，跳过传输")

        # 4) 归档
        out_dir = out_dir or self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = tag or self._timestamp()
        archive_path = os.path.join(out_dir, f"{ts}__{self.task_name}__inc.tar.gz")

        # 全部是删除时没有实体文件可打包，仍生成一个空 tar 以承载「删除」这一事实，
        # 保证恢复链上每个恢复点都有可校验的产物（R9 要求非空，故写入清单条目）。
        try:
            if changed:
                if src.get("type") == "local":
                    self._tar_files(base, changed, archive_path)
                else:
                    self._tar_remote_files(changed, src, archive_path)
            else:
                self._tar_manifest_only(archive_path, deleted)
        except Exception as e:
            self.logger.error("[%s] 实时增量打包失败: %s", self.task_name, e)
            return BackupResult(success=False, status=BackupStatus.FAILED, message=str(e))

        final = self._final_archive_path(archive_path)
        size = os.path.getsize(final) if os.path.exists(final) else 0
        if size <= 0:
            # 空包绝不入 journal（R9），直接清理并按失败上报
            try:
                if os.path.exists(final):
                    os.unlink(final)
            except OSError:
                pass
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="增量归档为空，已丢弃")

        # 5) 提交新基准（打包成功后才提交，失败时保持旧基准以便下轮重试）
        self._save_snapshot(sf)

        original = self._read_original_size(final)
        ratio = round(size / original, 6) if (original and size) else 0.0
        algo = self._resolve_compress_algo()
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=final, size_bytes=size,
            original_size_bytes=original, compress_algo=algo, compress_ratio=ratio,
            checksum=db.sha256_file(final),
            message=(f"实时增量完成 | 变化={len(changed)} 删除={len(deleted)} | "
                     f"{db.human_size(size)}"),
        )

    def _tar_manifest_only(self, archive_path: str, deleted: List[str]) -> bool:
        """仅含删除清单的归档（无新增/修改文件时使用）。

        清单以 ``.rt_deleted.txt`` 写入归档根，恢复时被 :meth:`_restore_filter`
        正常释放，供人工核对；不参与文件覆盖，因此不会破坏恢复结果。
        """
        payload = "\n".join((d or "").replace("\\", "/") for d in (deleted or []))
        payload = (payload + "\n").encode("utf-8")

        def _write(tmp_path: str) -> None:
            with tarfile.open(tmp_path, "w:gz") as tf:
                info = tarfile.TarInfo(name=".rt_deleted.txt")
                info.size = len(payload)
                info.mtime = int(time.time())
                tf.addfile(info, io.BytesIO(payload))

        self._atomic_write_archive(_write, archive_path)
        return True

    # ---------------- 各种传输组合实现 ----------------

    def _atomic_write_archive(self, writer, archive_path: str) -> None:
        """原子写入归档：先写到同目录临时文件，完成后 os.replace 替换目标。
        避免 Windows 防病毒/WinRAR 扫描导致目标文件被锁时写出空包。"""
        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".tar.gz", dir=parent)
        try:
            os.close(fd)
            writer(tmp_path)
            # 关闭文件句柄后再替换，避免 Windows 占用
            if os.path.exists(archive_path):
                os.unlink(archive_path)
            os.replace(tmp_path, archive_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def _tar_local(self, paths: List[str], archive_path: str):
        """本地多路径 → 本地归档（先进压缩：zstd，回退 gzip）。

        先以未压缩 tar 写入临时文件，再用 zstd（或 gzip 回退）流式压缩为
        ``<archive>.zst``，同时记录未压缩的 tar 字节数供计算压缩率。
        产物直接落盘到 ``archive_path``（含后缀），self 调用方用
        :meth:`_final_archive_path` 取真实路径。
        """
        algo = self._resolve_compress_algo()
        suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
        final_path = archive_path + suffix
        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_tar = tempfile.mkstemp(prefix=".tmp_", suffix=".tar", dir=parent)
        os.close(fd)
        try:
            common = os.path.commonpath(paths) if len(paths) > 1 else (paths[0] if paths else "/")
            with tarfile.open(tmp_tar, "w") as tf:
                for p in paths:
                    if os.path.exists(p):
                        tf.add(p, arcname=os.path.relpath(p, common) if common != "/" else os.path.basename(p))
            original = os.path.getsize(tmp_tar)
            if os.path.exists(final_path):
                os.unlink(final_path)
            self._compress_file(tmp_tar, final_path, algo)
            self._stash_original_size(final_path, original)
        finally:
            if os.path.exists(tmp_tar):
                os.unlink(tmp_tar)

    def _tar_files(self, base_path: str, rel_files: List[str], archive_path: str) -> bool:
        """把 base_path 下指定相对路径的文件打包成归档（仅含这些文件，保持相对路径）。

        先进压缩：zstd（回退 gzip）。先未压缩 tar，再压缩并记录原始大小。
        """
        base_path = base_path.replace("\\", "/")
        algo = self._resolve_compress_algo()
        suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
        final_path = archive_path + suffix
        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_tar = tempfile.mkstemp(prefix=".tmp_", suffix=".tar", dir=parent)
        os.close(fd)
        try:
            with tarfile.open(tmp_tar, "w") as tf:
                for rel in rel_files:
                    rel_norm = rel.replace("\\", "/")
                    full = os.path.join(base_path, rel_norm)
                    if os.path.exists(full) and os.path.isfile(full):
                        tf.add(full, arcname=rel_norm)
            original = os.path.getsize(tmp_tar)
            if os.path.exists(final_path):
                os.unlink(final_path)
            self._compress_file(tmp_tar, final_path, algo)
            self._stash_original_size(final_path, original)
        finally:
            if os.path.exists(tmp_tar):
                os.unlink(tmp_tar)
        return True

    def _tar_remote_files(self, changed: List[str], src: dict, archive_path: str) -> bool:
        """远程源 → 本地增量归档：通过 tar -T 仅打包变化文件（原子写入）。"""
        client = _get_ssh_client(src["host"])
        remote_base = src["paths"][0] if src["paths"] else "/"
        data, err, rc = _ssh_exec_pipe(
            client, f'tar -C {shlex.quote(remote_base)} -czf - -T -',
            input_data="\n".join(changed).encode("utf-8"),
        )
        if rc != 0:
            raise RuntimeError(f"远程打包失败: {err}")
        if not _safe_write_via_temp(archive_path, data, self.logger):
            raise RuntimeError("本地写入增量归档失败")
        return True

    def _upload_file_to_remote(self, local_path: str, dst: dict):
        """把本地归档上传到远程目标目录。"""
        client = _get_ssh_client(dst["host"])
        remote_dir = dst["path"]
        name = os.path.basename(local_path)
        _ssh_exec(client, f'mkdir -p {shlex.quote(remote_dir)}')
        sftp = client.open_sftp()
        try:
            sftp.put(local_path, f"{remote_dir}/{name}")
        finally:
            sftp.close()

    def _tar_local_to_remote(self, paths: List[str], dst: dict, archive_path: str):
        """本地 → 远程：先打 tar 再通过 SSH pipe 传过去解压。"""
        fd, flist = tempfile.mkstemp(suffix=".txt", text=True)
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(paths) + "\n")
            proc = subprocess.Popen(
                ["tar", "-C", "/", "-czf", "-", "-T", flist],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            data, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"本地打包失败: {err.decode('utf-8', 'replace')}")
            # 写本地存档副本（原子写入）
            if not _safe_write_via_temp(archive_path, data, self.logger):
                raise RuntimeError("本地写入归档副本失败")
            # 传到远程解压
            client = _get_ssh_client(dst["host"])
            _, e, rc = _ssh_exec_pipe(
                client,
                f'mkdir -p {shlex.quote(dst["path"])} && tar -C {shlex.quote(dst["path"])} -xzf -',
                input_data=data,
            )
            if rc != 0:
                raise RuntimeError(f"远程解压失败: {e}")
        finally:
            os.unlink(flist)

    def _tar_remote_to_local(self, src: dict, archive_path: str):
        """远程 → 本地：SSH 端 tar 打包 → SFTP/pipe 下载到本地。"""
        self.logger.info("[%s] [1/3] 连接远程主机: %s", self.task_name, src["host"])
        client = _get_ssh_client(src["host"])
        remote_base = src["paths"][0] if src["paths"] else "/"

        self.logger.info("[%s] [2/3] 远程打包中 (tar -C %s -czf - .)，请耐心等待...", self.task_name, remote_base)
        t0 = time.time()
        out, err, rc = _ssh_exec_pipe(
            client, f'tar -C {shlex.quote(remote_base)} -czf - . 2>/dev/null',
            timeout=1800,  # 大目录最多等 30 分钟
        )
        elapsed = round(time.time() - t0, 1)
        self.logger.info("[%s] SSH tar 完成, 耗时=%ss, rc=%d, size=%d bytes",
                         self.task_name, elapsed, rc, len(out))
        if rc != 0:
            raise RuntimeError(f"远程打包失败(rc={rc}): {err}")

        self.logger.info("[%s] [3/3] 写入本地归档: %s (%d bytes)", self.task_name, archive_path, len(out))
        # 使用原子写入，避开 Windows 防病毒/WinRAR 扫描导致的句柄锁
        if not _safe_write_via_temp(archive_path, out, self.logger):
            raise RuntimeError(f"写入本地归档失败: {archive_path}")
        self.logger.info("[%s] 归档写入完成: %s", self.task_name, archive_path)

    def _tar_remote_to_remote(self, src: dict, dst: dict, archive_path: str):
        """远程 → 远程（中转）：从源打包 → 经本机 pipe 到目标解压。"""
        client_src = _get_ssh_client(src["host"])
        remote_base = src["paths"][0] if src["paths"] else "/"
        data, err, rc = _ssh_exec_pipe(
            client_src, f'tar -C "{remote_base}" -czf - . 2>/dev/null',
        )
        if rc != 0:
            raise RuntimeError(f"源打包失败: {err}")
        # 存档到本地（data 已是 bytes，原子写入）
        if not _safe_write_via_temp(archive_path, data, self.logger):
            raise RuntimeError(f"写入本地归档失败: {archive_path}")
        # 传到目标
        client_dst = _get_ssh_client(dst["host"])
        _, e, rc2 = _ssh_exec_pipe(
            client_dst,
            f'mkdir -p {shlex.quote(dst["path"])} && tar -C {shlex.quote(dst["path"])} -xzf -',
            input_data=data,
        )
        if rc2 != 0:
            raise RuntimeError(f"目标解压失败: {e}")

    # ---------------- 恢复 ----------------

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 0) 跨主机恢复：SFTP 推送到目标主机 → tar 解压
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            target_dir = kwargs.get("target_db") or kwargs.get("target_host") or "/tmp/restore"
            return self._try_cross_host_restore(backup_path, target_host_info, target_dir)

        target = kwargs.get("target_db") or kwargs.get("target_host") or ""
        if not target:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="恢复失败：未指定目标目录（使用 target_db 字段）",
            )

        # 1) 构建恢复链：增量恢复必须先回全量，再按时间顺序应用增量
        #    PITR 场景由 PITRRestore 传入 chain_override（journal 精确解析结果），
        #    绕开对 backup_records 的模糊扫描，避免高频实时增量被 LIMIT 截断。
        chain = self._build_restore_chain(backup_path,
                                          chain_override=kwargs.get("chain_override"))
        if not chain:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="恢复失败：未找到可恢复的备份链",
            )

        self.logger.info(
            "[%s] 文件恢复链: %s -> %s (共 %d 个归档)",
            self.task_name, chain, target, len(chain),
        )
        try:
            os.makedirs(target, exist_ok=True)
            for item in chain:
                self.logger.info("[%s] 解压归档: %s", self.task_name, item)
                # 先按压缩算法解压（zstd/gzip 均可恢复），再用 tarfile 释放内容；
                # 若为非压缩 tar / tarfile 原生可识别格式，直接打开。
                if item.endswith((".zst", ".gz")) and not item.endswith(".tar.gz"):
                    dec = self.pipe_decompress("zstd" if item.endswith(".zst") else "gzip")
                    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tfh:
                        tmp_tar = tfh.name
                    proc = subprocess.Popen(dec, stdin=open(item, "rb"),
                                            stdout=open(tmp_tar, "wb"),
                                            stderr=subprocess.PIPE)
                    _, err = proc.communicate()
                    if proc.returncode != 0:
                        raise RuntimeError(f"解压失败: {err.decode('utf-8','replace')[:200]}")
                    with tarfile.open(tmp_tar, "r:") as tf:
                        tf.extractall(target, filter=self._restore_filter)
                    try:
                        os.unlink(tmp_tar)
                    except OSError:
                        pass
                else:
                    with tarfile.open(item, "r:*") as tf:
                        tf.extractall(target, filter=self._restore_filter)
            types = ",".join(
                "增量" if "_inc" in os.path.basename(p) else "全量" for p in chain
            )
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path,
                message=f"已恢复至 {target} | 链: {types}",
            )
        except Exception as e:
            self.logger.error("[%s] 文件恢复失败: %s", self.task_name, e)
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path,
                message=f"恢复失败: {e}",
            )

    def _build_restore_chain(self, backup_path: str,
                             chain_override: List[str] = None) -> List[str]:
        """根据备份路径构建恢复链。
        - 全量 -> [full]
        - 增量 -> [full, inc1, inc2, ..., selected_inc]（按 started_at 排序）
        支持跨任务：只要源路径相同，即可找到对应全量基准。
        若找不到库记录（如手动传入路径），直接返回 [backup_path]。

        Args:
            backup_path: 目标归档路径。
            chain_override: 由上层（PITRRestore）解析好的精确链。非空时直接采用，
                仅做「文件存在性」过滤，不再查询 backup_records。
        """
        if chain_override:
            chain = []
            for item in chain_override:
                if not item:
                    continue
                if os.path.exists(item) and item not in chain:
                    chain.append(item)
                elif not os.path.exists(item):
                    self.logger.warning("[%s] 恢复链缺失归档，已跳过: %s",
                                        self.task_name, item)
            if chain:
                return chain
            self.logger.warning("[%s] chain_override 全部缺失，回退自动解析",
                                self.task_name)

        if not backup_path or not os.path.exists(backup_path):
            return []
        norm = os.path.abspath(backup_path).replace("\\", "/")

        # 1) 尝试从快照直接拿到 last_full_path（最可靠，尤其跨任务场景）
        meta = self._load_snapshot_meta()
        last_full_from_snap = meta.get("last_full_path")

        # 2) 从数据库找到当前记录
        rec = db.query_one(
            "SELECT * FROM backup_records WHERE backup_path=? AND db_type='file' ORDER BY id DESC LIMIT 1",
            (norm,),
        )
        if not rec:
            # 退化成普通单文件恢复
            if last_full_from_snap and last_full_from_snap != backup_path and os.path.exists(last_full_from_snap):
                return [last_full_from_snap, backup_path]
            return [backup_path]

        # 当前是 full 就直接返回
        if rec.get("backup_type") == "full":
            return [backup_path]

        cur_started = rec.get("started_at") or ""
        cur_task_id = rec.get("task_id")

        # 3) 找全量基准：优先用快照里的 last_full_path，再按源路径匹配
        full_path = None
        full_started = ""
        if last_full_from_snap and os.path.exists(last_full_from_snap):
            full_path = last_full_from_snap
            # 尝试从数据库拿 started_at
            fr = db.query_one(
                "SELECT started_at FROM backup_records WHERE backup_path=? AND db_type='file' AND backup_type='full'",
                (last_full_from_snap.replace("\\", "/"),),
            )
            full_started = fr.get("started_at") or "" if fr else ""

        if not full_path and cur_task_id:
            # 同一任务内找最近的 full
            full_rec = db.query_one(
                "SELECT * FROM backup_records WHERE task_id=? AND db_type='file' "
                "AND backup_type='full' AND (started_at <= ? OR ? = '') "
                "AND backup_path IS NOT NULL AND backup_path != '' "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (cur_task_id, cur_started, cur_started),
            )
            if full_rec:
                full_path = full_rec["backup_path"]
                full_started = full_rec.get("started_at") or ""

        if not full_path:
            # 按源路径匹配：解析当前任务源路径，在历史 full 记录中找相同源的最近一条
            src_key = self._source_config_key()
            if src_key:
                candidates = db.query(
                    "SELECT * FROM backup_records WHERE db_type='file' AND backup_type='full' "
                    "AND (started_at <= ? OR ? = '') AND backup_path IS NOT NULL AND backup_path != '' "
                    "ORDER BY started_at DESC, id DESC LIMIT 200",
                    (cur_started, cur_started),
                )
                for c in candidates:
                    if self._same_source(c.get("extra_options"), src_key):
                        full_path = c["backup_path"]
                        full_started = c.get("started_at") or ""
                        break

        if not full_path:
            # 没有全量基准，只能单独恢复这个增量（可能不完整）
            return [backup_path]

        # 4) 收集 full 之后到当前记录之间的所有增量（跨任务、同源）
        src_key = self._source_config_key()
        inc_rows = db.query(
            "SELECT * FROM backup_records WHERE db_type='file' AND backup_type='incremental' "
            "AND (started_at >= ? OR ? = '') AND (started_at <= ? OR ? = '') "
            "AND backup_path IS NOT NULL AND backup_path != '' "
            "ORDER BY started_at ASC, id ASC",
            (full_started, full_started, cur_started, cur_started),
        )
        chain = [full_path]
        for r in inc_rows:
            bp = r.get("backup_path")
            if bp and os.path.exists(bp) and self._same_source(r.get("extra_options"), src_key):
                if bp not in chain:
                    chain.append(bp)
        # 确保当前记录也在链中
        if chain[-1] != backup_path and os.path.exists(backup_path):
            chain.append(backup_path)
        return chain

    def _source_config_key(self) -> Tuple[str, ...]:
        """返回源配置标识（源类型 + 清理后的源路径/主机），用于跨任务匹配同源备份。"""
        src = self._parse_source()
        paths = sorted(src.get("paths") or [])
        host = src.get("host", "")
        return (src.get("type", "local"), host, tuple(paths))

    def _same_source(self, extra_options, target_key: Tuple[str, ...]) -> bool:
        """判断 extra_options 是否与目标源配置相同。"""
        if not extra_options:
            return False
        try:
            if isinstance(extra_options, str):
                extra = json.loads(extra_options)
            else:
                extra = extra_options
            src_type = extra.get("source_type", "local")
            host = extra.get("source_host", "")
            paths = sorted(self._clean_path(p) for p in (extra.get("source_paths") or []) if self._clean_path(p))
            return (src_type, host, tuple(paths)) == target_key
        except Exception:
            return False

    # ---------- 先进压缩辅助 ----------
    def _compress_file(self, src_path: str, dst_path: str, algo: str) -> None:
        """把未压缩 tar(src_path) 用 zstd/gzip 流式压缩为 dst_path（可逆）。

        开启限速（bandwidth_limit>0）且系统存在 pv 时，压缩前先用 pv 限速读取
        源文件，近似控制整体备份吞吐（落盘带宽）。
        """
        comp = self.pipe_compress(algo)
        pv = self._pv_throttle()
        if pv:
            # pv -L <bytes> src_path | comp  （限速读取 → 压缩落盘）
            cmd = pv + [src_path] + comp
            with open(dst_path, "wb") as fout:
                proc = subprocess.Popen(cmd, stdout=fout, stderr=subprocess.PIPE)
                _, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"压缩失败({algo}): {err.decode('utf-8','replace')[:300]}")
        else:
            with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
                proc = subprocess.Popen(comp, stdin=fin, stdout=fout,
                                         stderr=subprocess.PIPE)
                _, err = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(f"压缩失败({algo}): {err.decode('utf-8','replace')[:300]}")

    def _stash_original_size(self, archive_path: str, original: int) -> None:
        """把未压缩原始大小暂存到 <archive>.orig.size，供落库时读取压缩率。"""
        try:
            with open(archive_path + ".orig.size", "w", encoding="utf-8") as f:
                f.write(str(int(original)))
        except OSError:
            pass

    def _read_original_size(self, archive_path: str) -> int:
        """读取暂存的原始大小；不存在返回 0。"""
        p = archive_path + ".orig.size"
        try:
            if os.path.exists(p):
                return int(open(p, "r", encoding="utf-8").read().strip() or 0)
        except (OSError, ValueError):
            pass
        return 0

    def _final_archive_path(self, base_archive_path: str) -> str:
        """返回真正落盘的归档路径（带 .zst/.gz 后缀）。"""
        for suf in (".zst", ".gz", ""):
            cand = base_archive_path + suf
            if os.path.exists(cand):
                return cand
        return base_archive_path

    @staticmethod
    def _restore_filter(member, path=""):
        """可选：恢复时过滤危险路径（防止路径穿越）。Python 3.12+ tarfile 会传 2 个参数。"""
        mpath = member.name
        if mpath.startswith("/") or ".." in mpath:
            return None
        return member

    # ---------- 合成全量（鼎甲迪备 §3.2 永久增量 / CDM 合成） ----------
    def synthesize_full(self, sets: list = None, target_storage_tier: int = None,
                        target_record_id: int = None) -> BackupResult:
        """把「全量 + 一串增量」合并为一份新的完整归档（合成全量）。

        文件备份的合成全量 = 按链顺序解压每个归档到临时目录，后写的同名文件
        覆盖先前的（增量语义），最后把临时目录重新打成一份 tar(.zst/.gz)。
        产物经 verify_record 校验后可直接作为新的全量基准即时恢复，中间增量
        副本由生命周期策略按 chain_status='merged' 回收，实现「一次全备永久增备」。

        返回 BackupResult：success 表示合成成功；simulated 恒为 False（真实合并）。
        """
        import shutil
        import tarfile

        sets = sets or self.list_sets()
        if not sets:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="无可用备份集用于合成")
        # 链头（full/synthetic_full）+ 其增量（parent_set_id 指向链头）
        base = next((s for s in sets
                     if s.get("set_type") in ("full", "synthetic_full")), None)
        if not base:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="找不到合成基准(full)")
        chain = [base] + [s for s in sets
                          if s.get("parent_set_id") == base["id"]
                          and s.get("set_type") == "incremental"]
        chain = [c for c in chain if c.get("object_key")
                 and os.path.isfile(c["object_key"])]
        if len(chain) < 2:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="增量链不足，无需合成")

        ts = self._timestamp()
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".syn_")
        try:
            # 1) 按链顺序解压到临时目录（增量覆盖全量）
            for c in chain:
                self._extract_archive(c["object_key"], tmp)
            # 2) 重新打包成合成全量
            final = os.path.join(out_dir, f"{ts}__{self.task_name}__syn_full.tar")
            with tarfile.open(final, "w") as tf:
                for root, _dirs, files in os.walk(tmp):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        arc = os.path.relpath(fp, tmp)
                        tf.add(fp, arcname=arc)
            # 3) 先进压缩（zstd 回退 gzip）
            algo = self._resolve_compress_algo()
            suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
            final_path = final + suffix
            self._compress_file(final, final_path, algo)
            self._stash_original_size(final_path, self._dir_size(tmp))
            size = os.path.getsize(final_path)
            checksum = db.sha256_file(final_path)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=final_path, size_bytes=size,
                original_size_bytes=self._dir_size(tmp),
                compress_algo=algo, checksum=checksum,
                simulated=False,
                message=f"合成全量完成（合并 {len(chain)-1} 个增量）| {db.human_size(size)}")
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"合成全量失败: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _extract_archive(self, archive_path: str, dest: str) -> None:
        """解压 .tar / .tar.gz / .tar.zst 到 dest（复用恢复路径的解压逻辑）。"""
        import tarfile
        import subprocess
        os.makedirs(dest, exist_ok=True)
        if archive_path.endswith((".zst", ".gz")) and not archive_path.endswith(".tar.gz"):
            algo = "zstd" if archive_path.endswith(".zst") else "gzip"
            dec = self.pipe_decompress(algo)
            fd, tmp_tar = tempfile.mkstemp(suffix=".tar")
            os.close(fd)
            proc = subprocess.Popen(dec, stdin=open(archive_path, "rb"),
                                    stdout=open(tmp_tar, "wb"),
                                    stderr=subprocess.PIPE)
            _, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"解压失败: {err.decode('utf-8','replace')[:200]}")
            with tarfile.open(tmp_tar, "r:") as tf:
                tf.extractall(dest, filter=self._restore_filter)
            try:
                os.unlink(tmp_tar)
            except OSError:
                pass
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(dest, filter=self._restore_filter)

    @staticmethod
    def _dir_size(path: str) -> int:
        total = 0
        for root, _d, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        return total

    def list_databases(self) -> List[str]:
        """文件引擎无需列举库，返回空。"""
        return []
