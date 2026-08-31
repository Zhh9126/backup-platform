#!/bin/bash
cd /root/CodeBuddy/20260826095855/backup-platform
# JDK 仅 JDBC 可选兜底通道需要；直连走原生 Python 驱动，无 Java 也能运行
[ -d /usr/lib/jvm/java-11-openjdk-11.0.23.0.9-2.el7_9.x86_64 ] && \
    export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-11.0.23.0.9-2.el7_9.x86_64
export PATH="/opt/database/bin:$PATH"
# 一级（L1）默认本地存储：所有备份文件落地到 /opt/backup-platform
export BACKUP_ROOT="/opt/backup-platform"
exec .venv/bin/python run.py
