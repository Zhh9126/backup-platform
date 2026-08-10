# -*- coding: utf-8 -*-
"""
PostgreSQL WAL 流式捕获守护（并作为 Kingbase 等 PG 协议兼容库的复用基类）。

实现方式：``pg_receivewal``（原 pg_receivexlog）以流复制协议持续接收 WAL 段，
落盘到 ``live/``。WAL 段固定 16MB，接收中的段带 ``.partial`` 后缀，
接收完成后自动去掉后缀 —— 这给了我们一个**天然且可靠的完整性判据**：

    文件名以 .partial 结尾 → 未完成，绝不封存
    文件名为 24 位十六进制 → 已完成，可封存

可选增强：
- ``RT_PG_CREATE_SLOT=true`` 时先创建物理复制槽（``--create-slot --if-not-exists``），
  防止源库在客户端断开期间回收尚未接收的 WAL（这是 WAL 缺口的头号成因）；
- 可选依赖 ``psycopg2`` 仅用于位点查询增强，缺失时回落 ``psql`` 命令行。

不满足条件时由 :mod:`core.cdc` 工厂降级到
:class:`core.cdc.simulated.SimulatedCDCDaemon`。

------------------------------------------------------------------------
T06 钩子化重构（设计变更 CH-T06-1）
------------------------------------------------------------------------
为让 Kingbase（PG 协议兼容的国产库）能**零复制**复用本实现，把原先硬编码在
方法体内的引擎差异全部上提为「类属性 + 钩子方法」两层：

类属性（6 项）
    ``DEFAULT_PORT`` / ``DEFAULT_USER`` / ``DEFAULT_DB``
    ``RECEIVE_CLIENT_CANDIDATES`` / ``QUERY_CLIENT_CANDIDATES`` / ``PASSWORD_ENV``

钩子方法（6 个）
    ``_resolve_client()``        定位流式接收客户端可执行名
    ``check_client()``           基于候选列表的客户端可用性探测
    ``_import_driver()``         惰性导入 Python 驱动（psycopg2 / ksycopg2）
    ``_current_lsn_fallbacks()`` 当前 LSN 的 SQL 探测语句序列（按序回退）
    ``_auth_env()``              密码注入环境变量（PGPASSWORD / KINGBASE 变体）
    ``_receive_cmd()``           组装流式接收命令行

PostgreSQL 自身的**行为契约逐字不变**：候选列表只含 ``pg_receivewal``，
密码环境变量仍是 ``PGPASSWORD``，LSN 语句仍是 ``SELECT pg_current_wal_lsn()``，
命令行参数顺序与原实现完全一致。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Sequence, Tuple

import config
import core.db as db

from .base import CDCDaemon

# 完成的 WAL 段文件名：24 位十六进制
_WAL_NAME_RE = re.compile(r"^[0-9A-F]{24}$")
# 每个 WAL 段的标准大小
_WAL_SEGMENT_SIZE = 16 * 1024 * 1024


def _import_psycopg2():
    """惰性导入 ``psycopg2``（可选依赖）。

    Returns:
        ``(module | None, 原因)``。
    """
    try:
        import psycopg2  # type: ignore
        return psycopg2, ""
    except ImportError as exc:
        return None, f"psycopg2 未安装（{exc}）"
    except Exception as exc:  # pragma: no cover
        return None, f"psycopg2 导入失败: {exc}"


class PostgresWALDaemon(CDCDaemon):
    """PostgreSQL 的 WAL 持续接收守护。"""

    engine_key = "pg_wal"
    display_name = "PostgreSQL WAL 流式接收"
    required_clients = ["pg_receivewal"]
    is_simulated = False
    seal_all_immediately = False

    # ------------------------------------------------------------------
    # 引擎差异点（子类只需覆盖这 6 个类属性即可适配 PG 协议兼容库）
    # ------------------------------------------------------------------
    #: 默认端口
    DEFAULT_PORT: int = 5432
    #: 默认登录用户
    DEFAULT_USER: str = "postgres"
    #: 默认库名
    DEFAULT_DB: str = "postgres"
    #: 流式接收客户端候选（按序探测，命中即用）
    RECEIVE_CLIENT_CANDIDATES: Tuple[str, ...] = ("pg_receivewal",)
    #: 交互式 SQL 客户端候选（位点探测回退用）
    QUERY_CLIENT_CANDIDATES: Tuple[str, ...] = ("psql",)
    #: 承载密码的环境变量名（可给多个，全部注入）
    PASSWORD_ENV: Tuple[str, ...] = ("PGPASSWORD",)
    #: 位点种类（CH-T06-2：与 wal_lsn/wal_end_lsn 列配套，UI 据此加前缀）
    POSITION_KIND: str = "lsn"
    #: 位点 UI 前缀
    POSITION_LABEL: str = "LSN"
    #: 复制槽名前缀
    SLOT_PREFIX: str = "rt_slot_"
    #: 当前 LSN 的探测语句（按序回退，首个成功即返回）
    #: PostgreSQL 保持与 T05 完全一致的单条语句，不引入额外回退以免改变行为契约。
    CURRENT_LSN_SQL: Tuple[str, ...] = ("SELECT pg_current_wal_lsn()",)

    def __init__(self, task: dict, rt_config, repo, logger=None) -> None:
        super().__init__(task, rt_config, repo, logger=logger)
        self.port = int(self.task.get("port") or self.DEFAULT_PORT)
        self.username = self.task.get("username") or self.DEFAULT_USER
        self.db_name = self.task.get("db_name") or self.DEFAULT_DB
        self.slot_name: str = f"{self.SLOT_PREFIX}{self.task_id}"
        self.position.setdefault("wal_lsn", "")
        self.position.setdefault("wal_end_lsn", "")

    # ------------------------------------------------------------------
    # 钩子 1/2：客户端定位与可用性探测
    # ------------------------------------------------------------------
    @classmethod
    def _resolve_client(cls) -> Tuple[str, str]:
        """在 :attr:`RECEIVE_CLIENT_CANDIDATES` 中定位流式接收客户端。

        Returns:
            ``(可执行名, 原因)``。命中时原因为空串；未命中时可执行名为空串。
        """
        for name in cls.RECEIVE_CLIENT_CANDIDATES:
            if shutil.which(name):
                return name, ""
        return "", f"缺少客户端工具: {', '.join(cls.RECEIVE_CLIENT_CANDIDATES)}"

    @classmethod
    def _resolve_query_client(cls) -> str:
        """定位交互式 SQL 客户端（位点探测回退用）；未命中返回空串。"""
        for name in cls.QUERY_CLIENT_CANDIDATES:
            if shutil.which(name):
                return name
        return ""

    @classmethod
    def check_client(cls) -> Tuple[bool, str]:
        """探测流式接收客户端是否可用（钩子化，替代基类的 required_clients 扫描）。

        Returns:
            ``(可用, 原因)``。对 PostgreSQL 而言候选列表只含 ``pg_receivewal``，
            与基类实现行为完全一致。
        """
        name, reason = cls._resolve_client()
        if not name:
            return False, reason
        return True, ""

    # ------------------------------------------------------------------
    # 钩子 3：Python 驱动惰性导入
    # ------------------------------------------------------------------
    @classmethod
    def _import_driver(cls):
        """惰性导入 Python 驱动（可选依赖，仅用于位点查询增强）。

        Returns:
            ``(module | None, 原因)``。
        """
        return _import_psycopg2()

    # ------------------------------------------------------------------
    # 钩子 4：当前 LSN 的 SQL 探测语句序列
    # ------------------------------------------------------------------
    @classmethod
    def _current_lsn_fallbacks(cls) -> Sequence[str]:
        """返回按序回退的「当前 LSN」查询语句。"""
        return cls.CURRENT_LSN_SQL

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    @classmethod
    def is_available(cls, task: dict) -> Tuple[bool, str]:
        """接收客户端可用 + 主机信息齐全时才可用。"""
        ok, reason = cls.check_client()
        if not ok:
            return False, reason
        if not (task or {}).get("host"):
            return False, "任务未配置数据库主机"
        return True, ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """（可选）创建复制槽，然后拉起流式接收客户端。"""
        state = {}
        try:
            state = self.repo.load_state() or {}
        except Exception:
            state = {}
        self.resume_from(state)

        if config.RT_PG_CREATE_SLOT:
            self._ensure_slot()

        cmd = self._receive_cmd()
        if not cmd:
            self.last_error = self._resolve_client()[1] or "接收客户端不可用"
            return False

        proc = self._popen(cmd, cwd=self.repo.live_dir(), env=self._auth_env())
        if proc is None:
            return False
        self.last_error = ""
        db.add_log("info", "rt.cdc",
                   f"任务 {self.task_name} 启动 WAL 流式接收"
                   f"（槽 {self.slot_name if config.RT_PG_CREATE_SLOT else '未使用'}）")
        return True

    # ------------------------------------------------------------------
    # 钩子 5：命令行组装
    # ------------------------------------------------------------------
    def _receive_cmd(self) -> List[str]:
        """组装流式接收命令行；客户端缺失时返回空列表。"""
        client, _reason = self._resolve_client()
        if not client:
            return []
        cmd = [
            client,
            "--directory", self.repo.live_dir(),
            "--host", str(self.host),
            "--port", str(self.port),
            "--username", str(self.username),
            "--no-password",
            "--no-loop",
        ]
        if config.RT_PG_CREATE_SLOT:
            cmd += ["--slot", self.slot_name]
        return cmd

    def _create_slot_cmd(self) -> List[str]:
        """组装物理复制槽创建命令；客户端缺失时返回空列表。"""
        client, _reason = self._resolve_client()
        if not client:
            return []
        return [
            client, "--create-slot", "--if-not-exists",
            "--slot", self.slot_name,
            "--host", str(self.host), "--port", str(self.port),
            "--username", str(self.username), "--no-password",
            "--directory", self.repo.live_dir(),
        ]

    # ------------------------------------------------------------------
    # 钩子 6：密码注入
    # ------------------------------------------------------------------
    def _auth_env(self) -> dict:
        """密码走环境变量，不进命令行（共享知识 #16：日志/命令行不落明文）。"""
        env = os.environ.copy()
        if self.password:
            for key in self.PASSWORD_ENV:
                env[key] = str(self.password)
        return env

    def _ensure_slot(self) -> None:
        """创建物理复制槽（幂等，失败只记录不阻断）。"""
        if config.DEMO_MODE == "on" or self.task.get("demo_only"):
            return
        cmd = self._create_slot_cmd()
        if not cmd:
            return
        try:
            out = subprocess.run(cmd, env=self._auth_env(), capture_output=True,
                                 text=True, timeout=20)
            if out.returncode == 0:
                self.logger.info("[rt.cdc] task=%s 复制槽 %s 就绪",
                                 self.task_id, self.slot_name)
            else:
                self.degrade_reason = (
                    f"复制槽创建失败（{(out.stderr or '').strip()[:120]}），"
                    f"源库回收 WAL 时可能产生缺口")
                self.logger.warning("[rt.cdc] task=%s %s", self.task_id,
                                    self.degrade_reason)
        except (OSError, subprocess.SubprocessError) as exc:
            self.degrade_reason = f"复制槽创建异常: {exc}"
            self.logger.warning("[rt.cdc] task=%s %s", self.task_id,
                                self.degrade_reason)

    # ------------------------------------------------------------------
    # 封存：.partial 段绝不封存
    # ------------------------------------------------------------------
    def seal_ready_segments(self, force: bool = False) -> list:
        """只封存**已完成**的 WAL 段（文件名为 24 位十六进制）。

        ``force=True``（停机场景）时也不会封存 ``.partial`` 段——半截 WAL
        无法参与恢复，入 journal 只会制造假可用性。
        """
        live = self.repo.live_dir()
        try:
            names = sorted(name for name in os.listdir(live)
                           if not name.startswith("."))
        except OSError:
            return []

        sealed = []
        for name in names:
            if not _WAL_NAME_RE.match(name):
                continue  # .partial / .history / 临时文件一律跳过
            path = os.path.join(live, name)
            if not os.path.isfile(path):
                continue
            try:
                info = self.repo.seal(path, kind="db-log")
            except Exception as exc:
                self.logger.warning("[rt.cdc] task=%s 封存 WAL %s 失败: %s",
                                    self.task_id, name, exc)
                continue
            if not info:
                continue
            info["position"] = self._position_for_segment(info)
            self.segments_sealed += 1
            self.bytes_sealed += int(info.get("size") or 0)
            self.position["sealed_at"] = info.get("sealed_at") or db.now_iso()
            self.position["last_segment"] = name
            sealed.append(info)
            self.logger.info("[rt.cdc] task=%s 封存 WAL 段 %s (%s)",
                             self.task_id, name,
                             db.human_size(int(info.get("size") or 0)))
        if sealed:
            try:
                self.repo.save_state(self.state())
            except Exception:
                pass
        return sealed

    def _position_for_segment(self, info: dict) -> dict:
        """由 WAL 段文件名推导起止 LSN。

        段名格式 ``TTTTTTTT XXXXXXXX YYYYYYYY``（各 8 位十六进制）：
        时间线 / 逻辑日志号 / 段号。段起始 LSN = ``X/Y*16MB``。
        """
        name = info.get("name") or ""
        start_lsn = self._name_to_lsn(name)
        end_lsn = self._shift_lsn(start_lsn, _WAL_SEGMENT_SIZE)
        pos = {"wal_lsn": start_lsn, "wal_end_lsn": end_lsn}
        self.position.update({"wal_lsn": end_lsn, "wal_end_lsn": end_lsn})
        return pos

    @staticmethod
    def _name_to_lsn(name: str) -> str:
        """WAL 段名 → 起始 LSN 字符串；不可解析返回空串。"""
        if not _WAL_NAME_RE.match(name or ""):
            return ""
        try:
            logical = int(name[8:16], 16)
            segment = int(name[16:24], 16)
            return f"{logical:X}/{segment * _WAL_SEGMENT_SIZE:X}"
        except ValueError:
            return ""

    @staticmethod
    def _shift_lsn(lsn: str, delta: int) -> str:
        """LSN 加上字节偏移；不可解析时原样返回。"""
        if not lsn:
            return ""
        try:
            high, low = lsn.split("/", 1)
            value = (int(high, 16) << 32) + int(low, 16) + int(delta)
            return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"
        except (ValueError, AttributeError):
            return lsn

    # ------------------------------------------------------------------
    # 位点
    # ------------------------------------------------------------------
    def source_position(self) -> dict:
        """查询源库当前 WAL LSN。Python 驱动优先，回落命令行客户端。"""
        if config.DEMO_MODE == "on" or self.task.get("demo_only"):
            return {}
        lsn = self._source_lsn_via_driver()
        if lsn:
            return {"wal_lsn": lsn}
        lsn = self._source_lsn_via_cli()
        if lsn:
            return {"wal_lsn": lsn}
        return {}

    def _source_lsn_via_driver(self) -> str:
        """用 Python 驱动查询当前 LSN；任何失败都返回空串（绝不抛出）。"""
        driver, _reason = self._import_driver()
        if driver is None:
            return ""
        try:
            conn = driver.connect(
                host=self.host, port=self.port, user=self.username,
                password=self.password, dbname=self.db_name,
                connect_timeout=5)
        except Exception as exc:
            self.logger.debug("[rt.cdc] task=%s 驱动连接失败: %s",
                              self.task_id, exc)
            return ""
        try:
            for sql in self._current_lsn_fallbacks():
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                        row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0])
                except Exception:
                    # 该语句在本引擎/版本上不存在，尝试下一条
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return ""

    def _source_lsn_via_cli(self) -> str:
        """用命令行客户端查询当前 LSN；任何失败都返回空串（绝不抛出）。"""
        client = self._resolve_query_client()
        if not client:
            return ""
        for sql in self._current_lsn_fallbacks():
            cmd = [client, "-h", str(self.host), "-p", str(self.port),
                   "-U", str(self.username), "-d", str(self.db_name),
                   "-tAc", f"{sql};"]
            try:
                out = subprocess.run(cmd, env=self._auth_env(),
                                     capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                return ""
            if out.returncode == 0 and (out.stdout or "").strip():
                return out.stdout.strip()
        return ""

    def lag_seconds(self) -> int:
        """用「源端 LSN − 已接收 LSN」的字节差换算成秒（按段接收速率估算）。"""
        src = self.source_position()
        cur = self.position.get("wal_end_lsn") or ""
        if not src.get("wal_lsn") or not cur:
            return super().lag_seconds()
        delta = self._lsn_to_int(src["wal_lsn"]) - self._lsn_to_int(cur)
        if delta <= 0:
            return 0
        segments_behind = delta / float(_WAL_SEGMENT_SIZE)
        return int(segments_behind * max(10, int(config.RT_DB_SEAL_INTERVAL_SEC)))

    @staticmethod
    def _lsn_to_int(lsn: str) -> int:
        """``0/1A2B3C48`` → int；不可解析返回 0。"""
        try:
            high, low = str(lsn).split("/", 1)
            return (int(high, 16) << 32) + int(low, 16)
        except (ValueError, AttributeError):
            return 0

    # ------------------------------------------------------------------
    def stop(self, timeout: float = 10.0) -> None:
        """停止接收。``.partial`` 段保留在 live/，下次启动继续追加。"""
        self._kill(timeout=timeout)
        try:
            self.seal_ready_segments()
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 停止时封存异常: %s",
                                self.task_id, exc)
        self._close_stderr()

    def describe(self) -> dict:
        """守护自述，补充位点种类（CH-T06-2：UI 据此渲染 LSN/SCN 前缀）。"""
        info = super().describe()
        info["position_kind"] = self.POSITION_KIND
        info["position_label"] = self.POSITION_LABEL
        return info

    def probe_psycopg2(self) -> dict:
        """探测可选依赖 ``psycopg2``（自检面板用）。"""
        module, reason = self._import_driver()
        return {"installed": module is not None, "reason": reason,
                "hint": "" if module else "pip install psycopg2-binary 可启用精确 LSN 探测"}
