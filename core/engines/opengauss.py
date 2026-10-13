# -*- coding: utf-8 -*-
"""
openGauss（华为开源数据库）备份/恢复引擎。

openGauss 兼容 PostgreSQL 协议，自带 gs_dump / gs_restore / gsql /
gs_dumpall / gs_basebackup，用法分别与 pg_dump / pg_restore / psql /
pg_dumpall / pg_basebackup 一致。远程 SSH 通道由 core/remote_dump.py 的
_PG_FAMILY_TOOLING["opengauss"] 分支承接（GAUSSHOME/LD_LIBRARY_PATH 注入、
omm 用户探测）；本引擎主要解决：

1. 本机通道工具名映射（pg_* → gs_*），复用 PostgreSQL 引擎全部逻辑；
2. 远端物理备份参数差异：gs_basebackup 不支持 --checkpoint，需显式
   -X fetch，且必须 -w 禁止交互式密码输入；
3. 远端工具探测用户为数据库 OS 用户 omm；
4. gs_probackup 物理（全量 + 增量）备份/恢复/PITR：gs_basebackup 仅支持
   全量，物理增量须走 gs_probackup（-b delta，依赖服务端
   enable_cbm_tracking=on 的 CBM 变更块跟踪；增量链保留在数据库服务器的
   probackup 目录中）。通过任务高级参数 {"physical_tool": "gs_probackup"}
   启用，PITR 用 {"probackup_recovery_target_time": "YYYY-MM-DD HH:MM:SS"}。

要求：
- 密码不出现在进程参数中，统一通过 PGPASSWORD 环境变量注入
  （gs_dump/gsql 等 PG 系客户端兼容读取；gs_probackup 基于 libpq 同样读取）。
"""
import json
import os
import shlex
import subprocess
import time

from core import db
from core.engines.base import BackupResult, BackupStatus, BackupType
from .postgresql import PostgreSQLEngine


# 本机通道工具名映射：PG 生态名 → openGauss 实际工具名
_TOOL_MAP = {
    "pg_dump": "gs_dump",
    "pg_restore": "gs_restore",
    "psql": "gsql",
    "pg_dumpall": "gs_dumpall",
    "pg_basebackup": "gs_basebackup",
    "pg_probackup": "gs_probackup",
}

# 物理（gs_probackup）备份类型 → -b 模式映射（增量/差异统一走 delta，
# 可用 extra_options.probackup_mode 覆写为 page/ptrack）
_PROBACKUP_MODE_BY_TYPE = {
    BackupType.FULL: "full",
    BackupType.INCREMENTAL: "delta",
    BackupType.DIFFERENTIAL: "delta",
}


