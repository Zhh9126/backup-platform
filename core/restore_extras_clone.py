# -*- coding: utf-8 -*-
"""
克隆扩展能力：秒级 COW 模板克隆 / LVM 快照克隆 / Oracle schema 克隆。

- pg_template_clone：PostgreSQL CREATE DATABASE ... TEMPLATE —— 真 COW，
  文件引用复制，秒级完成。
- mysql_snapshot_clone：MySQL LVM 快照克隆（快照卷 + 独立端口实例）；
  源不在 LVM 时明确报错，不静默退化为逻辑导入。
- oracle_schema_clone：expdp 源 schema → impdp REMAP_SCHEMA 克隆到同实例
  新 schema（行业标准逻辑克隆做法）。
- drop_clone_extended：oracle 等 schema 级克隆的清理。
"""
import os
import time
import shutil
import subprocess
from typing import Dict, Any


def pg_template_clone(src_db: str, clone_name: str,
                      pg_host: str = "127.0.0.1", pg_port: int = None,
                      pg_user: str = "postgres",
                      pg_password: str = "") -> Dict[str, Any]:
    """PostgreSQL 模板库秒级克隆（真 COW：文件引用复制，秒级完成）。

    原理：CREATE DATABASE ... TEMPLATE src —— PostgreSQL 以写时复制
    (copy-on-write) 方式克隆源库目录树，不拷贝数据文件，秒级返回。
    限制：克隆期间源库不能有其他连接（PG 会直接报错），且限同实例。
    """
    import psycopg2
    start = time.time()
    conn = psycopg2.connect(host=pg_host, port=pg_port or 5432,
                            user=pg_user, password=pg_password,
                            dbname="postgres", connect_timeout=10)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(f'DROP DATABASE IF EXISTS "{clone_name}"')
        try:
            cur.execute(f'CREATE DATABASE "{clone_name}" TEMPLATE "{src_db}"')
        except psycopg2.Error as e:
            err = str(e).strip()
            if "being accessed by other users" in err:
                return {"ok": False, "elapsed_sec": round(time.time() - start, 2),
                        "message": (f"模板克隆失败：源库 {src_db} 正被其他连接使用"
                                    "（COW 克隆要求源库克隆期间无其他连接）")}
            return {"ok": False, "elapsed_sec": round(time.time() - start, 2),
                    "message": f"模板克隆失败: {err[:250]}"}
    finally:
        cur.close()
        conn.close()
    elapsed = round(time.time() - start, 2)
    return {"ok": True, "elapsed_sec": elapsed, "cow": True,
            "message": f"秒级克隆完成（COW 模板方式，{elapsed}s）",
            "connection": f"psql -h {pg_host} -p {pg_port} -U {pg_user} -d {clone_name}",
            "instance_name": clone_name, "port": pg_port}


