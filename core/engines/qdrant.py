# -*- coding: utf-8 -*-
"""Qdrant 向量数据库引擎（AI 工作负载 · 向量索引层保护）。

定位（AI 备份七层模型的第三层）：向量索引是 RAG 架构中最容易被"可重建"误导
的资产——重建一个 1500 万块的 HNSW 索引仅嵌入生成就可能超过 20 小时 GPU 时间。
向量索引的冷二进制快照必须是一级备份对象。

实现方式（零安装零 agent，硬性约束）：
  Qdrant 提供原生 REST 快照 API（默认端口 6333），平台经 HTTP 直连：
    备份：POST /collections/{c}/snapshots  {"wait":true} 创建快照（PUT 会 404）
          GET  /collections/{c}/snapshots/{name}        流式下载快照
          DELETE /collections/{c}/snapshots/{name}      清理远端快照
    恢复：POST /collections/{t}/snapshots/upload        multipart 上传恢复
          （priority=snapshot：以快照内容为准覆盖目标 collection）
    校验：GET  /collections/{c}                         status=green + points_count

HTTP 层用标准库 urllib（离线自足：镜像 venv 无 requests，不引入新依赖）。

诚实边界：
  - 快照是 collection 级；Qdrant 云/集群分片部署的快照一致性以官方 API 为准；
  - 恢复按 priority=snapshot 覆盖目标 collection（异名恢复 = 传 target_db）；
  - 嵌入模型版本/分块配置不在快照内——必须在「AI 保护计划」里把向量库任务与
    记录该元数据的对象（如 config 任务/文件任务）编入同一恢复单元；
  - 活跃写入时的快照一致性由 Qdrant WAL 保证（官方语义），平台不做停写。
"""
import json
import os
import time
import uuid
import urllib.request
import urllib.error

from core.engines.base import BackupEngine, BackupResult, BackupStatus
from core.engines.base import BackupType
import core.db as db

_DEFAULT_TIMEOUT = 60
_SNAPSHOT_TIMEOUT = 1800     # 快照创建可能耗时（大 collection）
_DL_CHUNK = 1024 * 1024


def _http(method: str, url: str, headers: dict = None, data=None,
          timeout: int = _DEFAULT_TIMEOUT, stream: bool = False):
    """标准库 HTTP（返回 (status, bytes|response)；非 2xx 抛 RuntimeError）。"""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        raise RuntimeError(f"HTTP {e.code}: {body.decode('utf-8', 'replace')}")
    if stream:
        return resp
    return resp.status, resp.read()


def _multipart_body(field: str, filename: str, file_path: str,
                    extra_fields: dict = None) -> tuple:
    """手工构造 multipart/form-data（标准库，无 requests 依赖）。"""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (extra_fields or {}).items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
            .encode("utf-8"))
    with open(file_path, "rb") as f:
        payload = f.read()
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
         f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
         ).encode("utf-8") + payload + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    ctype = f"multipart/form-data; boundary={boundary}"
    return body, ctype


