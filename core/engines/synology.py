# -*- coding: utf-8 -*-
"""群晖 DSM 全量 + 增量备份引擎（FileStation Web API 通道，DSM 侧零安装）。

产物语义（与 AIDBM 的文件级增量链一致，可逐字节核验）：

* 每个备份集是一个 ``synology_<full|inc>_<ts>.tar.gz``，内含
  ``manifest.json``（文件索引：DSM 绝对路径 / size / mtime / sha256，
  以及 parent 指针与 deleted 清单）和 ``files/<DSM 路径>`` 目录树。
* **全量**：拉取选定共享目录下全部文件；
* **增量**：与上一次成功清单按 ``size + mtime`` 差分，只拉新增/变化文件，
  删除的文件记入 ``deleted``，恢复时回放；
* **恢复**：按 ``full → inc1 → inc2 …`` 顺序解包并应用 deleted，
  最后对每个文件做 **sha256 硬核对**，不一致即失败，绝不静默接受残缺数据；
* **断点续传**：单文件用 HTTP Range（``DSMClient.download(resume=True)``），
  平台侧 stage 缓存跨轮复用，只有 size/mtime 变化才重下。

诚实边界（写进产物 manifest 与消息，不虚报）：

1. 这是**文件级**备份。SAN Manager 输出的是裸 LUN，平台不直接读裸块，
   不承诺块级增量 / CBT；iSCSI 场景请纳管挂载 LUN 的业务主机备份挂载点。
2. 恢复落在**平台侧目录**，推回 DSM 共享目录需要 FileStation.Upload 通道，
   本轮未实现（恢复消息明确提示）。
3. 不保存 DSM/Windows ACL、权限位、配额与硬链接语义；中文文件名按 UTF-8 保真。
4. 无上一次全量却请求增量时，**诚实回退为全量**并在消息里标注原因。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import shutil
import tarfile
import time

from core import cancel as cancel_mod
from core import db
from core.engines.base import BackupEngine, BackupResult, BackupStatus, BackupType
from core.synology import DSMError, devices as devmod
from core.synology.client import DSMClient

MANIFEST_NAME = "manifest.json"
STATE_NAME = ".synology_state.json"
STAGE_NAME = ".synology_stage"
FILES_DIR = "files"
MANIFEST_VERSION = 1


def _human(n: int) -> str:
    try:
        return db.human_size(int(n or 0))
    except Exception:
        return "%d B" % int(n or 0)


def _safe_rel(path: str) -> str:
    """把 DSM 绝对路径映射为 stage 内相对路径，拒绝穿越。"""
    p = posixpath.normpath(str(path or "")).lstrip("/")
    if not p or p == "." or p.startswith("..") or "/.." in p:
        raise ValueError("非法的 DSM 路径: %r" % path)
    return p


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_excluded(path: str, patterns: list) -> bool:
    """fnmatch 排除规则：既匹配文件名，也匹配 DSM 相对路径及其任意父目录。

    父目录匹配用于剪掉整棵子树（如 ``backup/@tmp``），避免出现
    「目录排除了但目录里的文件还是被备份」的假排除。
    """
    if not patterns:
        return False
    parts = [p for p in str(path).strip("/").split("/") if p]
    if not parts:
        return False
    base = parts[-1]
    for raw in patterns:
        pat = str(raw).strip().lstrip("/")
        if not pat:
            continue
        if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch("/".join(parts), pat):
            return True
        for i in range(1, len(parts)):
            if fnmatch.fnmatch("/".join(parts[:i]), pat):
                return True
    return False


class SynologyEngine(BackupEngine):
    db_type = "synology"
    display_name = "群晖 NAS（DSM）"
    required_clients = []            # 纯标准库，平台侧零插件
    adapter_tier = "peripheral_api"

    # ---------------------------------------------------------------- 配置
    def _cfg(self) -> dict:
        """凭据解析：优先引用纳管设备（集中改密），否则用任务自身字段。"""
        extra = self.extra
        dev_id = extra.get("synology_device_id")
        cfg = {}
        if dev_id:
            dev = devmod.get_device(int(dev_id), include_secret=True)
            if not dev:
                raise ValueError("引用的群晖设备 %s 不存在（可能已被删除）" % dev_id)
            cfg = devmod.client_config(dev)
            cfg["device_id"] = int(dev_id)
            cfg["device_name"] = dev.get("name") or ""
        else:
            cfg = {
                "host": self.task.get("host") or "",
                "port": int(self.task.get("port") or 5000),
                "username": self.task.get("username") or "",
                "password": self.task.get("password") or "",
                "secure": bool(extra.get("synology_secure")),
                "verify_ssl": bool(extra.get("synology_verify_ssl")),
                "timeout": int(extra.get("synology_timeout") or 15),
                "session_name": "AIDBM",
                "otp_code": "",       # 一次性验证码绝不持久化
                "device_id": 0,
                "device_name": "",
            }
        paths = extra.get("synology_paths") or []
        if isinstance(paths, str):
            paths = [p.strip() for p in paths.split(",") if p.strip()]
        if not paths:
            raw = str(self.task.get("db_name") or "").strip()
            paths = [p.strip() for p in raw.split(",") if p.strip()]
        paths = [p for p in paths if p.startswith("/") and ".." not in p.split("/")]
        cfg["paths"] = paths
        cfg["max_files"] = int(extra.get("synology_max_files") or 200000)
        cfg["resume"] = bool(extra.get("synology_resume", True))
        cfg["allow_partial"] = bool(extra.get("synology_allow_partial", False))
        raw_ex = extra.get("synology_excludes") or []
        if isinstance(raw_ex, str):
            raw_ex = [p.strip() for p in raw_ex.split(",") if p.strip()]
        cfg["excludes"] = [str(p).strip() for p in raw_ex if str(p).strip()]
        return cfg

    def _check_cfg(self, cfg: dict) -> str:
        if not cfg.get("host"):
            return "缺少 DSM 地址（请先纳管群晖设备或填写任务主机）"
        if not cfg.get("username") or not cfg.get("password"):
            return "缺少 DSM 账号或密码（密码集中存于纳管设备，SM4 加密）"
        if not cfg.get("paths"):
            return "缺少要保护的共享文件夹路径（synology_paths / 数据库名）"
        return ""

    def check_client(self, *a, **kw):
        try:
            err = self._check_cfg(self._cfg())
        except Exception as e:
            return False, str(e)
        return (False, err) if err else (True, "无需外部客户端（平台内置 DSM Web API 实现）")

    def preflight(self):
        ok, detail = self.check_client()
        if not ok:
            return False, detail
        # 前置真实探活：能力发现 + 登录，连不上不进入备份主链路
        try:
            cfg = self._cfg()
            cli = DSMClient(**{k: v for k, v in cfg.items()
                               if k in ("host", "port", "secure", "verify_ssl",
                                        "username", "password", "otp_code",
                                        "timeout", "session_name")})
            caps = cli.capabilities()
            cli.login()
            cli.logout()
            if not caps.get("filestation"):
                return False, "该 DSM 未暴露 SYNO.FileStation.List，文件级备份通道不可用"
            return True, ("DSM 能力发现通过（%d 个 API，FileStation 可用）"
                          % caps.get("api_count", 0))
        except DSMError as e:
            return False, "DSM 前置探活失败: %s" % e
        except Exception as e:
            return False, "DSM 前置探活异常: %s" % (str(e).strip() or type(e).__name__)

    # ---------------------------------------------------------------- 状态
    def _state_path(self) -> str:
        return os.path.join(self._output_dir(), STATE_NAME)

    def _load_state(self) -> dict:
        p = self._state_path()
        try:
            with open(p, "r", encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
        return {"snapshot": {}, "chain": [], "full": "", "stage_index": {},
                "updated_at": ""}

    def _save_state(self, state: dict) -> None:
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        p = self._state_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, p)

    # ---------------------------------------------------------------- 备份
    def backup(self, backup_type: BackupType = BackupType.FULL) -> BackupResult:
        started = time.time()
        try:
            cfg = self._cfg()
        except Exception as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="群晖任务配置错误: %s" % e)
        err = self._check_cfg(cfg)
        if err:
            return BackupResult(False, BackupStatus.FAILED, message=err)

        raw_bt = backup_type.value if isinstance(backup_type, BackupType) \
            else str(backup_type or "")
        want_inc = raw_bt == "incremental"
        cli = DSMClient(**{k: v for k, v in cfg.items()
                           if k in ("host", "port", "secure", "verify_ssl",
                                    "username", "password", "otp_code",
                                    "timeout", "session_name")})
        out_dir = self._output_dir()
        state = self._load_state()
        stage = os.path.join(out_dir, STAGE_NAME)
        files_root = os.path.join(stage, FILES_DIR)
        os.makedirs(files_root, exist_ok=True)

        # 请求增量但没有全量基线 → 诚实回退全量（不伪造增量）
        fallback_note = ""
        if want_inc and not state.get("chain"):
            want_inc = False
            fallback_note = "；未找到全量基线，已诚实回退为全量"
        bt_str = "incremental" if want_inc else "full"

        try:
            cli.discover()
            caps = cli.capabilities()
            if not caps.get("filestation"):
                return BackupResult(False, BackupStatus.FAILED,
                                    message="DSM 未暴露 FileStation.List，无法枚举文件")
            cli.login()
            current: dict[str, dict] = {}
            unreadable = excluded = 0
            for root in cfg["paths"]:
                for f in cli.walk_files(root, max_files=cfg["max_files"]):
                    if f.get("error"):
                        unreadable += 1
                        continue
                    if f.get("isdir") or not f.get("path"):
                        continue
                    if _is_excluded(f["path"], cfg["excludes"]):
                        excluded += 1
                        continue
                    _safe_rel(f["path"])       # 提前拒非法路径
                    current[f["path"]] = {"size": int(f.get("size") or 0),
                                         "mtime": int(f.get("mtime") or 0)}
        except (DSMError, ValueError) as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="枚举 DSM 文件失败: %s" % e)

        prev = state.get("snapshot") or {}
        if want_inc:
            plan = [p for p, m in current.items()
                    if p not in prev or prev[p].get("size") != m["size"]
                    or prev[p].get("mtime") != m["mtime"]]
            deleted = sorted(p for p in prev if p not in current)
        else:
            plan, deleted = list(current), []

        cache = state.get("stage_index") or {}
        # 断点护栏：只有「上一轮中断时的下载计划」与「本轮计划」完全一致，
        # .aidbm_part 残件才可信可续。否则（源变化 / 换了一批文件 / 换了任务）
        # 一律清除，绝不让不同内容的字节拼接成一个错位文件。
        pending = state.get("pending_scope") or {}
        scope_ok = bool(pending) and set(pending) == set(plan) and all(
            pending.get(p) == current[p] for p in plan)
        if not scope_ok:
            # part 可能散落在任意子目录，完整遍历一次清除
            for root_dir, _dirs, names in os.walk(files_root):
                for nm in names:
                    if nm.endswith(".aidbm_part"):
                        try:
                            os.remove(os.path.join(root_dir, nm))
                        except OSError:
                            pass
        failures: list[str] = []
        entries: dict[str, dict] = {}
        pulled = reused = 0
        new_pending = {p: current[p] for p in plan}

        def _persist_partial():
            self._save_state({**state, "stage_index": cache,
                              "pending_scope": new_pending})

        try:
            for path in plan:
                cancel_mod.check()
                meta = current[path]
                rel = _safe_rel(path)
                dest = os.path.join(files_root, rel)
                real_dest = os.path.realpath(dest)
                if not real_dest.startswith(os.path.realpath(files_root) + os.sep):
                    failures.append("%s: 路径越界" % path)
                    continue
                cached = cache.get(path) or {}
                cached_file = os.path.join(files_root, cached.get("rel") or "")
                hit = (cached.get("size") == meta["size"]
                       and cached.get("mtime") == meta["mtime"]
                       and cached.get("sha256")
                       and os.path.isfile(cached_file)
                       and os.path.getsize(cached_file) == meta["size"])
                if hit:
                    digest = _sha256(cached_file)
                    if digest == cached.get("sha256"):
                        entries[path] = {"path": path, "size": meta["size"],
                                         "mtime": meta["mtime"], "sha256": digest,
                                         "rel": "%s/%s" % (FILES_DIR, rel)}
                        reused += 1
                        continue
                    # 缓存漂移：旧字节不可信，连同 part 一起清掉重新拉
                    try:
                        os.remove(cached_file)
                    except OSError:
                        pass
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                # dest 存在但没进 cache（上次下完未记账）：内容不可信，重下
                try:
                    os.remove(dest)
                except OSError:
                    pass
                if not scope_ok:
                    try:
                        os.remove(dest + ".aidbm_part")
                    except OSError:
                        pass
                try:
                    got = cli.download(path, dest, resume=cfg["resume"],
                                       expected_size=meta["size"])
                    if got != meta["size"] or os.path.getsize(dest) != meta["size"]:
                        raise DSMError("落盘大小不一致（源 %d / 盘 %d）"
                                       % (meta["size"], os.path.getsize(dest)))
                    digest = _sha256(dest)
                except (DSMError, OSError, ValueError) as e:
                    failures.append("%s: %s" % (path, str(e)[:200]))
                    continue
                entries[path] = {"path": path, "size": meta["size"],
                                 "mtime": meta["mtime"], "sha256": digest,
                                 "rel": "%s/%s" % (FILES_DIR, rel)}
                pulled += got
                cache[path] = {"size": meta["size"], "mtime": meta["mtime"],
                               "sha256": digest, "rel": rel}
        except cancel_mod.CancelledError:
            _persist_partial()      # 保留断点，重跑可续
            raise

        if failures and not cfg["allow_partial"]:
            _persist_partial()
            return BackupResult(
                False, BackupStatus.FAILED,
                message="群晖备份失败：%d 个文件传输失败（已完成 %d 个，stage 与断点已保留）"
                        "，首条：%s" % (len(failures), len(entries), failures[0]),
                stderr="\n".join(failures[:20]),
                duration_sec=round(time.time() - started, 2))

        # 增量无变化：仍产出一份空增量包（链不断、删除事件不丢）
        # 产物名必须唯一：_timestamp() 只到秒，同一秒内连续两次增量会同名
        # 互相覆盖，恢复链会读到被覆盖后的 manifest 导致数据丢失。
        ts = self._timestamp()
        base_name = "synology_%s_%s" % (bt_str, ts)
        seq, tar_name = 0, base_name + ".tar.gz"
        while os.path.exists(os.path.join(out_dir, tar_name)):
            seq += 1
            tar_name = "%s_%d.tar.gz" % (base_name, seq)
        tar_path = os.path.join(out_dir, tar_name)
        parent = state["chain"][-1] if (want_inc and state.get("chain")) else None
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "schema": "aidbm-synology-file-backup",
            "backup_type": bt_str,
            "parent": os.path.basename(parent) if parent else None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "task_id": self.task_id, "task_name": self.task_name,
            "device_id": cfg.get("device_id") or 0,
            "device_name": cfg.get("device_name") or "",
            "dsm_host": cfg["host"], "dsm_port": cfg["port"],
            "paths": cfg["paths"],
            "api_count": caps.get("api_count") or 0,
            "unofficial_api": caps.get("unofficial") or [],
            "file_count": len(entries),
            "deleted_count": len(deleted),
            "deleted": deleted,
            "failed_count": len(failures),
            "failed": [f.split(":")[0] for f in failures][:200],
            "unreadable_dirs": unreadable,
            "excluded_count": excluded,
            "excludes": cfg["excludes"],
            "reused_files": reused,
            "pulled_bytes": pulled,
            "files": [entries[p] for p in sorted(entries)],
            "limits": {
                "level": "文件级（size + mtime 差分），非块级增量/CBT",
                "restore": "恢复到平台侧目录；推回 DSM 共享目录需 FileStation.Upload（未实现）",
                "metadata": "不保存 ACL / 权限位 / 配额 / 硬链接语义；中文文件名 UTF-8 保真",
                "iscsi": "SAN Manager 裸 LUN 不直接读取；请备份挂载 LUN 的业务主机挂载点",
            },
        }
        manifest_path = os.path.join(stage, MANIFEST_NAME)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        try:
            with tarfile.open(tar_path, "w:gz") as tf:
                tf.add(manifest_path, arcname=MANIFEST_NAME)
                for p in sorted(entries):
                    rel = _safe_rel(p)
                    src = os.path.join(files_root, rel)
                    if os.path.isfile(src):
                        tf.add(src, arcname="%s/%s" % (FILES_DIR, rel))
        except (OSError, tarfile.TarError) as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="打包群晖备份产物失败: %s" % e,
                                duration_sec=round(time.time() - started, 2))
        try:
            cli.logout()
        except DSMError:
            pass

        size = os.path.getsize(tar_path)
        checksum = "sha256:" + _sha256(tar_path)
        if want_inc:
            chain = list(state.get("chain") or []) + [tar_path]
        else:
            chain = [tar_path]
        self._save_state({
            "snapshot": current, "chain": chain[-500:], "full": chain[0],
            "stage_index": cache, "pending_scope": {},
            "last_backup": tar_path, "last_backup_type": bt_str,
        })
        msg = ("群晖 %s 备份成功：%d 个文件（复用 %d / 新拉 %d，%s），删除 %d，产物 %s%s"
               % ("增量" if want_inc else "全量", len(entries), reused,
                  len(entries) - reused, _human(pulled), len(deleted),
                  _human(size), fallback_note))
        if unreadable:
            msg += "；%d 个目录无权限已如实跳过" % unreadable
        if failures:
            msg += "；%d 个文件失败（已允许部分成功，见 manifest）" % len(failures)
        return BackupResult(
            True, BackupStatus.SUCCESS, backup_path=tar_path, size_bytes=size,
            checksum=checksum, duration_sec=round(time.time() - started, 2),
            message=msg, verified=False)

    # ---------------------------------------------------------------- 恢复
    def _read_manifest(self, tar_path: str) -> dict:
        with tarfile.open(tar_path, "r:gz") as tf:
            member = tf.getmember(MANIFEST_NAME)
            f = tf.extractfile(member)
            if not f:
                raise ValueError("产物内缺少 manifest.json")
            return json.loads(f.read().decode("utf-8"))

    def _extract(self, tar_path: str, target: str) -> None:
        root = os.path.realpath(target)
        with tarfile.open(tar_path, "r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile() or m.name == MANIFEST_NAME:
                    continue
                name = m.name.lstrip("/")
                if name.startswith("..") or "/.." in name:
                    raise ValueError("备份产物含非法路径: %s" % m.name)
                dst = os.path.realpath(os.path.join(root, name))
                if not dst.startswith(root + os.sep):
                    raise ValueError("备份产物路径越界: %s" % m.name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    raise ValueError("无法读取成员: %s" % m.name)
                with src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        started = time.time()
        if not backup_path or not os.path.isfile(backup_path):
            return BackupResult(False, BackupStatus.FAILED,
                                message="群晖备份产物不存在: %s" % backup_path)
        target = (kwargs.get("target_path") or kwargs.get("target_db")
                  or self.extra.get("synology_restore_path") or "")
        if not target:
            target = os.path.join(
                self.storage_root, "synology_restore",
                "%s_%s" % (self.task_id, self._timestamp()))
        if not os.path.isabs(target):
            target = os.path.join(self.storage_root, target)
        state = self._load_state()
        chain = [p for p in (state.get("chain") or []) if os.path.isfile(p)]
        wanted = os.path.realpath(backup_path)
        if wanted in [os.path.realpath(p) for p in chain]:
            idx = [os.path.realpath(p) for p in chain].index(wanted)
            replay = chain[:idx + 1]
        else:
            # 产物被移动/从别的实例导入：按 parent 指针尽力回溯
            replay, cur, guard = [], wanted, 0
            while cur and guard < 500:
                replay.append(cur)
                mf = self._read_manifest(cur)
                parent = mf.get("parent")
                if not parent:
                    break
                cur = os.path.realpath(os.path.join(os.path.dirname(cur), parent))
                if not os.path.isfile(cur) or cur in [os.path.realpath(p) for p in replay]:
                    return BackupResult(False, BackupStatus.FAILED,
                                        message="增量链不全：找不到父备份集 %s" % parent)
                guard += 1
            replay.reverse()
        if not replay:
            return BackupResult(False, BackupStatus.FAILED,
                                message="无法解析群晖备份链")

        try:
            os.makedirs(target, exist_ok=True)
            expect: dict[str, str] = {}
            deleted_all = 0
            for pkg in replay:
                mf = self._read_manifest(pkg)
                self._extract(pkg, target)
                for e in mf.get("files") or []:
                    expect[e["path"]] = e.get("sha256") or ""
                for p in mf.get("deleted") or []:
                    # 删除事件要同时从磁盘与期望集合移除，否则硬核对会
                    # 把「已被正确删除」误判成「恢复后文件缺失」
                    expect.pop(p, None)
                    rel = _safe_rel(p)
                    victim = os.path.realpath(os.path.join(target, FILES_DIR, rel))
                    if victim.startswith(os.path.realpath(target) + os.sep) \
                            and os.path.isfile(victim):
                        os.remove(victim)
                        deleted_all += 1
            # 硬核对：逐文件 sha256 与最终期望集合一致
            bad = []
            for p, digest in expect.items():
                fp = os.path.join(target, FILES_DIR, _safe_rel(p))
                if not os.path.isfile(fp):
                    bad.append("%s: 缺失" % p)
                elif digest and _sha256(fp) != digest:
                    bad.append("%s: sha256 不一致" % p)
            if bad:
                return BackupResult(
                    False, BackupStatus.FAILED,
                    message="群晖恢复校验失败：%d 个文件不一致，首条：%s"
                            % (len(bad), bad[0]),
                    stderr="\n".join(bad[:20]),
                    duration_sec=round(time.time() - started, 2))
        except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError) as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="群晖恢复失败: %s" % e,
                                duration_sec=round(time.time() - started, 2))
        restored_bytes = sum(
            os.path.getsize(os.path.join(target, FILES_DIR, _safe_rel(p)))
            for p in expect
            if os.path.isfile(os.path.join(target, FILES_DIR, _safe_rel(p))))
        return BackupResult(
            True, BackupStatus.SUCCESS, backup_path=target,
            size_bytes=restored_bytes,
            duration_sec=round(time.time() - started, 2),
            message=("群晖恢复成功：回放 %d 个备份集，%d 个文件 SHA256 硬核对一致，"
                     "删除事件 %d 条，落地目录 %s。注：恢复产物在平台侧，"
                     "推回 DSM 共享目录需 FileStation.Upload 通道（尚未实现）"
                     % (len(replay), len(expect), deleted_all, target)),
            verified=True)

    # ---------------------------------------------------------------- 校验
    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        options = options or {}
        path = record.get("backup_path") or ""
        if not path or not os.path.isfile(path):
            return BackupResult(False, BackupStatus.FAILED,
                                message="群晖备份产物不存在或路径为空")
        checksum = record.get("checksum") or ""
        if checksum.startswith("sha256:") and _sha256(path) != checksum.split(":", 1)[1]:
            return BackupResult(False, BackupStatus.FAILED,
                                message="群晖备份产物 SHA256 校验失败")
        try:
            mf = self._read_manifest(path)
            with tarfile.open(path, "r:gz") as tf:
                names = {m.name for m in tf.getmembers() if m.isfile()}
            expected = {"%s/%s" % (FILES_DIR, _safe_rel(e["path"]))
                        for e in mf.get("files") or []}
            missing = sorted(expected - names)
            if missing:
                return BackupResult(False, BackupStatus.FAILED,
                                    message="产物缺少 %d 个文件，首条：%s"
                                            % (len(missing), missing[0]))
            checked = 0
            with tarfile.open(path, "r:gz") as tf:
                for e in (mf.get("files") or [])[:int(options.get("sample") or 50)]:
                    f = tf.extractfile("%s/%s" % (FILES_DIR, _safe_rel(e["path"])))
                    if f is None:
                        return BackupResult(False, BackupStatus.FAILED,
                                            message="无法读取成员: %s" % e["path"])
                    h = hashlib.sha256()
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                    if e.get("sha256") and h.hexdigest() != e["sha256"]:
                        return BackupResult(False, BackupStatus.FAILED,
                                            message="文件 %s 的 SHA256 不一致" % e["path"])
                    checked += 1
        except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError) as e:
            return BackupResult(False, BackupStatus.FAILED,
                                message="群晖产物校验失败: %s" % e)
        return BackupResult(
            True, BackupStatus.SUCCESS, verified=True,
            size_bytes=os.path.getsize(path),
            message=("群晖 %s 产物校验通过：manifest %d 个文件完整，抽样 %d 个 SHA256 一致"
                     % (mf.get("backup_type"), len(mf.get("files") or []), checked)))
