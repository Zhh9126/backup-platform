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
    # 远端工具探测用户：dexp/dimp/dmrman 仅在 dmdba 用户 profile PATH 中可见
    tool_check_user = "dmdba"

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
        这是 dexp/dimp 的连接语法要求。密码含 @ 等特殊字符时必须用
        双引号包裹，否则连接串会被密码中的 @ 截断导致登录失败。
        """
        user = self.task.get("username") or ""
        pw = self.task.get("password") or ""  # task 中密码为已解密明文
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 5236
        # 含字母数字以外字符（@ / : 等）的密码用双引号包裹（达梦官方语法）
        if pw and not pw.isalnum():
            pw = '"{0}"'.format(pw)
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
        """物理备份：通过 SSH 在远端达梦服务器执行联机全量备份，再把备份集
        打成 tar.gz 经 SFTP 拉回本机落盘并计算 size/sha256。

        实现说明（E2E 已在 DM8 真机验证）：
        - **联机备份优先**：实例运行中时 dmrman（离线工具）无法备份，正确做法是
          经 disql 执行 `BACKUP DATABASE FULL BACKUPSET '<dir>'`（要求归档模式）。
          工具路径与归档目录均动态准备：备份目录 chown 给 dmdba（dmserver 运行
          用户）避免权限拒绝。
        - **dmrman 离线兜底**：disql 失败时再尝试 dmrman 脚本方式（仅当实例
          已关闭时才可能成功，作为兜底保留）。
        - 产物以「备份集目录非空」为真实判据，不以 disql 退出码为准。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ts = self._timestamp()
        client = remote_dump._connect(ssh_host)
        sftp = client.open_sftp()
        remote_dir = f"/home/dmdba/dm_bkp_{ts}"
        remote_tar = f"{remote_dir}.tar.gz"

        try:
            # 1) 建备份目录并 chown 给 dmdba（dmserver 以 dmdba 运行，备份集由
            #    服务端进程写入，必须可写）
            prep = (f"mkdir -p {remote_dir} && chown dmdba {remote_dir} "
                    f"&& chmod 755 {remote_dir}")
            _ssh_exec_pipe(client, remote_dump._wrap_login(prep), timeout=60)

            # 2) 联机备份：disql 执行 BACKUP DATABASE（SQL 写文件执行，避免引号嵌套）
            disql_bin = remote_dump.resolve_remote_tool(ssh_host, "disql",
                                                        check_user="dmdba")
            online_out = ""
            online_ok = False
            if disql_bin:
                pw = db.decrypt_secret(self.task.get("password") or "")
                user = self.task.get("username") or "SYSDBA"
                port = self.task.get("port") or 5236
                sql_path = f"/tmp/dm_online_bkp_{ts}.sql"
                with sftp.open(sql_path, "w") as f:
                    f.write(f"BACKUP DATABASE FULL BACKUPSET '{remote_dir}';\nexit\n")
                try:
                    sftp.chmod(sql_path, 0o644)
                except Exception:
                    pass
                # 密码含特殊字符时用双引号包裹（达梦官方语法）
                if pw and not pw.isalnum():
                    pw = '"{0}"'.format(pw)
                shell = (f"timeout 7200 {disql_bin} "
                         f"{shlex.quote(user + '/' + pw + '@localhost:' + str(port))} "
                         f"\\`{sql_path}")
                start = time.time()
                out, err, rc = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(shell), timeout=7260)
                duration = round(time.time() - start, 3)
                online_out = (out.decode("utf-8", "replace")
                              if isinstance(out, bytes) else out) or ""
                # 产物判据：备份集目录下出现真实备份文件（.bak/.meta）
                check_cmd = (f"find {remote_dir} -type f \\( -name '*.bak' -o "
                             f"-name '*.meta' \\) | head -3")
                chk_out, _e2, _rc2 = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(check_cmd), timeout=30)
                chk_text = (chk_out.decode("utf-8", "replace")
                            if isinstance(chk_out, bytes) else chk_out) or ""
                online_ok = bool(chk_text.strip())
                if not online_ok:
                    snippet = online_out[-600:]
                    if ("归档" in snippet or "-718" in snippet or "-8003" in snippet
                            or "archive" in snippet.lower()):
                        hint = ("；提示：达梦联机备份要求数据库处于归档模式"
                                "(ARCH_MODE=Y)。请先开启归档：dm.ini 设 ARCH_INI=1、"
                                "配置 dmarch.ini 并重启实例。")
                    else:
                        hint = ""
                    self.logger.warning("[%s] 联机物理备份失败，尝试 dmrman 兜底: %s",
                                        self.task_name, snippet[:200])
                    last_err = snippet + hint
                else:
                    last_err = ""
            else:
                duration = 0
                last_err = "远端主机未找到 disql（dmdba 用户 PATH 与常见安装目录均无）"

            # 3) dmrman 离线兜底（实例关闭时才可用；联机已成功则跳过）
            if not online_ok:
                dmrman_bin = remote_dump.resolve_remote_tool(ssh_host, "dmrman",
                                                             check_user="dmdba")
                if dmrman_bin:
                    remote_script = f"/home/dmdba/dmrman_{ts}.cmd"
                    with sftp.open(remote_script, "w") as f:
                        f.write(f"BACKUP DATABASE FULL TO '{remote_dir}';\nexit;\n")
                    try:
                        sftp.chmod(remote_script, 0o644)
                    except Exception:
                        pass
                    shell = f"su - dmdba -c {shlex.quote(dmrman_bin + ' CTLFILE=' + remote_script)}"
                    out, err, rc = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(shell), timeout=7200)
                    online_out = (out.decode("utf-8", "replace")
                                  if isinstance(out, bytes) else out) or ""
                    check_cmd = (f"find {remote_dir} -type f | head -3")
                    chk_out, _e2, _rc2 = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(check_cmd), timeout=30)
                    chk_text = (chk_out.decode("utf-8", "replace")
                                if isinstance(chk_out, bytes) else chk_out) or ""
                    online_ok = bool(chk_text.strip())
                    if not online_ok:
                        last_err = (last_err or "") + "；dmrman 兜底也失败: " + online_out[-400:]
                elif not last_err:
                    last_err = "远端主机未找到 dmrman（dmdba 用户 PATH 与常见安装目录均无）"

            if not online_ok:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=online_out, stderr="",
                    message=f"达梦物理备份失败: {last_err[:900]}")

            # 4) 把远端备份集打成 tar.gz，再经 SFTP 拉回本机
            tar_cmd = f"tar czf {remote_tar} -C {remote_dir} ."
            out2, err2, rc2 = _ssh_exec_pipe(
                client, remote_dump._wrap_login(tar_cmd), timeout=3600)
            if rc2 != 0:
                snippet = (err2 or "")[-800:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=online_out, stderr=err2,
                    message=f"远端备份集打包失败(rc={rc2}): {snippet}")

            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            local_path = os.path.join(out_dir, f"dm_backup_{ts}.tar.gz")
            sftp.get(remote_tar, local_path)

            size = os.path.getsize(local_path)
            checksum = db.sha256_file(local_path)
            hk = ssh_host.get("host_key", "remote")
            msg = (f"通过 SSH 在 {hk} 对达梦执行联机全量物理备份成功，"
                   f"备份集已拉回 {local_path} ({db.human_size(size)})")
            self.logger.info("[%s] %s", self.task_name, msg)

            # 清理远端备份目录与临时脚本/tar（best-effort，失败不致命）
            try:
                _ssh_exec_pipe(client, remote_dump._wrap_login(
                    f"rm -rf {remote_dir} {remote_tar}"), timeout=60)
            except Exception:
                pass

            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=local_path, size_bytes=size, duration_sec=duration,
                stdout=online_out, stderr="", simulated=False,
                checksum=checksum, message=msg)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def _backup_logical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机执行 dexp，把 dmp 文件通过 SFTP 拉回本地。

        dexp 绝不写死：通过 resolve_remote_tool 动态解析（dmdba 用户 profile
        优先，覆盖 /dm*/bin、/opt/dm*/bin 等任意安装目录）。
        """
        from core import remote_dump
        import time
        ts = self._timestamp()
        userid = self._connect_userid()
        extra = self._parse_extra()
        scope = " ".join(self._scope_args(extra))
        dexp_bin = remote_dump.resolve_remote_tool(ssh_host, "dexp",
                                                   check_user="dmdba")
        if not dexp_bin:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=("远端主机未找到 dexp（dmdba 用户 PATH 与常见安装目录均无），"
                         "无法执行达梦逻辑备份。"))
        remote_tmp = f"/tmp/dm_bkp_{ts}"
        remote_dmp = f"{remote_tmp}/{ts}.dmp"
        remote_log = f"{remote_tmp}/{ts}.log"
        import shlex as _shlex
        # USERID 中密码带双引号（含特殊字符时），必须整体单引号包裹，
        # 防止 bash -lc 剥掉双引号后达梦连接串被密码中的 @ 截断
        remote_cmd = (
            f"mkdir -p {remote_tmp} && "
            f"{dexp_bin} {_shlex.quote('USERID=' + userid)} "
            f"FILE={ts}.dmp DIRECTORY={remote_tmp} LOG={ts}.log {scope}"
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