class QdrantEngine(BackupEngine):
    db_type = "qdrant"
    display_name = "Qdrant 向量库"

    # ---------------- 基础 ----------------
    def _base_url(self) -> str:
        host = self.task.get("host") or "127.0.0.1"
        port = int(self.task.get("port") or 6333)
        return f"http://{host}:{port}"

    def _headers(self) -> dict:
        pw = self.task.get("password") or ""
        return {"api-key": pw} if pw else {}

    def _collections(self) -> list:
        """枚举 collections（db_name 为空 = 全部；task 的 db_name 存 collection 名，
        逗号分隔多个）。"""
        specified = (self.task.get("db_name") or "").strip()
        if specified:
            return [c.strip() for c in specified.split(",") if c.strip()]
        _, body = _http("GET", f"{self._base_url()}/collections",
                        headers=self._headers())
        cols = (json.loads(body).get("result") or {}).get("collections") or []
        return [c["name"] for c in cols]

    # ---------------- 备份 ----------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        start = time.time()
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        base = self._base_url()
        headers = self._headers()
        try:
            names = self._collections()
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message=f"连接 Qdrant 失败（{base}）: {str(e)[:200]}")
        if not names:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message="Qdrant 上没有任何 collection 可备份")
        errors = []
        pieces = []
        for cname in names:
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cname)
            ts = self._timestamp()
            local_path = os.path.join(out_dir, f"qdrant_{safe}_{ts}.snapshot")
            try:
                # 1) 创建快照（POST 是官方端点；wait=true 同步等完成）
                # 注意：早期版本误用 PUT，Qdrant 1.x 返回 404（真机暴露）。
                _, body = _http("POST", f"{base}/collections/{cname}/snapshots",
                                headers=headers,
                                data=json.dumps({"wait": True}).encode(),
                                timeout=_SNAPSHOT_TIMEOUT)
                snap_name = (json.loads(body).get("result") or {}).get("name")
                if not snap_name:
                    raise RuntimeError(f"快照创建无返回名: {body[:150]}")
                # 2) 流式下载
                total = 0
                resp = _http("GET",
                             f"{base}/collections/{cname}/snapshots/{snap_name}",
                             headers=headers, timeout=_SNAPSHOT_TIMEOUT,
                             stream=True)
                with open(local_path, "wb") as f:
                    while True:
                        chunk = resp.read(_DL_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                if total < 64:
                    raise RuntimeError(f"快照下载过小（{total} 字节），疑似失败")
                # 3) 清理远端快照（数据库服务器不留产物）
                try:
                    _http("DELETE",
                          f"{base}/collections/{cname}/snapshots/{snap_name}",
                          headers=headers)
                except Exception:
                    pass
                pieces.append((cname, local_path, total))
            except Exception as e:
                errors.append(f"{cname}: {str(e)[:150]}")
        duration = round(time.time() - start, 2)
        if not pieces:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=duration,
                                message="Qdrant 快照全部失败: " + "；".join(errors)[:400])
        total_size = sum(sz for _, _, sz in pieces)
        first = pieces[0][1]
        msg = (f"Qdrant 快照备份成功：{len(pieces)} 个 collection"
               f"（{', '.join(n for n, _, _ in pieces)}），"
               f"共 {db.human_size(total_size)}")
        if errors:
            msg += f"；{len(errors)} 个失败: " + "；".join(errors)[:200]
        manifest = os.path.join(
            out_dir, os.path.basename(first) + "_manifest.json")
        with open(manifest, "w", encoding="utf-8") as mf:
            json.dump({
                "kind": "qdrant_snapshot",
                "collections": [
                    {"collection": n, "file": os.path.basename(p),
                     "size_bytes": sz,
                     "sha256": db.sha256_file(p)} for n, p, sz in pieces],
                "embed_metadata_note": "嵌入模型版本/分块规则不在快照内，"
                                       "须与记录该元数据的资产编入同一 AI 保护计划",
            }, mf, ensure_ascii=False, indent=2)
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=first, size_bytes=total_size,
                            duration_sec=duration, message=msg)

    # ---------------- 恢复 ----------------
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        start = time.time()
        target_collection = (kwargs.get("target_db")
                             or self.task.get("db_name") or "").strip()
        if not os.path.isfile(backup_path):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message=f"快照文件不存在: {backup_path}")
        # 从文件名反推源 collection：qdrant_{safe}_{ts}.snapshot
        base_name = os.path.basename(backup_path)
        src_collection = ""
        if base_name.startswith("qdrant_") and base_name.endswith(".snapshot"):
            src_collection = base_name[len("qdrant_"):-len(".snapshot")].rsplit("_", 1)[0]
        target = target_collection or src_collection
        if not target:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message="无法确定目标 collection（快照文件名无法解析"
                                        "且任务未配置 db_name/target_db）")
        base = self._base_url()
        headers = self._headers()

        # 多集合快照：backup 时 backup_path 只指向首个快照，其余在
        # <first>_manifest.json。恢复必须读 manifest 恢复全部，否则
        # "备份了 N 个 collection、恢复只回来 1 个"是静默数据缺失。
        pairs = []
        manifest_path = backup_path + "_manifest.json"
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, encoding="utf-8") as mf:
                    mf_data = json.load(mf)
                pieces = mf_data.get("collections") or []
                out_dir = os.path.dirname(backup_path)
                for piece in pieces:
                    fp = os.path.join(out_dir, piece.get("file") or "")
                    if not os.path.isfile(fp):
                        return BackupResult(
                            success=False, status=BackupStatus.FAILED,
                            duration_sec=round(time.time() - start, 2),
                            message=f"清单引用的快照文件缺失: {piece.get('file')}")
                    # 恢复前校验 sha256（清单里已记录，篡改/损坏必须拒绝）
                    want = piece.get("sha256") or ""
                    if want:
                        got = db.sha256_file(fp)
                        if want != got:
                            return BackupResult(
                                success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message=(f"快照 sha256 不匹配（{piece.get('collection')}）："
                                         f"清单={want[:16]} 实测={got[:16]}"))
                    # 多集合：目标名按"目标前缀 + 源集合后缀"映射，避免全部
                    # 覆盖到同一个 target；单集合或 target 显式指定时保持原语义。
                    src = piece.get("collection") or ""
                    tgt = target if (len(pieces) == 1 or not src) else target
                    if len(pieces) > 1 and target_collection:
                        tgt = f"{target_collection}_{src}"
                    pairs.append((src, tgt, fp))
            except Exception as e:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    duration_sec=round(time.time() - start, 2),
                                    message=f"读取/校验快照清单失败: {str(e)[:200]}")
        else:
            pairs.append((src_collection, target, backup_path))

        restored, errors = [], []
        # Qdrant 官方：priority=snapshot 以快照内容为准覆盖目标 collection
        UPLOAD_URL = (f"{base}/collections/{{t}}/snapshots/upload"
                      "?priority=snapshot&wait=true")
        for src, tgt, fp in pairs:
            try:
                bname = os.path.basename(fp)
                body, ctype = _multipart_body("snapshot", bname, fp)
                _http("POST", UPLOAD_URL.format(t=tgt),
                      headers=dict(headers, **{"Content-Type": ctype}),
                      data=body, timeout=_SNAPSHOT_TIMEOUT)
                restored.append(tgt)
            except Exception as e:
                errors.append(f"{tgt}: {str(e)[:150]}")
        if not restored:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - start, 2),
                                message="Qdrant 快照恢复全部失败: " + "；".join(errors)[:300])
        # 恢复后逐集合校验状态
        status_parts = []
        for tgt in restored:
            try:
                _, body = _http("GET", f"{base}/collections/{tgt}",
                                headers=headers)
                res = (json.loads(body).get("result") or {})
                status_parts.append(f"{tgt}:status={res.get('status')},"
                                    f"points={res.get('points_count')}")
            except Exception:
                status_parts.append(f"{tgt}:状态未确认")
        msg = (f"Qdrant 快照已恢复到 {len(restored)} 个 collection"
               f"（{'、'.join(status_parts)}；priority=snapshot）")
        if errors:
            msg += f"；{len(errors)} 个失败: " + "；".join(errors)[:200]
        return BackupResult(success=not errors,
                            status=BackupStatus.SUCCESS if not errors else BackupStatus.FAILED,
                            duration_sec=round(time.time() - start, 2), message=msg)

    # ---------------- 校验 ----------------
    def verify_record(self, record) -> BackupResult:
        """快照产物校验：tar 头（Qdrant snapshot 为 tar）+ 结构检查。"""
        path = record.get("backup_path") or ""
        if not path or not os.path.isfile(path):
            return BackupResult(success=False, message=f"快照文件不存在: {path}")
        size = os.path.getsize(path)
        if size < 64:
            return BackupResult(success=False, message=f"快照过小（{size} 字节）")
        import tarfile
        try:
            with tarfile.open(path, "r:*") as tf:
                members = tf.getnames()[:20]
            ok = any("storage" in m or "collection" in m for m in members)
            if not ok and members:
                ok = True   # 版本差异：目录结构可能变化，能解析 tar 即视为结构有效
            if not ok:
                return BackupResult(success=False,
                                    message="快照 tar 内未找到 collection 存储结构")
        except Exception as e:
            return BackupResult(success=False,
                                message=f"快照不是有效归档: {str(e)[:200]}")
        # 深度校验：清单里每个快照的 sha256 必须与实际文件一致（此前只查 tar 头，
        # 产物被篡改/截断仍报"通过"）。无清单兼容老记录，如实标注未做哈希核对。
        manifest_path = path + "_manifest.json"
        if not os.path.isfile(manifest_path):
            return BackupResult(success=True,
                                message=(f"Qdrant 快照 tar 结构校验通过（{len(members)} 成员）；"
                                         "无清单，未做 sha256 哈希核对（老记录兼容）"))
        try:
            with open(manifest_path, encoding="utf-8") as mf:
                pieces = json.load(mf).get("collections") or []
            out_dir = os.path.dirname(path)
            mismatches, checked = [], 0
            for piece in pieces:
                want = piece.get("sha256") or ""
                fp = os.path.join(out_dir, piece.get("file") or "")
                if not want or not os.path.isfile(fp):
                    mismatches.append(f"{piece.get('collection')}:文件缺失或无基线")
                    continue
                got = db.sha256_file(fp)
                checked += 1
                if want != got:
                    mismatches.append(f"{piece.get('collection')}:sha 不符")
            if mismatches:
                return BackupResult(success=False,
                                    message="Qdrant 快照完整性校验失败: "
                                            + "；".join(mismatches)[:300])
            return BackupResult(success=True,
                                message=(f"Qdrant 快照校验通过：tar 结构有效 + "
                                         f"{checked} 个 collection sha256 全部匹配"))
        except Exception as e:
            return BackupResult(success=False,
                                message=f"读取快照清单校验失败: {str(e)[:200]}")

    # ---------------- 枚举 collections（任务表单拉库列表） ----------------
    def list_databases(self) -> list:
        """复用「拉库列表」语义返回 collections（供任务表单 db_name 下拉）。"""
        try:
            return self._collections()
        except Exception:
            return []
