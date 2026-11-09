# -*- coding: utf-8 -*-
"""对象存储桶级备份与恢复引擎（MinIO / 阿里云 OSS / 腾讯云 COS / S3 兼容）。

为什么是这一套机制（对齐 AWS Backup / Veeam / Rubrik 的业界共识）：

1. **元索引优先**：产物不是"把桶里的数据抄一份"，而是
   ``manifest.json``（对象级索引：key / size / ETag / LastModified /
   VersionId / StorageClass / SHA256）+ 对象本体。有索引才谈得上
   **对象级细粒度恢复**、快速缺失检测与审计。
2. **永远增量**：每次备份与上一次对比后只传输 ETag / LastModified / Size
   发生变化的对象；每个包带 ``parent`` 指针，恢复按 full → inc → … 回放，
   删除事件同样被记录并回放。
3. **版本控制感知**：可选 ``os_include_versions=true`` 捕获全部历史版本，
   与对象存储 Versioning 配合形成时间点保护（AWS Backup 保护 S3 亦要求
   先开启版本控制）。以删除标记为主体的 key 视为已删除，不纳入备份。
4. **并行 workers + 流式落盘**：并发拉取、下载直接写本地文件，内存占用与
   对象大小解耦（本机曾因整包读进内存触发 MemoryError，这里从设计上规避）。
5. **不动被保护对象**：协议会话由平台侧发起，桶侧零安装、零改造。

失败一律如实：任何对象传输失败且未显式允许部分成功时，本次备份/恢复
判定为 FAILED 并列出失败对象，绝不把残缺数据包装成成功。
"""
from __future__ import annotations

import concurrent.futures as futures
import contextvars
import hashlib
import json
import os
import shutil
import tarfile
import time

from core import cancel as cancel_mod
from core.engines.base import (BackupEngine, BackupResult, BackupStatus,
                               BackupType)
from core.objectstore import client as osc
from core.objectstore import providers as osp
from core.objectstore import resume as osr

MANIFEST_VERSION = 1
_MANIFEST = osp.MANIFEST_NAME


def _obj_size(obj: dict) -> int:
    """对象大小取值：0 字节对象也要如实为 0（不能写成 `size or -1`）。"""
    val = obj.get("size")
    return int(val) if val is not None else -2


def _human(n: int) -> str:
    try:
        from core import db
        return db.human_size(n)
    except Exception:
        return "%d B" % int(n or 0)


