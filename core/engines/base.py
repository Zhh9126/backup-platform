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
import socket
import subprocess
import functools
from dataclasses import dataclass
from typing import Optional, List

import config
import core.db as db


def shlex_quote(s: str) -> str:
    """shell 单词安全引用（供自定义脚本环境变量注入使用）。"""
    return shlex.quote(str(s))


def _is_local_task(task: dict) -> bool:
    """判断任务的数据库是否就在平台本机（自定义脚本可走本地执行通道）。

    远端数据库一律走 SSH；仅当任务地址就是本机自身时，允许在平台本机
    bash 执行用户脚本（无需纳管 SSH 主机，便于平台同机部署场景）。
    """
    host = str(task.get("host") or "").strip()
    if not host:
        return False
    if host.lower() in ("127.0.0.1", "localhost", "::1", "0.0.0.0", ""):
        return True
    try:
        import socket
        addrs = {socket.gethostname()}
        try:
            addrs |= set(socket.gethostbyname_ex(socket.gethostname())[2])
        except Exception:
            pass
        if host in addrs:
            return True
    except Exception:
        pass
    return False


def _local_bash(script_path: str, env: dict, timeout: int):
    """在本机 bash 执行脚本，返回 (rc, out, err, duration)。"""
    import time as _time
    start = _time.time()
    try:
        proc = subprocess.run(["bash", script_path], env=env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = 124, _decode_maybe(getattr(e, "stdout", b"")).encode(), \
            f"脚本执行超时（{timeout}s）".encode()
    duration = round(_time.time() - start, 3)
    return rc, out, err, duration


def _decode_maybe(data) -> str:
    """把 subprocess 超时异常里可能携带的部分输出解码为文本。"""
    if not data:
        return ""
    try:
        if isinstance(data, bytes):
            return data.decode("utf-8", "ignore")
        return str(data)
    except Exception:
        return ""


def _is_network_error(exc: Exception) -> bool:
    """判断异常是否属于可重试的网络/连接错误。"""
    msg = str(exc).lower()
    network_keywords = (
        "connection", "connect", "network", "timeout", "refused", "reset",
        "broken pipe", "no route to host", "eof", "ssh", "sftp", "socket"
    )
    return any(k in msg for k in network_keywords)


def _local_failure_hint(err) -> str:
    """给"本机执行失败"配一句**针对性**的后续动作建议。

    此前的文案不分原因，一律写"请确认已安装客户端工具…再纳管 SSH 主机"：在
    "平台与数据库同机 + 磁盘写满(errno 28)"这类场景下完全是误导——用户既不需要
    SSH，也不需要装客户端。这里按真实错误分类，只给对得上的那一条建议。
    """
    e = str(err or "").lower()
    if any(m in e for m in ("errno 28", "no space left", "disk full", "enospc",
                            "quota exceeded", "not enough space", "空间不足")):
        return ("原因是磁盘空间不足（ENOSPC）。请清理对应分区，或把任务「产物根目录」"
                "改到空间充足的分区；临时工作目录可用环境变量 BP_WORK_DIR 指定。")
    if any(m in e for m in ("command not found", "命令不存在", "未找到 sql 客户端",
                            "未找到客户端", "no such file or directory",
                            "没有那个文件或目录", "未安装")):
        return ("原因是数据库客户端命令缺失。请在备份平台本机安装或指定对应客户端"
                "（如 mysqldump/pg_dump/expdp），也可在任务高级选项用 tool_path 指定目录。")
    if any(m in e for m in ("access denied", "认证", "1045", "connection refused",
                            "can't connect", "无法连接", "timed out", "超时")):
        return ("原因是连接或认证失败。请核对任务里的地址、端口、用户与密码，"
                "并确认数据库允许从平台机连接。")
    return ("该任务未纳管 SSH 备份机，因此仅在本机执行；如需在数据库服务器上执行，"
            "可在「SSH 主机」中纳管后重试。请按上面给出的原始错误继续排查。")


def _err_text(exc: BaseException) -> str:
    """异常文本；``str()`` 为空的异常（``MemoryError`` 等）退化为类型名。

    否则会产出「MySQL 全实例备份失败: 」这种看不到任何原因的消息——用户
    2026-09-17 实测遇到，排查成本极高。
    """
    return str(exc).strip() or type(exc).__name__


@functools.lru_cache(maxsize=1)
def _local_identifiers() -> frozenset:
    """本机主机名与全部网卡 IP（进程内缓存）。

    不用 DNS：离线环境下解析主机名会挂起或失败，这里只读本机信息。
    """
    ids = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    try:
        ids.add(socket.gethostname().lower())
        ids.add(socket.getfqdn().lower())
    except Exception:
        pass
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in (out or "").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "inet":
                ids.add(parts[3].split("/")[0])
    except Exception:
        pass
    return frozenset(x for x in ids if x)


def _is_platform_self(host) -> bool:
    """判断 SSH 目标 / 数据库地址是否就是备份平台本机。"""
    h = str(host or "").strip().lower()
    if not h:
        return False
    return h in _local_identifiers()


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
    # 派生对象引用（虚拟机克隆 / 恢复验证拉起的目标 VM，用于后续回收）
    target_ref: str = ""
    target_name: str = ""


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
                        f"远端未自带 {missing}（数据库服务器零安装），"
                        "备份时将由平台推送临时副本执行或回退平台服务端执行"
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
            from core import custom_scripts
            # 范围校验：单表/多表必须给出表名，否则脚本必然导出错对象
            scope = custom_scripts.normalize_scope(extra.get("custom_scope"))
            if scope == custom_scripts.SCOPE_TABLE and not custom_scripts.parse_tables(
                    extra.get("custom_tables")):
                return False, ("自定义脚本备份范围为「单表/多表」但未填写表名："
                               "请在高级选项→自定义脚本→表名中填写（逗号分隔）")
            ssh_host = remote_dump.resolve_ssh_host(self.task)
            if ssh_host:
                return True, (f"自定义脚本模式（SSH 执行，范围={scope}，"
                              "跳过客户端检查）")
            if _is_local_task(self.task):
                return True, f"自定义脚本模式（平台本机执行，范围={scope}）"
            return False, ("自定义脚本模式需要 SSH 主机执行脚本："
                           "请按数据库地址自动匹配或在本任务中指定 SSH 主机")

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
                f"{detail}。按数据库服务器零安装原则：可在平台服务端【备份插件】"
                f"页安装工具（备份时由平台临时推送执行，结束即清理），"
                f"或纳管 SSH 主机使用数据库自带工具。"
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

    def _run_with_stdin(self, cmd: List[str], text, env_extra: dict = None,
                        timeout: int = 3600) -> dict:
        """执行命令并把一段文本（str 或 bytes）作为 stdin 喂入（跨平台，不依赖 shell 管道）。"""
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
        # 二进制安全：bytes 原样透传；str 用 surrogateescape 编码，保证
        # 非 UTF-8 字节（BLOB / 二进制列 / 自定义转储）在"文本→字节"往返中无损。
        stdin_data = text if isinstance(text, bytes) else text.encode(
            "utf-8", "surrogateescape")
        try:
            proc = subprocess.run(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                input=stdin_data,
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

        - POSIX：优先用 bash 执行（引擎脚本普遍含 `set -o pipefail` 等 bash
          专有语法；部分发行版 /bin/sh 为 dash 不支持，如 Debian 系容器/宿主机，
          曾导致 "sh: 1: set: Illegal option -o pipefail")。无 bash 时才退回 sh。
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
        if shutil.which("bash"):
            return ["bash", "-c", script]
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

    # ---------------- 产物格式辅助（core.dump_format） ----------------
    @staticmethod
    def _pack_dir_tar_gz(src_dir: str, out_tar: str) -> None:
        """把目录型产物（pg_dump -Fd / mongodump --out 等）打包为单文件 tar.gz。

        打包时以 src_dir 的**内容**为根（不含最外层目录名），恢复端解开即可
        直接得到归档目录。
        """
        import tarfile
        with tarfile.open(out_tar, "w:gz") as tar:
            for name in sorted(os.listdir(src_dir)):
                tar.add(os.path.join(src_dir, name), arcname=name)

    @staticmethod
    def _tar_has_member(tar_path: str, member_name: str) -> bool:
        """判断 tar(.gz) 包中是否存在指定成员（按 basename 匹配）。"""
        import tarfile
        try:
            with tarfile.open(tar_path, "r:*") as tar:
                for m in tar.getmembers():
                    if os.path.basename(m.name) == member_name:
                        return True
        except Exception:
            return False
        return False

    @staticmethod
    def _untar_to_dir(tar_path: str, dest_dir: str) -> str:
        """解开 tar(.gz) 到 dest_dir，返回实际目录路径。"""
        import tarfile
        os.makedirs(dest_dir, exist_ok=True)
        with tarfile.open(tar_path, "r:*") as tar:
            tar.extractall(dest_dir)
        return dest_dir

    def _new_dump_path(self, backup_type: BackupType, ext: str) -> str:
        """生成产物落盘路径（与 _write_dump_file 命名规则一致，供流式落盘复用）。"""
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        bt = backup_type.value if isinstance(backup_type, BackupType) else str(backup_type)
        return os.path.join(out_dir, f"{ts}__{self.task_name}__{bt}{ext}")

    def _write_dump_file(self, data: bytes, backup_type: BackupType,
                          ssh_host: dict, ext: str, label: str) -> BackupResult:
        """将远程 dump 返回的字节流落盘，并返回 SUCCESS 结果。"""
        out_path = self._new_dump_path(backup_type, ext)
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
        remote_tried = False

        # 数据库服务器就是备份平台本机时，直接本机执行——本机客户端才是正解：
        # 未纳管 SSH 时本机是唯一通道；SSH 目标即本机时自己 SSH 自己只会多一层失败面。
        if ssh_host and _is_platform_self(ssh_host.get("host")):
            self.logger.info("[%s] %s: SSH 目标即备份平台本机，直接在本机执行",
                             self.task_name, label)
            ssh_host = None

        if ssh_host:
            remote_tried = True
            hk = ssh_host.get("host_key", "unknown")
            self.logger.info("[%s] %s: 优先尝试远程主机 %s", self.task_name, label, hk)
            # 重试次数/间隔走任务级高级选项（默认 3 次 / 间隔 60s）；
            # 大库备份失败后立刻重试没有意义，给链路与远端留出恢复时间。
            try:
                from core import remote_dump as _rd
                _tp = _rd.transfer_params(self.task)
                max_retries = int(_tp.get("retry_max") or 0)
                retry_interval = int(_tp.get("retry_interval") or 60)
            except Exception:
                max_retries = getattr(config, "BACKUP_RETRY_MAX", 3)
                retry_interval = getattr(config, "BACKUP_RETRY_INTERVAL", 60)
            for attempt in range(max_retries + 1):
                try:
                    result = remote_fn(ssh_host)
                    if result and result.success:
                        return result
                    remote_error = result.message if result else "远程执行未返回成功结果"
                    # 非网络错误直接结束重试
                    break
                except Exception as e:
                    # 远程侧整体落盘尚未全类型接入，大实例仍可能 MemoryError，
                    # 其 str() 为空，这里必须退化为类型名才看得出原因
                    remote_error = _err_text(e)
                    if attempt < max_retries and _is_network_error(e):
                        # 固定间隔重试（任务级，默认 60s）。大库备份的断点会被保留，
                        # 重试时从已传输字节继续，不会像以前那样从头重跑。
                        wait = retry_interval
                        self.logger.warning(
                            "[%s] %s 远程执行网络错误，%ss 后第 %d/%d 次重试: %s",
                            self.task_name, label, wait, attempt + 1, max_retries, remote_error)
                        time.sleep(wait)
                        continue
                    self.logger.warning("[%s] %s 远程执行失败: %s", self.task_name, label, remote_error)
                    break
        else:
            self.logger.info("[%s] %s: 未纳管 SSH 备份机，直接在本机执行",
                             self.task_name, label)

        if remote_tried:
            self.logger.info("[%s] %s: 远程不可用，回退到本机执行", self.task_name, label)
        try:
            result = local_fn()
            if result and result.success:
                return result
            local_error = (result.message if result else "") or "本机执行未返回成功结果"
        except FileNotFoundError as e:
            local_error = f"命令不存在: {e}"
        except Exception as e:
            local_error = _err_text(e)

        if remote_tried:
            msg = (f"{label} 失败。远程: {remote_error or '未尝试'}；"
                   f"本机: {local_error or '未尝试'}。"
                   f"请在数据库服务器上纳管 SSH 主机，或在备份平台安装对应客户端。")
        else:
            # 本机是唯一通道，此时提示"去纳管 SSH"是误导：同机场景本机客户端才是正解
            msg = (f"{label} 失败，本机执行未成功: {local_error or '原因未知'}。"
                   + _local_failure_hint(local_error))

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

    # 常见安装目录（glob）：客户端工具不在 PATH 时的兜底候选。
    # 实测坑：CentOS7 自带 /usr/bin/pg_dump 是 9.2，无法操作 PG14 服务器，
    # 而 /pgdb/pgsql/bin 下有 14.12——必须做版本择优，不能命中第一个就用。
    _COMMON_TOOL_DIR_GLOBS = (
        "/pgdb/pgsql/bin", "/usr/pgsql-*/bin", "/usr/local/pgsql/bin",
        "/usr/local/mysql*/bin", "/opt/mysql*/bin", "/opt/pgsql*/bin",
        "/usr/local/mariadb*/bin", "/opt/mariadb*/bin",
    )

    @staticmethod
    def _bin_version_tuple(path):
        """跑 `<bin> --version` 解析 (主版本, 次版本)；失败返回 None（候选淘汰）。"""
        import re as _re
        try:
            ret = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=10)
            m = _re.search(r"(\d+)\.(\d+)",
                           (ret.stdout or "") + (ret.stderr or ""))
            return (int(m.group(1)), int(m.group(2))) if m else None
        except Exception:
            return None

    def _resolve_local_tool(self, *names) -> str:
        """本机工具解析：任务级 tool_path 目录优先 → PATH → 常见安装目录。

        版本择优：同一名词存在多个候选时（CentOS7 自带 pg_dump 9.2 vs
        /pgdb/pgsql/bin 的 14.12），用 --version 输出选版本最高者——
        低版本客户端无法操作新版本服务器（pg_dump 报 server version mismatch）。
        --version 解析失败的候选（坏 shim/误装的同名二进制）直接淘汰。
        """
        import glob
        tp = self._task_tool_path()
        cands, seen = [], set()
        for d in filter(None, tp.split(":")):
            for n in names:
                p = os.path.join(d, n)
                if os.path.isfile(p) and os.access(p, os.X_OK) and p not in seen:
                    cands.append(p)
                    seen.add(p)
        for n in names:
            p = shutil.which(n)
            if p and p not in seen:
                cands.append(p)
                seen.add(p)
        for n in names:
            for pattern in self._COMMON_TOOL_DIR_GLOBS:
                for p in glob.glob(os.path.join(pattern, n)):
                    if (os.path.isfile(p) and os.access(p, os.X_OK)
                            and p not in seen):
                        cands.append(p)
                        seen.add(p)
        if not cands:
            return names[0] if names else ""
        if len(cands) == 1:
            return cands[0]
        best, best_ver = None, (-1, -1)
        for p in cands:
            v = self._bin_version_tuple(p)
            if v is None:
                continue
            if v > best_ver:
                best, best_ver = p, v
        return best or cands[0]

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
        if ssh_host:
            return self._backup_custom_remote(ssh_host, backup_type, extra)
        if _is_local_task(self.task):
            return self._backup_custom_local(backup_type, extra)
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            message="自定义备份脚本需要 SSH 主机执行：请纳管数据库服务器"
                    "（按任务地址自动匹配）或在任务中指定 SSH 主机")

    # ---------------- 自定义脚本：环境变量与范围 ----------------
    @staticmethod
    def _custom_scope_and_tables(extra: dict):
        """读取任务的自定义备份范围与表名列表（全实例/全库/单表）。"""
        from core import custom_scripts
        scope = custom_scripts.normalize_scope(extra.get("custom_scope"))
        tables = custom_scripts.parse_tables(extra.get("custom_tables"))
        return scope, tables

    def _custom_scope_label(self, extra: dict) -> str:
        """人类可读的备份范围（写进记录 message，便于在列表里区分全库/单表）。"""
        from core import custom_scripts
        scope, tables = self._custom_scope_and_tables(extra)
        label = custom_scripts.SCOPE_LABELS.get(scope, scope)
        if scope == custom_scripts.SCOPE_TABLE and tables:
            label += f"（表: {','.join(tables)}）"
        return label

    def _custom_backup_env_lines(self, backup_type, artifact_dir: str,
                                extra: dict, pw: str) -> list:
        """备份脚本注入的 PLATFORM_* 环境变量（shell 形式）。

        补充了范围相关变量，使同一份脚本能按「全实例 / 全库 / 单表」导出：
        PLATFORM_BACKUP_SCOPE、PLATFORM_TABLES、PLATFORM_DB_TYPE、PLATFORM_BACKUP_LEVEL。
        """
        scope, tables = self._custom_scope_and_tables(extra)
        db_type = getattr(self, "db_type", "") or self.task.get("db_type") or ""
        lines = []
        # 任务级工具路径（客户端不在默认 PATH 时，如 /opt/mysql840b/bin、/pgdb/pgsql/bin）
        tool_path = str(extra.get("tool_path") or "").strip()
        paths = [p.strip() for p in tool_path.replace(";", ":").split(":") if p.strip()]
        if paths:
            lines.append("export PATH=" + ":".join(shlex_quote(p) for p in paths) + ":$PATH")
        lines += [
            f"export PLATFORM_BACKUP_TYPE={backup_type.value if hasattr(backup_type, 'value') else backup_type}",
            f"export PLATFORM_BACKUP_LEVEL={backup_type.value if hasattr(backup_type, 'value') else backup_type}",
            f"export PLATFORM_TASK_ID={self.task.get('id') or ''}",
            f"export PLATFORM_TASK_NAME={shlex_quote(str(self.task.get('name') or ''))}",
            f"export PLATFORM_DB_TYPE={db_type}",
            f"export PLATFORM_DB_HOST={self.task.get('host') or ''}",
            f"export PLATFORM_DB_PORT={self.task.get('port') or ''}",
            f"export PLATFORM_DB_USER={self.task.get('username') or ''}",
            f"export PLATFORM_DB_NAME={self.task.get('db_name') or ''}",
            f"export PLATFORM_BACKUP_SCOPE={scope}",
            f"export PLATFORM_TABLES={shlex_quote(','.join(tables))}",
            f"export PLATFORM_BACKUP_DIR={shlex_quote(artifact_dir)}",
        ]
        if pw:
            lines.append(f"export PLATFORM_DB_PASSWORD={shlex_quote(pw)}")
        return lines

    def _custom_backup_env(self, backup_type, artifact_dir: str,
                           extra: dict, pw: str) -> list:
        return self._custom_backup_env_lines(backup_type, artifact_dir, extra, pw)

    @staticmethod
    def _custom_env_map(lines: list) -> dict:
        """把 shell 形式的 `export K='v'` 转成 subprocess 可用的 env dict。"""
        env = os.environ.copy()
        for line in lines:
            if not line.startswith("export "):
                continue
            body = line[len("export "):]
            if "=" not in body:
                continue
            k, v = body.split("=", 1)
            try:
                import shlex as _shlex
                parts = _shlex.split(v)
                v = parts[0] if parts else ""
            except Exception:
                v = v.strip("'\"")
            if k == "PATH":
                # shell 里的 $PATH 需还原为真实 PATH，否则子进程连 bash 都找不到
                base_path = os.environ.get("PATH", "")
                v = v.replace("${PATH}", base_path).replace("$PATH", base_path)
            env[k] = v
        return env

    def _backup_custom_local(self, backup_type, extra: dict) -> BackupResult:
        """自定义备份脚本的**平台本机**执行通道（数据库与平台同机，无 SSH 主机）。

        与 SSH 通道行为一致：注入相同 PLATFORM_* 变量、扫描产物目录、
        复制产物到任务输出目录并计算真实 size/sha256、生成 manifest。
        """
        import tempfile
        import time as _time

        ts = self._timestamp()
        task_id = self.task.get("id") or "x"
        script_body = str(extra.get("custom_script") or "")
        artifact_dir = (extra.get("custom_artifact_dir") or
                        f"/var/tmp/platform_backup/{task_id}/{ts}").rstrip("/")
        timeout_sec = int(extra.get("custom_timeout") or 7200)
        pw = db.decrypt_secret(self.task.get("password") or "")
        env_lines = self._custom_backup_env(backup_type, artifact_dir, extra, pw)

        try:
            os.makedirs(artifact_dir, exist_ok=True)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"无法创建产物目录 {artifact_dir}: {e}")

        fd, script_path = tempfile.mkstemp(prefix=f"platform_custom_{task_id}_",
                                           suffix=".sh")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script_body if script_body.endswith("\n") else script_body + "\n")
            os.chmod(script_path, 0o700)
            start = _time.time()
            rc, out_b, err_b, duration = _local_bash(
                script_path, self._custom_env_map(env_lines), timeout_sec)
            out_text = out_b.decode("utf-8", "replace") if isinstance(out_b, bytes) else (out_b or "")
            err_text = err_b.decode("utf-8", "replace") if isinstance(err_b, bytes) else (err_b or "")

            if rc != 0:
                snippet = (out_text or err_text)[-1500:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, duration_sec=duration,
                    stdout=out_text, stderr=err_text,
                    message=f"自定义备份脚本本机执行失败(rc={rc}): {snippet}")

            artifacts = []
            for fname in os.listdir(artifact_dir):
                p = os.path.join(artifact_dir, fname)
                if not os.path.isfile(p) or os.path.getsize(p) <= 0:
                    continue
                if os.path.getmtime(p) < start - 5:
                    continue
                artifacts.append((fname, p, os.path.getsize(p)))
            if not artifacts:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, duration_sec=duration,
                    stdout=out_text, stderr=err_text,
                    message=("自定义脚本执行成功但未产出备份文件："
                             f"请把产物写入 $PLATFORM_BACKUP_DIR（本次为 {artifact_dir}）"))

            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            local_files, total = [], 0
            for fname, src, _sz in artifacts:
                dst = os.path.join(out_dir, fname)
                shutil.copy2(src, dst)
                local_files.append((dst, os.path.getsize(dst)))
                total += os.path.getsize(dst)
            local_files.sort(key=lambda x: -x[1])
            primary = local_files[0][0]
            checksum = db.sha256_file(primary)

            manifest = os.path.join(out_dir, f"{ts}_custom_manifest.txt")
            with open(manifest, "w", encoding="utf-8") as mf:
                mf.write("Custom backup script (local channel)\n")
                mf.write(f"task: {self.task_name}\n")
                mf.write(f"backup_type: {getattr(backup_type, 'value', backup_type)}\n")
                mf.write(f"scope: {self._custom_scope_and_tables(extra)[0]}\n")
                mf.write(f"artifact_dir: {artifact_dir}\n")
                for p, sz in local_files:
                    mf.write(f"{os.path.basename(p)}\t{sz}\t{db.sha256_file(p)}\n")

            self._custom_cleanup_local(artifact_dir, extra)
            msg = (f"自定义备份脚本在平台本机执行成功（范围: {self._custom_scope_label(extra)}），"
                   f"产出 {len(local_files)} 个文件共 {db.human_size(total)}"
                   f"（主文件: {os.path.basename(primary)}）")
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                backup_path=primary, size_bytes=total,
                                duration_sec=duration, stdout=out_text, stderr=err_text,
                                simulated=False, checksum=checksum, message=msg)
        finally:
            try:
                os.unlink(script_path)
            except Exception:
                pass

    @staticmethod
    def _custom_cleanup_local(artifact_dir: str, extra: dict) -> None:
        """脚本产物留在远端/本机临时目录会持续占用磁盘，默认成功后清理。"""
        if str(extra.get("custom_cleanup") or "1") not in ("1", "true", "yes", "on"):
            return
        if extra.get("custom_artifact_dir"):
            return  # 用户指定目录：可能是归档路径，不擅自删除
        try:
            shutil.rmtree(artifact_dir, ignore_errors=True)
        except Exception:
            pass

    def _restore_custom_local(self, backup_path: str, script: str,
                              extra: dict, **kwargs) -> BackupResult:
        """自定义恢复脚本的平台本机执行通道（与 SSH 通道注入变量一致）。"""
        import tempfile
        import time as _time

        timeout_sec = int(extra.get("custom_timeout") or 7200)
        scope, tables = self._custom_scope_and_tables(extra)
        db_type = getattr(self, "db_type", "") or self.task.get("db_type") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")
        _tool = str(extra.get("tool_path") or "").strip()
        _paths = [p.strip() for p in _tool.replace(";", ":").split(":") if p.strip()]
        env_lines = ([("export PATH=" + ":".join(shlex_quote(p) for p in _paths) + ":$PATH")]
                     if _paths else []) + [
            f"export PLATFORM_BACKUP_FILE={shlex_quote(backup_path)}",
            f"export PLATFORM_RESTORE_DB={shlex_quote(str(kwargs.get('target_db') or self.task.get('db_name') or ''))}",
            f"export PLATFORM_RESTORE_SCOPE={scope}",
            f"export PLATFORM_TABLES={shlex_quote(','.join(tables))}",
            f"export PLATFORM_DB_TYPE={db_type}",
            f"export PLATFORM_TASK_ID={self.task.get('id') or ''}",
            f"export PLATFORM_DB_HOST={self.task.get('host') or ''}",
            f"export PLATFORM_DB_PORT={self.task.get('port') or ''}",
            f"export PLATFORM_DB_USER={self.task.get('username') or ''}",
            f"export PLATFORM_DB_NAME={self.task.get('db_name') or ''}",
        ]
        if pw:
            env_lines.append(f"export PLATFORM_DB_PASSWORD={shlex_quote(pw)}")

        fd, script_path = tempfile.mkstemp(prefix="platform_custom_restore_", suffix=".sh")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script if script.endswith("\n") else script + "\n")
            os.chmod(script_path, 0o700)
            rc, out_b, err_b, duration = _local_bash(
                script_path, self._custom_env_map(env_lines), timeout_sec)
            out_text = out_b.decode("utf-8", "replace") if isinstance(out_b, bytes) else (out_b or "")
            err_text = err_b.decode("utf-8", "replace") if isinstance(err_b, bytes) else (err_b or "")
            if rc != 0:
                detail = (out_text or err_text or "")[-1200:]
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    duration_sec=duration, stdout=out_text, stderr=err_text,
                                    message=f"自定义恢复脚本本机执行失败(rc={rc}): {detail}")
            msg = (f"自定义恢复脚本在平台本机执行成功（耗时 {duration}s，"
                   f"备份文件: {os.path.basename(backup_path)}）")
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                backup_path=backup_path, duration_sec=duration,
                                stdout=out_text, simulated=False, message=msg)
        finally:
            try:
                os.unlink(script_path)
            except Exception:
                pass

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
            env_lines = self._custom_backup_env(backup_type, artifact_dir, extra, pw)
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
            resumed_any = False
            for fname, fsize in artifacts:
                local_path = os.path.join(out_dir, fname)
                # 断点续传拉回：网络/隧道断开后重试从已传字节继续，脚本无需重跑
                try:
                    _r = remote_dump.sftp_pull_resumable(
                        client, f"{artifact_dir}/{fname}", local_path,
                        task=self.task, key=fname,
                        db_type=f"{self.db_type}_custom",
                        host_key=(ssh_host or {}).get("host_key", ""),
                        has_rc=False, stable_secs=3,
                        label=fname, min_size=1)
                    _lp, _sz = _r["path"], _r["size"]
                    resumed_any = resumed_any or bool(_r.get("resumed"))
                except Exception:
                    sftp.get(f"{artifact_dir}/{fname}", local_path)
                    _lp, _sz = local_path, os.path.getsize(local_path)
                local_files.append((_lp, _sz))
                total += _sz
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

            # 清理数据库服务器上的临时脚本与产物（默认开启，用户指定目录时不删）
            if (str(extra.get("custom_cleanup") or "1").lower() in ("1", "true", "yes", "on")
                    and not extra.get("custom_artifact_dir")):
                try:
                    _ssh_exec_pipe(
                        client,
                        remote_dump._wrap_login(
                            f"rm -rf {shlex_quote(artifact_dir)} {shlex_quote(remote_script)}"),
                        timeout=60)
                except Exception as e:
                    self.logger.warning("[%s] 远端临时产物清理失败（忽略）: %s",
                                        self.task_name, e)

            hk = ssh_host.get("host_key", "remote")
            msg = (f"自定义备份脚本在 {hk} 执行成功（范围: {self._custom_scope_label(extra)}），"
                   f"拉回 {len(local_files)} 个产物共 {db.human_size(total)}"
                   f"（主文件: {os.path.basename(primary)}）")
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
        ssh_host = kwargs.get("target_host_info") or remote_dump.resolve_ssh_host(self.task)
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"本地备份文件不存在: {backup_path}")

        if ssh_host:
            return self._restore_custom_remote(ssh_host, backup_path, script, extra, **kwargs)
        if _is_local_task(self.task):
            return self._restore_custom_local(backup_path, script, extra, **kwargs)
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            message="自定义恢复脚本需要 SSH 主机（恢复表单选择目标主机）")

    def _restore_custom_remote(self, ssh_host: dict, backup_path: str,
                               script: str, extra: dict, **kwargs) -> BackupResult:
        """自定义恢复脚本的实际执行（run_restore / CustomDBEngine.restore 复用）。

        步骤：SFTP 推送备份文件 → 上传脚本 → bash 执行（注入 PLATFORM_*）。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        import time as _time

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
            _scope, _tables = self._custom_scope_and_tables(extra)
            _tool = str(extra.get("tool_path") or "").strip()
            _paths = [p.strip() for p in _tool.replace(";", ":").split(":") if p.strip()]
            env_lines = ([("export PATH=" + ":".join(shlex_quote(p) for p in _paths) + ":$PATH")]
                         if _paths else []) + [
                f"export PLATFORM_BACKUP_FILE={shlex_quote(remote_file)}",
                f"export PLATFORM_RESTORE_DB={shlex_quote(str(kwargs.get('target_db') or self.task.get('db_name') or ''))}",
                f"export PLATFORM_RESTORE_SCOPE={_scope}",
                f"export PLATFORM_TABLES={shlex_quote(','.join(_tables))}",
                f"export PLATFORM_DB_TYPE={getattr(self, 'db_type', '') or self.task.get('db_type') or ''}",
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
            err_text = err or ""

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
        # 全实例 tar 产物（multi-db-tar）跨主机恢复：解包后逐库恢复，
        # 缺失的库自动创建（cross_host._build_full_instance_restore_cmd）
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
        # 把远端真实输出带回给用户：此前只返回"恢复失败(rc=1)"，看不到原因
        detail = ""
        if not res.get("ok"):
            detail = (res.get("stderr_tail") or res.get("stdout_tail") or "").strip()
        msg = res.get("message", "")
        if detail:
            msg = f"{msg} | 远端输出: {detail[-500:]}"
        result = BackupResult(
            success=res["ok"], status=BackupStatus.SUCCESS if res["ok"] else BackupStatus.FAILED,
            backup_path=backup_path, message=msg)
        result.detail_log = "远端 stdout/stderr:\n" + (
            (res.get("stdout_tail") or "") + "\n" + (res.get("stderr_tail") or ""))[:4000]
        return result

    def list_databases(self) -> List[str]:
        """可选：列出可备份的库/实例名。"""
        return []
