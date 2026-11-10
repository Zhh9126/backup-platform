# -*- coding: utf-8 -*-
"""磁带库存储后端（D2T，顺序介质 / 可移动介质）。

三种可核验的运行形态（由 endpoint 自动识别）：
1. **真实磁带机**：endpoint=/dev/nst*（Linux 非回绕设备）或
   `/dev/tape/by-id/...`（udev 持久化命名，推荐，重启不漂移）。
   依赖 `mt`（mt-st）、`tar`；机械手由 core/tape_library.py 的 mtx 通道管理。
   写入：设备独占锁 → `mt eod` → 大块 `tar -cf`（追加一段）→ 记录 filemark。
2. **目录模拟带库**：endpoint=/path/to/tapedir，每盘"带"是一个 tape_NNN.tar，
   按 max_tape_gb 滚动开新带。用于无硬件环境的全流程验证与演练。
3. 任何其它 `/dev/...` 一律拒绝（**P0**：旧实现会把 /dev/sda 当磁带覆写）。

联网调研吸收的工程口径：
- `mtx` 只认 generic SCSI `/dev/sgX`；`lsscsi -g` 中 mediumx 行对应机械手。
- Docker 必须 `--device` 透传设备节点并授权 cgroup；镜像需自带 mt-st/mtx/lsscsi。
- 顺序介质要流式大块写（block_kb 可配，默认 64KB，LTO 建议 256KB 对齐），
  避免 "shoeshining"；写后校验通过 checksum + 回读比对，写入目录落 tape_jobs。
- 介质不做随机删除；`delete_file` 恒 False 并标记"待退役"，绝不静默假删。

诚实边界：真实设备模式不会在写后自动 rewind+fsf 逐段回读（那会扰动带位
且拖慢每条备份），自动写后校验默认只在目录模拟模式启用；真实带库请用
`verify_object()` / 显式校验作业完成回读（会如实记录 verified 状态）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .base import StorageBackend
from core import db as _db
from core import tape_library as tl


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TapeStorageBackend(StorageBackend):
    display_name = "磁带库 (D2T)"
    tier = 3

    # ---------------------------------------------------------------- #
    def __init__(self, config: dict, logger=None):
        super().__init__(config, logger)
        cfg = dict(config or {})
        endpoint = (cfg.get("endpoint") or "").strip()
        extra = cfg.get("extra_options") or cfg.get("config_json") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        if not isinstance(extra, dict):
            extra = {}

        self.target_id = cfg.get("id")
        self.changer_device = (extra.get("changer_device") or "").strip()
        self.drive_index = int(extra.get("drive_index") or 0)
        # 配置断链修复：UI 写 extra_options，旧代码只读 config_json
        self.max_tape_gb = float(extra.get("max_tape_gb", 0) or 0)
        self.block_kb = max(1, min(int(extra.get("block_kb", 64) or 64), 2048))
        self.verify_after_write = str(extra.get("verify_after_write", "1")).lower() \
            not in ("0", "false", "no", "off")
        default_stage = os.path.join(os.path.dirname(endpoint) or ".", ".tape_staging") \
            if endpoint and not endpoint.startswith("/dev/") else os.path.join(
                tempfile.gettempdir(), "aidbm_tape_staging")
        self.staging_root = Path(extra.get("staging_dir") or default_stage)
        self.staging_root.mkdir(parents=True, exist_ok=True)

        self.mode = "device" if endpoint.startswith("/dev/") else "dir"
        self.device = endpoint if self.mode == "device" else ""
        self.tape_dir = self.staging_root if self.mode == "device" else Path(endpoint)
        if self.mode == "dir":
            self.tape_dir.mkdir(parents=True, exist_ok=True)
        # 只做白名单校验，不在构造时抛异常（列表/测试接口需要能返回原因）
        self.device_error = ""
        if self.mode == "device" and not tl.is_safe_tape_device(self.device):
            self.device_error = (
                f"非法磁带设备路径: {self.device}；只允许 /dev/nst*、/dev/st* "
                f"或 /dev/tape/by-id/*（禁止把磁盘等块设备当磁带覆写）")

    # ---------------------------------------------------------------- #
    # 内部工具
    # ---------------------------------------------------------------- #
    def _lock(self):
        return tl.device_lock(self.device or str(self.tape_dir))

    def _run(self, cmd: list, timeout: int = 3600) -> dict:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
            return {"rc": r.returncode, "out": r.stdout or "", "err": r.stderr or ""}
        except FileNotFoundError as e:
            return {"rc": -2, "out": "", "err": f"命令不存在: {e}"}
        except subprocess.TimeoutExpired:
            return {"rc": -1, "out": "", "err": "命令执行超时"}
        except Exception as e:  # noqa: BLE001
            return {"rc": -3, "out": "", "err": str(e)}

    def _mt(self, op: str, count: int = None) -> dict:
        if self.mode != "device":
            return {"rc": 0, "out": "dir 模拟模式无磁带操作", "err": ""}
        if self.device_error:
            return {"rc": -2, "out": "", "err": self.device_error}
        try:
            r = tl.TapeDrive(self.device).op(op, count)
            return {"rc": 0 if r.get("ok") else -1,
                    "out": r.get("out", ""), "err": r.get("err", "")}
        except Exception as e:  # noqa: BLE001
            return {"rc": -2, "out": "", "err": str(e)}

    def _current_tape(self) -> Path:
        tapes = sorted(self.tape_dir.glob("tape_*.tar"),
                       key=lambda p: int(re.search(r"(\d+)", p.name).group(1)))
        limit = self.max_tape_gb * (1024 ** 3)
        if tapes:
            last = tapes[-1]
            if not limit or last.stat().st_size < limit:
                return last
        n = (int(re.search(r"(\d+)", tapes[-1].name).group(1)) + 1) if tapes else 1
        return self.tape_dir / f"tape_{n:03d}.tar"

    def _precheck(self) -> str:
        """返回空串表示可写，否则为明确失败原因。"""
        if self.mode == "device":
            if self.device_error:
                return self.device_error
            if not shutil.which("mt"):
                return "mt 命令不存在（镜像需安装 mt-st）"
            if not shutil.which("tar"):
                return "tar 命令不存在"
            if not os.path.exists(self.device):
                return f"磁带设备不存在: {self.device}（容器需 --device 透传）"
            st = tl.TapeDrive(self.device).status()
            parsed = st.get("parsed") or {}
            if not st.get("ok"):
                return f"mt status 失败: {st.get('err') or st.get('out')}"
            if parsed.get("write_protected"):
                return "磁带处于写保护状态（WR_PROT），请先更换或解除写保护"
            if "/dev/st" in self.device and "nst" not in self.device:
                return ("当前为回绕设备 /dev/st*，多段追加会被自动倒带覆盖；"
                        "请改用 /dev/nst* 或 /dev/tape/by-id/*-nst")
        return ""

    # ---------------------------------------------------------------- #
    # StorageBackend 接口
    # ---------------------------------------------------------------- #
    def save_file(self, file_path: str, object_key: str = None,
                  dedup: bool = False, chunked: bool = False) -> bool:
        if dedup and self._dedup_prepare(file_path):
            return True
        err = self._precheck()
        if err:
            self.logger.error("[Tape] 写入前置检查失败: %s", err)
            return False
        file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            self.logger.error("[Tape] 源文件不存在: %s", file_path)
            return False
        member = (object_key or os.path.basename(file_path)).strip("/")
        size = os.path.getsize(file_path)
        checksum = _sha256(file_path)
        tl._ensure_tables()
        job_id = tl.job_start(self.target_id, member, size, checksum,
                              media_label=(self.device or ""),
                              operator="tape-backend")

        with self._lock():
            # 每次写用唯一暂存目录，杜绝并发写/回迁互相 rmtree（旧实现硬编码 /tmp）
            staging = tempfile.mkdtemp(prefix="w_", dir=str(self.staging_root))
            try:
                staged = os.path.join(staging, member)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                shutil.copy2(file_path, staged)
                blocks = str(self.block_kb * 2)  # tar -b 单位是 512B
                if self.mode == "device":
                    r = self._mt("eod")
                    if r["rc"] != 0:
                        raise RuntimeError(f"mt eod 失败: {r['err']}")
                    r = self._run(["tar", "-b", blocks, "-cf", self.device,
                                   "-C", staging, member], timeout=7200)
                    if r["rc"] != 0:
                        raise RuntimeError(f"tar 写带失败: {r['err'] or r['out']}")
                    tell = self._mt("tell")
                    filemark = None
                    m = re.search(r"At block\s+(\d+)", tell.get("out") or "", re.I)
                    if m:
                        filemark = int(m.group(1))
                    verified = False   # 真实带库走显式 verify_object，不自动扰动带位
                    label = self.device
                else:
                    tape = self._current_tape()
                    r = self._run(["tar", "-rf", str(tape), "-C", staging, member],
                                  timeout=7200)
                    if r["rc"] != 0:
                        raise RuntimeError(f"tar 追加失败: {r['err'] or r['out']}")
                    filemark = None
                    label = tape.name
                    # 目录模拟：立即回读比对 sha256（写后校验）
                    verified = False
                    if self.verify_after_write:
                        verified = self._verify_member(str(tape), member, checksum)
                        if not verified:
                            raise RuntimeError("写后校验失败：带内 member 与源 checksum 不一致")
                tl.job_finish(job_id, True, verified=verified,
                              filemark=filemark, error="")
                self.logger.info("[Tape] 已写入 %s member=%s size=%d sha=%s verified=%s",
                                 label, member, size, checksum[:12], verified)
                return True
            except Exception as e:  # noqa: BLE001
                tl.job_finish(job_id, False, error=str(e))
                self.logger.error("[Tape] 写入失败: %s", e)
                return False
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _verify_member(tape_path: str, member: str, checksum: str) -> bool:
        try:
            with tarfile.open(tape_path, "r:") as tf:
                f = tf.extractfile(member)
                if f is None:
                    return False
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                return h.hexdigest() == checksum
        except Exception:  # noqa: BLE001
            return False

    def verify_object(self, object_key: str, expected_checksum: str = None,
                      dest_path: str = None) -> dict:
        """显式写后校验：回读带内 member 并比对 sha256（真实带库可手动触发）。"""
        if self.device_error:
            return {"ok": False, "error": self.device_error}
        jobs = tl.job_find(object_key)
        checksum = expected_checksum or (jobs[0]["checksum"] if jobs else "")
        if not checksum:
            return {"ok": False, "error": "无写入目录记录，无法校验（请提供 expected_checksum）"}
        with self._lock():
            tmp = tempfile.mkdtemp(prefix="v_", dir=str(self.staging_root))
            try:
                got = self.get_file(object_key, dest_path=os.path.join(tmp, "verify.bin"))
                if not got:
                    return {"ok": False, "error": "带内未找到该对象"}
                return {"ok": _sha256(os.path.join(tmp, "verify.bin")) == checksum,
                        "checksum": checksum, "object_key": object_key}
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def get_file(self, object_key: str, dest_path: str = None):
        """从磁带/模拟带回迁文件（唯一暂存目录 + 设备独占锁）。"""
        member = object_key or ""
        member_name = os.path.basename(member)

        def _extract(tape_path: str) -> bool:
            lst = self._run(["tar", "-tf", tape_path], timeout=7200)
            members = [ln.strip() for ln in (lst["out"] or "").splitlines()
                       if ln.strip()]
            targets = [m for m in members
                       if m == member or os.path.basename(m) == member_name]
            if not targets:
                return False
            for tm in targets:
                r = self._run(["tar", "-xf", tape_path, "-C", stage, tm],
                              timeout=7200)
                if r["rc"] != 0:
                    continue
                found = os.path.join(stage, tm)
                if not os.path.isfile(found):
                    continue
                if dest_path:
                    if os.path.isdir(dest_path):
                        shutil.rmtree(dest_path, ignore_errors=True)
                    parent = os.path.dirname(os.path.abspath(dest_path))
                    os.makedirs(parent, exist_ok=True)
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    shutil.move(found, dest_path)
                return True
            return False

        stage = tempfile.mkdtemp(prefix="r_", dir=str(self.staging_root))
        try:
            with self._lock():
                if self.mode == "device":
                    if self.device_error:
                        return None
                    self._mt("rewind")
                    seg = 0
                    try:
                        while seg <= 512:
                            lst = self._run(["tar", "-tf", self.device], timeout=7200)
                            if lst["rc"] != 0:
                                break
                            if any(member_name in ln for ln in lst["out"].splitlines()):
                                return True if _extract(self.device) else None
                            r = self._mt("fsf", 1)
                            seg += 1
                            if r["rc"] != 0:
                                break
                    finally:
                        self._mt("rewind")
                    return None
                for tape in sorted(self.tape_dir.glob("tape_*.tar"), reverse=True):
                    if _extract(str(tape)):
                        return True
                return None
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def list_files(self) -> list:
        result = []
        with self._lock():
            if self.mode == "device":
                if self.device_error:
                    return []
                self._mt("rewind")
                seg = 0
                try:
                    while seg <= 512:
                        lst = self._run(["tar", "-tf", self.device], timeout=7200)
                        if lst["rc"] == 0:
                            for line in lst["out"].splitlines():
                                if line.strip():
                                    result.append({"tape": f"seg_{seg}",
                                                   "member": line.strip()})
                        r = self._mt("fsf", 1)
                        seg += 1
                        if r["rc"] != 0:
                            break
                finally:
                    self._mt("rewind")
            else:
                for tape in sorted(self.tape_dir.glob("tape_*.tar")):
                    r = self._run(["tar", "-tf", str(tape)], timeout=7200)
                    for line in (r["out"] or "").splitlines():
                        if line.strip():
                            result.append({"tape": tape.name, "member": line.strip()})
        return result

    def delete_file(self, object_key: str) -> bool:
        # 顺序介质不能随机删除；明确标记待退役，让生命周期不把"删不掉"当成功
        try:
            tl._ensure_tables()
            for j in tl.job_find(object_key):
                _db.execute("UPDATE tape_jobs SET status='pending_retire', "
                            "finished_at=? WHERE id=?", (tl.now_iso(), j["id"]))
        except Exception:  # noqa: BLE001
            pass
        self.logger.warning(
            "[Tape] 磁带为顺序介质，不支持随机删除单文件（%s）；"
            "已标记待退役，请通过介质整体退役/覆写流程回收。", object_key)
        return False

    def test_connection(self) -> tuple:
        if self.mode == "device":
            err = self._precheck()
            if err:
                return False, err
            r = self._mt("status")
            tools = tl.tool_status()
            missing = [k for k in ("mt", "mtx", "lsscsi")
                       if not tools.get(k, {}).get("available")]
            hint = ("；镜像缺少 " + ", ".join(missing)) if missing else ""
            return True, (f"磁带设备就绪: {self.device}；块大小 {self.block_kb}KB"
                          f"{hint}\n{r['out'][:200]}")
        try:
            probe = self.tape_dir / ".tape_write_test"
            probe.write_text(f"test_{int(time.time())}")
            probe.unlink()
            return True, (f"模拟带库目录可写: {self.tape_dir}"
                          f"（块大小 {self.block_kb}KB，写后校验 "
                          f"{'开' if self.verify_after_write else '关'}）")
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def file_exists(self, object_key: str) -> bool:
        if self.mode == "dir":
            for tape in self.tape_dir.glob("tape_*.tar"):
                r = self._run(["tar", "-tf", str(tape)], timeout=600)
                if any(object_key in ln for ln in (r["out"] or "").splitlines()):
                    return True
            return False
        # device 模式以写入目录为准（旧实现恒 False 会把带内副本误判为不存在）
        try:
            return any(j["status"] == "success" for j in tl.job_find(object_key))
        except Exception:  # noqa: BLE001
            return False

    def get_used_space(self) -> int:
        if self.mode == "dir":
            return sum(f.stat().st_size for f in self.tape_dir.glob("tape_*.tar"))
        try:
            return tl.media_summary(self.target_id).get("bytes_written", 0)
        except Exception:  # noqa: BLE001
            return 0
