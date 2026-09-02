# -*- coding: utf-8 -*-
"""
数据库部署引擎：参考 00-【知识库】下的各数据库单机安装脚本，通过 SSH 在目标主机上
安装 MySQL / PostgreSQL / Oracle / Kingbase / Redis / Dameng。

通用流程：
1. 通过 SFTP 上传安装包到目标主机
2. 生成参数化的安装脚本并上传
3. 通过 SSH 执行脚本，流式捕获输出
4. 更新部署记录的状态和进度
"""
import os
import json
import time
import threading

import core.db as db
from core import models

_logger = db.get_logger("deploy")

# 各类数据库部署参数模板（键名与前端表单对齐）
DEPLOY_PARAMS = {
    "mysql": {
        "base_dir": "/usr/local/mysql",
        "data_dir": "/data/mysql",
        "port": 3306,
        "password": "123456",
        "version": "9.3.0",
        "glibc_version": "2.17",
    },
    "postgresql": {
        "base_dir": "/usr/local/pgsql",
        "data_dir": "/data/pgsql",
        "port": 5432,
        "password": "postgres123",
    },
    "oracle": {
        "base_dir": "/u01/app/oracle",
        "data_dir": "/u01/oradata",
        "port": 1521,
        "password": "oracle123",
        "sid": "orcl",
    },
    "kingbase": {
        "base_dir": "/opt/Kingbase/ES/V9",
        "data_dir": "/data/kingbase",
        "port": 54321,
        "password": "kingbase123",
    },
    "redis": {
        "base_dir": "/usr/local/redis",
        "data_dir": "/data/redis",
        "port": 6379,
        "password": "redis123",
    },
    "dameng": {
        "base_dir": "/opt/dmdbms",
        "data_dir": "/data/dmdbms",
        "port": 5236,
        "password": "dameng123",
    },
    "mongodb": {
        "base_dir": "/usr/local/mongodb",
        "data_dir": "/data/mongodb",
        "port": 27017,
        "password": "mongo123",
    },
}

# 安装脚本模板路径（项目根下的 scripts/ 目录）
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")


def _get_ssh_client(host_id: int):
    """从 ssh_hosts 表获取连接参数并建立 SSH 连接。"""
    from core import ssh_hosts
    h = ssh_hosts.get_host(host_id, include_secret=True)
    if not h:
        raise RuntimeError(f"SSH 主机 #{host_id} 不存在")
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    hostname = h.get("hostname") or h.get("host") or ""
    port = h.get("port") or 22
    user = h.get("username") or "root"
    pw = db.decrypt_secret(h.get("password") or "")
    client.connect(hostname, port=port, username=user, password=pw, timeout=30,
                   allow_agent=False, look_for_keys=False)
    return client, h


def _get_ssh_client_from_dep(dep: dict):
    """根据部署记录建立 SSH 连接。优先 host_id（纳管主机），否则用 direct_* 字段。"""
    host_id = dep.get("host_id")
    if host_id:
        return _get_ssh_client(host_id)
    # 模式2：直接输入 IP/账号/密码
    # direct_* 可能直接存在 dep，也可能存在 config_json 中
    cfg = {}
    if dep.get("config_json"):
        try: cfg = json.loads(dep["config_json"])
        except Exception: cfg = {}
    direct_host = (dep.get("direct_host") or cfg.get("direct_host") or "").strip()
    if not direct_host:
        raise RuntimeError("目标主机未配置：请选择已纳管主机或直接输入 IP/账号/密码")
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    port = int(dep.get("direct_port") or cfg.get("direct_port") or 22)
    user = (dep.get("direct_user") or cfg.get("direct_user") or "root").strip()
    pw = dep.get("direct_password") or cfg.get("direct_password") or ""
    if not pw:
        raise RuntimeError("SSH 密码未填")
    client.connect(direct_host, port=port, username=user, password=pw, timeout=30,
                   allow_agent=False, look_for_keys=False)
    # 构造一个类似 ssh_hosts 的字典返回
    h = {
        "host": direct_host, "hostname": dep.get("hostname") or direct_host,
        "port": port, "username": user, "password": pw,
        "id": None, "name": f"direct-{direct_host}",
    }
    return client, h


