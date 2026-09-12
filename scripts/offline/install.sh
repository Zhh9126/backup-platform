#!/usr/bin/env bash
# =============================================================
# AIDBM（AI 原生智能数据库灾备管理平台）—— 离线一键安装器（air-gapped installer）
# 用法：
#   sudo ./install.sh --mode auto            # 自动探测（推荐）
#   sudo ./install.sh --mode docker          # 容器化部署（compose）
#   sudo ./install.sh --mode native          # venv + systemd（无 Docker）
# 可选：
#   --dir /opt/backup-platform               # 安装目录（默认 /opt/backup-platform）
#   --port 8080                              # Web 端口
#   --with-minio                             # 同时部署内置 MinIO（镜像包存在时）
#   --upgrade                                # 增量升级（保留 meta.db/配置）
# 设计对应文档：docs/dbackup-benchmark-offline-design-20260906.md 第三章
# =============================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="auto"; INSTALL_DIR="/opt/backup-platform"; WEB_PORT="8080"; WITH_MINIO=0; UPGRADE=0

log()  { echo -e "\033[32m[install]\033[0m $*"; }
warn() { echo -e "\033[33m[warn]\033[0m $*"; }
die()  { echo -e "\033[31m[error]\033[0m $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2;;
    --dir) INSTALL_DIR="$2"; shift 2;;
    --port) WEB_PORT="$2"; shift 2;;
    --with-minio) WITH_MINIO=1; shift;;
    --upgrade) UPGRADE=1; shift;;
    *) die "未知参数: $1";;
  esac
done

[[ $EUID -eq 0 ]] || die "请以 root 运行"

# ---------- 0) 环境预检 ----------
log "环境预检..."
command -v sha256sum >/dev/null || die "缺少 sha256sum"
( cd "$BASE_DIR" && sha256sum -c manifests/version.yaml >/dev/null 2>&1 ) \
  || warn "离线包校验和清单缺失或不匹配（继续，但建议核对 manifests/version.yaml）"
FREE_GB=$(df -BG --output=avail "$INSTALL_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
[[ "${FREE_GB:-0}" -ge 20 ]] || die "安装目录可用空间不足 20GB（当前 ${FREE_GB:-?}GB）"
log "磁盘可用 ${FREE_GB}GB ✓"

# ---------- 1) 模式决策 ----------
if [[ "$MODE" == "auto" ]]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    MODE="docker"; else MODE="native"; fi
fi
log "部署模式: $MODE → $INSTALL_DIR (Web:$WEB_PORT)"

# ---------- 2) 部署 ----------
if [[ "$MODE" == "docker" ]]; then
  command -v docker >/dev/null || die "Docker 不可用"
  for img in "$BASE_DIR"/images/*.tar; do
    [[ -e "$img" ]] || { warn "images/ 下无镜像包"; break; }
    log "docker load: $(basename "$img")"
    docker load -i "$img"
  done
  mkdir -p "$INSTALL_DIR"
  # 备份产物持久化目录：容器内落盘 /data/backups，映射到 ${INSTALL_DIR}/data（宿主持久卷）
  mkdir -p "$INSTALL_DIR/data/backups" "$INSTALL_DIR/data/instance" "$INSTALL_DIR/data/logs"
  cat > "$INSTALL_DIR/docker-compose.yml" <<EOF
services:
  backup-platform:
    image: ghcr.io/zhh9126/backup-platform:__BUNDLE_VERSION__
    container_name: backup-platform
    network_mode: host            # 直连内网数据库与 SSH 目标机
    environment:
      - WEB_PORT=${WEB_PORT}
      - TZ=Asia/Shanghai
      # 本地备份存储位置（L1 落点）：落在此挂载卷内，容器重建不丢数据；
      # 也可在平台「备份存储管理」页按界面配置覆盖（界面配置优先级更高）
      - BACKUP_ROOT=/data/backups
    volumes:
      - ${INSTALL_DIR}/data:/data
    restart: unless-stopped
EOF
  if [[ $WITH_MINIO -eq 1 ]]; then
    cat >> "$INSTALL_DIR/docker-compose.yml" <<EOF
  minio:
    image: minio/minio:latest
    container_name: backup-minio
    network_mode: host
    command: server /data --console-address :9001
    volumes:
      - ${INSTALL_DIR}/minio:/data
    restart: unless-stopped
EOF
  fi
  [[ $UPGRADE -eq 1 ]] && cp -a "$INSTALL_DIR/data/instance" "/tmp/meta_backup_$(date +%s)" 2>/dev/null || true
  ( cd "$INSTALL_DIR" && docker compose up -d )
  log "容器已启动"

else
  # 形态B：venv 离线安装 + systemd
  log "拷贝应用代码..."
  mkdir -p "$INSTALL_DIR"
  cp -a "$BASE_DIR/app" "$INSTALL_DIR/" 2>/dev/null || true
  for d in app core api static templates drivers skills; do
    cp -a "$BASE_DIR/$d" "$INSTALL_DIR/"
  done
  cp -a "$BASE_DIR"/*.py "$BASE_DIR/start.sh" "$BASE_DIR/requirements.txt" "$INSTALL_DIR/" 2>/dev/null || true
  cd "$INSTALL_DIR"
  log "创建 venv 并离线安装依赖（wheelhouse）..."
  python3 -m venv .venv
  .venv/bin/pip install --no-index --find-links "$BASE_DIR/wheelhouse" \
      -r requirements.txt jpype1 jaydebeapi
  log "写入 systemd 服务..."
  # 本地备份存储位置（L1 落点）：独立数据目录，避免把备份写进程序安装目录
  mkdir -p "$INSTALL_DIR/data/backups" "$INSTALL_DIR/data/logs"
  cat > /etc/systemd/system/backup-platform.service <<EOF
[Unit]
Description=Backup Platform (offline)
After=network.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python run.py
Environment=WEB_PORT=$WEB_PORT
Environment=TZ=Asia/Shanghai
Environment=BACKUP_ROOT=$INSTALL_DIR/data/backups
Environment=CODEBUDDY_SAFE_DELETE_ENABLED=0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now backup-platform
fi

# ---------- 3) 健康检查 ----------
log "等待服务就绪..."
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:${WEB_PORT}/login" || true)
  [[ "$CODE" == "200" ]] && break
  sleep 2
done
[[ "${CODE:-}" == "200" ]] || die "服务未就绪（HTTP=${CODE:-无响应}），请查日志"
log "✅ 部署完成: http://<本机IP>:${WEB_PORT}  （默认 admin/admin123，请立即改密）"
log "后续：①『设置-存储』配置 L1/L2/L3 ②『部署』推送数据库客户端 ToolPack ③ 建任务"