class ObjectStorageEngine(BackupEngine):
    db_type = "object_storage"
    display_name = "对象存储"
    required_clients = []          # 标准库即可，无需任何外部客户端
    adapter_tier = "peripheral_api"

    def __init__(self, task: dict, storage_root: str, logger=None):
        super().__init__(task, storage_root, logger)
        self.cfg = osp.task_cfg(task)
        self._cli = None

    # ------------------------------------------------------------------ 基础
    def _client(self) -> osc.ObjectStoreClient:
        if self._cli is None:
            self._cli = osp.build_client(self.cfg)
        return self._cli

    def _check_cfg(self, need_bucket: bool = True) -> str:
        """返回第一条缺失的配置描述；全部就绪返回空串。"""
        if not self.cfg.get("endpoint"):
            return "缺少端点 endpoint（任务字段：主机）"
        if not self.cfg.get("access_key") or not self.cfg.get("secret_key"):
            return "缺少 AccessKey / SecretKey（任务字段：用户名 / 密码）"
        if need_bucket and not self.cfg.get("bucket"):
            return "缺少要保护的桶名（任务字段：数据库名）"
        return ""

    def list_databases(self) -> list:
        """可选实现：对对象存储而言"库名"即桶名。"""
        err = self._check_cfg(need_bucket=False)
        if err:
            return []
        try:
            return [b["name"] for b in self._client().list_buckets()]
        except Exception as e:
            self.logger.warning("[%s] 列举桶失败: %s", self.task_name, e)
            return []

    def check_client(self, *a, **kw):
        """客户端检查：本通道零外部依赖，只要求 endpoint + 密钥齐备。"""
        err = self._check_cfg(need_bucket=False)
        if err:
            return False, err
        return True, "无需外部客户端（平台内置 S3 协议实现）"

    # ------------------------------------------------------------------ 备份
    def backup(self, backup_type: BackupType = BackupType.FULL) -> BackupResult:
        started = time.time()
        err = self._check_cfg()
        if err:
            return BackupResult(False, BackupStatus.FAILED, message=err)
        raw_bt = backup_type.value if isinstance(backup_type, BackupType) \
            else str(backup_type or "")
        bt = BackupType.INCREMENTAL if raw_bt == "incremental" else BackupType.FULL
        try:
            cli = self._client()
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="无法建立对象存储连接: %s" % e)

        out_dir = self._output_dir()
        snap_prev = self._load_snapshot()
        resume_on = bool(self.cfg.get("resume", True))
        ttl_h = float(self.cfg.get("resume_ttl_hours") or 72)
        max_retries = int(self.cfg.get("resume_max_retries") or 3)
        stage = (osr.stage_path(out_dir) if resume_on
                 else os.path.join(out_dir, ".stage_%s" % self._timestamp()))
        if not resume_on and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(os.path.join(stage, osp.OBJECTS_DIR), exist_ok=True)

        try:
            current, _tombstones = self._scan(cli)
            plan, deleted = self._diff(snap_prev, current, bt)
        except osc.ObjectStoreError as e:
            if not resume_on:
                shutil.rmtree(stage, ignore_errors=True)
            return BackupResult(False, BackupStatus.FAILED,
                                message="列举对象失败: %s" % e,
                                stderr=str(e.detail or "")[:500])

        bt_str = "incremental" if bt == BackupType.INCREMENTAL else "full"
        scope = osr.scope_digest(self.task_id, self.task_name, self.cfg, bt_str)
        cp, done_map = {}, {}
        if resume_on:
            done_map, cp = osr.load_valid(stage, scope, ttl_h)
            if cp:
                if int(cp.get("plan_total") or 0) != len(plan):
                    cp["plan_total"] = len(plan)
                    osr.save_meta(stage, cp)
            else:
                # 范围变化 / TTL 过期 / 首次运行：旧字节不可信，整体重建
                osr.clear(stage)
                os.makedirs(os.path.join(stage, osp.OBJECTS_DIR), exist_ok=True)
                cp = osr.new_checkpoint(self.task_id, self.task_name,
                                        self.cfg, bt_str, len(plan))
                osr.save_meta(stage, cp)   # 先落断点，首次运行中断也能续
                done_map = {}

        allow_partial = bool((self.extra or {}).get("os_allow_partial", False))
        workers = max(1, int(self.cfg.get("workers") or 8))
        failures: list[str] = []
        entries_by_unit: dict[str, dict] = {}
        pending: list[str] = []
        pulled = reused_n = reused_bytes = 0

        for unit in plan:
            meta = current[unit]
            entry = osr.reusable_entry(stage, done_map.get(unit), meta) \
                if resume_on else None
            if entry:
                entries_by_unit[unit] = entry
                pulled += int(entry["size"])
                reused_n += 1
                reused_bytes += int(entry["size"])
            else:
                # 源已变化 / 文件损坏：只重下这一个，其余断点继续复用
                done_map.pop(unit, None)
                pending.append(unit)

        def work(unit: str):
            cancel_mod.check()
            meta = current[unit]
            rel = osp.key_to_relpath(meta["key"], meta.get("version_id") or "")
            dest = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

            def on_chunk(_total):
                cancel_mod.check()

            try:
                got = cli.download_to_file(
                    self.cfg["bucket"], meta["key"], dest,
                    meta.get("version_id") or "", resume=resume_on,
                    expected_size=(int(meta["size"])
                                   if meta.get("size") is not None else None),
                    max_retries=max_retries, on_chunk=on_chunk)
            except cancel_mod.CancelledError:
                raise
            except Exception as e:
                return None, "%s: %s" % (meta["key"], str(e)[:200])
            real = os.path.getsize(dest)
            # 0 字节空对象不能写成 `size or -1`
            expect = int(meta["size"]) if meta.get("size") is not None else -1
            if real != expect or real != got:
                return None, "%s: 落盘大小不一致（源 %s / 盘 %d）" % (
                    meta["key"], meta.get("size"), real)
            return {
                "key": meta["key"],
                "version_id": meta.get("version_id") or "",
                "size": real,
                "etag": meta.get("etag") or "",
                "last_modified": meta.get("last_modified") or "",
                "storage_class": meta.get("storage_class") or "",
                "sha256": osc.ObjectStoreClient.sha256_file(dest),
                "path": rel,
                # 版本回放顺序判据：is_latest + seq（越小越新）
                "is_latest": bool(meta.get("is_latest")),
                "seq": int(meta.get("seq") or 0),
            }, None

        if pending:
            pool = futures.ThreadPoolExecutor(max_workers=workers)
            try:
                # ContextVar 不随线程池传播：每个 future 用主线程 context 的
                # 独立副本执行，子线程里 cancel.check() 才能看到「停止」指令。
                def _runner(ctx, unit):
                    return ctx.run(work, unit)

                futs = [(u, pool.submit(_runner, contextvars.copy_context(), u))
                        for u in pending]
                for unit, fut in futs:
                    cancel_mod.check()
                    entry, error = fut.result()
                    if error:
                        failures.append(error)
                    elif entry:
                        entries_by_unit[unit] = entry
                        pulled += int(entry["size"])
                        if resume_on:
                            osr.append_done(stage, unit, entry)
                if resume_on:
                    osr.save_meta(stage, cp)
            except cancel_mod.CancelledError:
                pool.shutdown(wait=False, cancel_futures=True)
                raise                # scheduler 标记 cancelled，stage 与断点保留
            else:
                pool.shutdown(wait=True)

        entries = [entries_by_unit[u] for u in sorted(plan)
                   if u in entries_by_unit]

        if failures and not allow_partial:
            # 失败不再删 stage：已成功对象与 .part 字节留到重跑续传
            if not resume_on:
                shutil.rmtree(stage, ignore_errors=True)
            return BackupResult(
                False, BackupStatus.FAILED,
                message="备份失败：%d 个对象传输失败，首条：%s"
                        "（已完成 %d 个对象的断点已保留，重跑可续传）"
                        % (len(failures), failures[0], len(entries_by_unit)),
                stderr="\n".join(failures[:20]),
                duration_sec=round(time.time() - started, 2))

        parent_name = (self._load_snapshot().get("source_backup") or "")
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "schema": "aidbm-object-backup",
            "provider": self.cfg.get("provider"),
            "endpoint": self.cfg.get("endpoint"),
            "region": self.cfg.get("region"),
            "bucket": self.cfg.get("bucket"),
            "prefix_filter": self.cfg.get("prefix") or "",
            "include_versions": bool(self.cfg.get("include_versions")),
            "backup_type": bt_str,
            "parent": parent_name if bt == BackupType.INCREMENTAL else None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "task_id": self.task_id,
            "task_name": self.task_name,
            "object_count": len(entries),
            "object_bytes": pulled,
            "deleted_count": len(deleted),
            "deleted": sorted(deleted),
            "failed_count": len(failures),
            "failed": [f.split(":")[0] for f in failures][:200],
            "resume": {"enabled": resume_on, "reused_objects": reused_n,
                       "reused_bytes": reused_bytes},
            "objects": entries,
        }
        with open(os.path.join(stage, _MANIFEST), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)

        tar_path = self._new_dump_path(bt, ".tar.gz")
        try:
            self._pack_stage(stage, tar_path)
        except Exception as e:
            if not resume_on:
                shutil.rmtree(stage, ignore_errors=True)
            return BackupResult(False, BackupStatus.FAILED,
                                message="打包产物失败: %s" % e)

        osr.clear(stage) if resume_on else shutil.rmtree(stage, ignore_errors=True)
        if not failures:
            self._save_snapshot(current, tar_path)

        size = os.path.getsize(tar_path)
        checksum = osc.ObjectStoreClient.sha256_file(tar_path)
        kind = "增量" if bt == BackupType.INCREMENTAL else "全量"
        deleted_note = "，删除 %d 个" % len(deleted) if deleted else ""
        failure_note = ("，失败 %d 个（已允许部分成功）" % len(failures)) if failures else ""
        resume_note = ("，断点续传复用 %d 个对象 / %s"
                       % (reused_n, _human(reused_bytes))) if reused_n else ""
        msg = "%s备份成功：%d 个对象 / %s%s%s%s → %s" % (
            kind, len(entries), _human(pulled), deleted_note, failure_note,
            resume_note, _human(size))
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS, backup_path=tar_path,
            size_bytes=size, original_size_bytes=pulled, compress_algo="gzip",
            compress_ratio=(round(size / pulled, 3) if pulled else 0.0),
            duration_sec=round(time.time() - started, 2), checksum=checksum,
            message=msg, stderr=("\n".join(failures[:20]) if failures else ""),
            detail_log="%s://%s/%s" % (self.cfg.get("provider"),
                                       self.cfg.get("endpoint"),
                                       self.cfg.get("bucket")))

    @staticmethod
    def _pack_stage(stage: str, tar_path: str) -> None:
        """打包 stage，但排除断点元数据与 .aidbm_part 残件（不进产物）。

        断点文件留在 stage 里供打包失败后重跑续传，因此只能在 tar.add 的
        filter 里剔除，不能先删后打包。
        """
        skip_top = {osr.CHECKPOINT_NAME, osr.CHECKPOINT_NAME + ".tmp",
                    osr.DONE_NAME, _MANIFEST + ".tmp"}

        def _filt(info):
            if info.name.split("/")[0] in skip_top:
                return None
            if info.name.endswith(osr.PART_SUFFIX):
                return None
            return info

        with tarfile.open(tar_path, "w:gz") as tar:
            for name in sorted(os.listdir(stage)):
                if name in skip_top:
                    continue
                tar.add(os.path.join(stage, name), arcname=name, filter=_filt)

    # ------------------------------------------------------- 扫描 / 差分
    def _scan(self, cli) -> tuple[dict, set]:
        """列举当前对象视图：键为 ``key``（版本模式下为 ``key@versionId``）。"""
        prefix = self.cfg.get("prefix") or ""
        bucket = self.cfg["bucket"]
        views: dict[str, dict] = {}
        if self.cfg.get("include_versions"):
            latest_marker: dict[str, bool] = {}
            # ``seq`` = ListObjectVersions 的列举序号。S3/MinIO 语义是**最新版本
            # 优先返回**，所以同一 key 内 seq 越小＝版本越新。恢复回放顺序不能只
            # 看 last_modified：它精度只到秒，同一秒内连续 PUT 的三个版本时间戳
            # 完全相同，排序退化成"包内顺序"，而包内顺序又被 _diff 的 sorted()
            # 打乱 → 实测出现过 v1 最后写入、恢复后读到 v1 的错版（E2E S6.3）。
            for _seq, item in enumerate(cli.iter_object_versions(bucket, prefix)):
                item["seq"] = _seq
                if item.get("delete_marker"):
                    if item.get("is_latest"):
                        latest_marker[item["key"]] = True
                    continue
                if latest_marker.get(item["key"]):
                    continue        # 最新状态是删除标记 ⇒ 该 key 视为已删除
                views["%s@%s" % (item["key"], item["version_id"] or "null")] = item
            return views, {k for k, v in latest_marker.items() if v}
        for item in cli.iter_objects(bucket, prefix):
            views[item["key"]] = item
        return views, set()

    @staticmethod
    def _diff(prev: dict, current: dict, bt: BackupType) -> tuple[list, list]:
        """返回 ``(需传输的对象键列表, 相对上次已删除的 key 列表)``。"""
        prev_map = prev.get("objects") or {}
        if bt != BackupType.INCREMENTAL or not prev_map:
            return sorted(current.keys()), []
        plan = []
        for unit, now in current.items():
            old = prev_map.get(unit)
            if old is None:
                plan.append(unit)
                continue
            if (str(old.get("etag")) != str(now.get("etag"))
                    or _obj_size(old) != _obj_size(now)
                    or str(old.get("last_modified")) != str(now.get("last_modified"))
                    or str(old.get("version_id") or "")
                    != str(now.get("version_id") or "")):
                plan.append(unit)
        alive = {now["key"] for now in current.values()}
        deleted = sorted({k.split("@")[0] for k in prev_map} - alive)
        return sorted(plan), deleted

    def _snapshot_file(self) -> str:
        return osp.snapshot_path(self._output_dir())

    def _load_snapshot(self) -> dict:
        path = self._snapshot_file()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_snapshot(self, current: dict, tar_path: str) -> None:
        """保存增量基准：``key[@version]`` → 最小指纹集合。"""
        snap = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source_backup": os.path.basename(tar_path or ""),
            "objects": {
                (m["key"] + ("@" + m["version_id"] if m.get("version_id") else "")): {
                    "key": m["key"], "version_id": m.get("version_id") or "",
                    "size": m.get("size"), "etag": m.get("etag"),
                    "last_modified": m.get("last_modified"),
                } for m in current.values()
            },
        }
        try:
            tmp = self._snapshot_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, self._snapshot_file())
        except Exception as e:
            self.logger.warning("[%s] 保存增量基准失败: %s", self.task_name, e)

    # ------------------------------------------------------------------ 恢复
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        started = time.time()
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(False, BackupStatus.FAILED,
                                message="备份产物不存在: %s" % backup_path)
        cfg = self.cfg
        target_bucket = (kwargs.get("target_bucket") or cfg.get("restore_bucket")
                         or cfg.get("bucket"))
        prefix_opt = kwargs.get("target_prefix")
        target_prefix = cfg.get("restore_prefix") if prefix_opt is None else prefix_opt
        overwrite = str(kwargs.get("overwrite") or cfg.get("restore_overwrite")
                        or "if_newer")
        workers = max(1, int(kwargs.get("workers") or cfg.get("workers") or 8))
        apply_deleted = bool(kwargs.get("apply_deleted",
                                        cfg.get("restore_apply_deleted")))
        # 是否自动创建目标桶：恢复弹窗开关优先，缺省回退任务级 os_restore_create_bucket
        create_bucket = bool(kwargs.get("create_bucket",
                                        cfg.get("restore_create_bucket")))
        # 对象级细粒度恢复：传 keys 时只回放指定对象（业界称颗粒级恢复）
        only_keys = kwargs.get("restore_keys") or kwargs.get("keys") or None
        only = set(only_keys) if only_keys else None
        if not target_bucket:
            return BackupResult(False, BackupStatus.FAILED,
                                message="未指定恢复目标桶")
        try:
            cli = self._client()
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="无法建立对象存储连接: %s" % e)

        if not cli.bucket_exists(target_bucket):
            if not create_bucket:
                return BackupResult(False, BackupStatus.FAILED,
                                    message="目标桶不存在且未允许自动创建: %s"
                                            % target_bucket)
            try:
                cli.create_bucket(target_bucket)
            except osc.ObjectStoreError as e:
                return BackupResult(False, BackupStatus.FAILED,
                                    message="创建目标桶失败: %s" % e)

        try:
            chain = self._restore_chain(backup_path)
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="解析恢复链失败: %s" % e)

        tmp_root = os.path.join(self.storage_root or ".",
                                ".objrestore_%s_%s" % (self.task_id,
                                                       time.strftime("%H%M%S")))
        written = skipped = failed = deleted_ok = 0
        failures: list[str] = []
        try:
            for level_path in chain:
                extract = os.path.join(tmp_root, os.path.basename(level_path))
                self._untar_to_dir(level_path, extract)
                man_path = os.path.join(extract, _MANIFEST)
                if not os.path.exists(man_path):
                    raise osc.ObjectStoreError("备份包缺少 manifest.json: %s"
                                               % level_path)
                with open(man_path, encoding="utf-8") as f:
                    man = json.load(f)

                tasks = []
                for obj in man.get("objects") or []:
                    if only is not None and obj.get("key") not in only:
                        continue
                    src = os.path.join(extract, obj.get("path") or "")
                    if not os.path.exists(src):
                        src = os.path.join(extract, osp.key_to_relpath(
                            obj["key"], obj.get("version_id") or ""))
                    if not os.path.exists(src):
                        failures.append("%s: 包内缺少对象数据" % obj["key"])
                        failed += 1
                        continue
                    key = obj["key"]
                    if target_prefix:
                        key = str(target_prefix).rstrip("/") + "/" + key
                    tasks.append((obj, src, key))

                def push_group(items):
                    """同一 key 的多版本按「旧 → 新」串行回放，保证最终版本是最新的。

                    排序判据（依次）：
                      1. last_modified 升序；
                      2. 时间戳相同时（精度只到秒，同秒内多次 PUT 完全一样），
                         用备份时记录的列举序号 seq：**seq 越小版本越新**，
                         故取 -seq 让旧版本排前面；
                      3. is_latest 兜底排最后。

                    并发 PUT 同一 key 会让服务端按到达顺序生成版本，最终版本随机，
                    因此同 key 的多版本必须串行回放（见下方 groups 分组）。
                    """
                    ordered = sorted(
                        items,
                        key=lambda t: (str(t[0].get("last_modified") or ""),
                                       -int(t[0].get("seq") or 0),
                                       bool(t[0].get("is_latest"))))
                    return [push(it) for it in ordered]

                def push(item):
                    obj, src, key = item
                    if overwrite != "always":
                        try:
                            head = cli.head_object(target_bucket, key)
                            if overwrite == "never":
                                return "skip", key
                            head_size = int(head["size"]) if head.get("size") is not None else -1
                            if overwrite == "if_newer" and head_size == _obj_size(obj) \
                                    and obj.get("etag") and \
                                    str(head.get("etag")) == str(obj.get("etag")):
                                return "skip", key
                        except osc.ObjectStoreError:
                            pass
                    try:
                        cli.upload_file(target_bucket, key, src)
                        return "ok", key
                    except Exception as e:
                        return "fail", "%s: %s" % (key, str(e)[:200])

                # 同一 key 的多个版本必须按时间顺序**串行**回放：并发 PUT 到同一 key
                # 时，服务端按到达顺序生成版本，最终版本会随机（实测出现过 v2 盖过 v3）。
                groups: dict = {}
                for item in tasks:
                    groups.setdefault(item[2], []).append(item)
                singles = [g[0] for g in groups.values() if len(g) == 1]
                multis = [g for g in groups.values() if len(g) > 1]

                if singles or multis:
                    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                        futs = [pool.submit(push, it) for it in singles]
                        futs += [pool.submit(push_group, items) for items in multis]
                        for fut in futs:
                            try:
                                res = fut.result()
                            except Exception as e:
                                failed += 1
                                failures.append(str(e)[:160])
                                continue
                            if isinstance(res, tuple):
                                res = [res]
                            for state, key in res:
                                if state == "ok":
                                    written += 1
                                elif state == "skip":
                                    skipped += 1
                                else:
                                    failed += 1
                                    failures.append(key)

                if apply_deleted:
                    for key in man.get("deleted") or []:
                        try:
                            cli.delete_object(target_bucket, key)
                            deleted_ok += 1
                        except osc.ObjectStoreError as e:
                            failures.append("删除 %s 失败: %s" % (key, e))
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="恢复失败: %s" % e,
                                stderr="\n".join(failures[:20]))
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        msg = ("恢复完成：写入 %d 个、跳过 %d 个（目标已存在且一致）、失败 %d 个、"
               "回放删除 %d 个；链路 %d 层 → %s" % (
                   written, skipped, failed, deleted_ok, len(chain), target_bucket))
        return BackupResult(
            success=(failed == 0),
            status=BackupStatus.SUCCESS if failed == 0 else BackupStatus.FAILED,
            backup_path=backup_path, size_bytes=written,
            duration_sec=round(time.time() - started, 2), message=msg,
            stderr=("\n".join(failures[:20]) if failures else ""),
            target_ref=target_bucket,
            detail_log="restore_prefix=%s, overwrite=%s, layers=%d" % (
                target_prefix or "-", overwrite, len(chain)))

    def _restore_chain(self, backup_path: str) -> list[str]:
        """按 parent 指针回溯出 full → … → 目标增量 的回放顺序。"""
        out, cur, seen = [], backup_path, set()
        while cur and os.path.exists(cur) and cur not in seen:
            seen.add(cur)
            out.append(cur)
            parent = None
            try:
                with tarfile.open(cur, "r:*") as tar:
                    for m in tar.getmembers():
                        if os.path.basename(m.name) == _MANIFEST:
                            man = json.loads(
                                tar.extractfile(m).read().decode("utf-8"))
                            parent = man.get("parent")
                            break
            except Exception:
                parent = None
            if not parent:
                break
            nxt = os.path.join(os.path.dirname(cur), os.path.basename(parent))
            if not os.path.exists(nxt):
                self.logger.warning("[%s] 增量链的父包缺失: %s",
                                    self.task_name, parent)
                break
            cur = nxt
        out.reverse()
        return out

    # ------------------------------------------------------------------ 校验
    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """深度校验：打开备份包逐项核对索引与数据（而非只看文件存在与否）。

        检查：① 包可正常打开；② manifest 存在且版本受支持；③ 索引里的对象
        在包内确实存在且大小一致；④ 抽样（默认 20 个，总预算 200MB）做
        SHA256 比对。任一项不符即判定失败并给出具体对象。
        """
        options = options or {}
        path = record.get("backup_path") or record.get("output_path") or ""
        if not path or not os.path.exists(path):
            return BackupResult(False, BackupStatus.FAILED,
                                message="备份产物不存在: %s" % path)
        sample = int(options.get("sample") or 20)
        missing, bad_size, bad_hash = [], [], []
        try:
            with tarfile.open(path, "r:*") as tar:
                names = {m.name: m for m in tar.getmembers()}
                man_member = next((v for k, v in names.items()
                                   if os.path.basename(k) == _MANIFEST), None)
                if man_member is None:
                    return BackupResult(False, BackupStatus.FAILED,
                                        message="备份包缺少 manifest.json，无法校验")
                man = json.loads(tar.extractfile(man_member).read().decode("utf-8"))
                objects = man.get("objects") or []
                checked, budget = 0, 200 * 1024 * 1024
                for obj in objects:
                    rel = obj.get("path") or osp.key_to_relpath(
                        obj["key"], obj.get("version_id") or "")
                    member = names.get(rel) or names.get("./" + rel)
                    if member is None:
                        missing.append(obj.get("key") or rel)
                        continue
                    if int(member.size) != _obj_size(obj):
                        bad_size.append(obj.get("key") or rel)
                        continue
                    if checked < sample and budget > int(member.size):
                        h = hashlib.sha256()
                        fobj = tar.extractfile(member)
                        while True:
                            chunk = fobj.read(4 << 20)
                            if not chunk:
                                break
                            h.update(chunk)
                        checked += 1
                        budget -= int(member.size)
                        if obj.get("sha256") and h.hexdigest() != obj["sha256"]:
                            bad_hash.append(obj.get("key") or rel)
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="校验失败（包损坏或不可解析）: %s" % e)

        if missing or bad_size or bad_hash:
            parts = []
            if missing:
                parts.append("包内缺失 %d 个（如 %s）" % (len(missing), missing[0]))
            if bad_size:
                parts.append("大小不符 %d 个（如 %s）" % (len(bad_size), bad_size[0]))
            if bad_hash:
                parts.append("SHA256 不符 %d 个（如 %s）" % (len(bad_hash), bad_hash[0]))
            return BackupResult(False, BackupStatus.FAILED,
                                message="对象存储备份包校验不通过：" + "；".join(parts))
        return BackupResult(
            True, BackupStatus.SUCCESS, backup_path=path,
            verified=True,
            message="校验通过：索引 %d 个对象齐全、大小一致，抽样 %d 个 SHA256 比对通过"
                    % (int(man.get("object_count") or 0), checked))