def _build_install_script(db_type: str, params: dict) -> str:
    """根据 db_type 和完整参数生成安装脚本。所有参数均从 config_json 读取。"""
    base = params.get("base_dir", "/opt/database")
    data = params.get("data_dir", "/data/db")
    port = params.get("port", 3306)
    pw = params.get("password", "Admin123!")
    pkg = params.get("package_path", "")
    version = params.get("version", "")

    if db_type == "mysql":
        charset = params.get("mysql_charset", "utf8mb4")
        maxconn = params.get("mysql_max_conn", "200")
        buffer = params.get("mysql_buffer", "512")
        srv_id = params.get("mysql_server_id", "1")
        binlog = params.get("mysql_binlog", "1")

        return f"""#!/bin/bash
# MySQL 多版本部署脚本（5.5/5.6/5.7/8.0/8.4/9.x）
# 关键步骤失败时显式 exit 1，让后端能根据返回码判断部署失败。
set +e
echo "[deploy] ====== MySQL {version} 部署开始 ======"
echo "[deploy] BASE={base}  DATA={data}  PORT={port}  CHARSET={charset}"

# ---------- 前置检查：避免误删用户已有数据 ----------
echo "[deploy] 前置环境检查..."
if pgrep -x mysqld >/dev/null 2>&1 || pgrep -x mariadbd >/dev/null 2>&1; then
    echo "[deploy] ERROR: 检测到目标机已有 MySQL/MariaDB 进程在运行。"
    echo "[deploy] ERROR: 请先停止并清理现有实例后再部署（systemctl stop mysql / service mysql stop）。"
    exit 1
fi
if [ -d "{data}" ] && [ "$(ls -A {data} 2>/dev/null)" ]; then
    echo "[deploy] ERROR: 数据目录 {data} 已存在文件，为避免误删数据，请先手动清理后重新部署。"
    echo "[deploy] ERROR: 清理命令参考：systemctl stop mysql; rm -rf {data}/* {base} /etc/my.cnf /root/.my.cnf"
    exit 1
fi
if [ -f "/etc/my.cnf" ] || [ -f "/etc/mysql/my.cnf" ]; then
    echo "[deploy] WARN: 检测到已有 MySQL 配置文件 /etc/my.cnf 或 /etc/mysql/my.cnf。"
    echo "[deploy] WARN: 若残留配置导致启动异常，请先备份并删除后再部署。"
fi

mkdir -p {base} {data} /data/backup/mysql
echo "[deploy] 解压安装包..."
tar -xf {pkg} -C {base} --strip-components=1
id mysql &>/dev/null || useradd -r -s /bin/false mysql

# ---------- 探测版本 ----------
MYSQLD={base}/bin/mysqld
[ ! -x "$MYSQLD" ] && [ -x {base}/bin/mysqld-debug ] && MYSQLD={base}/bin/mysqld-debug
RAW_VER=$($MYSQLD --version 2>/dev/null | grep -oiE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)
[ -z "$RAW_VER" ] && RAW_VER=$({base}/bin/mysql --version 2>/dev/null | grep -oiE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)
MAJOR=$(echo "$RAW_VER" | cut -d. -f1)
MINOR=$(echo "$RAW_VER" | cut -d. -f2)
echo "[deploy] 探测到 MySQL 版本: $RAW_VER (major=$MAJOR minor=$MINOR)"

# ---------- 初始化数据目录 ----------
MYSQL_INSTALL_DB={base}/bin/mysql_install_db
init_rc=0
if [ -x "$MYSQLD" ] && [ "$MAJOR" -ge 5 ] && ([ "$MINOR" -ge 7 ] || [ "$MAJOR" -ge 8 ]); then
    echo "[deploy] 使用 mysqld --initialize-insecure (MySQL 5.7+ / 8.0+ / 9.x)"
    # --no-defaults 必须是第一个参数：屏蔽 /etc/my.cnf 等外部配置中残留的
    # datadir/log-bin 等设置，避免初始化读到旧实例配置而失败
    $MYSQLD --no-defaults --initialize-insecure --user=mysql --datadir={data} --basedir={base} 2>&1 | head -30
    init_rc=${{PIPESTATUS[0]}}
    if [ $init_rc -ne 0 ]; then
        # 常见原因：datadir 有残留 / libaio 缺失
        echo "[deploy] ERROR: mysqld --initialize-insecure 初始化失败 (rc=$init_rc)。"
        echo "[deploy] ERROR: 请检查数据目录是否为空、是否已安装 libaio/numactl。"
        exit 1
    fi
elif [ -x "$MYSQL_INSTALL_DB" ]; then
    echo "[deploy] 使用 mysql_install_db (MySQL 5.6 及更早)"
    $MYSQL_INSTALL_DB --user=mysql --datadir={data} --basedir={base} 2>&1 | head -30
    init_rc=${{PIPESTATUS[0]}}
    if [ $init_rc -ne 0 ]; then
        echo "[deploy] ERROR: mysql_install_db 初始化失败 (rc=$init_rc)。"
        exit 1
    fi
else
    echo "[deploy] ERROR: 未找到可用的初始化工具，请检查安装包是否完整。"
    ls {base}/bin/ | head -20
    exit 1
fi

# ---------- 写入 /etc/my.cnf ----------
echo "[deploy] 写入 /etc/my.cnf ..."
# mysqlx 默认端口为 33060；若用户把 MySQL 主端口也设为 33060 会冲突导致启动失败，
# 因此显式将 mysqlx_port 设为主端口+1，避免绑定 "Address already in use"。
MYSQLX_PORT=$(( {port} + 1 ))
cat > /etc/my.cnf << 'EOF'
[mysqld]
basedir={base}
datadir={data}
port={port}
mysqlx_port={int(port)+1}
socket=/tmp/mysql.sock
bind-address=0.0.0.0
character-set-server={charset}
default-storage-engine=InnoDB
max_connections={maxconn}
innodb_buffer_pool_size={buffer}M
server-id={srv_id}
{"log-bin=" + data + "/mysql-bin" if binlog == "1" else "# binlog disabled"}
[mysql]
socket=/tmp/mysql.sock
default-character-set={charset}
[client]
socket=/tmp/mysql.sock
EOF
chown -R mysql:mysql {base} {data}

# ---------- 安装 mysql.server 启动脚本（便于后续管理，但不一定用于启动）----------
echo "[deploy] 安装 mysql.server 启动脚本..."
if [ -f {base}/support-files/mysql.server ]; then
    cp -f {base}/support-files/mysql.server /etc/init.d/mysql
    chmod +x /etc/init.d/mysql
    # 尝试注册为系统服务；忽略失败，某些精简系统没有 chkconfig/systemctl
    chkconfig --add mysql >/dev/null 2>&1 || systemctl enable mysql >/dev/null 2>&1 || true
else
    echo "[deploy] WARN: 安装包中未找到 support-files/mysql.server，将仅使用 mysqld_safe/mysqld 直接启动。"
fi

# ---------- 启动 MySQL（优先 mysqld_safe，已在 CentOS7/8/9 验证稳定）----------
echo "[deploy] 启动 MySQL..."
start_rc=0
if [ -x {base}/bin/mysqld_safe ]; then
    echo "[deploy] 使用 mysqld_safe 启动 (最通用，兼容 5.x/8.0/9.x)"
    nohup {base}/bin/mysqld_safe --user=mysql --datadir={data} >/var/log/mysqld_safe.log 2>&1 &
    start_rc=0
elif [ -x {base}/bin/mysqld ]; then
    echo "[deploy] mysqld_safe 不存在，使用 mysqld --daemonize 启动"
    nohup {base}/bin/mysqld --user=mysql --datadir={data} --daemonize >/var/log/mysqld.log 2>&1 &
    start_rc=0
else
    # 兜底：尝试 mysql.server
    echo "[deploy] 使用 /etc/init.d/mysql start 兜底启动"
    /etc/init.d/mysql start 2>&1 | head -20
    start_rc=${{PIPESTATUS[0]}}
fi

# 循环等待 socket 就绪（mysql 命令加 --connect-timeout，避免半开连接挂起导致无限等待）
SOCK=/tmp/mysql.sock
echo "[deploy] 等待 MySQL socket 就绪 ($SOCK)..."
wait_ok=0
for i in $(seq 1 60); do
    if [ -S "$SOCK" ]; then
        {base}/bin/mysql --connect-timeout=3 -u root -e "SELECT 1;" >/dev/null 2>&1
        if [ $? -eq 0 ]; then
            wait_ok=1
            break
        fi
    fi
    sleep 1
done
if [ $wait_ok -ne 1 ]; then
    echo "[deploy] ERROR: MySQL 启动后 60 秒内 socket 仍未就绪。"
    echo "[deploy] ERROR: 启动日志参考："
    tail -n 30 {data}/error.log 2>/dev/null || tail -n 30 /var/log/mysqld_safe.log 2>/dev/null || true
    exit 1
fi
echo "[deploy] MySQL socket 已就绪"

# ---------- 设置 root 密码 + 开放远程访问 ----------
echo "[deploy] 设置 root 密码..."
MYSQLCLI="{base}/bin/mysql -u root"
# MySQL 8.0+ 默认 caching_sha2_password，老驱动/老客户端可能连不上；
# 显式用 mysql_native_password 以获得最大兼容性。
if [ "$MAJOR" -ge 8 ]; then
    AUTH_PLUGIN="mysql_native_password"
else
    AUTH_PLUGIN=""
fi

# 各版本构造“单条核心语句”（ALTER/CREATE/GRANT），逐条执行，避免一条失败导致整体 rc!=0 误判。
# 注意：8.0.11+ 不允许 GRANT ... WITH GRANT OPTION 组合；GRANT PROXY 在 9.x 会报错，故省略。
build_stmts() {{
    local auth_clause=""
    [ -n "$AUTH_PLUGIN" ] && auth_clause=" IDENTIFIED WITH $AUTH_PLUGIN BY '{pw}'" || auth_clause=" IDENTIFIED BY '{pw}'"
    if [ "$MAJOR" -eq 5 ] && [ "$MINOR" -lt 7 ]; then
        # 5.5 / 5.6（无 ALTER USER，用 SET PASSWORD + GRANT 隐式建用户）
        echo "SET PASSWORD FOR 'root'@'localhost' = PASSWORD('{pw}');"
        echo "GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost' IDENTIFIED BY '{pw}' WITH GRANT OPTION;"
        echo "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' IDENTIFIED BY '{pw}' WITH GRANT OPTION;"
        echo "FLUSH PRIVILEGES;"
    else
        # 5.7.6+ / 8.x / 9.x
        echo "ALTER USER 'root'@'localhost' IDENTIFIED BY '{pw}';"
        echo "CREATE USER IF NOT EXISTS 'root'@'%'$auth_clause;"
        echo "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;"
        echo "FLUSH PRIVILEGES;"
    fi
}}

# 逐条执行（用 --connect-expired-password 应对初始空密码 / 过期密码）
echo "[deploy] 逐条执行授权语句..."
build_stmts | while IFS= read -r stmt; do
    [ -z "$stmt" ] && continue
    echo "[deploy]   > $stmt"
    $MYSQLCLI --connect-expired-password -e "$stmt" 2>&1 | head -5
    # 单条失败仅告警，不致命（例如某些 9.x 对 WITH GRANT OPTION 的细微差异）
done

# 以“能否用新密码登录”作为最终成功判据（而非批处理 rc）
echo "[deploy] 使用新密码验证登录..."
LOGIN_OK=0
for i in $(seq 1 10); do
    $MYSQLCLI -p'{pw}' -e "SELECT VERSION();" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        LOGIN_OK=1
        break
    fi
    sleep 1
done
if [ $LOGIN_OK -ne 1 ]; then
    echo "[deploy] ERROR: 使用 root 密码登录验证失败。"
    exit 1
fi

# ---------- 写入 /root/.my.cnf 并验证 ----------
cat > /root/.my.cnf << 'MYCNF'
[client]
user=root
password={pw}
socket=/tmp/mysql.sock
MYCNF
chmod 600 /root/.my.cnf
echo "[deploy] 验证 root 密码登录..."
{base}/bin/mysql -e "SELECT VERSION();" 2>&1 | head -3
if [ $? -ne 0 ]; then
    echo "[deploy] ERROR: 使用 root 密码登录验证失败。"
    exit 1
fi

# 验证远程端口监听
ss -lntp 2>/dev/null | grep -E ':{port}\\s' || netstat -lntp 2>/dev/null | grep -E ':{port}\\s' || true

echo "[deploy] 写入 /etc/profile.d/mysql.sh ..."
cat > /etc/profile.d/mysql.sh << 'PROFILEEOF'
export MYSQL_HOME={base}
export PATH=$MYSQL_HOME/bin:$PATH
export MYSQL_DATADIR={data}
PROFILEEOF
chmod 644 /etc/profile.d/mysql.sh
ldconfig

echo "[deploy] MySQL 部署完成! 版本=$RAW_VER 端口={port}  数据目录={data}"
echo "提示：新开 SSH 终端前请先 source /etc/profile 或重新登录"
echo "DEPLOY_OK"
exit 0

"""
    elif db_type == "postgresql":
        encoding = params.get("pg_encoding", "UTF8")
        pg_maxconn = params.get("pg_max_conn", "100")
        pg_shared = params.get("pg_shared_buf", "128MB")
        pg_wal = params.get("pg_wal", "replica")
        pg_locale = params.get("pg_locale", "zh_CN.UTF-8")

        return f"""#!/bin/bash
set -e
echo "[deploy] ====== PostgreSQL {version} 部署开始 ======"
mkdir -p {base} {data}
tar -xf {pkg} -C {base} --strip-components=1
id postgres &>/dev/null || useradd -r -s /bin/false postgres
chown -R postgres:postgres {base} {data}
# 兼容 PG 14+（pg_createcluster 也可），本脚本采用通用 initdb
INITDB={base}/bin/initdb
if [ ! -x "$INITDB" ]; then
    # PG 14-17 in some distros installs to /usr/lib/postgresql/<ver>/bin/
    INITDB=$(ls -d /usr/lib/postgresql/*/bin/initdb 2>/dev/null | head -1)
    if [ -n "$INITDB" ]; then echo "[deploy] 使用发行版 initdb: $INITDB"; fi
fi
echo "[deploy] 初始化数据库 (encoding={encoding}, locale={pg_locale})..."
su - postgres -c "$INITDB -D {data} --encoding={encoding} --locale={pg_locale}" || \\
su - postgres -c "$INITDB -D {data} --encoding={encoding} --no-locale"
# 配置 pg_hba.conf 允许密码认证（远程连入用）
PG_HBA={data}/pg_hba.conf
cat > $PG_HBA << 'HBAEOF'
local   all             all                                     peer
host    all             all             127.0.0.1/32            md5
host    all             all             ::1/128                 md5
host    all             all             0.0.0.0/0               md5
HBAEOF
chown postgres:postgres $PG_HBA
cat >> {data}/postgresql.conf << 'CONFEOF'
port={port}
listen_addresses='*'
max_connections={pg_maxconn}
shared_buffers={pg_shared}
wal_level={pg_wal}
CONFEOF
echo "[deploy] 启动 PostgreSQL..."
su - postgres -c "{base}/bin/pg_ctl -D {data} -l {data}/pg.log -o '-p {port}' start" 2>&1 | head -5
sleep 3
# 设置密码（多种方式都试）
su - postgres -c "{base}/bin/psql -c \\"ALTER USER postgres WITH PASSWORD '{pw}';\\" 2>&1" | head -3
su - postgres -c "{base}/bin/psql -c 'SELECT version();' 2>&1" | head -3
echo "[deploy] 写入 /etc/profile.d/postgres.sh ..."
cat > /etc/profile.d/postgres.sh << 'PROFILEEOF'
export PG_HOME={base}
export PGDATA={data}
export PATH=$PG_HOME/bin:$PATH
PROFILEEOF
chmod 644 /etc/profile.d/postgres.sh
ldconfig
echo "[deploy] PostgreSQL 部署完成! 端口={port}  数据目录={data}  环境变量: /etc/profile.d/postgres.sh"
echo "提示：新开 SSH 终端前请先 source /etc/profile 或重新登录"
echo "DEPLOY_OK"

"""
    elif db_type == "oracle":
        sid = params.get("ora_sid", "orcl")
        charset = params.get("ora_charset", "AL32UTF8")
        ncharset = params.get("ora_ncharset", "AL16UTF16")
        mem_pct = params.get("ora_mem", "40")
        cdb = params.get("ora_cdb", "1")
        pdb = params.get("ora_pdb", "pdb")
        # 预判 ORACLE_HOME 路径（写入 profile.d 时直接用字面值）
        orcl_home = f"{base}/product/19c/dbhome_1"

        return f"""#!/bin/bash
set -e
echo "[deploy] ====== Oracle {version} 部署开始 ======"
echo "[deploy] SID={sid}  CHARSET={charset}  CDB={cdb}  MEM={mem_pct}%"
echo "[deploy] 安装包: {pkg}"
echo "[deploy] 请确保 /etc/hosts 已正确配置主机名"
echo "[deploy] 安装目录: {base}"
mkdir -p {base} /u01/{sid} /u01/oradata
echo "[deploy] 解压安装包..."
if [[ "{pkg}" == *.zip ]]; then
    unzip -q {pkg} -d {base}/
elif [[ "{pkg}" == *.tar.gz ]]; then
    tar -xzf {pkg} -C {base}/
fi
echo "[deploy] 安装包解压完成"
echo "[deploy] Oracle 安装需在图形环境或静默模式下进行"
# Oracle 11g/12c/19c/21c/26ai 的 ORACLE_HOME 不一样
ORACLE_HOME={base}/product/19c/dbhome_1
if [ -d "{base}/product/19c" ]; then ORACLE_HOME=$(ls -d {base}/product/19c/dbhome_* 2>/dev/null | head -1); fi
if [ -d "{base}/product/21c" ]; then ORACLE_HOME=$(ls -d {base}/product/21c/dbhome_* 2>/dev/null | head -1); fi
if [ -d "{base}/product/12.2.0/dbhome_1" ]; then ORACLE_HOME={base}/product/12.2.0/dbhome_1; fi
if [ -d "{base}/product/11.2.0/dbhome_1" ]; then ORACLE_HOME={base}/product/11.2.0/dbhome_1; fi
echo "[deploy] 写入 /etc/profile.d/oracle.sh (ORACLE_HOME=$ORACLE_HOME)..."
cat > /etc/profile.d/oracle.sh << 'PROFILEEOF'
export ORACLE_BASE={base}
export ORACLE_HOME={orcl_home}
export ORACLE_SID={sid}
export PATH=$ORACLE_HOME/bin:$PATH
export LD_LIBRARY_PATH=$ORACLE_HOME/lib:$LD_LIBRARY_PATH
PROFILEEOF
chmod 644 /etc/profile.d/oracle.sh
echo "[deploy] 建议参数: -silent -responseFile $ORACLE_HOME/assistants/dbca/dbca.rsp"
echo "[deploy] 字符集: {charset}  国家字符集: {ncharset}  内存占比: {mem_pct}%"
echo "[deploy] Oracle 部署准备完成 (SID={sid} 字符集={charset} CDB={cdb} PDB={pdb})"
echo "DEPLOY_OK"

"""
    elif db_type == "kingbase":
        kb_mode = params.get("kb_mode", "pg")
        kb_encoding = params.get("kb_encoding", "UTF8")
        kb_maxconn = params.get("kb_max_conn", "200")
        kb_shared = params.get("kb_shared_buf", "256MB")

        return f"""#!/bin/bash
set -e
echo "[deploy] ====== Kingbase {version} 部署开始 ======"
echo "[deploy] MODE={kb_mode}  ENCODING={kb_encoding}  PORT={port}"
mkdir -p {base} {data}
id kingbase &>/dev/null || useradd -r -s /bin/false kingbase
chown -R kingbase:kingbase /opt/Kingbase
echo "[deploy] 解压安装包..."
tar -xf {pkg} -C {base} --strip-components=1
echo "[deploy] 使用 sys_ctl 初始化..."
su - kingbase -c "{base}/Server/bin/initdb -D {data} --encoding={kb_encoding} --mode={kb_mode}"
cat >> {data}/kingbase.conf << 'EOF'
port={port}
listen_addresses='*'
max_connections={kb_maxconn}
shared_buffers={kb_shared}
EOF
su - kingbase -c "{base}/Server/bin/sys_ctl -D {data} start" 2>&1 | head -5
sleep 3
# Kingbase 兼容两种 ALTER USER 语法
su - kingbase -c "{base}/Server/bin/ksql -c \\"ALTER USER SYSTEM WITH PASSWORD '{pw}';\\"" 2>&1 | head -3
echo "[deploy] 写入 /etc/profile.d/kingbase.sh ..."
cat > /etc/profile.d/kingbase.sh << 'PROFILEEOF'
export KB_HOME={base}
export KB_DATA={data}
export PATH=$KB_HOME/Server/bin:$PATH
export LD_LIBRARY_PATH=$KB_HOME/Server/lib:$LD_LIBRARY_PATH
PROFILEEOF
chmod 644 /etc/profile.d/kingbase.sh
ldconfig
echo "[deploy] Kingbase 部署完成! 端口={port}  数据目录={data}  环境变量: /etc/profile.d/kingbase.sh"
echo "DEPLOY_OK"

"""
    elif db_type == "dameng":
        dm_instance = params.get("dm_instance", "DMSERVER")
        dm_charset = params.get("dm_charset", "1")
        dm_page = params.get("dm_page", "32")
        dm_case = params.get("dm_case", "1")
        dm_sysdba = params.get("dm_sysdba", "Dameng123")

        return f"""#!/bin/bash
set -e
echo "[deploy] ====== DM 达梦 {version} 部署开始 ======"
echo "[deploy] INSTANCE={dm_instance}  CHARSET={dm_charset}  PAGE_SIZE={dm_page}"
mkdir -p {base} {data}
id dmdba &>/dev/null || useradd -r -s /bin/false dmdba
chown -R dmdba:dmdba {base} {data}
echo "[deploy] 解压安装包..."
if [[ "{pkg}" == *.iso ]] && command -v mount &>/dev/null; then
    mkdir -p /tmp/dm_mnt
    mount -o loop,ro {pkg} /tmp/dm_mnt 2>/dev/null || (mkdir -p /tmp/dm_mnt && tar -xf {pkg} -C /tmp/dm_mnt)
    DMINST=$(ls /tmp/dm_mnt/DMInstall.bin 2>/dev/null)
elif [[ "{pkg}" == *.iso ]] || [[ "{pkg}" == *.bin ]]; then
    mkdir -p /tmp/dm_mnt
    cp {pkg} /tmp/dm_mnt/DMInstall.bin 2>/dev/null || true
    chmod +x /tmp/dm_mnt/DMInstall.bin 2>/dev/null
    DMINST=/tmp/dm_mnt/DMInstall.bin
else
    mkdir -p /tmp/dm_mnt
    tar -xf {pkg} -C /tmp/dm_mnt 2>/dev/null
    DMINST=$(find /tmp/dm_mnt -name "DMInstall.bin" 2>/dev/null | head -1)
fi
if [ -z "$DMINST" ] || [ ! -f "$DMINST" ]; then
    echo "[deploy] 未在安装包中找到 DMInstall.bin，请手动挂载 ISO 或解压"
    echo "[deploy] 提示: DM 8 安装需要 X 图形环境（runInstaller）或静默参数"
    DM_HOME_BIN={base}/bin
else
    echo "[deploy] 检测到 DM 安装程序: $DMINST"
    echo "[deploy] 由于达梦需要图形环境，这里只准备静默安装所需的目录与环境"
fi
echo "[deploy] 准备 dminit 初始化数据库..."
# 寻找 dminit
DMINIT={base}/bin/dminit
if [ ! -x "$DMINIT" ] && [ -x /opt/dmdbms/bin/dminit ]; then
    DMINIT=/opt/dmdbms/bin/dminit
    chown -R dmdba:dmdba /opt/dmdbms
fi
if [ -x "$DMINIT" ]; then
    echo "[deploy] 使用 dminit 初始化..."
    su - dmdba -c "$DMINIT PATH={data} INSTANCE_NAME={dm_instance} PORT_NUM={port} CHARSET={dm_charset} PAGE_SIZE={dm_page} CASE_SENSITIVE={dm_case} SYSDBA_PWD={dm_sysdba} 2>&1" | head -20
    echo "[deploy] 启动 DM 服务..."
    DMSERVER={base}/bin/dmserver
    if [ -x "$DMSERVER" ] && [ -d "{data}/{dm_instance}" ]; then
        su - dmdba -c "nohup {base}/bin/dmserver path={data}/{dm_instance} > /var/log/dmserver.log 2>&1 &"
        sleep 3
        echo "[deploy] DM 服务已启动"
    fi
else
    echo "[deploy] 未找到 dminit，请先安装达梦数据库 (PATH=$DMINIT)"
fi
echo "[deploy] 写入 /etc/profile.d/dameng.sh ..."
cat > /etc/profile.d/dameng.sh << 'PROFILEEOF'
export DM_HOME={base}
export PATH=$DM_HOME/bin:$PATH
export LD_LIBRARY_PATH=$DM_HOME/bin:$LD_LIBRARY_PATH
PROFILEEOF
chmod 644 /etc/profile.d/dameng.sh
ldconfig
echo "[deploy] 达梦部署完成 (instance={dm_instance} SYSDBA={dm_sysdba})"
echo "DEPLOY_OK"

"""
    elif db_type == "redis":
        redis_maxmem = params.get("redis_maxmem", "1gb")
        redis_evict = params.get("redis_evict", "allkeys-lru")
        redis_aof = params.get("redis_aof", "yes")
        redis_cluster = params.get("redis_cluster", "")
        dep_pkg = params.get("dep_pkg", "")
        if dep_pkg and dep_pkg != "NONE":
            dep_lines = f"""
# ====== 安装依赖包（如 gcc，离线 RPM）======
echo "[deploy] 检测 gcc / 编译工具链..."
if command -v gcc >/dev/null 2>&1; then
    echo "[deploy] gcc 已存在，跳过依赖安装"
else
    echo "[deploy] 未检测到 gcc，开始安装依赖包: {dep_pkg}"
    mkdir -p {data}/dependency
    if [[ "{dep_pkg}" == *.zip ]]; then
        command -v unzip >/dev/null 2>&1 && unzip -o -q {dep_pkg} -d {data}/dependency || (cd {data}/dependency && python3 -c "import zipfile,sys;zipfile.ZipFile('{dep_pkg}').extractall('.')")
    else
        tar -xf {dep_pkg} -C {data}/dependency 2>/dev/null || cp -r {dep_pkg}/* {data}/dependency/ 2>/dev/null
    fi
    RPM_LIST=$(find {data}/dependency -name '*.rpm' 2>/dev/null)
    if [ -n "$RPM_LIST" ]; then
        echo "[deploy] 安装 RPM: $RPM_LIST"
        rpm -Uvh --force --nodeps $RPM_LIST 2>&1 | tail -20 || echo "[deploy] 部分 RPM 安装失败，继续"
    fi
    DEP_INSTALL=$(find {data}/dependency -maxdepth 3 -name 'install.sh' 2>/dev/null | head -1)
    if [ -n "$DEP_INSTALL" ]; then
        echo "[deploy] 执行依赖安装脚本: $DEP_INSTALL"
        bash "$DEP_INSTALL" 2>&1 | tail -20
    fi
    if command -v gcc >/dev/null 2>&1; then
        echo "[deploy] gcc 安装成功"
    else
        echo "[deploy] 警告: gcc 仍未就绪，源码编译可能失败"
    fi
fi
"""
        else:
            dep_lines = """
# 依赖包未提供，直接尝试编译（若无 gcc 会失败）
echo "[deploy] 未提供依赖包，直接尝试编译"
"""

        cluster_cfg = ""
        if redis_cluster:
            cluster_cfg = f"cluster-enabled yes\ncluster-config-file {data}/nodes.conf\ncluster-node-timeout 5000\n"

        return f"""#!/bin/bash
set -e
echo "[deploy] ====== Redis {version} 部署开始 ======"
mkdir -p {data}
{dep_lines}
# 清理可能存在的旧残留目录，避免误用上次失败的源码
echo "[deploy] 清理可能残留的旧安装目录..."
rm -rf {base} {data}/redis_* /tmp/redis_build 2>/dev/null || true
mkdir -p {base} {data}
echo "[deploy] 解压 Redis 源码包 -> /tmp/redis_build ..."
mkdir -p /tmp/redis_build
tar -xf {pkg} -C /tmp/redis_build
SRC_DIR=$(find /tmp/redis_build -maxdepth 1 -type d -name 'redis-*' 2>/dev/null | head -1)
[ -z "$SRC_DIR" ] && SRC_DIR=/tmp/redis_build/redis-6.2.6
echo "[deploy] 源码目录: $SRC_DIR"
cd "$SRC_DIR"
echo "[deploy] 编译 Redis (MALLOC=libc)..."
make MALLOC=libc
echo "[deploy] 安装 Redis..."
make install
mkdir -p {base}/bin
cp -f "$SRC_DIR"/src/redis-server {base}/bin/
cp -f "$SRC_DIR"/src/redis-cli {base}/bin/
cp -f "$SRC_DIR"/src/redis-sentinel {base}/bin/ 2>/dev/null || true
cp -f "$SRC_DIR"/src/redis-check-rdb {base}/bin/ 2>/dev/null || true
cp -f "$SRC_DIR"/src/redis-check-aof {base}/bin/ 2>/dev/null || true
cp -f "$SRC_DIR"/src/redis-benchmark {base}/bin/ 2>/dev/null || true
echo "[deploy] Redis 二进制已安装到 {base}/bin"
id redis &>/dev/null || useradd -r -s /bin/false redis
chown -R redis:redis {base} {data}
cat > {base}/redis.conf << EOF
bind 0.0.0.0
port {port}
requirepass {pw}
dir {data}
logfile {data}/redis.log
pidfile {data}/redis.pid
daemonize yes
maxmemory {redis_maxmem}
maxmemory-policy {redis_evict}
appendonly {redis_aof}
tcp-keepalive 60
timeout 300
{cluster_cfg}
EOF
{base}/bin/redis-server {base}/redis.conf 2>&1 | head -5
sleep 2
if REDISCLI_AUTH={pw} {base}/bin/redis-cli PING 2>/dev/null | grep -q PONG; then
    echo "[deploy] Redis 启动成功 (PONG)"
else
    echo "[deploy] 警告: PING 未返回 PONG，见上方日志"
fi
cat > /usr/local/bin/redis-start.sh << 'STARTEOF'
#!/bin/bash
{base}/bin/redis-server {base}/redis.conf
STARTEOF
chmod +x /usr/local/bin/redis-start.sh
echo "[deploy] 写入 /etc/profile.d/redis.sh ..."
cat > /etc/profile.d/redis.sh << 'PROFILEEOF'
export REDIS_HOME={base}
export PATH=$REDIS_HOME/bin:$PATH
PROFILEEOF
chmod 644 /etc/profile.d/redis.sh
ldconfig
echo "[deploy] Redis 部署完成! 端口={port}  环境变量: /etc/profile.d/redis.sh"
echo "DEPLOY_OK"

"""
    elif db_type == "mongodb":
        mongo_repl = params.get("mongo_repl", "")
        mongo_cache = params.get("mongo_cache", "1")
        mongo_auth = params.get("mongo_auth", "1")

        repl_cfg = ""
        if mongo_repl:
            repl_cfg = f'replication:\n  replSetName: "{mongo_repl}"\n'

        auth_cfg = 'authorization: enabled' if mongo_auth == "1" else '# authorization: disabled'
        # 认证开启时，需要先以无认证方式启动创建管理员账号，再重启启用认证
        need_auth_user = (mongo_auth == "1")

        return f"""#!/bin/bash
set +e
echo "[deploy] ====== MongoDB {version} 部署开始 ======"
echo "[deploy] BASE={base}  DATA={data}  PORT={port}  AUTH={mongo_auth}  REPL={mongo_repl}"

# ---------- 前置检查 ----------
echo "[deploy] 前置环境检查..."
if pgrep -x mongod >/dev/null 2>&1; then
    echo "[deploy] ERROR: 检测到目标机已有 mongod 进程在运行，请先停止后再部署。"
    exit 1
fi

# ---------- 解压安装包 ----------
mkdir -p {base} {data}/conf
if [ ! -f "{pkg}" ]; then
    echo "[deploy] ERROR: 安装包不存在: {pkg}"
    exit 1
fi
echo "[deploy] 解压安装包: {pkg} -> {base}"
tar -xf {pkg} -C {base} --strip-components=1
if [ $? -ne 0 ]; then
    echo "[deploy] ERROR: 解压失败，请检查安装包是否完整。"
    exit 1
fi

id mongod &>/dev/null || useradd -r -s /bin/false mongod
chown -R mongod:mongod {base} {data}

MONGOD={base}/bin/mongod
if [ ! -x "$MONGOD" ]; then MONGOD=$(find {base} -name "mongod" -type f -executable 2>/dev/null | head -1); fi
if [ -z "$MONGOD" ]; then echo "[deploy] ERROR: 找不到 mongod 二进制"; exit 1; fi
echo "[deploy] mongod 路径: $MONGOD"

# ---------- 生成配置文件（先写无认证版本，便于初始化账号）----------
cat > {base}/mongod.conf << EOF
systemLog:
  destination: file
  path: {data}/mongod.log
  logAppend: true
storage:
  dbPath: {data}
  wiredTiger:
    engineConfig:
      cacheSizeGB: {mongo_cache}
net:
  port: {port}
  bindIp: 0.0.0.0
{repl_cfg}EOF
if [ "{mongo_auth}" != "1" ]; then
    cat >> {base}/mongod.conf << 'EOF2'
security:
  authorization: disabled
EOF2
fi

# ---------- 启动 mongod（fork 模式）----------
echo "[deploy] 启动 mongod..."
$MONGOD --config {base}/mongod.conf --fork 2>&1 | head -20
start_rc=$?
if [ $start_rc -ne 0 ]; then
    echo "[deploy] ERROR: mongod 启动失败，日志末尾："
    tail -n 30 {data}/mongod.log 2>/dev/null || true
    exit 1
fi

# ---------- 等待端口就绪（用 mongo/mongosh 客户端 ping 探测，最可靠）----------
echo "[deploy] 等待 MongoDB 端口 {port} 就绪..."
CLI={base}/bin/mongosh
[ ! -x "$CLI" ] && CLI=$(which mongosh 2>/dev/null || which mongo 2>/dev/null)
[ ! -x "$CLI" ] && CLI={base}/bin/mongo
ready=0
for i in $(seq 1 60); do
    if [ -x "$CLI" ] && $CLI --port {port} --quiet --eval "db.runCommand({{ping:1}}).ok" 2>/dev/null | grep -q 1; then
        ready=1
        break
    fi
    sleep 1
done
if [ $ready -ne 1 ]; then
    echo "[deploy] ERROR: MongoDB 端口 {port} 在限时内未就绪"
    tail -n 30 {data}/mongod.log 2>/dev/null || true
    exit 1
fi
echo "[deploy] MongoDB 版本: $($CLI --port {port} --quiet --eval "db.version()" 2>&1 | head -1)"
echo "[deploy] mongod 启动完成"

# ---------- 创建管理员账号（认证开启时）----------
if [ "{mongo_auth}" = "1" ]; then
    echo "[deploy] 创建管理员账号 admin / 指定密码..."
    CLI={base}/bin/mongosh
    [ ! -x "$CLI" ] && CLI=$(which mongosh 2>/dev/null || which mongo 2>/dev/null)
    [ ! -x "$CLI" ] && CLI={base}/bin/mongo
    if [ -z "$CLI" ] || [ ! -x "$CLI" ]; then
        echo "[deploy] WARN: 未找到 mongosh/mongo 客户端，跳过账号创建（认证启用后需手动建账号）"
    else
        # 副本集 + 认证必须配置 keyFile，否则 mongod 启动报错
        if [ -n "{mongo_repl}" ]; then
            echo "[deploy] 生成副本集 keyFile..."
            if command -v openssl >/dev/null 2>&1; then
                openssl rand -base64 756 > {base}/keyfile
            else
                head -c 756 /dev/urandom | base64 > {base}/keyfile
            fi
            chmod 600 {base}/keyfile
            chown mongod:mongod {base}/keyfile
        fi
        # 若配置了副本集，需先初始化单节点副本集，否则无法创建用户（not master）
        if [ -n "{mongo_repl}" ]; then
            echo "[deploy] 初始化副本集 {mongo_repl} ..."
            REPL_NAME="{mongo_repl}"
            REPL_PORT={port}
            cat > /tmp/rs_init.js << RSEOF
rs.initiate({{
  _id: "{mongo_repl}",
  members: [{{ _id: 0, host: "127.0.0.1:{port}" }}]
}})
RSEOF
            $CLI --port {port} admin --quiet --eval "$(cat /tmp/rs_init.js)" 2>&1 | head -5
            echo "[deploy] 等待副本集变为 PRIMARY ..."
            for i in $(seq 1 30); do
                role=$($CLI --port {port} --quiet --eval "rs.status().myState" 2>/dev/null | head -1)
                if [ "$role" = "1" ]; then
                    echo "[deploy] 副本集已为 PRIMARY"
                    break
                fi
                sleep 1
            done
        fi
        echo "[deploy] 创建 admin 账号..."
        $CLI --port {port} admin --quiet --eval '
            db.createUser({{ user: "admin", pwd: "{pw}", roles: [ {{ role: "root", db: "admin" }} ] }})
        ' 2>&1 | head -5
        # 关闭 mongod 并以认证模式重启
        echo "[deploy] 关闭 mongod 并以认证模式重启..."
        $CLI --port {port} admin -u admin -p "{pw}" --authenticationDatabase admin --quiet --eval "db.shutdownServer()" 2>&1 | head -3 || true
        sleep 1
        $CLI --port {port} admin --quiet --eval "db.shutdownServer()" 2>&1 | head -3 || true
        # 等待 mongod 真正退出（端口释放）
        for i in $(seq 1 30); do
            if ! ss -lntp 2>/dev/null | grep -qE ":{port}\\s" && ! netstat -lntp 2>/dev/null | grep -qE ":{port}\\s"; then
                break
            fi
            sleep 1
        done
        # 启用 authorization（追加 security 段到配置文件末尾，避免 sed 转义问题）
        if [ -n "{mongo_repl}" ]; then
            cat >> {base}/mongod.conf << 'SECEOF'

security:
  authorization: enabled
  keyFile: {base}/keyfile
SECEOF
        else
            cat >> {base}/mongod.conf << 'SECEOF'

security:
  authorization: enabled
SECEOF
        fi
        $MONGOD --config {base}/mongod.conf --fork 2>&1 | head -10
        # 用新账号验证登录
        AUTH_OK=0
        for i in $(seq 1 10); do
            if $CLI --port {port} -u admin -p "{pw}" --authenticationDatabase admin --quiet --eval "db.runCommand({{ping:1}})" >/dev/null 2>&1; then
                AUTH_OK=1
                break
            fi
            sleep 1
        done
        if [ $AUTH_OK -ne 1 ]; then
            echo "[deploy] ERROR: 使用 admin 账号认证登录验证失败。"
            exit 1
        fi
        echo "[deploy] 管理员账号认证验证通过"
    fi
fi

echo "[deploy] 写入 /etc/profile.d/mongodb.sh ..."
cat > /etc/profile.d/mongodb.sh << 'PROFILEEOF'
export MONGO_HOME={base}
export PATH=$MONGO_HOME/bin:$PATH
PROFILEEOF
chmod 644 /etc/profile.d/mongodb.sh
ldconfig
echo "[deploy] MongoDB 部署完成! 端口={port}  数据目录={data}  环境变量: /etc/profile.d/mongodb.sh"
echo "提示：新开 SSH 终端前请先 source /etc/profile 或重新登录"
echo "DEPLOY_OK"

"""
    else:
        raise RuntimeError(f"不支持的数据库类型: {db_type}")


