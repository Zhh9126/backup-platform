# -*- coding: utf-8 -*-
"""
DM 达梦（Dameng）逻辑备份/恢复引擎。

本文件实现 DamengEngine，基于达梦官方逻辑导出/导入工具 dexp / dimp 完成
备份（backup）与恢复（restore）。仅依赖 Python 标准库与达梦客户端可执行文件，
不引入任何第三方包。

================== 达梦 dexp / dimp 前置条件 ==================
1. 必须在执行节点安装 DM 数据库客户端（如 DM8），dexp 与 dimp 位于客户端
   安装目录的 bin 子目录下（例如 $DM_HOME/bin/dexp、bin/dimp）。
2. 需将 dexp、dimp 所在目录加入系统 PATH，或确保 `shutil.which("dexp")`
   / `shutil.which("dimp")` 能够探测到，否则引擎将走仿真兜底。
3. 连接目标库需要数据库用户名、密码、主机与端口（达梦默认端口 5236）。
   达梦逻辑导出工具的惯例是将密码直接写在 USERID 参数中，形如:
       USERID=用户名/密码@主机:端口
   即密码随命令行参数传递（而非独立 -p 选项）。这是达梦 dexp/dimp 的固定
   语法，平台已在 message 中明确说明，便于运维知悉此行为。
4. 本引擎执行的是“逻辑导出”（dexp 生成 .dmp 逻辑转储文件），属于逻辑备份，
   与物理备份工具 dmrman 不同。达梦 dexp 本身不支持真正的增量/差异逻辑导出，
   因此本引擎对 incremental / differential 类型回退为 full，并在 message
   中提示应使用 dmrman 做物理增量备份。
==============================================================
"""

import os
import time
import json
import shlex

import config
import core.db as db
from core.engines.base import (
    BackupEngine,
    BackupType,
    BackupMode,
    BackupStatus,
    BackupResult,
)


