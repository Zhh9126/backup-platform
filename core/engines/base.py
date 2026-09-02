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
import shlex
import subprocess
import functools
from dataclasses import dataclass
from typing import Optional, List

import config
import core.db as db


def shlex_quote(s: str) -> str:
    """shell 单词安全引用（供自定义脚本环境变量注入使用）。"""
    return shlex.quote(str(s))


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
    # 本机常见客户端安装目录（平台侧兜底；命中后自动注入 PATH）
    _LOCAL_TOOL_FALLBACK_GLOBS = (
        "/opt/*/bin", "/opt/*/*/bin", "/usr/local/*/bin",
        "/data/*/bin", "/opt/database/bin",
    )

    def _ensure_local_clients_on_path(self, tools: list) -> tuple:
        """PATH 找不到的客户端工具：任务级 tool_path → 常见安装目录 逐级兜底。

        命中后把目录注入 os.environ["PATH"]（进程级，后续 _run 子进程继承），
        避免因平台服务启动方式不同、PATH 未包含客户端目录而误报
        "缺少客户端工具"。返回 (仍缺失列表, 兜底命中说明列表)。
        """
        import glob as _glob
        still = []
        hit_notes = []
        cand_dirs = [d for d in (self._task_tool_path() or "").split(":") if d]
        cand_dirs.append("/opt/database/bin")
        for pat in self._LOCAL_TOOL_FALLBACK_GLOBS:
            cand_dirs.extend(_glob.glob(pat))
        seen = set()
        cand_dirs = [d for d in cand_dirs if not (d in seen or seen.add(d))]
        for c in tools:
            if shutil.which(c):
                continue
            for d in cand_dirs:
                p = os.path.join(d, c)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    cur = os.environ.get("PATH", "")
                    if d not in cur.split(os.pathsep):
                        os.environ["PATH"] = d + os.pathsep + cur
                    hit_notes.append(f"{c}@{d}")
                    break
            else:
                still.append(c)
        return still, hit_notes

    def check_client(self) -> (bool, str):
        missing, hits = self._ensure_local_clients_on_path(
            list(self.required_clients))
        if missing:
            return False, ("缺少客户端工具: " + ", ".join(missing)
                           + "（已尝试 PATH 与常见安装目录自动探测；"
                             "请安装客户端，或在任务高级选项配置 tool_path 指向 bin 目录）")
        if hits:
            return True, "ok（客户端工具已自动注入 PATH: " + ", ".join(hits) + "）"
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

        # 1. 自带工具（若引擎声明了服务运行用户如 oracle，先以该用户探测——
        #    工具往往只在其 profile PATH 中可见，root 探测会误报缺失）
        check_user = getattr(self, "tool_check_user", None)
        tool_path = None
        try:
            from core import remote_dump as _rd
            tool_path = _rd.task_tool_path(self.task) or None
        except Exception:
            pass
        if self.physical_bundled_tools and check_user:
            from core import remote_dump
            missing = [t for t in self.physical_bundled_tools
                       if not remote_dump.remote_has_tool(
                           ssh_host, t, check_user=check_user,
                           extra_paths=tool_path)]
            if missing:
                return (False,
                        f"远端 {check_user} 用户环境缺少 {', '.join(missing)}，"
                        f"请确认数据库软件安装与用户 profile 配置")
            return True, f"ok（以 {check_user} 用户探测）"
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
        """逻辑备份远端前置检查：在 SSH 主机上探测 required_clients 是否就绪。

        - 若引擎声明 tool_check_user（如 Oracle 的工具仅 oracle 用户 profile 可见），
          则以该用户身份探测；
        - required_clients 中只要主工具（第一个）就绪即放行，其余缺失仅告警
          （引擎层有回退逻辑，如 exp 不可用回退 expdp）。
        """
        from core import remote_dump
        if not self.required_clients:
            return True, "ok"
        check_user = getattr(self, "tool_check_user", None)
        tool_path = None
        try:
            tool_path = remote_dump.task_tool_path(self.task) or None
        except Exception:
            pass
        missing = []
        for tool in self.required_clients:
            if not remote_dump.remote_has_tool(
                    ssh_host, tool, check_user=check_user, extra_paths=tool_path):
                missing.append(tool)
        primary = self.required_clients[0]
        if primary in missing:
            return False, (
                "远端 SSH 主机缺少客户端工具: " + ", ".join(missing) +
                "（请安装并在 PATH 中）"
            )
        if missing:
            self.logger.warning(
                "[%s] 远端部分客户端缺失(%s)，主工具 %s 就绪，放行（引擎内含回退逻辑）",
                self.task_name, ", ".join(missing), primary)
        return True, "远端工具就绪"

    def preflight(self) -> (bool, str):
        """备份前置检查：检测依赖是否就绪。

        - 自定义脚本模式：仅需 SSH 主机，跳过客户端工具检查。
        - 物理备份：先查远端（SSH 主机上的物理工具），再查本机自带工具。
          不依赖 check_client()（那是逻辑备份工具检查）。
        - 逻辑备份：优先 check_client()；本机缺失时，若任务目标有 SSH 主机，
          则到远端探测 required_clients，远端有即放行。不再仿真兜底。
        """
        try:
            extra = self._parse_task_extra()
        except Exception:
            extra = {}
        if extra.get("custom_script"):
            from core import remote_dump
            ssh_host = remote_dump.resolve_ssh_host(self.task)
            if not ssh_host:
                return False, ("自定义脚本模式需要 SSH 主机执行脚本："
                               "请按数据库地址自动匹配或在本任务中指定 SSH 主机")
            return True, "自定义脚本模式（SSH 执行，跳过客户端检查）"

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
            # 查本机自带工具（同样走兜底探测，避免 PATH 问题误报）
            if self.physical_bundled_tools:
                miss_phys, _ = self._ensure_local_clients_on_path(
                    list(self.physical_bundled_tools))
                if not miss_phys:
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
        # 任务级自定义环境变量（extra_options.env_vars，所有数据库类型通用）
        self._apply_task_env_vars(env)
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
        # 任务级自定义环境变量（extra_options.env_vars，所有数据库类型通用）
        self._apply_task_env_vars(env)
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
    # ------------------------------------------------------------------ #
    # 自定义备份/恢复脚本（全数据库类型通用）
    # ------------------------------------------------------------------ #
    def _parse_task_extra(self) -> dict:
        """解析任务 extra_options（JSON 字符串或 dict），返回 dict。"""
        raw = self.task.get("extra_options")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            import json as _json
            try:
                return _json.loads(raw)
            except Exception:
                return {}
        return {}

    def _task_tool_path(self) -> str:
        """任务级工具路径兜底（extra_options.tool_path，冒号分隔的 bin 目录）。"""
        try:
            from core import remote_dump
            return remote_dump.task_tool_path(self.task)
        except Exception:
            return ""

    def _resolve_local_tool(self, *names) -> str:
        """本机工具解析：任务级 tool_path 目录优先，其次 PATH。"""
        import shutil
        tp = self._task_tool_path()
        for d in filter(None, tp.split(":")):
            for n in names:
                p = os.path.join(d, n)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
        for n in names:
            p = shutil.which(n)
            if p:
                return p
        return names[0] if names else ""

    def _apply_task_env_vars(self, env: dict) -> None:
        """把任务级自定义环境变量（extra_options.env_vars）注入执行环境。

        所有数据库类型通用；本机命令（_run/_run_with_stdin/_env_with_tool_path）
        与远程 SSH 命令（scheduler 设置的 _wrap_login 前缀）均会注入。
        PATH 特殊处理：用户配置的 PATH 以「前缀」方式合并而非覆盖。
        """
        try:
            from core.remote_dump import parse_task_env_vars
            env_vars = parse_task_env_vars(self.task)
        except Exception:
            return
        if not env_vars:
            return
        user_path = env_vars.pop("PATH", None)
        env.update(env_vars)
        if user_path:
            env["PATH"] = user_path + os.pathsep + env.get("PATH", "")

    def _env_with_tool_path(self, extra_env: dict = None) -> dict:
        """构造本机执行环境：注入任务级 tool_path 到 PATH 前缀。"""
        env = os.environ.copy()
        # 任务级自定义环境变量（所有数据库类型通用）
        self._apply_task_env_vars(env)
        tp = self._task_tool_path()
        if tp:
            env["PATH"] = tp + os.pathsep + env.get("PATH", "")
        if extra_env:
            env.update(extra_env)
        return env

    def run_backup(self, backup_type: BackupType) -> BackupResult:
        """统一备份入口：配置了自定义脚本（extra_options.custom_script）时
        优先执行用户脚本，否则走引擎原生备份。

        自定义脚本在数据库服务器（SSH 主机）上执行，平台注入 PLATFORM_*
        环境变量，并把脚本产出的备份文件拉回本机落盘（真实 size/sha256）。
        """
        extra = self._parse_task_extra()
        if not extra.get("custom_script"):
            return self.backup(backup_type)

        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="自定义备份脚本需要 SSH 主机执行：请纳管数据库服务器"
                        "（按任务地址自动匹配）或在任务中指定 SSH 主机")
        return self._backup_custom_remote(ssh_host, backup_type, extra)

    def _backup_custom_remote(self, ssh_host: dict, backup_type: BackupType,
                              extra: dict) -> BackupResult:
        """在数据库服务器上执行用户自定义备份脚本。

        约定：
        - 脚本以 bash 运行（root 身份），退出码 0 = 成功；
        - 脚本必须把备份产物写入环境变量 PLATFORM_BACKUP_DIR 指向的目录
          （平台每次运行为其分配独立子目录，避免污染）；
        - 平台在脚本执行后扫描该目录，把产物 SFTP 拉回本机并计算
          真实 size/sha256；无产物视为失败。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        import time as _time

        ts = self._timestamp()
        task_id = self.task.get("id") or "x"
        script_body = str(extra.get("custom_script") or "")
        artifact_dir = (extra.get("custom_artifact_dir") or
                        f"/var/tmp/platform_backup/{task_id}/{ts}").rstrip("/")
        timeout_sec = int(extra.get("custom_timeout") or 7200)

        client = remote_dump._connect(ssh_host)
        sftp = client.open_sftp()
        try:
            remote_script = f"/tmp/platform_custom_{task_id}_{ts}.sh"
            with sftp.open(remote_script, "w") as f:
                f.write(script_body if script_body.endswith("\n") else script_body + "\n")
            try:
                sftp.chmod(remote_script, 0o700)
            except Exception:
                pass

            pw = db.decrypt_secret(self.task.get("password") or "")
            start = _time.time()
            env_lines = [
                f"export PLATFORM_BACKUP_TYPE={backup_type.value}",
                f"export PLATFORM_TASK_ID={task_id}",
                f"export PLATFORM_TASK_NAME={shlex_quote(str(self.task.get('name') or ''))}",
                f"export PLATFORM_DB_HOST={self.task.get('host') or ''}",
                f"export PLATFORM_DB_PORT={self.task.get('port') or ''}",
                f"export PLATFORM_DB_USER={self.task.get('username') or ''}",
                f"export PLATFORM_DB_NAME={self.task.get('db_name') or ''}",
                f"export PLATFORM_BACKUP_DIR={artifact_dir}",
            ]
            if pw:
                env_lines.append(f"export PLATFORM_DB_PASSWORD={shlex_quote(pw)}")
            inner = ("mkdir -p " + artifact_dir + " && "
                     + " && ".join(env_lines)
                     + f" && bash {remote_script}")
            shell = remote_dump._wrap_login(inner)
            out, err, rc = _ssh_exec_pipe(client, shell, timeout=timeout_sec)
            duration = round(_time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            err_text = err or ""
            self.logger.info("[%s] 自定义脚本返回 rc=%s", self.task_name, rc)

            if rc != 0:
                snippet = (out_text or err_text)[-1500:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=f"自定义备份脚本执行失败(rc={rc}): {snippet}")

            # 扫描产物目录（仅拉取本次运行期间产生/修改的常规文件）
            start_floor = start - 5
            artifacts = []
            try:
                for attr in sftp.listdir_attr(artifact_dir):
                    if attr.st_size <= 0:
                        continue
                    if getattr(attr, "st_mtime", 0) and attr.st_mtime < start_floor:
                        continue  # 跳过旧文件（用户指定目录可能已有历史产物）
                    artifacts.append((attr.filename, attr.st_size))
            except IOError as e:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=f"自定义脚本产物目录不存在或不可读: {artifact_dir} ({e})")

            if not artifacts:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=("自定义脚本执行成功但未产出备份文件："
                             f"请让脚本把产物写入 $PLATFORM_BACKUP_DIR（本次为 {artifact_dir}）"))

            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            local_files = []
            total = 0
            for fname, fsize in artifacts:
                local_path = os.path.join(out_dir, fname)
                sftp.get(f"{artifact_dir}/{fname}", local_path)
                local_files.append((local_path, os.path.getsize(local_path)))
                total += os.path.getsize(local_path)
            local_files.sort(key=lambda x: -x[1])
            primary = local_files[0][0]
            checksum = db.sha256_file(primary)

            manifest = os.path.join(out_dir, f"{ts}_custom_manifest.txt")
            with open(manifest, "w", encoding="utf-8") as mf:
                mf.write("Custom backup script via SSH\n")
                mf.write(f"ssh_host: {ssh_host.get('host_key', '')}\n")
                mf.write(f"task: {self.task_name}\n")
                mf.write(f"backup_type: {backup_type.value}\n")
                mf.write(f"artifact_dir: {artifact_dir}\n")
                for p, sz in local_files:
                    mf.write(f"{os.path.basename(p)}\t{sz}\t{db.sha256_file(p)}\n")

            hk = ssh_host.get("host_key", "remote")
            msg = (f"自定义备份脚本在 {hk} 执行成功，拉回 {len(local_files)} 个产物"
                   f"共 {db.human_size(total)}（主文件: {os.path.basename(primary)}）")
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=primary, size_bytes=total,
                duration_sec=duration, stdout=out_text, stderr=err_text,
                simulated=False, checksum=checksum, message=msg)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    def run_restore(self, backup_path: str, **kwargs) -> BackupResult:
        """统一恢复入口：配置了自定义恢复脚本（extra_options.custom_restore_script）
        时执行用户恢复脚本（备份文件先 SFTP 推到目标 SSH 主机），否则走引擎原生恢复。
        """
        extra = self._parse_task_extra()
        script = extra.get("custom_restore_script")
        if not script:
            return self.restore(backup_path, **kwargs)

        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        import time as _time

        ssh_host = kwargs.get("target_host_info") or remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="自定义恢复脚本需要 SSH 主机（恢复表单选择目标主机）")

        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"本地备份文件不存在: {backup_path}")

        ts = self._timestamp()
        timeout_sec = int(extra.get("custom_timeout") or 7200)
        client = remote_dump._connect(ssh_host)
        sftp = client.open_sftp()
        try:
            remote_file = f"/tmp/platform_restore_{ts}_{os.path.basename(backup_path)}"
            sftp.put(backup_path, remote_file)
            remote_script = f"/tmp/platform_custom_restore_{ts}.sh"
            with sftp.open(remote_script, "w") as f:
                f.write(script if script.endswith("\n") else script + "\n")
            try:
                sftp.chmod(remote_script, 0o700)
            except Exception:
                pass

            start = _time.time()
            pw2 = db.decrypt_secret(self.task.get("password") or "")
            env_lines = [
                f"export PLATFORM_BACKUP_FILE={shlex_quote(remote_file)}",
                f"export PLATFORM_RESTORE_DB={shlex_quote(str(kwargs.get('target_db') or self.task.get('db_name') or ''))}",
                f"export PLATFORM_TASK_ID={self.task.get('id') or ''}",
                f"export PLATFORM_DB_HOST={self.task.get('host') or ''}",
                f"export PLATFORM_DB_PORT={self.task.get('port') or ''}",
                f"export PLATFORM_DB_USER={self.task.get('username') or ''}",
                f"export PLATFORM_DB_NAME={self.task.get('db_name') or ''}",
            ]
            if pw2:
                env_lines.append(f"export PLATFORM_DB_PASSWORD={shlex_quote(pw2)}")
            inner = " && ".join(env_lines) + f" && bash {remote_script}"
            shell = remote_dump._wrap_login(inner)
            out, err, rc = _ssh_exec_pipe(client, shell, timeout=timeout_sec)
            duration = round(_time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")

            if rc != 0:
                detail = (out_text or err_text or "")[-1200:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=f"自定义恢复脚本执行失败(rc={rc}): {detail}")
            msg = (f"自定义恢复脚本在 {ssh_host.get('host_key', '')} 执行成功"
                   f"（耗时 {duration}s，备份文件: {os.path.basename(backup_path)}）")
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path, duration_sec=duration,
                stdout=out_text, simulated=False, message=msg)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

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
        # 全实例 tar 产物（multi-db-tar）暂不支持跨主机恢复：需在目标实例
        # 重建原库清单，请使用「恢复到本任务实例」（引擎会自动建缺失的库）
        if (backup_path or "").lower().endswith((".tar.gz", ".tgz")):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path,
                message="全实例备份产物（.tar.gz）暂不支持跨主机恢复，"
                        "请使用恢复到本任务实例（缺失的库会自动创建）")
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