def mysql_snapshot_clone(instance_name: str, src_port: int = 3306,
                         mysql_host: str = "127.0.0.1",
                         mysql_user: str = "root",
                         mysql_password: str = "") -> Dict[str, Any]:
    """MySQL LVM 快照秒级克隆：快照源数据目录 + 独立端口拉起新实例。

    1) 取源 datadir，检测其所在块设备是否 LVM 逻辑卷；
    2) 是 → lvcreate -s 快照 → 挂载 → 独立配置（新端口/新 server-uuid）
       启动克隆实例；
    3) 源不在 LVM → 明确报错（不静默退化为逻辑导入，避免"假秒级"）。
    """
    env = os.environ.copy()
    if mysql_password:
        env["MYSQL_PWD"] = mysql_password
    r = subprocess.run(
        ["mysql", "--no-defaults", "-h", mysql_host, "-P", str(src_port),
         "-u", mysql_user, "-N", "-e", "SELECT @@datadir, @@version"],
        env=env, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return {"ok": False, "message": f"连接源实例失败: {r.stderr.strip()[:200]}"}
    parts = r.stdout.strip().split("\t")
    datadir = parts[0] if parts else ""
    if not datadir:
        return {"ok": False, "message": "无法获取源实例 datadir"}
    r = subprocess.run(["df", "--output=source", datadir],
                       capture_output=True, text=True, timeout=15)
    dev = r.stdout.strip().splitlines()[-1] if r.returncode == 0 else ""
    base_dev = dev.rstrip("0123456789") or dev
    r2 = subprocess.run(["lsblk", "--noheadings", "-o", "TYPE", base_dev],
                        capture_output=True, text=True, timeout=15)
    if r2.returncode != 0 or "lvm" not in r2.stdout:
        return {"ok": False,
                "message": (f"源数据目录 {datadir}（设备 {dev}）不在 LVM 逻辑卷上，"
                            "无法执行快照(COW)秒级克隆。可选："
                            "① 将 MySQL datadir 迁移至 LVM 卷后重试；"
                            "② 使用逻辑导入克隆（流式导入）。")}
    # LVM 快照
    vg_lv = dev.split("/")[-1]           # /dev/mapper/klas-root → klas-root
    vg, lv = vg_lv.split("-", 1) if "-" in vg_lv else ("", "")
    snap = f"{lv}_clone_{int(time.time())}"
    # VG 剩余空间预检（快照预留 2G；不足时给出可行动的明确错误）
    r3 = subprocess.run(["vgs", "--noheadings", "--units", "g", "-o", "vg_free", vg],
                        capture_output=True, text=True, timeout=15)
    if r3.returncode == 0:
        try:
            free_g = float(r3.stdout.strip().split()[0].replace("g", "").replace("<", ""))
            if free_g < 2.0:
                return {"ok": False,
                        "message": (f"LVM 卷组 {vg} 剩余空间 {free_g:.1f}G 不足以创建"
                                    " 2G 快照卷。请扩容 VG（vgextend）或清理空间后重试。")}
        except (ValueError, IndexError):
            pass
    for cmd in (["lvcreate", "-s", "-n", snap, "-L", "2G", f"/dev/{vg}/{lv}"],
                ["mkdir", "-p", f"/mnt/{snap}"],
                ["mount", f"/dev/{vg}/{snap}", f"/mnt/{snap}"]):
        rc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if rc.returncode != 0:
            return {"ok": False,
                    "message": f"快照步骤失败 {' '.join(cmd[:2])}: {rc.stderr[:200]}"}
    # 独立端口拉起克隆实例（删 auto.cnf 使其生成新 server_uuid）
    clone_port = src_port + 1000
    subprocess.run(["rm", "-f", f"/mnt/{snap}/mysql/auto.cnf"], timeout=10)
    cnf = (f"[mysqld]\nport={clone_port}\nsocket=/tmp/{instance_name}.sock\n"
           f"datadir=/mnt/{snap}/mysql\npid-file=/tmp/{instance_name}.pid\n"
           f"log-error=/tmp/{instance_name}.err\nserver-id="
           f"{799900 + int(time.time()) % 100}\n")
    cnf_path = f"/tmp/{instance_name}.cnf"
    with open(cnf_path, "w") as f:
        f.write(cnf)
    mysqld_bin = shutil.which("mysqld") or "/usr/sbin/mysqld"
    rc = subprocess.run([mysqld_bin, f"--defaults-file={cnf_path}",
                         "--user=mysql", "--daemonize"],
                        capture_output=True, text=True, timeout=90)
    if rc.returncode != 0:
        return {"ok": False, "message": f"克隆实例启动失败: {rc.stderr[:250]}"}
    return {"ok": True, "cow": True, "snapshot": snap,
            "message": f"LVM 快照克隆完成（快照 {snap}，实例端口 {clone_port}）",
            "instance_name": instance_name, "port": clone_port,
            "connection": f"mysql -h 127.0.0.1 -P {clone_port} -u root"}


def oracle_schema_clone(src_schema: str, clone_schema: str,
                        host: str, port: int = 1521, service: str = "ORCL",
                        user: str = "system", password: str = "",
                        ssh_host: dict = None) -> Dict[str, Any]:
    """Oracle schema 克隆：在线 expdp 源 schema → impdp REMAP_SCHEMA 克隆。

    在数据库服务器本机（oracle 用户）执行，连接串走 127.0.0.1 回环
    （listener 对外部 IP 注册可能不稳定）。同实例 schema 克隆的行业标准做法。
    """
    if not ssh_host:
        return {"ok": False, "message": "Oracle schema 克隆需要 SSH 主机（数据库服务器）"}
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ssh_host.get("hostname"), int(ssh_host.get("port") or 22),
                   username=ssh_host.get("username"),
                   password=ssh_host.get("password"),
                   timeout=15, banner_timeout=30)

    def run(cmd: str, t: int = 900) -> str:
        _, o, e = client.exec_command(cmd + " </dev/null 2>&1", timeout=t)
        o.channel.settimeout(t)
        return o.read().decode("utf-8", "replace")

    conn = f"{user}/{password}@//127.0.0.1:{port}/{service}"
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = run(
        "su - oracle -c \"expdp %s SCHEMAS=%s DIRECTORY=DATA_PUMP_DIR "
        "DUMPFILE=clone_%s.dmp LOGFILE=clone_%s.log REUSE_DUMPFILES=Y\""
        % (conn, src_schema, ts, ts))
    if "successfully completed" not in out and "RC=0" not in out:
        client.close()
        return {"ok": False,
                "message": f"在线 expdp 导出源 schema 失败: {out[-300:]}"}
    imp = run(
        "su - oracle -c \"impdp %s DUMPFILE=clone_%s.dmp REMAP_SCHEMA=%s:%s "
        "TRANSFORM=SEGMENT_ATTRIBUTES:N\""
        % (conn, ts, src_schema, clone_schema))
    ok = "RC=0" in imp or "completed" in imp.lower()
    client.close()
    if not ok:
        return {"ok": False, "message": f"impdp 克隆导入失败: {imp[-300:]}"}
    return {"ok": True,
            "message": f"Oracle schema 克隆完成: {src_schema} → {clone_schema}",
            "instance_name": clone_schema}


def drop_clone_extended(db_type: str, instance_name: str,
                        host: str = "127.0.0.1", port: int = None,
                        user: str = "system", password: str = "",
                        service: str = "") -> Dict[str, Any]:
    """扩展类型克隆清理：oracle —— DROP USER ... CASCADE。"""
    if db_type == "oracle":
        import oracledb
        try:
            conn = oracledb.connect(user=user, password=password,
                                    dsn=f"{host}:{port or 1521}/{service or 'ORCL'}",
                                    tcp_connect_timeout=15)
        except TypeError:
            conn = oracledb.connect(user=user, password=password,
                                    dsn=f"{host}:{port or 1521}/{service or 'ORCL'}")
        except Exception as e:
            return {"ok": False, "message": f"连接失败: {str(e)[:150]}"}
        cur = conn.cursor()
        try:
            cur.execute(f'DROP USER "{instance_name}" CASCADE')
            conn.commit()
            return {"ok": True, "message": f"已删除 schema {instance_name}"}
        except Exception as e:
            return {"ok": False, "message": f"删除失败: {str(e)[:150]}"}
        finally:
            cur.close()
            conn.close()
    return {"ok": False, "message": f"不支持的类型 {db_type}"}
