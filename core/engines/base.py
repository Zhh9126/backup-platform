# -*- coding: utf-8 -*-
"""
备份引擎抽象基类与结果对象。

所有具体数据库引擎（MySQL / PostgreSQL / Oracle / Kingbase / DM / Redis /
MongoDB）都继承 BackupEngine，实现 backup() 与 restore()。基类统一提供：
- 客户端工具探测（check_client）
- 演示/兜底模式（客户端缺失时生成“标记仿真”的占位备份）
- 通用命令执行、环境变量注入、输出目录解析
"""
import enum
import os
import json
import time
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, List

import config
import core.db as db


class BackupType(str, enum.Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    DIFFERENTIAL = "differential"
    SNAPSHOT = "snapshot"  # Redis / MongoDB 逻辑导出用


class BackupMode(str, enum.Enum):
    """备份模式：物理备份（raw files）vs 逻辑备份（SQL dump）。"""
    LOGICAL = "logical"    # mysqldump / pg_dump / expdp / dexp
    PHYSICAL = "physical"  # XtraBackup / pg_basebackup / RMAN / dmrman


class BackupStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SIMULATED = "simulated"
    RUNNING = "running"


@dataclass
class BackupResult:
    success: bool
    status: str = BackupStatus.SUCCESS
    backup_path: Optional[str] = None
    size_bytes: int = 0
    original_size_bytes: int = 0   # 压缩前原始数据量（用于计算压缩率）
    compress_algo: str = ""        # none | gzip | zstd
    compress_ratio: float = 0.0    # 压缩率 = 压缩后 / 压缩前（0~1，越小越优）
    duration_sec: float = 0.0
    stdout: str = ""
    stderr: str = ""
    simulated: bool = False
    checksum: str = ""
    message: str = ""
    # CDC 基线（用于 PITR/对象级/克隆）
    binlog_file: str = ""
    binlog_pos: int = 0
    wal_lsn: str = ""
    # 校验结果
    verified: bool = False
    verify_msg: str = ""


class BackupEngine:
    """所有数据库备份引擎的基类。

    适配层契约（AdapterContract）：所有引擎向上统一暴露 5 类方法，供上层服务
    门面（Phase2 service_facade）屏蔽底层差异调用：
        1. backup / restore          —— 备份 / 恢复（各子类必须实现）
        2. synthesize_full           —— 合成全量（增量链合并，Phase1 落地）
        3. list_sets                 —— 列出任务备份集（Phase1 落地）
        4. clone_to_test / verify    —— 克隆测试库 / 校验（Phase2 落地）
    本 Phase 仅在基类补充 synthesize_full() / list_sets() 契约占位，具体实现在
    后续 Phase 由各引擎补齐。
    """

    db_type: str = "base"
    display_name: str = ""
    # 适配层分级：core_self（核心库自研）| peripheral_api（外围 API 集成）
    adapter_tier: str = "peripheral_api"
    # 该类引擎依赖的客户端可执行文件名（用于 PATH 探测）
    required_clients: List[str] = []

    def __init__(self, task: dict, storage_root: str, logger=None):
        self.task = task
        self.storage_root = storage_root
        self.logger = logger or db.get_logger(f"engine.{self.db_type}")
        self.task_id = task.get("id")
        self.task_name = task.get("name") or f"task_{self.task_id}"

    @property
    def backup_mode(self) -> BackupMode:
        raw = self.task.get("backup_mode") or ""
        if raw.lower() == "physical":
            return BackupMode.PHYSICAL
        return BackupMode.LOGICAL  # 默认逻辑备份（兼容旧数据）

    @property
    def extra(self) -> dict:
        """解析 task.extra_options 为字典。"""
        raw = self.task.get("extra_options") or ""
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    # ---------------- 压缩（先进算法 + 可恢复） ----------------
    # 进阶压缩算法优先级：zstd(系统二进制) > zstd(Python 库) > gzip。
    # zstd 压缩率与速度均显著优于传统 gzip；gzip 作为最后的兜底保证任何环境
    # 都能完成备份且可解压恢复。所有算法均<双向可逆>，恢复侧配套解压命令。
    _ZSTD_LEVEL = 10         # 默认 zstd 级别（基准：综合压缩率 24.3%，比 gzip -6 省 7.5%）
                               # 磁盘空间极紧张时可调到 19（综合 22.4%，省 14.8%，但更慢）
    _GZIP_LEVEL = 6          # 兜底 gzip 级别（与原实现保持一致）

    @staticmethod
    def _compression_enabled() -> bool:
        return bool(getattr(config, "COMPRESS_BY_DEFAULT", True))

    @staticmethod
    def _zstd_cli() -> Optional[str]:
        """返回可用的 zstd 可执行文件路径（系统 PATH 优先），否则 None。"""
        return shutil.which("zstd")

    @staticmethod
    def _zstd_module():
        """返回 Python zstandard 模块；不可用返回 None（避免硬依赖）。"""
        try:
            import zstandard  # noqa: F401
            return zstandard
        except Exception:
            return None

    def _resolve_compress_algo(self) -> str:
        """决定本次备份使用的压缩算法：zstd > gzip。关闭压缩时返回 'none'。"""
        if not self._compression_enabled():
            return "none"
        if self._zstd_cli() or self._zstd_module():
            return "zstd"
        return "gzip"

    def pipe_compress(self, algo: str = None, level: int = None) -> List[str]:
        """返回可插入「数据管道」的压缩命令（stdin→stdout 流式压缩）。

        采用 Python 标准库/zstandard 库实现（不依赖系统外部二进制），保证
        Windows 开发机与 Linux 部署机行为一致、且必定可解压恢复。若部署机
        存在系统 ``zstd`` 则优先使用以获得更高吞吐。

        - zstd: 流式压缩，恢复侧用配套解压命令，天然可逆。
        - gzip: 流式压缩（Python gzip 标准库），恢复侧用配套解压命令。
        - none: 不压缩（cat 透传）。
        """
        algo = algo or self._resolve_compress_algo()
        if algo == "none":
            return ["cat"]
        if algo == "zstd":
            lvl = level if level is not None else self._ZSTD_LEVEL
            cli = self._zstd_cli()
            if cli:
                return [cli, "-{}".format(lvl), "-c", "-"]
            # Python 库实现（跨平台、零外部依赖）
            return ["python", "-c",
                    "import sys,zstandard as z;"
                    "c=z.ZstdCompressor(level=%d);"
                    "sys.stdout.buffer.write(c.stream_reader(sys.stdin.buffer).read())" % lvl]
        # gzip（Python 标准库，不依赖系统 gzip 二进制）
        lvl = level if level is not None else self._GZIP_LEVEL
        return ["python", "-c",
                "import sys,gzip;"
                "sys.stdout.buffer.write(gzip.compress(sys.stdin.buffer.read(), %d))" % lvl]

    def pipe_decompress(self, algo: str = None) -> List[str]:
        """返回与 pipe_compress 配套的解压命令（stdin→stdout 流式解压）。

        恢复必须能正确解压，因此按相同优先级回退：zstd 系统 > zstd 库 > gzip 库。
        """
        algo = algo or self._resolve_compress_algo()
        if algo == "none":
            return ["cat"]
        if algo == "zstd":
            cli = self._zstd_cli()
            if cli:
                return [cli, "-dc", "-"]
            return ["python", "-c",
                    "import sys,zstandard as z;"
                    "sys.stdout.buffer.write(z.ZstdDecompressor().stream_reader("
                    "sys.stdin.buffer).read())"]
        # gzip（Python 标准库）
        return ["python", "-c",
                "import sys,gzip;"
                "sys.stdout.buffer.write(gzip.decompress(sys.stdin.buffer.read()))"]

    def _measure_original_size(self, stream_proc) -> int:
        """从「数据管道」的前置进程读 stdout 字节，统计原始(未压缩)数据量大小。

        早期实现只记录压缩后大小，无法计算压缩率。这里在 [dump | gzip] 之间
        插入一个 tee 计数进程，把原始 dump 字节数累积下来（memo 统一回传）。
        """
        total = 0
        try:
            while True:
                chunk = stream_proc.stdout.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
        except Exception:
            pass
        return total

    # ---------------- 通用工具 ----------------
    def check_client(self) -> (bool, str):
        missing = [c for c in self.required_clients if not shutil.which(c)]
        if missing:
            return False, "缺少客户端工具: " + ", ".join(missing) + "（请安装并在 PATH 中）"
        return True, "ok"

    def preflight(self) -> (bool, str):
        """备份前置检查：检测依赖客户端是否就绪。

        - 客户端全部就绪：返回 (True, "ok")
        - 客户端缺失：
            * 物理备份：硬失败，提示前往【备份插件】安装对应客户端
            * 逻辑备份：返回 (True, detail)；上层在 DEMO_MODE 开启或
              _should_simulate() 返回 True 时会走仿真兜底
        """
        ok, detail = self.check_client()
        if ok:
            return True, "ok"
        # 物理备份：禁止仿真兜底，必须有真实客户端
        try:
            mode = self.backup_mode
        except Exception:
            mode = BackupMode.LOGICAL
        if mode == BackupMode.PHYSICAL:
            return False, (
                f"{detail}。当前任务为【物理备份】，需要真实客户端；"
                f"请前往【备份插件】页安装「{self.db_type}」对应的物理备份插件后重试。"
            )
        # 逻辑备份：允许仿真，detail 透传给上层
        return True, detail

    def _should_simulate(self) -> (bool, str):
        """根据 DEMO_MODE 与客户端可用性，决定是否走仿真兜底。"""
        if self.task.get("demo_only"):
            return True, "任务标记为演示(demo_only)"
        mode = config.DEMO_MODE
        if mode == "on":
            return True, "DEMO_MODE=on 强制仿真"
        if mode == "off":
            return False, ""
        # auto：客户端缺失则仿真
        ok, detail = self.check_client()
        return (not ok), detail

    def _run(self, cmd: List[str], env_extra: dict = None, timeout: int = 3600) -> dict:
        env = os.environ.copy()
        # 注入解密后的密码到环境变量，避免明文出现在进程参数中
        pw = db.decrypt_secret(self.task.get("password") or "")
        if pw:
            env["DB_BACKUP_PASSWORD"] = pw
        if env_extra:
            env.update(env_extra)
        self.logger.info("[%s] 执行命令: %s", self.task_name, " ".join(
            c if not c.startswith("DB_BACKUP_PASSWORD") else "***" for c in cmd))
        try:
            proc = subprocess.run(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout)
            out = proc.stdout.decode("utf-8", "ignore")
            err = proc.stderr.decode("utf-8", "ignore")
            return {"returncode": proc.returncode, "stdout": out, "stderr": err}
        except subprocess.TimeoutExpired:
            return {"returncode": -1, "stdout": "", "stderr": "命令执行超时"}
        except FileNotFoundError as e:
            return {"returncode": -2, "stdout": "", "stderr": f"命令不存在: {e}"}

    def _timestamp(self) -> str:
        return time.strftime("%Y%m%d_%H%M%S")

    def _output_dir(self) -> str:
        d = os.path.join(self.storage_root, self.db_type,
                         f"{self.task_id}_{self.task_name}")
        os.makedirs(d, exist_ok=True)
        return d

    def _simulate_backup(self, backup_type: BackupType, reason: str) -> BackupResult:
        """生成标记仿真的占位备份文件，使平台在无客户端环境也能运行/演示。"""
        d = self._output_dir()
        ts = self._timestamp()
        fname = f"{ts}__{self.task_name}__{backup_type.value}.sim"
        fpath = os.path.join(d, fname)
        payload = {
            "simulated": True,
            "note": "该文件为演示/兜底占位备份，并非真实数据。原因: " + reason,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "db_type": self.db_type,
            "host": self.task.get("host"),
            "port": self.task.get("port"),
            "db_name": self.task.get("db_name"),
            "backup_type": backup_type.value,
            "generated_at": db.now_iso(),
        }
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        size = os.path.getsize(fpath)
        self.logger.warning("[%s] 生成仿真备份(占位): %s | %s", self.task_name, fpath, reason)
        return BackupResult(
            success=True, status=BackupStatus.SIMULATED, backup_path=fpath,
            size_bytes=size, simulated=True,
            message="仿真备份(占位)成功；" + reason)

    def _simulate_restore(self, backup_path: str, reason: str) -> BackupResult:
        return BackupResult(
            success=True, status=BackupStatus.SIMULATED, backup_path=backup_path,
            simulated=True, message="仿真恢复(占位)成功；" + reason)

    def _write_dump_file(self, data: bytes, backup_type: BackupType,
                          ssh_host: dict, ext: str, label: str) -> BackupResult:
        """将远程 dump 返回的字节流落盘，并返回 SUCCESS 结果。"""
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        bt = backup_type.value if isinstance(backup_type, BackupType) else str(backup_type)
        fname = f"{ts}__{self.task_name}__{bt}{ext}"
        out_path = os.path.join(out_dir, fname)
        with open(out_path, "wb") as af:
            af.write(data)
        size = os.path.getsize(out_path)
        checksum = db.sha256_file(out_path)
        hk = (ssh_host or {}).get("host_key", "remote")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=out_path, size_bytes=size, duration_sec=0.0,
            checksum=checksum,
            message=f"通过 SSH 在数据库服务器({hk})执行 {label} 成功 | {db.human_size(size)}",
        )

    # ---------------- 子类需实现 ----------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        raise NotImplementedError

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        raise NotImplementedError

    def synthesize_full(self, sets: list = None, target_storage_tier: int = None,
                        target_record_id: int = None) -> BackupResult:
        """合成全量：将增量链合并为一份完整备份集（仅 1%~10% 增量数据）。

        Args:
            sets: 待合并的备份集列表。每个元素为 BackupSet dict 或其 id；
                  若为空，引擎自行按任务查找增量链。列表中的"全量/合成全量"
                  作为链头，其余 incremental 依次合并。
            target_storage_tier: 合成产物落盘的存储层级（1/2/3）。
            target_record_id: 关联的备份记录 id（用于登记 BackupSet）。

        Returns:
            BackupResult，backup_path 为合成产物路径（或 None）；
            调用方（engines.synthesize_full_for_task）负责将结果登记为
            set_type=synthetic_full 的备份集。

        默认实现抛出 NotImplementedError，由各引擎在 Phase1 落地：
        - 物理备份走 xtrabackup --prepare --incremental-dir 合并；
        - 逻辑备份走"全量 SQL + 增量 binlog 重放"在恢复时合成。
        """
        raise NotImplementedError

    def list_sets(self) -> list:
        """列出该任务关联的备份集（BackupSet）列表，供校验/克隆/生命周期使用。

        默认实现读取 backup_sets 模型（按 task_id 过滤）；子类可覆盖。
        """
        import core.models as models
        return models.list_backup_sets(task_id=self.task_id)

    def _try_cross_host_restore(self, backup_path: str, target_host_info: dict,
                                  target_db: str = "") -> BackupResult:
        """跨主机恢复：通过 SFTP+SSH 在目标主机执行恢复。
        各引擎如启用跨主机功能，可在 restore() 入口检测 kwargs 中的
        target_host_info 并调用此方法。返回 BackupResult。
        """
        from core import cross_host
        from core import ssh_hosts as _ssh
        # 需要解密密码
        target = dict(target_host_info)
        target["password"] = db.decrypt_secret(target.get("password") or "")
        # 收集额外参数（密码、连接信息等）
        extra = {
            "source_host": self.task.get("host"),
            "source_port": self.task.get("port"),
            "source_username": self.task.get("username"),
            "source_password": db.decrypt_secret(self.task.get("password") or ""),
            "source_db": self.task.get("db_name"),
            "base_dir": self.task.get("base_dir") or "",
        }
        def log(msg):
            self.logger.info("[%s] %s", self.task_name, msg)
        res = cross_host.cross_host_restore(
            db_type=self.db_type, backup_path=backup_path,
            target_host_info=target, target_db=target_db, extra=extra, log=log)
        return BackupResult(
            success=res["ok"], status=BackupStatus.SUCCESS if res["ok"] else BackupStatus.FAILED,
            backup_path=backup_path, message=res.get("message", ""))

    def list_databases(self) -> List[str]:
        """可选：列出可备份的库/实例名。"""
        return []