def run_deployment(dep_id: int) -> None:
    """后台线程执行数据库部署。"""
    dep = models.get_deployment(dep_id)
    if not dep:
        _logger.error("[deploy] 部署记录 #%s 不存在", dep_id)
        return

    models.update_deployment(dep_id, {"status": "running", "started_at": db.now_iso()})
    _logger.info("[deploy] 开始部署 #%s (%s)", dep_id, dep.get("name"))

    log_buf = []
    def log(msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        log_buf.append(line)
        _logger.info("[deploy #%s] %s", dep_id, msg)
        # 每写 5 行刷一次 DB（避免过度 I/O）
        if len(log_buf) % 5 == 0:
            models.update_deployment(dep_id, {"log_output": "\n".join(log_buf)})

    try:
        log(f"连接部署目标...")
        client, h = _get_ssh_client_from_dep(dep)
        log(f"已连接: {h.get('hostname') or h.get('host')}")

        # 上传安装包（如果提供了本地路径）
        pkg_path = (dep.get("package_path") or "").strip()
        log(f"部署包路径: {pkg_path}")
        if pkg_path:
            if os.path.isfile(pkg_path):
                remote_pkg = "/tmp/" + os.path.basename(pkg_path)
                # 本机部署防自毁：目标主机即平台本机时 remote_pkg 可能与
                # 源路径相同，sftp.put 自己到自己会把包截断成 0 字节
                if os.path.abspath(remote_pkg) == os.path.abspath(pkg_path):
                    log(f"安装包已在目标主机同路径（本机部署），跳过上传: {remote_pkg}")
                else:
                    try:
                        log(f"上传安装包: {pkg_path} -> {remote_pkg}")
                        sftp = client.open_sftp()
                        sftp.put(pkg_path, remote_pkg)
                        sftp.close()
                        log(f"安装包上传完成")
                        # 更新 remote package path
                        models.update_deployment(dep_id, {"package_path": remote_pkg})
                        pkg_path = remote_pkg
                    except Exception as e:
                        log(f"安装包上传失败: {e}")
                        raise
            elif pkg_path.startswith(("/", "~")):
                # 用户填的是远程路径，检查目标主机是否存在
                log(f"检查远程包是否存在: {pkg_path}")
                out, _, rc = _ssh_exec(client, f'test -f "{pkg_path}" && echo "EXISTS" || echo "NOT_FOUND"')
                exists = 'EXISTS' in out
                log(f"远程包 {pkg_path}: {'已存在' if exists else '未找到'}")
                if not exists:
                    raise RuntimeError(f"远程安装包不存在: {pkg_path}")
            else:
                raise RuntimeError(f"安装包路径无效或本平台暂存文件已丢失: {pkg_path}")

        # 上传依赖包（如 gcc RPM 离线包，Redis 编译需要）
        dep_pkg_path = (dep.get("dependency_path") or "").strip()
        dep_pkg_remote = "NONE"
        if dep_pkg_path:
            if os.path.isfile(dep_pkg_path):
                dep_pkg_remote = "/tmp/" + os.path.basename(dep_pkg_path)
                try:
                    log(f"上传依赖包: {dep_pkg_path} -> {dep_pkg_remote}")
                    sftp = client.open_sftp()
                    sftp.put(dep_pkg_path, dep_pkg_remote)
                    sftp.close()
                    log(f"依赖包上传完成")
                except Exception as e:
                    log(f"依赖包上传失败: {e}")
                    raise
            elif dep_pkg_path.startswith(("/", "~")):
                log(f"检查远程依赖包: {dep_pkg_path}")
                out, _, _ = _ssh_exec(client, f'test -f "{dep_pkg_path}" && echo "EXISTS" || echo "NOT_FOUND"')
                dep_pkg_remote = dep_pkg_path if 'EXISTS' in out else "NONE"
                log(f"远程依赖包 {dep_pkg_path}: {'已存在' if dep_pkg_remote != 'NONE' else '未找到'}")
            else:
                log(f"依赖包路径无效: {dep_pkg_path}，将跳过依赖安装")
                dep_pkg_remote = "NONE"

        # 构建并写入安装脚本
        params = json.loads(dep.get("config_json") or "{}")
        params["package_path"] = pkg_path
        params["dep_pkg"] = dep_pkg_remote
        params["base_dir"] = dep.get("base_dir") or params.get("base_dir", "")
        params["data_dir"] = dep.get("data_dir") or params.get("data_dir", "")
        params["port"] = dep.get("port") or params.get("port", 0)
        params["password"] = dep.get("password") or params.get("password", "")

        script = _build_install_script(dep.get("db_type"), params)
        remote_script = "/tmp/deploy_install.sh"
        log("上传安装脚本...")
        # 通过 echo 写入脚本到远程（多行处理）
        _ssh_exec(client, f"cat > {remote_script} << 'SCRIPTEOF'\n{script}\nSCRIPTEOF")
        _ssh_exec(client, f"chmod +x {remote_script}")

        models.update_deployment(dep_id, {"progress_pct": 30})

        # 执行脚本
        log(f"开始执行安装脚本...")
        _, _, _ = _ssh_exec(client, f"chmod +x {remote_script}")
        models.update_deployment(dep_id, {"progress_pct": 50})

        # 流式执行并捕获输出
        transport = client.get_transport()
        session = transport.open_session()
        session.exec_command(f"bash {remote_script}")
        while not session.exit_status_ready():
            if session.recv_ready():
                chunk = session.recv(4096).decode("utf-8", "replace")
                for line in chunk.strip().split("\n"):
                    if line.strip():
                        log(line.strip())
                        if line.strip() == "DEPLOY_OK":
                            pass
            if session.recv_stderr_ready():
                chunk = session.recv_stderr(4096).decode("utf-8", "replace")
                for line in chunk.strip().split("\n"):
                    s = line.strip()
                    if not s:
                        continue
                    # 仅在明显是错误时才加 ERR: 标签；MySQL/PG 等会把 info/warning 也写 stderr
                    if _is_real_error(s):
                        log(f"ERR: {s}")
                    else:
                        log(s)
            time.sleep(0.1)
        # drain remaining
        while session.recv_ready():
            chunk = session.recv(4096).decode("utf-8", "replace")
            for line in chunk.strip().split("\n"):
                if line.strip():
                    log(line.strip())
        rc = session.recv_exit_status()

        all_log = "\n".join(log_buf)
        if rc == 0:
            log("部署完成!")
            models.update_deployment(dep_id, {
                "status": "success", "progress_pct": 100,
                "finished_at": db.now_iso(), "log_output": all_log})
            _logger.info("[deploy] #%s 部署成功", dep_id)
        else:
            log(f"部署失败 (exit={rc})")
            models.update_deployment(dep_id, {
                "status": "failed", "progress_pct": 50,
                "finished_at": db.now_iso(), "log_output": all_log})
            _logger.error("[deploy] #%s 部署失败 rc=%d", dep_id, rc)

    except Exception as e:
        log(f"部署异常: {e}")
        all_log = "\n".join(log_buf)
        models.update_deployment(dep_id, {
            "status": "failed", "progress_pct": 0,
            "finished_at": db.now_iso(), "log_output": all_log})
        _logger.exception("[deploy] #%s 部署异常", dep_id)


def _ssh_exec(client, cmd: str, timeout: int = 30):
    """在已连接的 SSH 客户端上执行命令。"""
    _, sout, serr = client.exec_command(cmd, timeout=timeout)
    out = sout.read().decode("utf-8", "replace")
    err = serr.read().decode("utf-8", "replace")
    rc = sout.channel.recv_exit_status()
    return out, err, rc


def _is_real_error(s: str) -> bool:
    """判断 stderr 一行是否真的表示错误（MySQL/PG/Redis 大量 info/warning 写 stderr）。"""
    up = s.upper()
    # 显式错误标记
    if "[ERROR]" in up or up.startswith("ERROR "):
        return True
    if "FATAL" in up and "ERROR" in up:
        return True
    # MySQL 错误格式：2025-... [ERROR] [MY-...] message
    if " [ERROR] " in s and "[MY-" in s:
        return True
    return False


def _write_env_profile(path_dir: str, lines: list) -> str:
    """生成 /etc/profile.d/XXX.sh 内容（用于持久化 PATH 等环境变量）。"""
    body = "\n".join(lines) + "\n"
    return f"""
echo "[deploy] 写入 /etc/profile.d/{os.path.basename(path_dir)}.sh ..."
cat > {path_dir} << 'PROFILEEOF'
{body}
PROFILEEOF
chmod 644 {path_dir}
"""


def run_deployment_async(dep_id: int) -> None:
    """异步执行部署（后台线程）。"""
    t = threading.Thread(target=run_deployment, args=(dep_id,), daemon=True,
                         name=f"deploy-{dep_id}")
    t.start()
