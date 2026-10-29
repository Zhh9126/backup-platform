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
import re
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

    def _scope_args(self, extra: dict, target_db: str = None,
                  include_tables: bool = True) -> list:
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

        # 单表导出（dexp TABLES=表1,表2；配合 SCHEMAS 限定模式）。
        # 恢复路径（dimp）单独用 kwargs.tables 处理，避免重复参数
        tables = extra.get("tables")
        if include_tables and tables:
            if isinstance(tables, (list, tuple)):
                names = ",".join(str(x).strip() for x in tables if str(x).strip())
            else:
                names = str(tables)
            if names:
                args = []
                if extra.get("schema") or extra.get("db_name"):
                    sc = extra.get("schema") or extra.get("db_name")
                    args.append("SCHEMAS={0}".format(sc))
                args.append("TABLES={0}".format(names))
                return args

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
        # 固定产物形态：远端路径**确定性**（按任务固定，不含随机时间戳），
        # 备份成功后写完成标记 → 拉回中断时重试直接续传，**不重跑 DM 备份**。
        tid = int(self.task.get("id") or 0)
        _dm_os_user = self._resolve_check_user(ssh_host)
        remote_dir = f"/home/{_dm_os_user}/dm_bkp_t{tid}"
        remote_tar = f"{remote_dir}.tar.gz"

        try:
            # 0) 复用探测：上次联机/dmrman 备份已跑完并打包完成，只是拉回中断
            ready = remote_dump.remote_artifact_ready(client, remote_tar, min_size=1)
            reuse = bool(ready["reusable"])
            if reuse:
                self.logger.info("[%s] 复用远端已完成备份集 %s（%s），跳过 DM 备份执行",
                                 self.task_name, remote_tar,
                                 db.human_size(ready["size"]))

            # 1) 建备份目录并 chown 给数据库 OS 用户（dmserver 以该用户运行，
            #    备份集由服务端进程写入，必须可写）；非复用路径清空上次残留
            if not reuse:
                prep = (f"rm -rf {remote_dir} {remote_tar} && mkdir -p {remote_dir} "
                        f"&& chown {_dm_os_user} {remote_dir} && chmod 755 {remote_dir}")
                _ssh_exec_pipe(client, remote_dump._wrap_login(prep), timeout=60)

            # 2) 联机备份：disql 执行 BACKUP DATABASE（SQL 写文件执行，避免引号嵌套）
            disql_bin = remote_dump.resolve_remote_tool(ssh_host, "disql",
                                                        check_user=_dm_os_user)
            online_out = ""
            online_ok = bool(reuse)      # 复用时视作已成功，跳过全部备份执行
            if disql_bin and not reuse:
                pw = db.decrypt_secret(self.task.get("password") or "")
                user = self.task.get("username") or "SYSDBA"
                port = self.task.get("port") or 5236
                sql_path = f"/tmp/dm_online_bkp_{ts}.sql"
                # 增量物理备份：基于最近一次基备份集；PITR 链路 = 全量基备 +
                # 增量链 + 归档日志（recover until time）
                incr_kw = "INCREMENT " if str(
                    getattr(backup_type, "value", backup_type)) == "incremental" else ""
                with sftp.open(sql_path, "w") as f:
                    f.write(f"BACKUP DATABASE {incr_kw}BACKUPSET '{remote_dir}';\n"
                            f"SELECT CUR_LSN FROM V$RLOG;\nexit\n")
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
                last_err = f"远端主机未找到 disql（{_dm_os_user} 用户 PATH 与常见安装目录均无）"

            # 3) dmrman 离线兜底（实例关闭时才可用；联机已成功则跳过）
            if not online_ok:
                dmrman_bin = remote_dump.resolve_remote_tool(ssh_host, "dmrman",
                                                             check_user=_dm_os_user)
                if dmrman_bin:
                    user = _dm_os_user
                    remote_script = f"/tmp/dmrman_{ts}.cmd"
                    with sftp.open(remote_script, "w") as f:
                        f.write(f"BACKUP DATABASE FULL TO '{remote_dir}';\nexit;\n")
                    try:
                        sftp.chmod(remote_script, 0o644)
                    except Exception:
                        pass
                    shell = f"su - {user} -c {shlex.quote(dmrman_bin + ' CTLFILE=' + remote_script)}"
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
                    last_err = f"远端主机未找到 dmrman（{_dm_os_user} 用户 PATH 与常见安装目录均无）"

            if not online_ok:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=online_out, stderr="",
                    message=f"达梦物理备份失败: {last_err[:900]}")

            # 4) 把远端备份集打成 tar.gz，再断点续传拉回本机
            #    （复用时 tar 包已存在，无需重新打包）
            if reuse:
                out2, err2, rc2 = "", "", 0
            else:
                tar_cmd = f"tar czf {remote_tar} -C {remote_dir} ."
                out2, err2, rc2 = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(tar_cmd), timeout=3600)
            if rc2 != 0:
                snippet = (err2 or "")[-800:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=online_out, stderr=err2,
                    message=f"远端备份集打包失败(rc={rc2}): {snippet}")

            # 4.5) 写远端完成标记：下次重试据此复用，**不重跑 DM 备份**
            if not reuse:
                try:
                    _tsz = int(sftp.stat(remote_tar).st_size or -1)
                except Exception:
                    _tsz = -1
                remote_dump.mark_remote_artifact_done(
                    client, remote_tar, size=_tsz,
                    note=f"dameng {ssh_host.get('host_key','')}")

            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            pulled = remote_dump.sftp_pull_resumable(
                client, remote_tar, os.path.join(out_dir, f"dm_backup_{ts}.tar.gz"),
                task=self.task, key="dm.tar.gz", db_type=f"{self.db_type}_phys",
                host_key=ssh_host.get("host_key", ""), has_rc=False,
                stable_secs=3, label="达梦备份集", min_size=1024)
            local_path = pulled["path"]

            size = os.path.getsize(local_path)
            checksum = db.sha256_file(local_path)
            hk = ssh_host.get("host_key", "remote")
            btype = str(getattr(backup_type, "value", backup_type))
            # 记录备份完成时的 LSN/时间（PITR 恢复点元数据，前端展示与恢复向导用）
            pitr_info = ""
            try:
                _u = self.task.get("username") or "SYSDBA"
                _p = db.decrypt_secret(self.task.get("password") or "")
                if _p and not _p.isalnum():
                    _p = '"{0}"'.format(_p)
                _disql = remote_dump.resolve_remote_tool(ssh_host, "disql",
                                                         check_user=_dm_os_user) \
                         or "disql"
                lsn_out, _e, _rc = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(
                        f"{_disql} {shlex.quote(_u + '/' + _p + '@localhost:5236')} "
                        f"<<'SQL'\nSELECT CUR_LSN FROM V$RLOG;\nexit\nSQL"),
                    timeout=60)
                m = re.search(r"CUR_LSN\s*\n-+\s*\n(\d+)",
                              (lsn_out.decode("utf-8", "replace")
                               if isinstance(lsn_out, bytes) else lsn_out) or "")
                if m:
                    pitr_info = (f" | PITR点: LSN={m.group(1)}"
                                 f"@{time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception:
                pass
            msg = (f"通过 SSH 在 {hk} 对达梦执行联机{('增量' if btype == 'incremental' else '全量')}"
                   f"物理备份成功，"
                   f"备份集已拉回 {local_path} ({db.human_size(size)}){pitr_info}"
                   f"{'（复用上次远端产物，未重跑备份）' if reuse else ''}"
                   f"{'（断点续传）' if pulled['resumed'] else ''}")
            self.logger.info("[%s] %s", self.task_name, msg)

            # 清理远端备份目录、tar 与完成标记（best-effort，失败不致命）
            try:
                _ssh_exec_pipe(client, remote_dump._wrap_login(
                    f"rm -rf {remote_dir} {remote_tar} "
                    f"{remote_dump.marker_path(remote_tar)}"), timeout=60)
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
        _dm_os_user = self._resolve_check_user(ssh_host)
        dexp_bin = remote_dump.resolve_remote_tool(ssh_host, "dexp",
                                                   check_user=_dm_os_user)
        if not dexp_bin:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=(f"远端主机未找到 dexp（{_dm_os_user} 用户 PATH 与常见安装目录均无），"
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
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        local_path = os.path.join(out_dir, f"{ts}.dmp")
        # 落盘续传通道：dmp 以断点续传方式拉回（限速 + 瞬时重试内置），
        # 替代旧 sftp.getfo 整包读进内存
        res_d = remote_dump.remote_exec_fetch_to_file(
            ssh_host, remote_cmd, remote_dmp, local_path, task=self.task,
            timeout=7200, label="达梦 dexp dmp 拉回")
        duration = round(time.time() - t0, 3)
        size = int(res_d.get("size") or 0)
        note = "（断点续传拉回）" if res_d.get("resumed") else ""
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=local_path, size_bytes=size, duration_sec=duration,
            simulated=False, checksum=res_d.get("sha256") or "",
            message=f"达梦 远程逻辑备份(dexp)成功: {local_path}{note}")

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
    def _restore_pitr(self, backup_path: str, **kwargs) -> BackupResult:
        """达梦 PITR 时点恢复（物理备份集 + 归档日志）。

        流程（对标 DBackup 恢复编排，默认非破坏性演练恢复）：
        1. 定位 SSH 目标主机（必须与备份源同环境，需要 dmrman + 归档目录）
        2. 本地 tar.gz 经 SFTP 推回目标机并解包出备份集目录
        3. dmrman RESTORE DATABASE '<ini>' TO '<还原目录>' FROM BACKUPSET '...'
        4. dmrman RECOVER DATABASE '<还原目录>/dm.ini' WITH ARCHIVEDIR '<归档目录>'
           UNTIL TIME 'YYYY-MM-DD HH:MM:SS'（target_time 缺省=恢复至最新）
        5. RECOVER ... UPDATE DB_MAGIC（使还原副本可独立 OPEN）
        kwargs:
          target_time     PITR 时间点（'YYYY-MM-DD HH:MM:SS'）；缺省恢复到最新
          target_host_info 纳管主机信息（含 SSH 凭据）；缺省按 ssh_host_id/任务配置解析
          pitr_restore_dir 还原目录（默认 /home/dmdba/pitr_restore_<ts>）
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ts = self._timestamp()
        target_time = (kwargs.get("target_time") or "").strip()

        # 1) SSH 目标
        ssh_host = kwargs.get("target_host_info")
        if not ssh_host:
            from core import remote_dump as _rd
            ssh_host = _rd.resolve_ssh_host(self.task)
        # 数据库 OS 用户（标准变量：任务 > 主机 > 引擎默认 dmdba）
        _dm_os_user = self._resolve_check_user(ssh_host)
        restore_dir = (kwargs.get("pitr_restore_dir")
                       or f"/home/{_dm_os_user}/pitr_restore_{ts}").rstrip("/")
        if not ssh_host:
            return BackupResult(
                success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                message="达梦 PITR 恢复需要 SSH 目标主机（纳管主机或直接输入），未找到")
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                message=f"备份产物不存在: {backup_path}")

        client = remote_dump._connect(ssh_host)
        sftp = client.open_sftp()
        logs = []
        try:
            remote_pkg = f"/tmp/dm_pitr_{ts}.tar.gz"
            bset_dir = f"/home/{_dm_os_user}/dm_pitr_bset_{ts}"
            # 2) 推送并解包备份集
            logs.append(f"[PITR] 推送备份集: {os.path.basename(backup_path)}")
            remote_dump.sftp_put_resumable(
                client, backup_path, remote_pkg,
                rate_kbps=int(self.task.get("bandwidth_limit") or 0),
                label="达梦 PITR 备份集推送")
            out, err, rc = _ssh_exec_pipe(client, remote_dump._wrap_login(
                f"mkdir -p {bset_dir} {restore_dir} && chown -R {_dm_os_user} {bset_dir} "
                f"{restore_dir} && tar xzf {remote_pkg} -C {bset_dir} && "
                f"find {bset_dir} -type f | head -3"), timeout=300)
            if rc != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                    stderr=(err or b"").decode("utf-8", "ignore") if isinstance(err, bytes) else (err or ""),
                    message=f"PITR 备份集解包失败(rc={rc})")
            logs.append("[PITR] 备份集已解包")

            # 3) 动态定位 dm.ini 与归档目录（dmserver 进程 cmdline）
            out, _e, _rc = _ssh_exec_pipe(client, remote_dump._wrap_login(
                "ps -ef | grep dmserver | grep -v grep | head -1"), timeout=30)
            m_ini = re.search(r"path=(\S+?dm\.ini)",
                              (out.decode("utf-8", "replace")
                               if isinstance(out, bytes) else out) or "")
            if not m_ini:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                    message="未在目标机发现运行中的 dmserver（dm.ini 路径无法确定）")
            src_ini = m_ini.group(1)
            data_dir = os.path.dirname(src_ini)
            arch_dir = ""
            out, _e, _rc = _ssh_exec_pipe(client, remote_dump._wrap_login(
                f"grep -iE '^ARCH_DEST' {data_dir}/dmarch.ini 2>/dev/null | head -1"),
                timeout=30)
            m_arch = re.search(r"ARCH_DEST\s*=\s*(\S+)",
                               (out.decode("utf-8", "replace")
                                if isinstance(out, bytes) else out) or "")
            if m_arch:
                arch_dir = m_arch.group(1)
            logs.append(f"[PITR] 源 dm.ini={src_ini} 归档目录={arch_dir or '未配置'}")

            dmrman_bin = remote_dump.resolve_remote_tool(ssh_host, "dmrman",
                                                         check_user=_dm_os_user)
            if not dmrman_bin:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                    message=f"目标机未找到 dmrman（{_dm_os_user} 用户）")

            # 3.5) dminit 初始化新实例骨架（官方"还原到指定目录"做法）：
            #      新 dm.ini 必须存在且 SYSTEM_PATH 指向新目录，否则
            #      RESTORE 报 -104 Invalid INI / -137 实例运行冲突
            inst_name = os.path.basename(data_dir) or "DAMENG"
            new_ini = os.path.join(restore_dir, inst_name, "dm.ini")
            dminit_bin = remote_dump.resolve_remote_tool(ssh_host, "dminit",
                                                         check_user=_dm_os_user)
            if not dminit_bin:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                    message=f"目标机未找到 dminit（{_dm_os_user} 用户，用于初始化还原目录）")
            # 还原副本用独立端口，避免与运行中的源实例冲突；
            # SYSDBA_PWD 必须显式给（达梦新安全策略要求复杂密码，缺省会 init 失败）
            replica_port = int(self.task.get("port") or 5236) + 100
            src_pwd = db.decrypt_secret(self.task.get("password") or "") or "Ceshi@5235"
            # 注：非安全版 dminit 不接受 SYSSSO_PWD（会报
            # "Current dminit is not a secure version, you can't set [SYSSSO_PWD]"）
            # 页大小/簇大小必须与源实例一致，否则 RESTORE 报
            # DM[-105] Invalid control file（备份集与目标实例页大小不匹配）
            page_size, extent_size = 8, 16
            try:
                from core.sync.plugins.dameng import _jdbc_connect as _dmconn
                _c = _dmconn(self.task.get("host") or "127.0.0.1",
                             int(self.task.get("port") or 5236),
                             self.task.get("username") or "SYSDBA",
                             db.decrypt_secret(self.task.get("password") or ""))
                _cur = _c.cursor()
                _cur.execute("SELECT SF_GET_PAGE_SIZE()")
                # dminit PAGE_SIZE 单位=K（合法值 4/8/16/32），
                # SF_GET_PAGE_SIZE 返回字节 → 换算为 K
                page_size = max(int(_cur.fetchone()[0]) // 1024, 4) or 8
                try:
                    _cur.execute("SELECT SF_GET_EXTENT_SIZE()")
                    extent_size = max(int(_cur.fetchone()[0]) // 1024, 16)
                except Exception:
                    extent_size = 16
                _cur.close()
                _c.close()
                logs.append(f"[PITR] 源实例页大小={page_size}K 簇大小={extent_size}K")
            except Exception as _e:
                logs.append(f"[PITR] 页大小探测失败（用默认 8K）: {_e}")
            dminit_args = (dminit_bin + " path=" + restore_dir +
                           " PORT_NUM=" + str(replica_port) +
                           " PAGE_SIZE=" + str(page_size) +
                           " EXTENT_SIZE=" + str(extent_size) +
                           " SYSDBA_PWD=" + src_pwd +
                           " SYSAUDITOR_PWD=" + src_pwd)
            init_shell = f"su - {_dm_os_user} -c " + shlex.quote(dminit_args)
            out, _e, _rc = _ssh_exec_pipe(
                client, remote_dump._wrap_login(init_shell) + " 2>&1 | tail -3",
                timeout=300)
            logs.append(f"[PITR] dminit 新实例骨架: {restore_dir} "
                        f"(PORT_NUM={replica_port})")
            # 动态定位生成的 dm.ini（实例目录名可能为 DAMENG/源实例名等）
            ini_chk, _e, _rc = _ssh_exec_pipe(client, remote_dump._wrap_login(
                f"find {restore_dir} -name dm.ini -type f 2>/dev/null | head -1"),
                timeout=30)
            found_ini = (ini_chk.decode("utf-8", "replace")
                         if isinstance(ini_chk, bytes) else ini_chk or "").strip()
            if not found_ini:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                    stdout="\n".join(logs),
                    message=f"dminit 未在 {restore_dir} 生成 dm.ini，还原目录初始化失败")
            new_ini = found_ini.split("\n")[0].strip()
            logs.append(f"[PITR] 新实例 dm.ini: {new_ini}")

            def _dmrman(stmt: str, timeout: int = 3600) -> dict:
                """写 dmrman 脚本（.txt，官方 CTLFILE 示例后缀；.cmd/.sql 会报
                -2423 postfix invalid）并执行。
                两个真机踩坑（137 PROD 实例验证）：
                - 脚本 owner 必须为数据库 OS 用户，否则 -8291 failed to open ctl file
                - 脚本内不能写 exit;（dmrman 无此语句，报 -2007 语法错误）"""
                script_path = f"/home/{_dm_os_user}/dmrman_pitr_{ts}.txt"
                with sftp.open(script_path, "w") as f:
                    f.write(stmt + "\n")
                try:
                    sftp.chmod(script_path, 0o644)
                except Exception:
                    pass
                # chown 与 dmrman 串在同一条 shell 原子执行（分开两次 exec
                # 时 chown 竞态不生效，dmrman 会报 -8291）
                shell = (f"chown {_dm_os_user} {script_path} && "
                         f"su - {_dm_os_user} -c {shlex.quote(dmrman_bin + ' CTLFILE=' + script_path)}")
                o, e, rc = _ssh_exec_pipe(client, remote_dump._wrap_login(shell),
                                          timeout=timeout)
                text = (o.decode("utf-8", "replace") if isinstance(o, bytes) else o) or ""
                errt = (e.decode("utf-8", "replace") if isinstance(e, bytes) else e) or ""
                bad = any(k in text for k in ("失败", "error", "Error", "[-"))
                return {"ok": (not bad), "output": text + errt}

            # 4a) RESTORE 到独立目录（非破坏）：DATABASE 参数直接用新目录的
            #     dm.ini（保留 dminit 后动态定位的 found_ini——此前此处被
            #     os.path.join(restore_dir, "dm.ini") 硬编码覆盖，导致
            #     DM[-140] Can not access INI file）
            #     dm.ini 路径（DM8 dmrman 还原到指定目录的语法；不带 TO 子句）
            r1 = _dmrman(
                f"RESTORE DATABASE '{new_ini}' "
                f"FROM BACKUPSET '{bset_dir}'")
            logs.append(f"[PITR] RESTORE: {'成功' if r1['ok'] else '失败'}")
            if not r1["ok"]:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                    stdout=r1["output"],
                    message="PITR RESTORE 失败: " + r1["output"][-500:],
                    detail_log="\n".join(logs))

            # 4b) RECOVER（归档前滚，支持 UNTIL TIME）
            until_clause = f" UNTIL TIME '{target_time}'" if target_time else ""
            r2 = _dmrman(
                f"RECOVER DATABASE '{new_ini}' WITH ARCHIVEDIR '{arch_dir}'"
                f"{until_clause}")
            logs.append(f"[PITR] RECOVER(until {target_time or '最新'}): "
                        f"{'成功' if r2['ok'] else '失败'}")
            if not r2["ok"]:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                    stdout=r2["output"],
                    message="PITR RECOVER 失败: " + r2["output"][-500:],
                    detail_log="\n".join(logs))

            # 4c) UPDATE DB_MAGIC（副本可独立 OPEN）
            r3 = _dmrman(f"RECOVER DATABASE '{new_ini}' UPDATE DB_MAGIC")
            logs.append(f"[PITR] UPDATE DB_MAGIC: {'成功' if r3['ok'] else '失败'}")

            msg = (f"达梦 PITR 恢复完成（{restore_dir}），"
                   f"时点: {target_time or '最新可用'}"
                   f"{'；UPDATE DB_MAGIC ' + ('成功' if r3['ok'] else '失败')}"
                   f"。如需对外提供，可用 dmserver path={new_ini} 启动该副本")
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS, backup_path=backup_path,
                stdout="\n".join(logs) + "\n" + (r3["output"] or "")[:800],
                simulated=False, message=msg,
                detail_log="\n".join(logs))
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                _ssh_exec_pipe(client, remote_dump._wrap_login(
                    f"rm -f /tmp/dm_pitr_{ts}.tar.gz"), timeout=60)
            except Exception:
                pass

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        """执行达梦恢复：按产物类型分发。

        - .tar.gz（物理备份集，PITR）：走 _restore_pitr —— dmrman
          RESTORE ... TO '<还原目录>' + RECOVER ... WITH ARCHIVEDIR ... UNTIL
          TIME '<target_time>'，默认恢复到独立目录（非破坏演练），可选覆盖原库
        - .dmp（逻辑导出，dimp）：表/schema/整库导入
        """
        # ---- PITR 物理时点恢复（tar.gz 物理备份集 + target_time）----
        if backup_path.endswith(".tar.gz") or kwargs.get("target_time"):
            return self._restore_pitr(backup_path, **kwargs)

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
            # 平台机无 dexp/dimp（零安装原则）→ 走 SSH 远端 dimp（与备份通道对称）
            from core import remote_dump as _rd
            _ssh = _rd.resolve_ssh_host(self.task)
            if _ssh and backup_path and os.path.exists(backup_path):
                self.logger.info("[%s] 本机无 dimp 客户端，改走 SSH 远端恢复", self.task_name)
                return self._restore_logical_remote(_ssh, backup_path, **kwargs)
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
            # 恢复语义=覆盖既有对象（与远端 dimp 分支、Oracle impdp 对齐；
            # 不加 IGNORE=Y——会跳过重建直接插数据触发 -6602，与 REPLACE 互斥）
            "TABLE_EXISTS_ACTION=REPLACE",
        ]
        cmd.extend(self._scope_args(extra, target_db=target_db,
                                    include_tables=False))

        # 表级恢复：tables 参数 → dimpr TABLES=表1,表2
        sel_tables = kwargs.get("tables") or []
        if sel_tables:
            names = ",".join(str(t).strip().strip('"') for t in sel_tables)
            cmd.append("TABLES=" + names)

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

    def _restore_logical_remote(self, ssh_host: dict, backup_path: str,
                                **kwargs) -> BackupResult:
        """SSH 远端 dimp 逻辑恢复（平台机零安装：SFTP 推 .dmp → 远端 dimp 导入）。"""
        from core import remote_dump
        import time as _time
        ts = self._timestamp()
        dimp_bin = remote_dump.resolve_remote_tool(ssh_host, "dimp",
                                                   check_user=self._resolve_check_user(ssh_host))
        if not dimp_bin:
            return BackupResult(
                success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                message=(f"远端主机未找到 dimp（{self._resolve_check_user(ssh_host)} "
                         "用户 PATH 与常见安装目录均无），无法执行达梦逻辑恢复。"))
        extra = self._parse_extra()
        userid = self._connect_userid()
        target_db = kwargs.get("target_db")
        remote_tmp = "/tmp/dm_rst_{0}".format(ts)
        remote_dmp = "{0}/{1}.dmp".format(remote_tmp, ts)
        try:
            client = remote_dump._connect(ssh_host)
            # 先建远端目录再 SFTP 推送（put 不自动建目录）
            from core.engines.file import _ssh_exec_pipe as _sep
            _sep(client, remote_dump._wrap_login(
                "mkdir -p {0}".format(remote_tmp)), timeout=60)
            sftp = client.open_sftp()
            sftp.close()
            remote_dump.sftp_put_resumable(
                client, backup_path, remote_dmp,
                rate_kbps=int(self.task.get("bandwidth_limit") or 0),
                label="达梦 dmp 恢复推送")
        except Exception as e:  # noqa: BLE001
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path,
                                message="恢复产物推送远端失败: " + str(e)[:200])
        scope = " ".join(self._scope_args(extra, target_db=target_db,
                                          include_tables=False))
        # dimp 必须用解析出的绝对路径：root 登录 shell 的 PATH 通常没有
        # dimp（裸命令 rc=127，137 实测），dimp_bin 已在方法头解析
        cmd_parts = [dimp_bin or "dimp", "USERID={0}".format(userid),
                     "FILE={0}.dmp".format(ts),
                     "DIRECTORY={0}".format(remote_tmp),
                     "LOG={0}.log".format(ts),
                     # 恢复语义=以备份为准覆盖既有对象：目标 schema 已存在
                     # 同名表时 dimp 默认整表 fail（rc=2，137 实测：SYSDBA
                     # 存量测试表导致恢复失败）。REPLACE=drop+重建后重导。
                     # 注意不能加 IGNORE=Y：它会让 dimp 在表已存在时跳过
                     # 重建直接插数据，触发 -6602 唯一约束冲突（137 实测），
                     # 与 REPLACE 语义互斥。
                     "TABLE_EXISTS_ACTION=REPLACE"]
        if scope:
            cmd_parts.append(scope)
        sel_tables = kwargs.get("tables") or []
        if sel_tables:
            names = ",".join(str(x).strip().strip('"') for x in sel_tables)
            if names:
                cmd_parts.append("TABLES=" + names)
        import shlex as _shlex
        from core.engines.file import _ssh_exec_pipe as _sep
        remote_cmd = " ".join(_shlex.quote(p) for p in cmd_parts)
        remote_cmd = "mkdir -p {0} && {1}".format(_shlex.quote(remote_tmp), remote_cmd)
        start = _time.time()
        out, err, rc = _sep(
            client, remote_dump._wrap_login(remote_cmd), timeout=3600)
        duration = round(_time.time() - start, 3)
        otext = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        etext = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
        try:
            remote_dump.cleanup_remote_artifacts(client, [], dirs=[remote_tmp])
        except Exception:  # noqa: BLE001
            pass
        conn_note = "（达梦 dimp 将密码写入 USERID 参数，属官方语法惯例）"
        target_note = " 目标库/模式={0}".format(target_db) if target_db else ""
        if rc != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                duration_sec=duration, stdout=otext, stderr=etext,
                message="远端 dimp 恢复失败(returncode={0}){1}{2}: {3}".format(
                    rc, conn_note, target_note, (otext + etext)[-500:]))
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS, backup_path=backup_path,
            duration_sec=duration, stdout=otext, stderr=etext,
            checksum=db.sha256_file(backup_path),
            message="达梦 远程 dimp 逻辑恢复成功{0}{1}".format(conn_note, target_note))

    # ---------------- 列出数据库 ----------------
    def list_databases(self) -> list:
        """列举达梦实例/数据库名。

        完整列举需使用 disql 连接后查询，本引擎为简化实现直接返回空列表，
        由用户在任务配置中指定 db_name / schemas / owner。
        """
        return []
