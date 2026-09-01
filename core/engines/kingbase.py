# -*- coding: utf-8 -*-
"""
Kingbase 人大金仓 备份/恢复引擎。

KingbaseES 由人大金仓开发，兼容 PostgreSQL 协议，自带 sys_dump /
sys_restore / ksql 三件套，用法分别与 pg_dump / pg_restore / psql 一致。

本引擎仅依赖 Python 标准库，并复用基类的命令执行、仿真兜底、输出目录
管理等通用能力。真实备份通过调用上述外部客户端完成；当平台处于演示模式
或客户端缺失时，自动回退到仿真占位备份，保证平台可运行、可演示。

要求：
- 不 import 任何第三方库。
- 密码不出现在进程参数中，统一通过环境变量 PGPASSWORD 注入（sys_dump 等
  兼容 PostgreSQL 客户端鉴权，读取 PGPASSWORD）。
"""
import os
import time
import shlex
import shutil
import subprocess

import config
import core.db as db
from core.engines.base import (
    BackupEngine,
    BackupType,
    BackupMode,
    BackupStatus,
    BackupResult,
)


class KingbaseEngine(BackupEngine):
    """Kingbase 人大金仓 备份/恢复引擎。"""

    db_type = "kingbase"
    display_name = "kingbase"
    required_clients = ["sys_dump", "sys_restore", "ksql"]
    # 物理备份：Kingbase 自带 sys_basebackup 工具
    physical_bundled_tools = ["sys_basebackup"]
    # 远端工具探测用户：金仓客户端工具在 kingbase 用户 profile PATH 中
    tool_check_user = "kingbase"

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _password(self) -> str:
        """返回明文密码（task 中已是解密后的明文，decrypt_secret 对明文原样返回）。"""
        return db.decrypt_secret(self.task.get("password") or "")

    def _host(self) -> str:
        return self.task.get("host") or "127.0.0.1"

    def _port(self) -> int:
        # task 关键字段说明：port 为 int，默认值 54321
        return int(self.task.get("port") or 54321)

    def _user(self) -> str:
        return self.task.get("username") or ""

    def _db_name(self) -> str:
        return self.task.get("db_name") or ""

    def _compress(self) -> int:
        """compress 取值 0/1，默认 1（开启压缩）。"""
        return int(self.task.get("compress") or 1)

    def _env_with_pwd(self) -> dict:
        """构造注入密码的环境变量，避免密码出现在命令行参数中。

        V8 兼容 PostgreSQL 的 PGPASSWORD；V9 起改用 KINGBASE_PASSWORD，
        两个同时注入，兼容新旧版本共存环境。
        另注入任务级 tool_path 到 PATH 前缀（自动探测失败时的手动兜底）。
        """
        env = {"PGPASSWORD": self._password(),
               "KINGBASE_PASSWORD": self._password()}
        tp = self._task_tool_path()
        if tp:
            env["PATH"] = tp + os.pathsep + os.environ.get("PATH", "")
        return env

    def _compute_size_and_checksum(self, path: str):
        """计算备份产物的大小与校验和。

        - 文件：使用 db.sha256_file 计算校验和，大小为文件字节数。
        - 目录：递归累加所有文件字节数，校验和留空（目录无单一摘要）。
        """
        if os.path.isfile(path):
            try:
                checksum = db.sha256_file(path)
            except Exception:
                checksum = ""
            try:
                size = os.path.getsize(path)
            except Exception:
                size = 0
            return size, checksum

        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total += os.path.getsize(fp)
                    except Exception:
                        pass
            return total, ""
        return 0, ""

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        # 按备份模式分发
        if self.backup_mode == BackupMode.PHYSICAL:
            # 物理备份：优先 SSH 远端 sys_basebackup，失败再回退本机
            return self._try_remote_then_local(
                lambda ssh_host: self._backup_physical_remote(ssh_host, backup_type),
                lambda: self._backup_physical(backup_type),
                "Kingbase 物理备份(sys_basebackup)",
            )
        # 逻辑备份：优先 SSH 远程执行，失败再回退本机
        return self._try_remote_then_local(
            lambda ssh_host: self._backup_logical_remote(ssh_host, backup_type),
            lambda: self._backup_logical_local(backup_type),
            "Kingbase 逻辑备份(sys_dump)",
        )

    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        """物理备份：sys_basebackup。"""
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        target = os.path.join(out_dir, f"sys_basebackup_{ts}")
        cmd = ["sys_basebackup", "-D", target, "-Ft", "-z", "--checkpoint=fast"]
        start = time.time()
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        dur = round(time.time()-start, 3)
        if ret.returncode != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"sys_basebackup 失败: {ret.stderr[:500]}")
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=target, duration_sec=dur,
                            message="Kingbase 物理备份(sys_basebackup)成功")

    def _backup_physical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """物理备份：通过 SSH 在远端 KingbaseES 服务器执行 sys_basebackup（-Ft -z
        生成 tar.gz），再经 SFTP 拉回本机落盘并计算 size/sha256。

        复用 core.remote_dump.remote_physical_backup，避免路径/端口写死；
        Kingbase 客户端兼容 PG 鉴权（读取 PGPASSWORD）。
        """
        from core import remote_dump
        client = remote_dump._connect(ssh_host)
        res = remote_dump.remote_physical_backup(
            self.task, ssh_host,
            tool="sys_basebackup", default_port=54321, default_user="system",
            extra_args_key="sys_basebackup_extra_args", tool_label="sys_basebackup",
            check_user="kingbase",
        )
        if not res["ok"]:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=res["message"],
                stdout=res.get("stdout", ""), stderr=res.get("stderr", ""))

        out_dir = self._output_dir()
        pieces = remote_dump._pull_remote_tars(client, res["remote_dir"], out_dir)
        if not pieces:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                stdout=res.get("stdout", ""), stderr=res.get("stderr", ""),
                message=f"远端 sys_basebackup 执行成功但未在 {res['remote_dir']} 找到 *.tar[.gz] 产物。")

        total_size = sum(sz for _, sz in pieces)
        first_local = pieces[0][0]
        checksum = db.sha256_file(first_local)
        hk = ssh_host.get("host_key", "remote")
        msg = (f"通过 SSH 在 {hk} 执行 sys_basebackup 物理备份成功，"
               f"已拉回 {len(pieces)} 个 tar 包，共 {db.human_size(total_size)}"
               f"（主包: {os.path.basename(first_local)}）")
        self.logger.info("[%s] %s", self.task_name, msg)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=first_local, size_bytes=total_size,
            duration_sec=0, stdout=res.get("stdout", ""), stderr=res.get("stderr", ""),
            simulated=False, checksum=checksum, message=msg)

    def _backup_logical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机/数据库服务器上执行 sys_dump，把流拉回到本地落盘。"""
        from core import remote_dump
        comp = int(self.task.get("compress") or 0)
        data, compressed, fmt = remote_dump.remote_db_dump(self.task, ssh_host, "kingbase", comp)
        if fmt == "multi-db-tar":
            # 全实例：逐库 tar.gz（远端已 gzip，manifest.json 标注库清单）
            res = self._write_dump_file(data, backup_type, ssh_host, ".tar.gz", "sys_dump")
            res.compress_algo = "gzip"
            return res
        if fmt == "dumpall":
            # 整实例 SQL 流（extra.all_db_mode="dumpall"）
            res = self._write_dump_file(data, backup_type, ssh_host, ".sql", "sys_dumpall")
            res.compress_algo = "none"
            return res
        # 单库：远程 sys_dump 用 -Fc 自带压缩，落盘为 .dump（不再外挂 gzip）
        ext = ".dump" if compressed else ".sql"
        res = self._write_dump_file(data, backup_type, ssh_host, ext, "sys_dump")
        res.compress_algo = "zlib" if compressed else "none"
        return res

    def _backup_logical_local(self, backup_type: BackupType) -> BackupResult:
        """逻辑备份：sys_dump（沿用原有实现）。"""
        # 客户端探测
        ok, detail = self.check_client()
        if not ok:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                simulated=False,
                message="客户端检查失败: " + detail,
            )

        # 2.5) 全实例（勾选全部库或库名为空）：sys_dump 是单库工具，不存在
        #      --all-databases 参数，改为逐库 tar.gz + globals + manifest
        extra = self._parse_task_extra()
        if ((extra.get("use_all_db") or not self._db_name())
                and not extra.get("schemas") and not extra.get("tables")):
            return self._backup_full_instance_local(backup_type)

        # 3) Kingbase 仅支持逻辑全量（custom/纯文本）。
        #    incremental / differential 在逻辑层无法原生实现，统一回退到 full，
        #    并在 message 中说明建议改用 WAL 归档 / 物理备份方案。
        if backup_type not in (BackupType.FULL,):
            fallback_msg = (
                "Kingbase 逻辑备份不支持 %s；已回退为全量(full)备份。"
                "若需增量/差异，请改用 WAL 归档 或 物理备份方案。"
                % backup_type.value
            )
            self.logger.warning("[%s] %s", self.task_name, fallback_msg)
        else:
            fallback_msg = ""

        # 4) 构造输出路径与命令
        out_dir = self._output_dir()
        ts = self._timestamp()
        host, port, user, dbname = (
            self._host(), self._port(), self._user(), self._db_name()
        )

        # 是否关闭压缩：compress==0 时使用 -Fp 纯文本，否则 -Fc custom 格式（自带压缩）
        if self._compress() == 0:
            out_path = os.path.join(out_dir, "%s.sql" % ts)
            cmd = [
                "sys_dump",
                "--host", host,
                "--port", str(port),
                "--username", user,
                "-d", dbname,
                "-Fp",              # 纯文本格式（不压缩）
                "-f", out_path,
            ]
        else:
            out_path = os.path.join(out_dir, "%s.dump" % ts)
            cmd = [
                "sys_dump",
                "--host", host,
                "--port", str(port),
                "--username", user,
                "-d", dbname,
                "-Fc",              # custom 格式（自带压缩）
                "-f", out_path,
            ]

        # 5) 执行
        start = time.time()
        res = self._run(cmd, env_extra=self._env_with_pwd())
        duration = round(time.time() - start, 3)

        # 6) 判定结果
        if res["returncode"] != 0:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=None,
                duration_sec=duration,
                stdout=res["stdout"],
                stderr=res["stderr"],
                simulated=False,
                message="sys_dump 执行失败 (returncode=%s): %s"
                        % (res["returncode"], fallback_msg),
            )

        # 7) 计算大小与校验和
        size, checksum = self._compute_size_and_checksum(out_path)

        message = "Kingbase 全量备份成功: %s" % out_path
        if fallback_msg:
            message = fallback_msg + " " + message

        return BackupResult(
            success=True,
            status=BackupStatus.SUCCESS,
            backup_path=out_path,
            size_bytes=size,
            duration_sec=duration,
            stdout=res["stdout"],
            stderr=res["stderr"],
            simulated=False,
            checksum=checksum,
            message=message,
        )

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 跨主机恢复
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            return self._try_cross_host_restore(backup_path, target_host_info,
                                                 kwargs.get("target_db") or "")

        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                simulated=False,
                message="恢复失败：备份文件不存在: %s" % backup_path,
            )

        # 远程优先：经 SSH 在数据库服务器恢复（与备份对称，工具自动发现）
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if ssh_host:
            try:
                with open(backup_path, "rb") as f:
                    dump_bytes = f.read()
                is_custom = backup_path.endswith(".dump")
                remote_dump.remote_db_restore(
                    self.task, ssh_host, "kingbase", dump_bytes,
                    is_custom=is_custom)
                hk = ssh_host.get("host_key", "remote")
                target_db = kwargs.get("target_db") or self._db_name()
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    backup_path=backup_path,
                    message=f"通过 SSH 在数据库服务器({hk})恢复成功"
                            f"{f'，目标库: {target_db}' if target_db else ''}")
            except Exception as e:
                # 远程失败回退本机执行（本机有客户端时）
                self.logger.warning(
                    "[%s] 远程恢复失败，回退本机: %s", self.task_name, e)

        # 本机回退：客户端探测
        ok, detail = self.check_client()
        if not ok:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                simulated=False,
                message="客户端检查失败: " + detail,
            )

        # 目标库：优先 kwargs 指定的 target_db，否则使用任务原库名
        target_db = kwargs.get("target_db") or self._db_name()
        host, port, user = self._host(), self._port(), self._user()

        # 3) 根据文件后缀选择恢复工具
        if backup_path.endswith((".tar.gz", ".tgz")):
            return self._restore_full_instance_local(backup_path)
        if backup_path.endswith(".dump"):
            # custom 格式 -> sys_restore（-c 清理已存在对象，-C 创建目标库）
            cmd = [
                "sys_restore",
                "--host", host,
                "--port", str(port),
                "--username", user,
                "--dbname", target_db,
                "-c", "-C",
                backup_path,
            ]
        elif backup_path.endswith(".sql"):
            # 纯文本格式 -> ksql 执行 SQL 脚本（-f 指定脚本）
            cmd = [
                "ksql",
                "--host", host,
                "--port", str(port),
                "--username", user,
                "-d", target_db,
                "-f", backup_path,
            ]
        else:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                simulated=False,
                message="恢复失败：无法识别的备份文件类型(需 .dump 或 .sql): %s"
                        % backup_path,
            )

        # 4) 执行
        start = time.time()
        res = self._run(cmd, env_extra=self._env_with_pwd())
        duration = round(time.time() - start, 3)

        if res["returncode"] != 0:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                duration_sec=duration,
                stdout=res["stdout"],
                stderr=res["stderr"],
                simulated=False,
                message="恢复失败 (returncode=%s)" % res["returncode"],
            )

        return BackupResult(
            success=True,
            status=BackupStatus.SUCCESS,
            backup_path=backup_path,
            duration_sec=duration,
            stdout=res["stdout"],
            stderr=res["stderr"],
            simulated=False,
            checksum="",
            message="Kingbase 恢复成功: 目标库=%s, 来源=%s" % (target_db, backup_path),
        )

    # ------------------------------------------------------------------
    # 全实例（逐库 tar）备份/恢复 —— db_name 为空时的路径
    # ------------------------------------------------------------------
    def _backup_full_instance_local(self, backup_type: BackupType) -> BackupResult:
        """全实例逻辑备份：枚举库 → 逐库 sys_dump + sys_dumpall 全局对象 → tar.gz。

        PG 系没有 --all-databases；逐库快照各自一致，globals 单独导出。
        """
        from core import logical_full
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{self._timestamp()}.tar.gz")
        dump_tool = self._resolve_local_tool("sys_dump")
        query_tool = self._resolve_local_tool("ksql", "sys_psql", "psql")
        dumpall_tool = (shutil.which("sys_dumpall") or shutil.which("kb_dumpall")
                        or shutil.which("ksy_dumpall")
                        or shutil.which("pg_dumpall") or "")
        try:
            manifest = logical_full.backup_full_instance(
                "kingbase",
                host=self._host(), port=self._port(), user=self._user(),
                password=db.decrypt_secret(self.task.get("password") or ""),
                dump_tool=dump_tool, out_path=out_path,
                query_tool=query_tool, dumpall_tool=dumpall_tool,
                include_system_dbs=bool(extra.get("include_system_dbs")))
        except Exception as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=None, simulated=False,
                message=f"Kingbase 全实例备份失败: {e}")
        size, checksum = self._compute_size_and_checksum(out_path)
        dbs_txt = ", ".join(manifest.get("databases") or [])
        msg = (f"Kingbase 全实例备份成功: {len(manifest['databases'])} 个库"
               f"（{dbs_txt}）+ 全局对象({manifest.get('globals')})，"
               f"产物 {out_path}")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=out_path, size_bytes=size, duration_sec=0,
            simulated=False, checksum=checksum, message=msg)

    def _restore_full_instance_local(self, backup_path: str) -> BackupResult:
        """全实例恢复：解包 → globals → 缺失库自动建库 → 逐库 sys_restore。"""
        from core import logical_full
        restore_tool = self._resolve_local_tool("sys_restore")
        query_tool = self._resolve_local_tool("ksql", "sys_psql", "psql")
        try:
            result = logical_full.restore_full_instance(
                "kingbase",
                host=self._host(), port=self._port(), user=self._user(),
                password=db.decrypt_secret(self.task.get("password") or ""),
                backup_path=backup_path,
                restore_tool=restore_tool, query_tool=query_tool)
        except Exception as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, simulated=False,
                message=f"Kingbase 全实例恢复失败: {e}")
        dbs_txt = ", ".join(result.get("restored") or [])
        msg = (f"Kingbase 全实例恢复成功: {len(result['restored'])} 个库"
               f"（{dbs_txt}），全局对象{'已恢复' if result.get('globals') else '跳过/已存在'}")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=backup_path, duration_sec=0,
            simulated=False, checksum="", message=msg)

    # ------------------------------------------------------------------
    # 列出可备份的数据库
    # ------------------------------------------------------------------
    def list_databases(self) -> list:
        if self.task.get("demo_only") or config.DEMO_MODE == "on":
            return []
        ok, _ = self.check_client()
        if not ok:
            return []

        host, port, user = self._host(), self._port(), self._user()
        cmd = [
            "ksql",
            "--host", host,
            "--port", str(port),
            "--username", user,
            "-t",                                   # 不显示表头/边框
            "-c", "SELECT datname FROM sys_database WHERE NOT datistemplate",
        ]
        res = self._run(cmd, env_extra=self._env_with_pwd())
        if res["returncode"] != 0:
            self.logger.warning(
                "[%s] 列举数据库失败: %s", self.task_name, res["stderr"])
            return []

        # 解析 stdout：逐行取非空、去除首尾空白
        dbs = []
        for line in res["stdout"].splitlines():
            name = line.strip()
            if name:
                dbs.append(name)
        return dbs