class DamengEngine(BackupEngine):
    """达梦 DM 逻辑备份引擎，基于 dexp / dimp。"""

    db_type = "dameng"
    display_name = "DM 达梦"
    # dexp 负责导出（备份），dimp 负责导入（恢复）
    required_clients = ["dexp", "dimp"]
    # 物理备份：达梦自带 dmrman 工具
    physical_bundled_tools = ["dmrman"]

    # ---------------- 内部辅助 ----------------
    def _parse_extra(self) -> dict:
        """解析 task 中的 extra_options（JSON 字符串），返回字典。

        可能包含:
            schemas: 需要导出的模式列表或逗号字符串，如 ["SYSDBA","TEST"] 或 "SYSDBA,TEST"
            owner:   按属主（用户）导出，字符串
        解析失败时返回空字典，不影响主流程。
        """
        raw = self.task.get("extra_options") or ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _connect_userid(self) -> str:
        """构造达梦 dexp/dimp 的连接串 USERID=用户/密码@主机:端口。

        注意：按达梦惯例，密码直接嵌入 USERID（明文出现在命令行参数），
        这是 dexp/dimp 的连接语法要求。
        """
        user = self.task.get("username") or ""
        pw = self.task.get("password") or ""  # task 中密码为已解密明文
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 5236
        return "{user}/{pw}@{host}:{port}".format(
            user=user, pw=pw, host=host, port=port
        )

    def _scope_args(self, extra: dict, target_db: str = None) -> list:
        """根据范围参数生成 dexp/dimp 的限定参数。

        优先级:
            1. 恢复时若传入 target_db，则作为目标 schema 映射到 SCHEMAS。
            2. 否则若 extra_options 指定了 schemas，则使用 SCHEMAS=...。
            3. 否则若 extra_options 指定了 owner，则使用 OWNER=...。
            4. 否则使用 FULL=Y（整库导出/导入）。
        """
        # 目标库映射到 SCHEMAS（恢复场景）
        if target_db:
            return ["SCHEMAS={0}".format(target_db)]

        schemas = extra.get("schemas")
        if schemas:
            # 兼容 list 与逗号字符串两种写法
            if isinstance(schemas, (list, tuple)):
                schemas_str = ",".join(str(s) for s in schemas)
            else:
                schemas_str = str(schemas)
            return ["SCHEMAS={0}".format(schemas_str)]

        owner = extra.get("owner")
        if owner:
            return ["OWNER={0}".format(owner)]

        return ["FULL=Y"]

    # ---------------- 备份 ----------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        """达梦备份：按 backup_mode 分发物理(dmrman)/逻辑(dexp)。

        逻辑备份优先在 SSH 备份机/数据库服务器执行 dexp，失败再回退本机。
        """
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        if self.backup_mode == BackupMode.PHYSICAL:
            # 物理备份：优先 SSH 远端 dmrman，失败再回退本机
            return self._try_remote_then_local(
                lambda ssh_host: self._backup_physical_remote(ssh_host, backup_type),
                lambda: self._backup_physical(backup_type),
                "达梦 物理备份(dmrman)",
            )
        return self._try_remote_then_local(
            lambda ssh_host: self._backup_logical_remote(ssh_host, backup_type),
            lambda: self._backup_logical_local(backup_type),
            "达梦 逻辑备份(dexp)",
        )

    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        """物理备份：dmrman。"""
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        script = os.path.join(out_dir, f"dmrman_{ts}.cmd")
        with open(script, "w") as f:
            f.write(f"BACKUP DATABASE FULL TO {out_dir};\nexit;\n")
        start = time.time()
        ret = self._run(["dmrman", f"CTLFILE={script}"], timeout=7200)
        dur = round(time.time()-start, 3)
        if ret["returncode"] != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"dmrman 物理备份失败: {ret.get('stderr','')[:500]}")
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=out_dir, duration_sec=dur,
                            message="达梦 物理备份(dmrman)成功")

    def _backup_physical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """物理备份：通过 SSH 在远端达梦服务器以 dmdba 用户执行 dmrman 联机全量
        备份，再把备份集打成 tar.gz 经 SFTP 拉回本机落盘并计算 size/sha256。

        - dmrman 位于 dmdba 的 DM_HOME/bin 下，故用 `su - dmdba -c`（login shell
          自动加载 dmdba profile 获得 PATH），失败再回退为直接执行。
        - 备份目录置于 /home/dmdba 下并 chown 给 dmdba，避免权限拒绝。
        - 无达梦测试机，本方法仅保证代码正确性与逻辑一致，未做 E2E。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ts = self._timestamp()
        client = remote_dump._connect(ssh_host)
        sftp = client.open_sftp()
        remote_dir = f"/home/dmdba/dm_bkp_{ts}"
        remote_tar = f"{remote_dir}.tar.gz"
        remote_script = f"/home/dmdba/dmrman_{ts}.cmd"
        # dmrman 联机全量备份脚本（TO 后为远端备份目录）
        script_body = f"BACKUP DATABASE FULL TO '{remote_dir}';\nexit;\n"

        try:
            # 1) 建目录并 chown 给 dmdba，避免 dmrman 以 dmdba 运行时权限拒绝
            prep = (f"mkdir -p {remote_dir} && chown dmdba {remote_dir} "
                    f"&& chmod 755 {remote_dir}")
            _ssh_exec_pipe(client, remote_dump._wrap_login(prep), timeout=60)

            # 2) 写 dmrman 脚本（644，dmdba 可读）
            with sftp.open(remote_script, "w") as f:
                f.write(script_body)
            try:
                sftp.chmod(remote_script, 0o644)
            except Exception:
                pass

            # 3) 优先以 dmdba 用户执行；失败回退为直接以当前 SSH 用户执行
            inner = f"dmrman CTLFILE={remote_script}"
            shell = f"su - dmdba -c {shlex.quote(inner)}"
            start = time.time()
            out, err, rc = _ssh_exec_pipe(client, remote_dump._wrap_login(shell), timeout=7200)
            if rc != 0:
                self.logger.warning("[%s] su - dmdba 执行 dmrman 失败(rc=%s)，回退直接执行",
                                    self.task_name, rc)
                out, err, rc = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(inner), timeout=7200)
            duration = round(time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            self.logger.info("[%s] 远端 dmrman 返回 rc=%s", self.task_name, rc)

            if rc != 0:
                snippet = (out_text or err)[-1200:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err,
                    message=f"远端 dmrman 物理备份失败(rc={rc}): {snippet}")

            # 4) 把远端备份集打成 tar.gz，再经 SFTP 拉回本机
            tar_cmd = f"tar czf {remote_tar} -C {remote_dir} ."
            out2, err2, rc2 = _ssh_exec_pipe(
                client, remote_dump._wrap_login(tar_cmd), timeout=3600)
            if rc2 != 0:
                snippet = (err2 or "")[-800:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err2,
                    message=f"远端 dmrman 备份集打包失败(rc={rc2}): {snippet}")

            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            local_path = os.path.join(out_dir, f"dmrman_{ts}.tar.gz")
            sftp.get(remote_tar, local_path)

            size = os.path.getsize(local_path)
            checksum = db.sha256_file(local_path)
            hk = ssh_host.get("host_key", "remote")
            msg = (f"通过 SSH 在 {hk} 以 dmdba 用户执行 dmrman 物理备份成功，"
                   f"已拉回 {local_path} ({db.human_size(size)})")
            self.logger.info("[%s] %s", self.task_name, msg)

            # 清理远端备份目录与临时脚本/tar（best-effort，失败不致命）
            try:
                _ssh_exec_pipe(client, remote_dump._wrap_login(
                    f"rm -rf {remote_dir} {remote_tar} {remote_script}"), timeout=60)
            except Exception:
                pass

            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=local_path, size_bytes=size, duration_sec=duration,
                stdout=out_text, stderr=err, simulated=False,
                checksum=checksum, message=msg)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def _backup_logical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机执行 dexp，把 dmp 文件通过 SFTP 拉回本地。"""
        from core import remote_dump
        import time
        ts = self._timestamp()
        userid = self._connect_userid()
        extra = self._parse_extra()
        scope = " ".join(self._scope_args(extra))
        remote_tmp = f"/tmp/dm_bkp_{ts}"
        remote_dmp = f"{remote_tmp}/{ts}.dmp"
        remote_log = f"{remote_tmp}/{ts}.log"
        remote_cmd = (
            f"mkdir -p {remote_tmp} && "
            f"dexp USERID={userid} FILE={ts} DIRECTORY={remote_tmp} LOG={ts}.log {scope}"
        )
        t0 = time.time()
        data = remote_dump.remote_exec_and_fetch(ssh_host, remote_cmd, remote_dmp, timeout=7200)
        duration = round(time.time() - t0, 3)
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        local_path = os.path.join(out_dir, f"{ts}.dmp")
        with open(local_path, "wb") as f:
            f.write(data)
        size = os.path.getsize(local_path)
        checksum = db.sha256_file(local_path)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=local_path, size_bytes=size, duration_sec=duration,
            simulated=False, checksum=checksum,
            message=f"达梦 远程逻辑备份(dexp)成功: {local_path}")

    def _backup_logical_local(self, backup_type: BackupType) -> BackupResult:
        """逻辑备份：dexp（沿用原有实现）。"""
        # 客户端探测
        ok, detail = self.check_client()
        if not ok:
            # 非仿真模式下客户端不可用，直接返回失败
            self.logger.error("[%s] 客户端不可用，无法执行真实备份: %s",
                              self.task_name, detail)
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=None,
                size_bytes=0,
                stdout="",
                stderr=detail,
                simulated=False,
                checksum="",
                message="备份失败: " + detail,
            )

        extra = self._parse_extra()
        out_dir = self._output_dir()
        ts = self._timestamp()
        userid = self._connect_userid()

        # 3) 构造 dexp 命令
        # dexp USERID=... FILE={ts} DIRECTORY={dir} LOG={ts}.log <范围参数>
        cmd = [
            "dexp",
            "USERID={0}".format(userid),
            "FILE={0}".format(ts),
            "DIRECTORY={0}".format(out_dir),
            "LOG={0}.log".format(ts),
        ]

        # 达梦 dexp 不提供真正的增量/差异逻辑导出，回退为 full
        effective_type = backup_type
        fallback_msg = ""
        if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL):
            effective_type = BackupType.FULL
            fallback_msg = (
                "达梦逻辑导出 dexp 无真正增量/差异能力，已回退为整库 FULL 导出；"
                "如需增量备份请使用物理备份工具 dmrman。"
            )

        cmd.extend(self._scope_args(extra))

        # 说明密码内置于 USERID 的达梦惯例
        conn_note = "（达梦 dexp 将密码写入 USERID 参数，属官方语法惯例）"

        start = time.time()
        res = self._run(cmd, timeout=3600)
        duration = round(time.time() - start, 3)

        # 4) 判定结果
        if res["returncode"] != 0:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=None,
                size_bytes=0,
                duration_sec=duration,
                stdout=res["stdout"],
                stderr=res["stderr"],
                simulated=False,
                checksum="",
                message="dexp 备份失败(returncode={0}) {1}".format(
                    res["returncode"], conn_note),
            )

        # 计算生成的 .dmp 文件大小与校验和
        dmp_path = os.path.join(out_dir, ts + ".dmp")
        size_bytes = 0
        checksum = ""
        if os.path.exists(dmp_path):
            size_bytes = os.path.getsize(dmp_path)
            checksum = db.sha256_file(dmp_path)
        else:
            self.logger.warning("[%s] dexp 返回成功但未找到预期文件: %s",
                                self.task_name, dmp_path)

        message = "dexp 逻辑备份成功{0} {1}".format(
            "（{0}）".format(fallback_msg) if fallback_msg else "", conn_note)

        return BackupResult(
            success=True,
            status=BackupStatus.SUCCESS,
            backup_path=dmp_path,
            size_bytes=size_bytes,
            duration_sec=duration,
            stdout=res["stdout"],
            stderr=res["stderr"],
            simulated=False,
            checksum=checksum,
            message=message,
        )

    # ---------------- 恢复 ----------------
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        """执行达梦逻辑导入（dimp）恢复。

        backup_path 为待恢复的 .dmp 文件路径。target_db（kwargs）可作为目标
        schema 映射到 SCHEMAS 参数；否则沿用任务 extra_options 中的
        schemas / owner，再否则整库 FULL 导入。
        """
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 跨主机恢复（达梦 disql/dimp）
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            return self._try_cross_host_restore(backup_path, target_host_info,
                                                 kwargs.get("target_db") or "")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 客户端探测
        ok, detail = self.check_client()
        if not ok:
            self.logger.error("[%s] 客户端不可用，无法执行真实恢复: %s",
                              self.task_name, detail)
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                size_bytes=0,
                stdout="",
                stderr=detail,
                simulated=False,
                checksum="",
                message="恢复失败: " + detail,
            )

        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                size_bytes=0,
                stdout="",
                stderr="备份文件不存在: {0}".format(backup_path),
                simulated=False,
                checksum="",
                message="恢复失败: 备份文件不存在",
            )

        extra = self._parse_extra()
        userid = self._connect_userid()
        target_db = kwargs.get("target_db")
        # dimp 的 FILE 为文件名（不含扩展名），DIRECTORY 为文件所在目录
        dmp_dir = os.path.dirname(backup_path)
        dmp_base = os.path.splitext(os.path.basename(backup_path))[0]
        ts = self._timestamp()

        # 3) 构造 dimp 命令
        # dimp USERID=... FILE={base} DIRECTORY={dir} LOG={ts}.log <范围参数>
        cmd = [
            "dimp",
            "USERID={0}".format(userid),
            "FILE={0}".format(dmp_base),
            "DIRECTORY={0}".format(dmp_dir),
            "LOG={0}.log".format(ts),
        ]
        cmd.extend(self._scope_args(extra, target_db=target_db))

        conn_note = "（达梦 dimp 将密码写入 USERID 参数，属官方语法惯例）"
        target_note = " 目标库/模式={0}".format(target_db) if target_db else ""

        start = time.time()
        res = self._run(cmd, timeout=3600)
        duration = round(time.time() - start, 3)

        if res["returncode"] != 0:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                size_bytes=0,
                duration_sec=duration,
                stdout=res["stdout"],
                stderr=res["stderr"],
                simulated=False,
                checksum="",
                message="dimp 恢复失败(returncode={0}){1}{2}".format(
                    res["returncode"], conn_note, target_note),
            )

        return BackupResult(
            success=True,
            status=BackupStatus.SUCCESS,
            backup_path=backup_path,
            size_bytes=os.path.getsize(backup_path),
            duration_sec=duration,
            stdout=res["stdout"],
            stderr=res["stderr"],
            simulated=False,
            checksum=db.sha256_file(backup_path),
            message="dimp 逻辑恢复成功{0}{1}".format(conn_note, target_note),
        )

    # ---------------- 列出数据库 ----------------
    def list_databases(self) -> list:
        """列举达梦实例/数据库名。

        完整列举需使用 disql 连接后查询，本引擎为简化实现直接返回空列表，
        由用户在任务配置中指定 db_name / schemas / owner。
        """
        return []
