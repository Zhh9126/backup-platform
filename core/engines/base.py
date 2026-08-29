# -*- coding: utf-8 -*-
"""
备份引擎抽象基类与结果对象。

所有具体数据库引擎（MySQL / PostgreSQL / Oracle / Kingbase / DM / Redis /
MongoDB）都继承 BackupEngine，实现 backup() 与 restore()。基类统一提供：
- 客户端工具探测（check_client）
- 网络重试机制（_with_network_retry / _retry_ssh_call）
- 通用命令执行、环境变量注入、输出目录解析

注意：自 2026-08-14 起不再提供仿真/兜底占位备份；客户端或连接缺失即失败。
"""
import enum
import os
import sys
import json
import time
import shutil
import subprocess
import functools
from dataclasses import dataclass
from typing import Optional, List

import config
import core.db as db


def _is_network_error(exc: Exception) -> bool:
    """判断异常是否属于可重试的网络/连接错误。"""
    msg = str(exc).lower()
    network_keywords = (
        "connection", "connect", "network", "timeout", "refused", "reset",
        "broken pipe", "no route to host", "eof", "ssh", "sftp", "socket"
    )
    return any(k in msg for k in network_keywords)


def _with_network_retry(retries=None, delay=None, backoff=2.0):
    """装饰器：对网络/连接类错误进行重试。

    retries: 最大重试次数（默认读取 config.BACKUP_RETRY_MAX 或 3）
    delay: 首次重试间隔秒数（默认读取 config.BACKUP_RETRY_DELAY 或 5）
    backoff: 退避倍数
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            max_retries = retries if retries is not None else getattr(
                config, "BACKUP_RETRY_MAX", 3)
            base_delay = delay if delay is not None else getattr(
                config, "BACKUP_RETRY_DELAY", 5)
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt >= max_retries or not _is_network_error(e):
                        raise
                    wait = base_delay * (backoff ** attempt)
                    # 从 logger 所在实例取 logger；否则用 print
                    logger = getattr(args[0], "logger", None) if args else None
                    msg = (f"[{fn.__name__}] 网络/连接错误，"
                           f"{wait:.0f}s 后第 {attempt + 1}/{max_retries} 次重试: {e}")
                    if logger:
                        logger.warning(msg)
                    else:
                        print(msg)
                    time.sleep(wait)
            raise last_exc
        return wrapper
    return deco


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
    detail_log: str = ""


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
    # 物理备份：数据库自带工具（如 rman / pg_basebackup / dmrman）
    physical_bundled_tools: List[str] = []
    # 物理备份：外部插件 plugin_id（如 percona-xtrabackup-80 / mariabackup）
    physical_external_plugins: List[str] = []

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
    _ZSTD_LEVEL = 19         # 默认 zstd 级别（最高实用档：压缩率最优，xtrabackup/zstd 均支持到 19）
                               # 更高要求可任务级 compress_level=22（ultra 模式，更慢）
    _GZIP_LEVEL = 9          # 兜底 gzip 级别（最高档，压缩率最大）

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

    @property
    def compress_level(self) -> int:
        """任务级压缩级别：0 表示跟随引擎默认（_ZSTD_LEVEL / _GZIP_LEVEL）。

        仅在 compress=1（开启压缩）且 compress_level>0 时生效。
        """
        if not self._compression_enabled():
            return 0
        try:
            lv = int(self.task.get("compress_level") or 0)
        except (TypeError, ValueError):
            lv = 0
        return lv

    @property
    def bandwidth_limit(self) -> int:
        """任务级限速（KB/s）：0 表示不限制。"""
        try:
            bw = int(self.task.get("bandwidth_limit") or 0)
        except (TypeError, ValueError):
            bw = 0
        return bw

    def _pv_throttle(self) -> List[str]:
        """返回 pv 限速管道片段（KB/s → 字节/秒）。

        仅当 bandwidth_limit>0 且系统存在 ``pv`` 时返回有效片段；
        否则返回空列表（调用方用 ``+ self._pv_throttle()`` 直接拼接，无需判空）。
        缺 pv 时由调用方在日志中提示「限速被跳过」。
        """
        bw = self.bandwidth_limit
        if not bw or not shutil.which("pv"):
            return []
        # pv -L 接受字节/秒；KB/s → 字节/秒
        return ["pv", "-L", f"{bw * 1024}"]

    _zstd_version_cache: tuple = None

    def _zstd_version(self) -> tuple:
        """探测系统 zstd CLI 版本 (major, minor)；探测失败返回 (0, 0)。

        用于决定是否启用 -T0（多线程，zstd>=1.3.3）与 --long（长距离匹配，
        zstd>=1.3.2）。带缓存，仅首次探测。
        """
        if self._zstd_version_cache is not None:
            return self._zstd_version_cache
        try:
            import re
            import subprocess
            p = subprocess.run(["zstd", "--version"], capture_output=True,
                               text=True, timeout=5)
            # 部分版本把版本号打到 stderr，合并读取
            out = (p.stdout or "") + (p.stderr or "")
            m = re.search(r"v?(\d+)\.(\d+)", out)
            self._zstd_version_cache = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        except Exception:
            self._zstd_version_cache = (0, 0)
        return self._zstd_version_cache

    def pipe_compress(self, algo: str = None, level: int = None) -> List[str]:
        """返回可插入「数据管道」的压缩命令（stdin→stdout 流式压缩）。

        采用 Python 标准库/zstandard 库实现（不依赖系统外部二进制），保证
        Windows 开发机与 Linux 部署机行为一致、且必定可解压恢复。若部署机
        存在系统 ``zstd`` 则优先使用以获得更高吞吐。

        - zstd: 流式压缩，恢复侧用配套解压命令，天然可逆。
        - gzip: 流式压缩（Python gzip 标准库），恢复侧用配套解压命令。
        - none: 不压缩（cat 透传）。

        level 为 None 时，优先使用任务级 compress_level（>0），否则用引擎默认值。
        """
        algo = algo or self._resolve_compress_algo()
        if algo == "none":
            return ["cat"]
        if algo == "zstd":
            lvl = level if level is not None else (self.compress_level or self._ZSTD_LEVEL)
            cli = self._zstd_cli()
            if cli:
                cli_ver = self._zstd_version()
                if cli_ver and cli_ver >= (1, 4):
                    # 并行(-T0 自动吃满多核) + 长距离匹配(--long=27≈256MB 窗口)：
                    # 对结构化 dump（同表大量 INSERT）压缩率显著提升且吞吐更高
                    return [cli, "-{}".format(lvl), "--long=27", "-T0", "-c", "-"]
                return [cli, "-{}".format(lvl), "-c", "-"]
            # Python 库实现（跨平台、零外部依赖）；用当前解释器保证 zstandard 可用
            return [sys.executable, "-c",
                    "import sys,zstandard as z;"
                    "c=z.ZstdCompressor(level=%d);"
                    "sys.stdout.buffer.write(c.stream_reader(sys.stdin.buffer).read())" % lvl]
        # gzip（Python 标准库，不依赖系统 gzip 二进制）
        lvl = level if level is not None else (self.compress_level or self._GZIP_LEVEL)
        return [sys.executable, "-c",
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
            return [sys.executable, "-c",
                    "import sys,zstandard as z;"
                    "sys.stdout.buffer.write(z.ZstdDecompressor().stream_reader("
                    "sys.stdin.buffer).read())"]
        # gzip（Python 标准库）
        return [sys.executable, "-c",
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

    def _preflight_remote_physical(self, ssh_host: dict) -> (bool, str):
        """物理备份远端前置检查：在 SSH 远端主机上探测工具是否就绪。

        检查顺序：
        1. 自带工具（physical_bundled_tools）：plugin_runtime.remote_check_clients
        2. 外部插件（physical_external_plugins）：plugin_catalog.check_installed_on_host
           —— 外部插件只需有一个就绪即放行（如 MySQL 的多个 xtrabackup 变体）

        缺工具返回 (False, "远端未安装 X，请到备份插件页为该主机安装")；
        全部就绪返回 (True, "ok")。
        """
        from core import plugin_runtime, plugin_catalog

        # 1. 自带工具
        if self.physical_bundled_tools:
            chk = plugin_runtime.remote_check_clients(
                ssh_host, self.physical_bundled_tools)
            if not chk["installed"]:
                missing = ", ".join(chk["missing"])
                return (False,
                        f"远端未安装 {missing}，请到备份插件页为该主机安装"
                        f"或确认数据库自带工具路径")
            return True, "ok"

        # 2. 外部插件：至少有一个就绪即放行
        if self.physical_external_plugins:
            for pid in self.physical_external_plugins:
                try:
                    st = plugin_catalog.check_installed_on_host(pid, ssh_host)
                    if st.get("installed"):
                        return True, f"远端已安装 {pid}"
                except Exception:
                    continue
            return (False,
                    f"远端未安装任何物理备份插件（{', '.join(self.physical_external_plugins)}），"
                    f"请到备份插件页为该主机安装")

        # 既无自带工具也无外部插件声明（不该到达此处）
        return True, "ok"

    def _preflight_remote_logical(self, ssh_host: dict) -> (bool, str):
        """逻辑备份远端前置检查：在 SSH 主机上探测 required_clients 是否就绪。"""
        from core import remote_dump
        if not self.required_clients:
            return True, "ok"
        missing = []
        for tool in self.required_clients:
            if not remote_dump.remote_has_tool(ssh_host, tool):
                missing.append(tool)
        if missing:
            return False, (
                "远端 SSH 主机缺少客户端工具: " + ", ".join(missing) +
                "（请安装并在 PATH 中）"
            )
        return True, "远端工具就绪"

    def preflight(self) -> (bool, str):
        """备份前置检查：检测依赖是否就绪。

        - 物理备份：先查远端（SSH 主机上的物理工具），再查本机自带工具。
          不依赖 check_client()（那是逻辑备份工具检查）。
        - 逻辑备份：优先 check_client()；本机缺失时，若任务目标有 SSH 主机，
          则到远端探测 required_clients，远端有即放行。不再仿真兜底。
        """
        try:
            mode = self.backup_mode
        except Exception:
            mode = BackupMode.LOGICAL

        if mode == BackupMode.PHYSICAL:
            # 物理备份：先查远端
            try:
                from core import remote_dump
                ssh_host = remote_dump.resolve_ssh_host(self.task)
            except Exception:
                ssh_host = None
            if ssh_host:
                ok2, msg2 = self._preflight_remote_physical(ssh_host)
                if ok2:
                    return True, f"远端工具就绪（{msg2}）"
                # 远端缺失不再直接失败：回退检查本机（执行层远端失败同样会回退本机）
                self.logger.warning(
                    "[%s] 远端物理备份工具缺失（%s），转检查本机",
                    self.task_name, msg2)
            # 查本机自带工具
            if self.physical_bundled_tools:
                missing = [t for t in self.physical_bundled_tools
                           if not shutil.which(t)]
                if not missing:
                    if ssh_host:
                        return True, ("远端未安装物理备份工具，本机具备，"
                                      "将回退本机执行")
                    return True, "本机已检测到物理备份工具"
            # 无远端也无本机自带工具
            _, detail = self.check_client()
            return False, (
                f"{detail}。物理备份需要远端或本机具备对应工具，"
                f"请前往【备份插件】页为该主机安装，或纳管 SSH 主机。"
            )

        # 逻辑备份：本机有客户端直接放行
        ok, detail = self.check_client()
        if ok:
            return True, "ok"

        # 本机缺失时，若目标主机已纳管 SSH，则去远端探测
        try:
            from core import remote_dump
            ssh_host = remote_dump.resolve_ssh_host(self.task)
        except Exception:
            ssh_host = None
        if ssh_host:
            ok2, msg2 = self._preflight_remote_logical(ssh_host)
            if ok2:
                return True, msg2
            return False, msg2

        return False, detail

    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """恢复校验：验证一条备份记录是否可恢复。

        基类默认实现仅做通用检查（文件存在、非空、checksum）。
        各具体引擎可覆盖本方法实现数据库相关的深度校验（如 xtrabackup --prepare、
        pg_verifybackup 等）。
        """
        options = options or {}
        backup_path = record.get("backup_path") or record.get("output_path") or ""
        db_type = record.get("db_type") or self.db_type
        if not backup_path:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="备份路径为空，无法校验")
        if not os.path.exists(backup_path):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"备份文件不存在: {backup_path}")
        size = os.path.getsize(backup_path)
        if size == 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="备份文件大小为 0")
        # checksum 校验（简单 SHA256 或 CRC）
        checksum = record.get("checksum") or ""
        if checksum and checksum.startswith("sha256:"):
            import hashlib
            h = hashlib.sha256()
            try:
                with open(backup_path, "rb") as f:
                    while True:
                        chunk = f.read(4 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                if h.hexdigest() != checksum.split(":", 1)[1]:
                    return BackupResult(success=False, status=BackupStatus.FAILED,
                                        message="SHA256 校验失败")
            except Exception as e:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message=f"校验文件失败: {e}")
        if backup_path.endswith(".sim"):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="不支持仿真备份，请删除该记录后重新执行真实备份",
                                verified=False)
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            message=f"{db_type}: backup files verified",
                            verified=True, size_bytes=size)

    def _should_simulate(self) -> (bool, str):
        """不再走仿真兜底，永远返回 False。"""
        return False, ""

    def _run(self, cmd: List[str], env_extra: dict = None, timeout: int = 3600,
             input_file: str = None) -> dict:
        env = os.environ.copy()
        # 注入解密后的密码到环境变量，避免明文出现在进程参数中
        pw = db.decrypt_secret(self.task.get("password") or "")
        if pw:
            env["DB_BACKUP_PASSWORD"] = pw
        if env_extra:
            env.update(env_extra)

        # 跨平台兼容：MySQL/PostgreSQL 等引擎在 POSIX 下用 `sh -c "<script>"`
        # 串联管道；Windows 没有 `sh`，需翻译为 `cmd /c` 执行，否则会抛出
        # FileNotFoundError(WinError 2)，导致本机备份/恢复直接失败。
        if len(cmd) == 3 and cmd[0] == "sh" and cmd[1] == "-c":
            cmd = self._translate_shell_script(cmd[2])

        self.logger.info("[%s] 执行命令: %s", self.task_name, " ".join(
            c if not c.startswith("DB_BACKUP_PASSWORD") else "***" for c in cmd))
        try:
            proc = subprocess.run(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                input=self._read_decompressed(input_file) if input_file else None,
                timeout=timeout)
            out = proc.stdout.decode("utf-8", "ignore")
            err = proc.stderr.decode("utf-8", "ignore")
            return {"returncode": proc.returncode, "stdout": out, "stderr": err}
        except subprocess.TimeoutExpired:
            return {"returncode": -1, "stdout": "", "stderr": "命令执行超时"}
        except FileNotFoundError as e:
            return {"returncode": -2, "stdout": "", "stderr": f"命令不存在: {e}"}

    def _run_with_stdin(self, cmd: List[str], text: str, env_extra: dict = None,
                        timeout: int = 3600) -> dict:
        """执行命令并把一段文本作为 stdin 喂入（跨平台，不依赖 shell 管道）。"""
        env = os.environ.copy()
        pw = db.decrypt_secret(self.task.get("password") or "")
        if pw:
            env["DB_BACKUP_PASSWORD"] = pw
        if env_extra:
            env.update(env_extra)
        if len(cmd) == 3 and cmd[0] == "sh" and cmd[1] == "-c":
            cmd = self._translate_shell_script(cmd[2])
        self.logger.info("[%s] 执行命令(stdin): %s", self.task_name, " ".join(
            c if not c.startswith("DB_BACKUP_PASSWORD") else "***" for c in cmd))
        try:
            proc = subprocess.run(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                input=text.encode("utf-8", "ignore"),
                timeout=timeout)
            return {"returncode": proc.returncode,
                    "stdout": proc.stdout.decode("utf-8", "ignore"),
                    "stderr": proc.stderr.decode("utf-8", "ignore")}
        except subprocess.TimeoutExpired:
            return {"returncode": -1, "stdout": "", "stderr": "命令执行超时"}
        except FileNotFoundError as e:
            return {"returncode": -2, "stdout": "", "stderr": f"命令不存在: {e}"}

    def _translate_shell_script(self, script: str) -> List[str]:
        """把 `sh -c "<script>"` 形式的命令翻译为当前平台可执行的命令。

        - POSIX（有 sh）：原样返回 ["sh", "-c", script]。
        - Windows（无 sh）：去掉 `set -o pipefail` 等 bash 专有语法，将单引号
          替换为双引号（Windows cmd 只认双引号），再交给 `cmd /c` 执行。
          这样既保留了管道 `|`、输入重定向 `< file`、以及 `mysql < file` 等
          标准用法，又避免了 `WinError 2 系统找不到指定的文件`（找不到 sh）。
        """
        if getattr(os, "name", "") == "nt" or not shutil.which("sh"):
            s = script
            # 移除 bash 专有选项（cmd 不支持）
            s = s.replace("set -o pipefail;", "").replace("set -o pipefail", "")
            # 单引号包裹的路径/字符串在 cmd 下需改为双引号（前提是脚本中
            # 不存在双引号与单引号混用的冲突场景，备份引擎脚本满足此约束）
            s = s.replace("'", '"')
            return ["cmd", "/c", s.strip()]
        return ["sh", "-c", script]

    def _read_decompressed(self, path: str) -> bytes:
        """读取备份文件内容（自动按扩展名解压），返回喂给子进程 stdin 的字节。"""
        if not path:
            return b""
        lower = path.lower()
        if lower.endswith(".gz"):
            import gzip
            with gzip.open(path, "rb") as f:
                return f.read()
        if lower.endswith(".zst"):
            zstd = self._zstd_module()
            if zstd is not None:
                dctx = zstd.ZstdDecompressor()
                with open(path, "rb") as f:
                    return dctx.stream_reader(f).read()
            zcli = self._zstd_cli()
            if zcli:
                r = subprocess.run([zcli, "-dc", path], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=600)
                if r.returncode == 0:
                    return r.stdout
        with open(path, "rb") as f:
            return f.read()

    def _timestamp(self) -> str:
        return time.strftime("%Y%m%d_%H%M%S")

    def _output_dir(self) -> str:
        d = os.path.join(self.storage_root, self.db_type,
                         f"{self.task_id}_{self.task_name}")
        os.makedirs(d, exist_ok=True)
        return d

    def _simulate_backup(self, backup_type: BackupType, reason: str) -> BackupResult:
        """不再支持仿真/兜底占位备份。保留方法仅为了兼容旧调用。"""
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            message=f"缺少必要客户端/连接，无法执行真实备份: {reason}")

    def _simulate_restore(self, backup_path: str, reason: str,
                          detail_log: str = "") -> BackupResult:
        """不再支持仿真/兜底占位恢复。保留方法仅为了兼容旧调用。"""
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            message=f"缺少必要客户端/连接，无法执行真实恢复: {reason}")

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

    # ---------------- 远程优先回退策略 ----------------
    def _try_remote_then_local(self, remote_fn, local_fn, label: str) -> BackupResult:
        """先尝试在 SSH 备份机/数据库服务器执行，失败再回退到本机。

        这是为了解决"备份平台所在机器没有 mysqldump 等客户端"的问题：
        数据库服务器本身通常自带这些命令，因此优先在远端执行，把数据流
        通过 SSH 拉回到备份平台落盘。只有在远端也没有命令或 SSH 不可用
        时，才回退到本机执行。
        """
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        remote_error = None

        if ssh_host:
            hk = ssh_host.get("host_key", "unknown")
            self.logger.info("[%s] %s: 优先尝试远程主机 %s", self.task_name, label, hk)
            max_retries = getattr(config, "BACKUP_RETRY_MAX", 3)
            base_delay = getattr(config, "BACKUP_RETRY_DELAY", 5)
            for attempt in range(max_retries + 1):
                try:
                    result = remote_fn(ssh_host)
                    if result and result.success:
                        return result
                    remote_error = result.message if result else "远程执行未返回成功结果"
                    # 非网络错误直接结束重试
                    break
                except Exception as e:
                    remote_error = str(e)
                    if attempt < max_retries and _is_network_error(e):
                        wait = base_delay * (2 ** attempt)
                        self.logger.warning(
                            "[%s] %s 远程执行网络错误，%s 后第 %d/%d 次重试: %s",
                            self.task_name, label, wait, attempt + 1, max_retries, remote_error)
                        time.sleep(wait)
                        continue
                    self.logger.warning("[%s] %s 远程执行失败: %s", self.task_name, label, remote_error)
                    break
        else:
            remote_error = "未配置 SSH 备份机"

        self.logger.info("[%s] %s: 远程不可用，回退到本机执行", self.task_name, label)
        try:
            result = local_fn()
            if result and result.success:
                return result
            local_error = result.message if result else "本机执行未返回成功结果"
        except FileNotFoundError as e:
            local_error = f"命令不存在: {e}"
        except Exception as e:
            local_error = str(e)

        msg = f"{label} 失败。远程: {remote_error or '未尝试'}；本机: {local_error or '未尝试'}。" \
              f"请在数据库服务器上纳管 SSH 主机，或在备份平台安装对应客户端。"
        return BackupResult(success=False, status=BackupStatus.FAILED, message=msg)

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
                                  target_db: str = "", target_port: int = None) -> BackupResult:
        """跨主机恢复：通过 SFTP+SSH 在目标主机执行恢复。
        各引擎如启用跨主机功能，可在 restore() 入口检测 kwargs 中的
        target_host_info 并调用此方法。返回 BackupResult。

        target_port: 目标主机上数据库实例端口；为空时回退到源任务端口。
        """
        from core import cross_host
        from core import ssh_hosts as _ssh
        # 需要解密密码
        target = dict(target_host_info)
        target["password"] = db.decrypt_secret(target.get("password") or "")
        # 收集额外参数（密码、连接信息等）
        extra = {
            "source_host": self.task.get("host"),
            "source_port": target_port or self.task.get("port"),
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
