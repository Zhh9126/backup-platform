# 数据备份管理平台 —— 离线运行 Docker 镜像
# 构建：docker build -t backup-platform:local .
# 运行：docker run -d -p 8080:8080 -v /data/backup-platform:/data backup-platform:local
#
# 镜像内已烘焙全部 Python 依赖（含 pymysql/psycopg2/oracledb 原生直连驱动），
# 运行时零联网、零外部安装；
# 直连（连接测试/拉库列表/数据对比）默认走原生 Python 驱动，无需 Java；
# JRE + drivers/ JDBC jar 仅作为可选兜底通道（如 Oracle 11g 瘦模式不支持时）。
# 元数据/备份/日志持久化到 /data 挂载卷。
#
# 【物理备份零安装（平台推送二进制）】
# 物理 xtrabackup/mariabackup 二进制体积大且与远端主机 OS 相关，默认不烘焙
# 进镜像，部署时从宿主机只读挂载（数据库服务器零安装，平台按服务器版本
# 自动选择并推送到远端 /tmp 执行）：
#   docker run ... \
#     -v /opt/xtrabackup24:/opt/xtrabackup24:ro \   # xtrabackup 2.4（MySQL 5.5-5.7）
#     -v /opt/mariabackup:/opt/mariabackup:ro \     # mariabackup（MariaDB 10.x）
#     -v /opt/xtrabackup8:/opt/xtrabackup8:ro       # xtrabackup 8.0（MySQL 8.0+，可选，覆盖内置路径）
# 路径可用环境变量 XTRABACKUP_8_PATH / XTRABACKUP_24_PATH / MARIABACKUP_PATH 覆盖。

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    BACKUP_ROOT=/data/backups \
    INSTANCE_DIR=/data/instance \
    LOG_DIR=/data/logs

# tzdata 供时区；default-jre-headless（OpenJDK 17）仅供 JDBC 可选兜底通道
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       default-jre-headless \
       tzdata \
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
COPY skills/ ./skills/

RUN chmod +x start.sh \
    # 运行时持久化目录（挂载卷）
    && mkdir -p /data/backups /data/instance /data/logs

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/login',timeout=4).status==200 else 1)" || exit 1

CMD ["python", "run.py"]
