# -*- coding: utf-8 -*-
"""
磁带库存储后端（D2T，对标 DBackup 2.11 备份到磁带）。

支持两种模式（由 endpoint 自动识别）：
1. **真实磁带设备**：endpoint=/dev/nst*（Linux 非回流磁带设备，LTO SAS/FC）。
   依赖 mt-st 工具包（mt 命令）。写入流程：mt eod 定位到带末尾 →
   tar 追加写带 → 记录带内位置。磁带特性：顺序介质，不支持随机删除单文件。
2. **目录模拟带库**：endpoint=/path/to/tapedir（目录）。每盘"带"是一个
   .tar 文件（tape_N.tar），按容量滚动开新带。用于无硬件环境的全流程
   验证与演练，API 与真实磁带完全一致。

对象组织：object_key 作为 tar 内的成员路径（保持 object_key_base 结构），
同一带内可追加多份备份；索引由 tar 头自带（tar -t 列举）。

安全说明：磁带介质不可随机删除/覆写中间数据（顺序介质物理特性），
delete_file 一律返回 False 并提示"请通过介质整体退役/覆写流程处理"。
"""
import os
import subprocess
import tarfile
import time
from pathlib import Path

from .base import StorageBackend


class TapeStorageBackend(StorageBackend):
    display_name = "磁带库 (D2T)"
    tier = 3

    BLOCK_SIZE = 65536  # 磁带块大小（建议为磁带机物理块大小的倍数）

    # ---------------------------------------------------------------- #
    def __init__(self, config: dict, logger=None):
        super().__init__(config, logger)
        endpoint = (config.get("endpoint") or "").strip()
        extra = config.get("config_json") or {}
        if isinstance(extra, str):
            import json
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        self.mode = "device" if endpoint.startswith("/dev/") else "dir"
        self.device = endpoint if self.mode == "device" else ""
        self.tape_dir = Path(endpoint) if self.mode == "dir" else Path(
            extra.get("staging_dir", "/tmp/tape_staging"))
        self.max_tape_gb = float(extra.get("max_tape_gb", 0) or 0)  # 0=不限（dir 模拟）
        if self.mode == "dir":
            self.tape_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- #
    # 内部工具
    # ---------------------------------------------------------------- #
    def _run(self, cmd: list, timeout: int = 3600) -> dict:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
            return {"rc": r.returncode, "out": r.stdout, "err": r.stderr}
        except FileNotFoundError as e:
            return {"rc": -2, "out": "", "err": f"命令不存在: {e}"}
        except subprocess.TimeoutExpired:
            return {"rc": -1, "out": "", "err": "命令执行超时"}

    def _mt(self, op: str, count: int = None) -> dict:
        """对真实磁带设备执行 mt 操作（rewind/eod/status/offline）。"""
        if self.mode != "device":
            return {"rc": 0, "out": "dir 模拟模式无磁带操作", "err": ""}
        if not os.path.exists(self.device):
            return {"rc": -2, "out": "", "err": f"磁带设备不存在: {self.device}"}
        args = ["mt", "-f", self.device, op]
        if count is not None:
            args.append(str(count))
        return self._run(args, timeout=300)

    def _current_tape(self) -> Path:
        """dir 模拟：返回当前写入带（未满的最后一盘）。"""
        import re
        tapes = sorted(self.tape_dir.glob("tape_*.tar"),
                       key=lambda p: int(re.search(r"(\d+)", p.name).group(1)))
        limit = self.max_tape_gb * (1024 ** 3)
        if tapes:
            last = tapes[-1]
            if not limit or last.stat().st_size < limit:
                return last
        n = (int(re.search(r"(\d+)", tapes[-1].name).group(1)) + 1) if tapes else 1
        return self.tape_dir / f"tape_{n:03d}.tar"

    # ---------------------------------------------------------------- #
    # StorageBackend 接口
    # ---------------------------------------------------------------- #
    def save_file(self, file_path: str, object_key: str = None,
                  dedup: bool = False, chunked: bool = False) -> bool:
        if dedup and self._dedup_prepare(file_path):
            return True
        file_path = os.path.abspath(file_path)
        # 按 object_key 的目录结构组织 tar 成员（保证按 object_key 精确回迁）：
        # staging/<object_key 的相对路径>
        member = (object_key or os.path.basename(file_path)).strip("/")
        staging = os.path.join("/tmp", "tape_staging", str(int(time.time() * 1000)))
        staged = os.path.join(staging, member)
        os.makedirs(os.path.dirname(staged), exist_ok=True)
        import shutil as _sh
        _sh.copy2(file_path, staged)
        staging_root = os.path.join(staging)

        if self.mode == "device":
            # 定位到带末尾（EOD），追加写入
            r = self._mt("eod")
            if r["rc"] != 0:
                self.logger.error("[Tape] mt eod 失败: %s", r["err"])
                return False
            r = self._run(["tar", "-b", str(self.BLOCK_SIZE // 512), "-cf",
                           self.device, "-C", staging_root, member],
                          timeout=7200)
            # 追加后再次 eod，方便下一次追加
            self._mt("eod")
        else:
            tape = self._current_tape()
            r = self._run(["tar", "-rf", str(tape), "-C", staging_root, member],
                          timeout=7200)

        _sh.rmtree(staging, ignore_errors=True)
        if r["rc"] != 0:
            self.logger.error("[Tape] 写入失败: %s", r["err"] or r["out"])
            return False
        self.logger.info("[Tape] 已写入%s: %s (member=%s)",
                         "磁带设备" if self.mode == "device" else "模拟带",
                         self.device or self._current_tape(), member)
        return True

    def get_file(self, object_key: str, dest_path: str = None):
        """从磁带/模拟带读取文件。

        object_key 支持两种形式：
        - 完整 member 路径（写入时的 object_key）
        - 仅文件名（在所有带上查找）
        """
        member = object_key
        member_name = os.path.basename(member)

        def _extract(tape_path: str) -> bool:
            # 提取到独立 staging 目录（与 dest_path 完全解耦）：
            # 之前 tar -C <dest_path> 提取会在 dest_path 下创建 member 的父
            # 目录链，与"移动到 dest_path 精确位置"自相冲突（真机踩坑）。
            import shutil as _sh
            stage = "/tmp/tape_restore_stage"
            _sh.rmtree(stage, ignore_errors=True)
            os.makedirs(stage, exist_ok=True)
            dest = stage
            # 列出带内成员，找与 member 匹配的条目：
            # 优先完整路径匹配，其次按文件名（tar 内成员为写入时的原始文件名）
            lst = self._run(["tar", "-tf", tape_path], timeout=7200)
            members = [ln.strip() for ln in
                       (lst["out"] or "").splitlines() if ln.strip()]
            target_members = [m for m in members
                              if m == member or os.path.basename(m) == member_name]
            if not target_members:
                return False
            for tm in target_members:
                r = self._run(["tar", "-xf", tape_path, "-C", dest, tm],
                              timeout=7200)
                if r["rc"] != 0:
                    continue
                found = os.path.join(dest, tm)
                if os.path.isfile(found):
                    import shutil as _sh
                    if dest_path:
                        # tar -C 提取时会在 dest 下创建 member 的父目录链，
                        # 若 dest_path 恰为其中的目录则先清理；
                        # shutil.move 目标为已存在目录时会移入目录内——统一
                        # 处理为「移动到 dest_path 精确文件位置」
                        if os.path.isdir(dest_path):
                            _sh.rmtree(dest_path, ignore_errors=True)
                        _parent = os.path.dirname(os.path.abspath(dest_path))
                        os.makedirs(_parent, exist_ok=True)
                        if os.path.exists(dest_path):
                            os.remove(dest_path)
                        _sh.move(found, dest_path)
                    self.logger.info("[Tape] 已回迁: %s (member=%s) -> %s",
                                     member_name, tm,
                                     dest_path or found)
                    return True
            return False

        if self.mode == "device":
            # 回读：rewind → 顺序扫描各段（tar -t 匹配 → 提取）
            self._mt("rewind")
            seg = 0
            while True:
                lst = self._run(["tar", "-tf", self.device], timeout=7200)
                if lst["rc"] != 0:
                    break
                if any(member_name in line for line in
                       lst["out"].splitlines()):
                    self._mt("rewind")
                    return True if _extract(self.device) else None
                # 跳到下一个 tar 段
                r = self._mt("fsf", 1)
                seg += 1
                if r["rc"] != 0 or seg > 512:
                    break
            self._mt("rewind")
            self.logger.error("[Tape] 带内未找到: %s", member_name)
            return None

        # dir 模拟：从最新带往前找
        for tape in sorted(self.tape_dir.glob("tape_*.tar"), reverse=True):
            if _extract(str(tape)):
                return True
        self.logger.error("[Tape] 模拟带中未找到: %s", member_name)
        return None

    def list_files(self) -> list:
        """列出磁带/模拟带内全部成员。"""
        result = []
        if self.mode == "device":
            self._mt("rewind")
            seg = 0
            while True:
                lst = self._run(["tar", "-tf", self.device], timeout=7200)
                if lst["rc"] == 0:
                    for line in lst["out"].splitlines():
                        if line.strip():
                            result.append({"tape": f"seg_{seg}",
                                           "member": line.strip()})
                r = self._mt("fsf", 1)
                seg += 1
                if r["rc"] != 0 or seg > 512:
                    break
            self._mt("rewind")
        else:
            for tape in sorted(self.tape_dir.glob("tape_*.tar")):
                r = self._run(["tar", "-tf", str(tape)], timeout=7200)
                for line in (r["out"] or "").splitlines():
                    if line.strip():
                        result.append({"tape": tape.name, "member": line.strip()})
        return result

    def delete_file(self, object_key: str) -> bool:
        self.logger.warning(
            "[Tape] 磁带为顺序介质，不支持随机删除单文件（%s）。"
            "如需回收空间请执行介质整体退役或覆写流程。", object_key)
        return False

    def test_connection(self) -> tuple:
        if self.mode == "device":
            if not os.path.exists(self.device):
                return False, f"磁带设备不存在: {self.device}"
            r = self._mt("status")
            if r["rc"] != 0:
                return False, f"mt status 失败: {r['err']}"
            return True, f"磁带设备就绪: {self.device}\n{r['out'][:200]}"
        try:
            probe = self.tape_dir / ".tape_write_test"
            probe.write_text(f"test_{int(time.time())}")
            probe.unlink()
            return True, f"模拟带库目录可写: {self.tape_dir}"
        except Exception as e:
            return False, str(e)

    def file_exists(self, object_key: str) -> bool:
        if self.mode == "dir":
            for tape in self.tape_dir.glob("tape_*.tar"):
                r = self._run(["tar", "-tf", str(tape)], timeout=600)
                if any(object_key in ln
                       for ln in (r["out"] or "").splitlines()):
                    return True
        return False

    def get_used_space(self) -> int:
        if self.mode == "dir":
            total = 0
            for f in self.tape_dir.glob("tape_*.tar"):
                total += f.stat().st_size
            return total
        return 0  # 真实磁带容量由 mt status/驱动上报，此处不统计