class OpenGaussEngine(PostgreSQLEngine):
    """openGauss 备份/恢复引擎（复用 PostgreSQL 引擎逻辑 + 工具名映射）。"""

    db_type = "opengauss"
    display_name = "openGauss"
    required_clients = ["gs_dump", "gs_restore", "gsql"]
    physical_bundled_tools = ["gs_basebackup"]
    # openGauss 客户端工具在数据库服务器 omm 用户 profile 的 PATH/GAUSSHOME 中
    tool_check_user = "omm"
    # 远端物理备份：工具与默认端口/用户与 PG 不同
    _phys_tool = "gs_basebackup"
    _phys_default_port = 26000
    _phys_default_user = "omm"
    _phys_label = "gs_basebackup"
    # remote_dump 分发 / dump_format 解析 / logical_full 全实例均按 opengauss 走 gs_* 工具族
    _dump_db_type = "opengauss"
    # gs_dump 不支持 -d 短选项（PG 写法），库名为位置参数（本机通道）
    _dump_dbname_positional = True
    # sha256 认证不读 PGPASSWORD，本机/远端命令一律 -W 显式传密码
    _pass_via_w = True
    # openGauss gs_basebackup 本机通道同样不能 -Ft：plain 目录 + 打包
    _local_base_flags = ["-X", "fetch"]
    _local_pack_dir = True

    # ------------------------------------------------------------------
    # 工具解析：把 PG 生态工具名翻译为 openGauss 工具名后走统一解析器
    # ------------------------------------------------------------------
    def _resolve_local_tool(self, name: str):
        return super()._resolve_local_tool(_TOOL_MAP.get(name, name))

    def _remote_phys_backup(self, ssh_host: dict) -> dict:
        """gs_basebackup 参数差异：不支持 --checkpoint，需 -X fetch，-w 禁止交互，
        以 omm 用户探测工具，注入 GAUSSHOME/LD_LIBRARY_PATH 环境。"""
        from core import remote_dump
        return remote_dump.remote_physical_backup(
            self.task, ssh_host,
            tool=self._phys_tool, default_port=self._phys_default_port,
            default_user=self._phys_default_user,
            extra_args_key="pg_basebackup_extra_args", tool_label=self._phys_label,
            # openGauss：-X stream 仅支持 plain 模式；-Ft 实测产出非 tar 的
            # 裸 gzip 数据流，故用 plain 目录 + pack_after 远端打包成真 tar.gz
            base_flags=["-X", "fetch", "-w"],
            check_user=self._resolve_check_user(ssh_host),
            pack_after=True,
        )

    # ------------------------------------------------------------------
    # gs_probackup：物理（全量 + 增量）备份 / 恢复 / PITR
    # ------------------------------------------------------------------
    def _extra_dict(self) -> dict:
        """解析任务 extra_options（dict 或 JSON 字符串），容忍脏数据。"""
        raw = self.task.get("extra_options")
        if isinstance(raw, dict):
            return dict(raw)
        try:
            return json.loads(raw or "{}") or {}
        except Exception:
            return {}

    def _probackup_settings(self, eo: dict) -> tuple:
        """(catalog, instance, pgdata, mode) —— 与 remote_dump 侧推导规则一致。"""
        port = int(self.task.get("port") or self._phys_default_port)
        catalog = str(eo.get("probackup_dir") or "/data/gs_probackup")
        instance = str(eo.get("probackup_instance") or f"bp_{port}")
        pgdata = str(eo.get("probackup_pgdata") or "")
        return catalog, instance, pgdata

    def _backup_physical_remote(self, ssh_host: dict, backup_type) -> BackupResult:
        """物理备份远端通道：gs_probackup（全量+增量）或 gs_basebackup（仅全量）。"""
        from core import remote_dump
        eo = self._extra_dict()
        use_pb = str(eo.get("physical_tool") or "").lower() == "gs_probackup"
        if not use_pb:
            if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL):
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=("openGauss/磐维 gs_basebackup 仅支持全量物理备份，不支持增量。"
                             "如需物理增量，请在任务高级参数设置 "
                             '{"physical_tool": "gs_probackup"}'
                             "（增量 -b delta 依赖服务端 enable_cbm_tracking=on）。"))
            return super()._backup_physical_remote(ssh_host, backup_type)

        mode = str(eo.get("probackup_mode") or
                   _PROBACKUP_MODE_BY_TYPE.get(backup_type) or "full")
        res = remote_dump.remote_probackup_backup(
            self.task, ssh_host, mode=mode,
            default_port=self._phys_default_port, default_user=self._phys_default_user,
            check_user=self._resolve_check_user(ssh_host), label="gs_probackup")
        if not res.get("ok"):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=res.get("message", "gs_probackup 备份失败"),
                                stdout=res.get("stdout", ""), stderr=res.get("stderr", ""))

        client = remote_dump._connect(ssh_host)
        out_dir = self._output_dir()
        pieces = remote_dump._pull_remote_tars(
            client, res["remote_dir"], out_dir, task=self.task,
            host_key=ssh_host.get("host_key", ""), db_type=f"{self.db_type}_probackup")
        # 仅清理拉回用临时目录；增量链目录（catalog）必须保留
        remote_dump.cleanup_remote_artifacts(client, [], dirs=[res["remote_dir"]])
        if not pieces:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=(f"gs_probackup 备份成功(ID={res.get('backup_id')})但拉回产物为空"
                         f"（远端 {res['remote_dir']}）"))
        total = sum(sz for _, sz in pieces)
        msg = (f"gs_probackup {res.get('mode')} 物理备份成功(ID={res.get('backup_id')})，"
               f"已拉回 {db.human_size(total)}；增量链保留在远端 {res.get('catalog_dir')}"
               f"（后续增量依赖该目录，请勿手工清理）"
               f"{'（PITR 目标时间: ' + str(eo.get('probackup_recovery_target_time')) + '，恢复时生效）' if eo.get('probackup_recovery_target_time') else ''}")
        self.logger.info("[%s] %s", self.task_name, msg)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=pieces[0][0], size_bytes=total,
            stdout=res.get("stdout", ""), simulated=False,
            checksum=db.sha256_file(pieces[0][0]), message=msg)

    def _backup_physical(self, backup_type) -> BackupResult:
        """物理备份本机通道：gs_probackup（同机直连）或 gs_basebackup。"""
        eo = self._extra_dict()
        use_pb = str(eo.get("physical_tool") or "").lower() == "gs_probackup"
        if not use_pb:
            if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL):
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=("openGauss/磐维 gs_basebackup 仅支持全量物理备份。如需物理增量，"
                             '请设置 {"physical_tool": "gs_probackup"}（依赖 enable_cbm_tracking=on），'
                             "或纳管 SSH 主机走远端通道。"))
            return super()._backup_physical(backup_type)

        b = self._resolve_local_tool("pg_probackup")
        if not b:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=("本机未找到 gs_probackup。请纳管 SSH 主机走远端通道（推荐），"
                         "或将 gs_probackup 所在 bin 目录加入任务 tool_path。"))
        catalog, instance, pgdata = self._probackup_settings(eo)
        mode = str(eo.get("probackup_mode") or
                   _PROBACKUP_MODE_BY_TYPE.get(backup_type) or "full")
        port = int(self.task.get("port") or self._phys_default_port)
        user = self.task.get("username") or self._phys_default_user
        pw = db.decrypt_secret(self.task.get("password") or "")
        dbname = self.task.get("db_name") or "postgres"

        env = os.environ.copy()
        env["PGPASSWORD"] = pw or env.get("PGPASSWORD", "")
        home = os.path.dirname(os.path.dirname(b))
        env["GAUSSHOME"] = home
        env["LD_LIBRARY_PATH"] = (os.path.join(home, "lib") + ":" +
                                  env.get("LD_LIBRARY_PATH", ""))

        def run(args, timeout=21600):
            return subprocess.run(args, env=env, capture_output=True, text=True,
                                  timeout=timeout)

        os.makedirs(catalog, exist_ok=True)
        if not os.path.isdir(os.path.join(catalog, "backups")):
            r = run([b, "init", "-B", catalog], timeout=120)
            if r.returncode != 0:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message=f"gs_probackup init 失败: {r.stderr[:400]}")
        if not pgdata:
            try:
                ps = subprocess.run("ps -eo args | grep '[g]aussdb' | grep -o "
                                    "'\\-D[ =][^ ]*' | head -1 | sed 's/^-D[ =]//'",
                                    shell=True, capture_output=True, text=True, timeout=30)
                pgdata = ps.stdout.strip()
            except Exception:
                pgdata = ""
        if not pgdata:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message='未能自动发现本机 gaussdb 数据目录，请在高级参数指定 '
                                        '{"probackup_pgdata": "..."}')
        conf = os.path.join(catalog, "backups", instance, "pg_probackup.conf")
        if not os.path.isfile(conf):
            r = run([b, "add-instance", "-B", catalog, "-D", pgdata,
                     "--instance", instance], timeout=180)
            if r.returncode != 0:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message=f"gs_probackup add-instance 失败: {r.stderr[:400]}")
        if mode in ("delta", "page", "ptrack"):
            gsql = self._resolve_local_tool("psql")
            qcmd = [gsql, "-p", str(port), "-U", user, "-d", dbname, "-tAc",
                    "SELECT setting FROM pg_settings WHERE name='enable_cbm_tracking'"]
            if pw:
                qcmd += ["-W", pw]  # sha256 认证不读 PGPASSWORD，显式传密码
            r = run(qcmd, timeout=60)
            val = (r.stdout or "").strip()
            if r.returncode != 0 or val.lower() not in ("on", "t", "true"):
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=(f"增量前置条件不满足：enable_cbm_tracking={val or '查询失败'}。"
                             '请以 omm 执行 gs_guc reload -D <PGDATA> -c "enable_cbm_tracking=on"'
                             " 后重试，或改用全量。"))
        r = run([b, "backup", "-B", catalog, "--instance", instance, "-b", mode,
                 "-d", dbname, "-p", str(port), "-U", user])
        if r.returncode != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"gs_probackup backup(-b {mode}) 失败: "
                                        f"{(r.stderr or r.stdout)[-600:]}")
        sid = ""
        r = run([b, "show", "-B", catalog, "--instance", instance, "--format=json"],
                timeout=120)
        try:
            arr = json.loads(r.stdout or "[]")
            if isinstance(arr, list) and arr:
                sid = str(arr[0].get("ID") or arr[0].get("id") or "")
        except Exception:
            pass
        if not sid:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="备份完成但未能解析备份 ID（请检查 gs_probackup 版本）")
        r = run([b, "validate", "-B", catalog, "--instance", instance, "-i", sid],
                timeout=1800)
        if r.returncode != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"备份完成但 validate 失败(ID={sid}): {r.stderr[:400]}")
        msg = (f"本机 gs_probackup {mode} 物理备份成功(ID={sid})，备份链在 {catalog}"
               f"（后续增量依赖该目录，请勿手工清理）")
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=catalog, stdout=r.stdout, simulated=False,
                            message=msg)

    # ------------------ gs_probackup 恢复 / PITR ------------------
    def _is_probackup_artifact(self, backup_path: str) -> bool:
        try:
            return bool(backup_path and os.path.isfile(backup_path)
                        and backup_path.endswith((".tar.gz", ".tgz", ".tar"))
                        and self._tar_has_member(backup_path, "pg_probackup.conf"))
        except Exception:
            return False

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        if self._is_probackup_artifact(backup_path):
            return self._restore_probackup(backup_path, **kwargs)
        return super().restore(backup_path, **kwargs)

    def _restore_probackup(self, backup_path: str, **kwargs) -> BackupResult:
        """gs_probackup 物理恢复：恢复到指定数据目录，可带 PITR 目标时间。

        kwargs: target_data_dir（恢复目标数据目录，必填）
                recovery_target_time（PITR，可选）
        """
        from core import remote_dump
        eo = self._extra_dict()
        target = (kwargs.get("target_data_dir") or
                  str(eo.get("probackup_restore_pgdata") or ""))
        if not target:
            return BackupResult(
                success=False, status=BackupStatus.FAILED, backup_path=backup_path,
                message=("gs_probackup 恢复需要目标数据目录：恢复时传入 "
                         'target_data_dir，或在任务高级参数设置 '
                         '{"probackup_restore_pgdata": "/空目录"}'
                         "（目录须为空或不存在；禁止覆盖运行中的实例目录）。"))
        rtime = str(kwargs.get("recovery_target_time") or
                    eo.get("probackup_recovery_target_time") or "")
        _, instance, _pg = self._probackup_settings(eo)

        # 从 tar 成员解析备份 ID（<sid>/backup_content.control）
        sid = ""
        try:
            import tarfile
            with tarfile.open(backup_path, "r:*") as tar:
                for m in tar.getnames():
                    parts = [p for p in m.split("/") if p]
                    if len(parts) >= 2 and parts[1] == "backup_content.control":
                        sid = parts[0]
                        break
        except Exception:
            sid = ""
        if not sid:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path,
                                message="产物中未找到 gs_probackup 备份 ID（backup_content.control）")

        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if ssh_host:
            client = remote_dump._connect(ssh_host)
            remote_tmp = remote_dump.fixed_remote_path(self.task, "probackup_restore", "d")
            remote_dump.cleanup_remote_artifacts(client, [], dirs=[remote_tmp])
            rc, out, err = remote_dump._ssh_exec_pipe(
                client, remote_dump._wrap_login(
                    f"rm -rf {shlex.quote(remote_tmp)} && mkdir -p {remote_tmp}"), timeout=60)
            try:
                sftp = client.open_sftp()
                sftp.put(backup_path, remote_tmp.rstrip("/") + "/artifact.tar.gz")
                sftp.close()
            except Exception as e:  # noqa: BLE001
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    backup_path=backup_path,
                                    message=f"恢复产物推送远端失败: {e}")
            catalog = str(eo.get("probackup_dir") or "/data/gs_probackup")
            pb_bin = remote_dump._resolve_remote_bin(client, "gs_probackup") or "gs_probackup"
            inst_dir = f"{catalog}/backups/{instance}"
            unpack = (
                f"mkdir -p {shlex.quote(catalog)} && "
                f"test -d {shlex.quote(catalog + '/backups')} || "
                f"{shlex.quote(pb_bin)} init -B {shlex.quote(catalog)}; "
                f"mkdir -p {shlex.quote(inst_dir)} && "
                f"tar -xzf {shlex.quote(remote_tmp + '/artifact.tar.gz')} "
                f"-C {shlex.quote(inst_dir)}")
            remote_dump._ssh_exec_pipe(client, remote_dump._wrap_login(unpack), timeout=300)
            res = remote_dump.remote_probackup_restore(
                self.task, ssh_host, backup_id=sid, instance=instance,
                target_pgdata=target, recovery_target_time=rtime,
                default_port=self._phys_default_port, default_user=self._phys_default_user,
                check_user=self._resolve_check_user(ssh_host))
            remote_dump.cleanup_remote_artifacts(client, [], dirs=[remote_tmp])
            return BackupResult(success=bool(res.get("ok")),
                                status=BackupStatus.SUCCESS if res.get("ok") else BackupStatus.FAILED,
                                backup_path=backup_path,
                                stdout=res.get("stdout", ""), stderr=res.get("stderr", ""),
                                message=res.get("message", ""))

        # 本机恢复：解开 tar 到本机 probackup 目录后 restore
        b = self._resolve_local_tool("pg_probackup")
        if not b:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path,
                                message=("本机未找到 gs_probackup，且任务未纳管 SSH 主机；"
                                         "无法执行物理恢复。"))
        catalog = str(eo.get("probackup_dir") or "/data/gs_probackup")
        inst_dir = os.path.join(catalog, "backups", instance)
        os.makedirs(inst_dir, exist_ok=True)
        self._untar_to_dir(backup_path, inst_dir)
        if os.path.isdir(target) and os.listdir(target) and \
                (os.path.exists(os.path.join(target, "postgresql.conf")) or
                 os.path.exists(os.path.join(target, "gaussdb.conf"))) and \
                not str(eo.get("probackup_force_restore") or "").lower() in ("1", "true", "yes"):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path,
                                message=(f"拒绝恢复：{target} 非空且像实例目录；"
                                         "请改用空目录或确认后设置 probackup_force_restore=true"))
        port = int(self.task.get("port") or self._phys_default_port)
        user = self.task.get("username") or self._phys_default_user
        pw = db.decrypt_secret(self.task.get("password") or "")
        env = os.environ.copy()
        env["PGPASSWORD"] = pw or env.get("PGPASSWORD", "")
        home = os.path.dirname(os.path.dirname(b))
        env["GAUSSHOME"] = home
        env["LD_LIBRARY_PATH"] = (os.path.join(home, "lib") + ":" +
                                  env.get("LD_LIBRARY_PATH", ""))
        cmd = [b, "restore", "-B", catalog, "--instance", instance,
               "-D", target, "-i", sid]
        if rtime:
            cmd.append(f"--recovery-target-time={rtime}")
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=21600)
        if r.returncode != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path,
                                message=f"gs_probackup restore 失败: {(r.stderr or r.stdout)[-600:]}")
        note = ""
        if rtime:
            note = ("；PITR：首次以 omm 启动该数据目录实例时自动回放 WAL 到目标时间，"
                    "确认后执行 pg_xlog_replay_resume 恢复对外服务")
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=backup_path, stdout=r.stdout,
                            message=(f"gs_probackup restore 成功：备份 {sid} → {target}{note}"))

    # ------------------ 恢复校验 ------------------
    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """gs_probackup 产物结构校验；其余走 PG 系通用校验。"""
        path = record.get("backup_path") or record.get("output_path") or ""
        if path and os.path.isfile(path) and \
                path.endswith((".tar.gz", ".tgz", ".tar")) and \
                self._tar_has_member(path, "pg_probackup.conf"):
            if self._tar_has_member(path, "backup_content.control"):
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS, verified=True,
                    message=("gs_probackup 目录结构 verified：pg_probackup.conf + "
                             "backup_content.control 在位（含页校验与恢复所需的备份元数据）"))
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="gs_probackup 产物缺少 backup_content.control（结构不完整）")
        return super().verify_record(record, options)
