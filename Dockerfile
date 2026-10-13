# AIDBM（AI 原生智能数据库灾备管理平台）—— 离线运行 Docker 镜像
# 构建：docker build -t backup-platform:local .
# 运行：docker run -d -p 8080:8080 -v /data/backup-platform:/data backup-platform:local
#
# 镜像内已烘焙全部 Python 依赖（含 pymysql/psycopg2/oracledb 原生直连驱动）+
# JRE + drivers/ JDBC jar，运行时零联网、零外部安装；
# 直连（连接测试/拉库列表/数据对比）默认走原生 Python 驱动，无需 Java；
# JRE + JDBC 作为可选兜底通道（如 Oracle 11g 瘦模式不支持时）。
# 元数据/备份/日志持久化到 /data 挂载卷。
#
# 【镜像自足：备份/恢复工具随镜像烘焙，启动即用（无需宿主机挂载）】
# 构建前在平台机执行 scripts/docker_prep_tools.sh 生成 .docker-bake/，
# 把 MySQL/MariaDB 一族备份恢复所需工具拷进镜像：
#   - MySQL 8.0.40 客户端 mysql/mysqldump/mysqlbinlog/... 与 mysqld（物理恢复
#     临时实例校验）位于 /opt/mysql840b/（自带 private openssl，可直接运行）
#   - Percona XtraBackup 8.0 → /usr/bin/xtrabackup（MySQL 8.0+）
#   - Percona XtraBackup 2.4 → /opt/xtrabackup24/usr/bin/xtrabackup（MySQL 5.5-5.7）
#   - MariaDB Backup      → /opt/mariabackup/usr/bin/mariabackup（MariaDB 10.x）
#   - 上述 CentOS7 编译二进制依赖的旧版动态库 → /opt/toolpack-libs64/
#     （libssl.so.10 等 Debian 镜像缺失，经 LD_LIBRARY_PATH 注入）
# 其余数据库类型（金仓/Oracle/MSSQL 等）经 SSH 通道使用 DBMS 自带工具执行
# 备份（远端零安装）；PG 客户端（pg_dump/pg_restore/psql/pg_basebackup）随
# 镜像烘焙——覆盖「PG 与平台同机（容器部署）」场景，Debian bookworm 默认
# PG 15 客户端可操作 PG 14/15 服务器。
# 达梦原生驱动 dmPython（cp311 预编译 C 扩展 + libdmdpi 动态库）随镜像烘焙
# （dm-driver/，离线零安装；直连探测/恢复硬核对通道），v1.4.17 容器实测补齐。
# 未运行 docker_prep_tools.sh 时 .docker-bake 仅含 .keep，仍可构建
# “纯应用”镜像（CI 场景），备份工具相关功能按既有提示走。

# 3.11 而非 3.12/3.14：dmPython 只有 cp311 预编译 C 扩展（达梦本地编译，
# 非 PyPI 包），3.12 镜像无法加载；oracledb 等在 3.11 均有预编译 wheel。
# （3.14 基础镜像下 pip 报 "Could not find a version ... (from versions: none)"
# 导致镜像构建失败（本地与 GitHub Actions 同样复现）
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    BACKUP_ROOT=/data/backups \
    INSTANCE_DIR=/data/instance \
    LOG_DIR=/data/logs \
    LOG_LEVEL=INFO \
    LOG_MAX_BYTES=20971520 \
    LOG_BACKUP_COUNT=10 \
    OPLOG_RETENTION_DAYS=30

# 运行期日志（失败排查入口，均位于 /data/logs，随挂载卷持久化）：
#   /data/logs/platform.log    全量日志（默认 20MB × 10 份轮转）—— 容器 / 可执行文件通用
#   /data/logs/error.log       仅 ERROR 及以上（排查时先看这份）
#   /data/logs/crash.log       段错误 / 致命崩溃栈（faulthandler）
#   /data/logs/operations/     每次备份/恢复的独立详细日志（命令原文、退出码、
#                              stdout/stderr 全文、各阶段耗时）
# 查看方式：
#   docker logs -f <container>                      实时控制台（含启动横幅：日志/备份目录）
#   docker exec <container> tail -f /data/logs/error.log
#   docker exec <container> ls -lt /data/logs/operations/$(date +%Y%m%d)
#   平台界面「日志」页 → 操作日志 / 诊断包导出（一键 zip，可离线外发）
# 日志目录不可写时会自动降级到用户目录/临时目录，并在启动横幅与页面标出真实路径。

# tzdata 供时区；default-jre-headless（OpenJDK 17）仅供 JDBC 可选兜底通道；
# gzip/zstd 供备份产物压缩 CLI；libaio1/libnuma1 供烘焙的 mysqld 临时校验实例；
# perl 供烘焙的 xtrabackup 2.4/8.0 包装脚本（slim 镜像 perl-base 缺 English.pm，
# 容器实测报 "Can't locate English.pm in @INC"）；libdbd-mysql-perl/libdbi-perl
# 供 xtrabackup 2.4 连接 MySQL（容器实测报 "DBD::mysql module is not installed"）；
# postgresql-client 供 PG 与平台同机（容器部署）场景的本机备份/恢复
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       default-jre-headless \
       tzdata \
       gzip \
       zstd \
       libaio1 \
       libnuma1 \
       perl \
       libdbi-perl \
       libdbd-mysql-perl \
       postgresql-client \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用层缓存）：requirements.txt 为主，JDBC 兜底另装 jpype/jaydebeapi
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir jpype1 jaydebeapi

# 再拷贝应用代码与资源（drivers/ JDBC 驱动 jar 供 JDBC 兜底通道加载）
COPY app.py run.py init_db.py config.py auth.py start.sh ./
COPY core/ ./core/
COPY api/ ./api/
COPY static/ ./static/
COPY templates/ ./templates/
COPY drivers/ ./drivers/
COPY tools/xtrabackup_libs/ /opt/xtrabackup_libs/
COPY skills/ ./skills/
COPY tools/ ./tools/

# 达梦原生驱动 dmPython（cp311 C 扩展）+ libdmdpi 运行库：离线零安装直连通道。
# 布局与宿主机一致（lib 在 /app/lib，libdmdpi 按 ./lib 相对路径找 libcrypto）
COPY dm-driver/lib/ /app/lib/
COPY dm-driver/dmPython/ /opt/dm_pyext/

RUN chmod +x start.sh \
    # 运行时持久化目录（挂载卷）
    && mkdir -p /data/backups /data/instance /data/logs

# 烘焙 MySQL/MariaDB 备份恢复工具（布局与宿主机一致，config 默认路径即命中；
# 未执行 docker_prep_tools.sh 时该层为空目录，不影响构建）
COPY .docker-bake/ /

# 工具注入 PATH；CentOS7 旧版动态库经 LD_LIBRARY_PATH 提供给烘焙二进制；
# /app/lib 供 libdmdpi（dmPython）；/opt/dm_pyext 供 dmPython 模块导入；
# XTRABACKUP/MARIABACKUP 路径显式化（与 config.py 默认值一致）
ENV PATH="/opt/mysql840b/bin:${PATH}" \
    LD_LIBRARY_PATH="/app/lib:/opt/toolpack-libs64" \
    PYTHONPATH="/opt/dm_pyext" \
    XTRABACKUP_8_PATH=/usr/bin/xtrabackup \
    XTRABACKUP_24_PATH=/opt/xtrabackup24/usr/bin/xtrabackup \
    MARIABACKUP_PATH=/opt/mariabackup/usr/bin/mariabackup

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/login',timeout=4).status==200 else 1)" || exit 1

CMD ["python", "run.py"]
