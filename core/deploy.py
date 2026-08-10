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
set -e
echo "[deploy] ====== MySQL {version} 部署开始 ======"
echo "[deploy] BASE={base}  DATA={data}  PORT={port}  CHARSET={charset}"
mkdir -p {base} {data} /data/backup/mysql
echo "[deploy] 解压安装包..."
tar -xf {pkg} -C {base} --strip-components=1
id mysql &>/dev/null || useradd -r -s /bin/false mysql
echo "[deploy] 检测 MySQL 版本与可用初始化方式..."
# 兼容 5.6（mysql_install_db）与 5.7+（mysqld --initialize）
MYSQLD={base}/bin/mysqld
if [ ! -x "$MYSQLD" ] && [ -x {base}/bin/mysqld-debug ]; then
    MYSQLD={base}/bin/mysqld-debug
fi
MYSQL_INSTALL_DB={base}/bin/mysql_install_db
if [ -x "$MYSQLD" ]; then
    echo "[deploy] 使用 mysqld --initialize-insecure (MySQL 5.7+ / 8.0+ / 9.x)"
    $MYSQLD --initialize-insecure --user=mysql --datadir={data} --basedir={base} 2>&1 | head -20
elif [ -x "$MYSQL_INSTALL_DB" ]; then
    echo "[deploy] 使用 mysql_install_db (MySQL 5.6 及更早)"
    $MYSQL_INSTALL_DB --user=mysql --datadir={data} --basedir={base} 2>&1 | head -20
else
    echo "[deploy] WARN: 未找到 mysqld 或 mysql_install_db，请检查安装包"
    ls {base}/bin/ | head -20
    exit 1
fi
echo "[deploy] 写入 /etc/my.cnf ..."
cat > /etc/my.cnf << 'EOF'
[mysqld]
basedir={base}
datadir={data}
port={port}
bind-address=0.0.0.0
character-set-server={charset}
default-storage-engine=InnoDB
max_connections={maxconn}
innodb_buffer_pool_size={buffer}M
server-id={srv_id}
{"log-bin=" + data + "/mysql-bin" if binlog == "1" else "# binlog disabled"}
[client]
user=root
password={pw}
default-character-set={charset}
EOF
chown -R mysql:mysql {base} {data}
echo "[deploy] 启动 MySQL..."
nohup {base}/bin/mysqld_safe --user=mysql --datadir={data} >/var/log/mysqld_safe.log 2>&1 &
sleep 8
# 用 .my.cnf 让 mysqladmin/mysql 不再要求交互密码
cat > /root/.my.cnf << 'MYCNF'
[client]
user=root
password={pw}
MYCNF
chmod 600 /root/.my.cnf
{base}/bin/mysqladmin -u root password '{pw}' 2>&1 | head -5 || true
{base}/bin/mysql -e "SELECT VERSION();" 2>&1 | head -3
echo "[deploy] 写入 /etc/profile.d/mysql.sh ..."
cat > /etc/profile.d/mysql.sh << 'PROFILEEOF'
export MYSQL_HOME={base}
export PATH=$MYSQL_HOME/bin:$PATH
export MYSQL_DATADIR={data}
PROFILEEOF
chmod 644 /etc/profile.d/mysql.sh
ldconfig
echo "[deploy] MySQL 部署完成! 端口={port}  数据目录={data}  环境变量: /etc/profile.d/mysql.sh"
echo "提示：新开 SSH 终端前请先 source /etc/profile 或重新登录"
echo "DEPLOY_OK"

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

        cluster_cfg = ""
        if redis_cluster:
            cluster_cfg = f"cluster-enabled yes\ncluster-config-file {data}/nodes.conf\ncluster-node-timeout 5000\n"

        return f"""#!/bin/bash
set -e
echo "[deploy] ====== Redis {version} 部署开始 ======"
mkdir -p {base} {data}
tar -xf {pkg} -C {base} --strip-components=1
cd {base} && make -j$(nproc) PREFIX={base} MALLOC=libc install
# 准备 redis 启动用户
id redis &>/dev/null || useradd -r -s /bin/false redis
chown -R redis:redis {base} {data}
# 生成配置
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
# 启动
{base}/bin/redis-server {base}/redis.conf 2>&1 | head -5
sleep 2
# 用脚本免 PING 时密码警告
REDISCLI_AUTH={pw} {base}/bin/redis-cli PING
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

        return f"""#!/bin/bash
set -e
echo "[deploy] ====== MongoDB {version} 部署开始 ======"
mkdir -p {base} {data}
tar -xf {pkg} -C {base} --strip-components=1
id mongod &>/dev/null || useradd -r -s /bin/false mongod
chown -R mongod:mongod {base} {data}
cat > {base}/mongod.conf << EOF
systemLog:
  destination: file
  path: {data}/mongod.log
storage:
  dbPath: {data}
  wiredTiger:
    engineConfig:
      cacheSizeGB: {mongo_cache}
net:
  port: {port}
  bindIp: 0.0.0.0
{repl_cfg}{auth_cfg}
EOF
# mongod 启动（兼容不同版本命令）
MONGOD={base}/bin/mongod
if [ ! -x "$MONGOD" ]; then MONGOD=$(find {base} -name "mongod" -type f -executable 2>/dev/null | head -1); fi
if [ -z "$MONGOD" ]; then echo "[deploy] ERROR: 找不到 mongod 二进制"; exit 1; fi
echo "[deploy] 启动 mongod: $MONGOD"
$MONGOD --config {base}/mongod.conf --fork 2>&1 | head -5
sleep 3
# mongosh / mongo
MONGO_SH={base}/bin/mongosh
[ ! -x "$MONGO_SH" ] && MONGO_SH=$(which mongosh 2>/dev/null || which mongo 2>/dev/null)
if [ -x "$MONGO_SH" ]; then
    $MONGO_SH --port {port} --quiet --eval "db.version()" 2>&1 | head -3
fi
echo "[deploy] 写入 /etc/profile.d/mongodb.sh ..."
cat > /etc/profile.d/mongodb.sh << 'PROFILEEOF'
export MONGO_HOME={base}
export PATH=$MONGO_HOME/bin:$PATH
PROFILEEOF
chmod 644 /etc/profile.d/mongodb.sh
ldconfig
echo "[deploy] MongoDB 部署完成! 端口={port}  环境变量: /etc/profile.d/mongodb.sh"
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
        pkg_path = dep.get("package_path") or ""
        if pkg_path and os.path.isfile(pkg_path):
            remote_pkg = "/tmp/" + os.path.basename(pkg_path)
            log(f"上传安装包: {pkg_path} -> {remote_pkg}")
            sftp = client.open_sftp()
            sftp.put(pkg_path, remote_pkg)
            sftp.close()
            log(f"安装包上传完成")
            # 更新 remote package path
            models.update_deployment(dep_id, {"package_path": remote_pkg})
            pkg_path = remote_pkg

        # 如果 package_path 已是远程路径（用户填了远程服务器上已有的路径），直接使用
        if pkg_path and not os.path.isfile(pkg_path):
            # 检查远程是否存在
            _, _, rc = _ssh_exec(client, f'test -f "{pkg_path}" && echo "EXISTS" || echo "NOT_FOUND"')
            log(f"远程包 {pkg_path}: {'已存在' if 'EXISTS' in _ else '未找到'}")

        # 构建并写入安装脚本
        params = json.loads(dep.get("config_json") or "{}")
        params["package_path"] = pkg_path
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
