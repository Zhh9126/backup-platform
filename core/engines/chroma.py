# -*- coding: utf-8 -*-
"""Chroma 向量数据库引擎（AI 工作负载 · 目录级快照）。

Chroma（持久化模式）的全部状态落在一个目录：
  chroma.sqlite3（元数据）+ <uuid>/data_level0.bin（HNSW 索引二进制）等。
无官方在线快照 API——采用目录 tar 快照 + sqlite 完整性校验 + manifest 记录。

诚实边界（如实告知用户）：
  - 活跃写入时打目录快照可能捕获到 sqlite 与 HNSW 二进制不一致的内部状态
    （与"对活跃数据库做文件级快照"同一风险）；生产建议：备份窗口内暂停写入
    （Chroma 无原生命令，业务侧停写），或使用 Chroma 0.4.x+ 的
    client.create_backup？——无该 API，故一致性责任在调用窗口。
  - 恢复 = 目录回放（tar 推到目标主机解包），需目标侧 Chroma 版本兼容。
"""
import json
import os
import time

from core.engines.base import BackupEngine, BackupResult, BackupStatus, BackupType
import core.db as db


class ChromaEngine(BackupEngine):
    db_type = "chroma"
    display_name = "Chroma 向量库"

    def _remote_dir(self) -> str:
        extra = self._parse_extra() or {}
        return (extra.get("chroma_dir")
                or self.task.get("db_name") or "").rstrip("/") or "/var/lib/chroma"

    # ---------------- 备份 ----------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        """目录 tar 快照：优先远端 SSH（数据库服务器不留产物），本机兜底。"""
        start = time.time()
        from core.engines.file import _get_ssh_client, _ssh_exec_pipe   # 复用 SSH 通道
        from core import remote_dump

        ssh_host = remote_dump.resolve_ssh_host(self.task)
        rdir = self._remote_dir()
        ts = self._timestamp()
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        local_path = os.path.join(out_dir, f"chroma_{ts}.tar.gz")

        tar_cmd = (
            f"tar czf {local_path}.tmp -C {rdir} . && "
            f"sqlite3 {rdir}/chroma.sqlite3 'PRAGMA integrity_check;' 2>/dev/null || true; "
            f"mv {local_path}.tmp {local_path} && ls -la {local_path}")
        try:
            if ssh_host:
                client = remote_dump._connect(ssh_host)
                try:
                    # 远端打包（含 sqlite 完整性检查结果记录）
                    out, err, rc = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(
                            f"bash -lc 'if command -v sqlite3 >/dev/null; then "
                            f"sqlite3 {rdir}/chroma.sqlite3 \"PRAGMA integrity_check;\"; "
                            f"else echo NO_SQLITE3; fi'"), timeout=120)
                    integrity = (out or b"").decode("utf-8", "replace").strip()[:40]
                    out, err, rc = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(tar_cmd), timeout=1800)
                    if rc != 0:
                        return BackupResult(success=False, status=BackupStatus.FAILED,
                                            duration_sec=round(time.time() - start, 2),
                                            message=f"远端打包失败 rc={rc}: "
                                                    f"{(err or b'').decode('utf-8','replace')[:200]}")
                    # 断点续传拉回
                    res = remote_dump.sftp_pull_resumable(
                        client, local_path, local_path, task=self.task,
                        key=os.path.basename(local_path), db_type="chroma",
                        host_key=ssh_host.get("host_key", ""), has_rc=False,
                        stable_secs=3, label="chroma dir snapshot", min_size=512)
                    size = res["size"]
                    # 清理远端
                    try:
                        remote_dump.cleanup_remote_artifacts(client, [local_path])
                    except Exception:
                        pass
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                # 本机路径
                if not os.path.isdir(rdir):
                    return BackupResult(success=False, status=BackupStatus.FAILED,
                                        duration_sec=round(time.time() - start, 2),
                                        message=f"Chroma 目录不存在: {rdir}")
                import subprocess
                rc = subprocess.run(["tar", "czf", local_path, "-C", rdir, "."],
                                    capture_output=True, text=True, timeout=1800)
                if rc.returncode != 0:
                    return BackupResult(success=False, status=BackupStatus.FAILED,
                                        duration_sec=round(time.time() - start, 2),
                                        message=f"本机打包失败: {rc.stderr[:200]}")
                size = os.path.getsize(local_path)
                integrity = "local"
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message=f"Chroma 快照失败: {str(e)[:200]}")

        with open(local_path + ".manifest.json", "w", encoding="utf-8") as mf:
            json.dump({"kind": "chroma_dir", "remote_dir": rdir,
                       "size_bytes": size,
                       "sqlite_integrity": integrity,
                       "sha256": db.sha256_file(local_path),
                       "note": "活跃写入窗口内的一致性由调用方停写保证（诚实边界）"},
                      mf, ensure_ascii=False, indent=2)
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=local_path, size_bytes=size,
                            duration_sec=round(time.time() - start, 2),
                            message=f"Chroma 目录快照成功（{db.human_size(size)}，"
                                    f"sqlite 完整性: {integrity or '未检查'}）"
                                    f"——注意写入窗口一致性边界")

    # ---------------- 恢复 ----------------
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        start = time.time()
        target_dir = (kwargs.get("target_db")
                      or self.task.get("db_name") or "").strip()
        if not target_dir or not target_dir.startswith("/"):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message="Chroma 恢复目标必须是目录绝对路径"
                                        "（target_db 填 Chroma 持久化目录）")
        if not os.path.isfile(backup_path):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message=f"快照文件不存在: {backup_path}")
        from core.engines.file import _get_ssh_client
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        try:
            if ssh_host:
                client = remote_dump._connect(ssh_host)
                try:
                    sftp = client.open_sftp()
                    remote_tmp = f"/tmp/chroma_restore_{int(time.time())}.tar.gz"
                    sftp.put(backup_path, remote_tmp)
                    sftp.close()
                    cmd = (f"mkdir -p {target_dir} && "
                           f"tar xzf {remote_tmp} -C {target_dir} && rm -f {remote_tmp} "
                           f"&& ls {target_dir} | head -5")
                    from core.engines.file import _ssh_exec_pipe
                    out, err, rc = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(cmd), timeout=600)
                    if rc != 0:
                        return BackupResult(success=False, status=BackupStatus.FAILED,
                                            duration_sec=round(time.time() - start, 2),
                                            message=f"远端解包失败 rc={rc}: "
                                                    f"{(err or b'').decode('utf-8','replace')[:200]}")
                    listing = (out or b"").decode("utf-8", "replace").strip()[:120]
                    return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                        duration_sec=round(time.time() - start, 2),
                                        message=f"Chroma 快照已回放到 {target_dir}"
                                                f"（内容: {listing}）")
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                import subprocess
                os.makedirs(target_dir, exist_ok=True)
                rc = subprocess.run(["tar", "xzf", backup_path, "-C", target_dir],
                                    capture_output=True, text=True, timeout=600)
                if rc.returncode != 0:
                    return BackupResult(success=False, status=BackupStatus.FAILED,
                                        duration_sec=round(time.time() - start, 2),
                                        message=f"本机解包失败: {rc.stderr[:200]}")
                listing = ",".join(sorted(os.listdir(target_dir))[:5])
                return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                    duration_sec=round(time.time() - start, 2),
                                    message=f"Chroma 快照已回放到 {target_dir}"
                                            f"（内容: {listing}）")
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message=f"Chroma 恢复失败: {str(e)[:200]}")

    # ---------------- 校验 ----------------
    def verify_record(self, record) -> BackupResult:
        path = record.get("backup_path") or ""
        if not path or not os.path.isfile(path):
            return BackupResult(success=False, message=f"快照不存在: {path}")
        import tarfile
        try:
            with tarfile.open(path, "r:*") as tf:
                names = tf.getnames()
            has_sqlite = any(n.endswith("chroma.sqlite3") or n == "chroma.sqlite3"
                             for n in names)
            has_hnsw = any("data_level0.bin" in n or "head_metadata.bin" in n
                           or n.endswith(".bin") for n in names)
            if not has_sqlite:
                return BackupResult(success=False,
                                    message="快照内未找到 chroma.sqlite3（目录结构异常）")
            return BackupResult(success=True,
                                message=f"Chroma 快照校验通过：sqlite 元数据在位，"
                                        f"HNSW 二进制{'在位' if has_hnsw else '未检出（可能为空库）'}，"
                                        f"共 {len(names)} 个成员")
        except Exception as e:
            return BackupResult(success=False, message=f"快照不是有效归档: {str(e)[:200]}")
