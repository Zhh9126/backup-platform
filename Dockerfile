# 数据备份管理平台 —— 离线运行 Docker 镜像
# 构建：docker build -t backup-platform:local .
# 运行：docker run -d -p 8080:8080 -v /data/backup-platform:/data backup-platform:local
#
# 镜像内已烘焙全部 Python 依赖与 JRE（JDBC 桥接用），
# 运行时零联网、零外部安装；元数据/备份/日志持久化到 /data 挂载卷。

FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    BACKUP_ROOT=/data/backups \
    INSTANCE_DIR=/data/instance \
    LOG_DIR=/data/logs

# default-jre-headless（OpenJDK 17）满足 JDBC 桥接（ojdbc11 需 Java 11+）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       default-jre-headless \
       tzdata \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝应用代码与资源（drivers/ JDBC 驱动 jar、skills/、static/、templates/ 一并打入）
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
