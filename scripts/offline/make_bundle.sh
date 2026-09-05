#!/usr/bin/env bash
# =============================================================
# 离线交付包构建器（在有外网的构建机上运行）
# 产出：backup-platform-offline-<ver>.tar.gz（自包含，≤2GB 目标）
# 结构见 docs/dbackup-benchmark-offline-design-20260906.md 3.2 节
# 用法：
#   ./make_bundle.sh                    # 全量构建（镜像+wheelhouse+app+安装器）
#   ./make_bundle.sh --skip-image      # 跳过 docker build（复用已有镜像）
# =============================================================
set -euo pipefail

VER="${VER:-1.3.3}"
IMG="ghcr.io/zhh9126/backup-platform"
OUT_DIR="${OUT_DIR:-/tmp/offline_bundle}"
BUNDLE="$OUT_DIR/backup-platform-offline-${VER}"
SKIP_IMAGE=0
[[ "${1:-}" == "--skip-image" ]] && SKIP_IMAGE=1

log() { echo -e "\033[32m[bundle]\033[0m $*"; }

mkdir -p "$BUNDLE"/{images,wheelhouse,clients,tools,manifests,app}

# ---------- 1) 平台镜像（docker save）----------
if [[ $SKIP_IMAGE -eq 0 ]]; then
  log "构建平台镜像 ${IMG}:${VER} ..."
  docker build -t "${IMG}:${VER}" -t "${IMG}:latest" .
else
  log "跳过镜像构建（--skip-image）"
fi
log "导出镜像..."
docker save "${IMG}:${VER}" | gzip > "$BUNDLE/images/backup-platform_${VER}.tar.gz"
# MinIO 镜像（离线环境对象存储）
docker pull minio/minio:latest >/dev/null 2>&1 || true
docker save minio/minio:latest | gzip > "$BUNDLE/images/minio_latest.tar.gz"

# ---------- 2) 离线 PyPI wheelhouse（按平台 requirements）----------
log "下载离线 wheelhouse（python 3.12 / linux x86_64）..."
python3 -m pip download -r "$(dirname "$0")/../../requirements.txt" \
    jpype1 jaydebeapi \
    --dest "$BUNDLE/wheelhouse" \
    --only-binary=:all: --python-version 3.12 --implementation cp \
    --abi cp312 --platform manylinux2014_x86_64 \
    >/dev/null 2>&1 \
|| pip3 download -r "$(dirname "$0")/../../requirements.txt" \
    jpype1 jaydebeapi --dest "$BUNDLE/wheelhouse" \
    --only-binary=:all: --python-version 3.12 --platform manylinux2014_x86_64
log "wheelhouse: $(ls "$BUNDLE/wheelhouse" | wc -l) 个包"

# ---------- 3) 应用代码（非容器部署方式）----------
log "收集应用代码..."
SRC="$(dirname "$0")/../.."
for d in core api static templates drivers skills; do cp -a "$SRC/$d" "$BUNDLE/app/"; done
cp "$SRC"/*.py "$SRC/start.sh" "$SRC/requirements.txt" "$BUNDLE/app/" 2>/dev/null || true
cp "$SRC/scripts/offline/install.sh" "$BUNDLE/install.sh"
chmod +x "$BUNDLE/install.sh"
cp "$SRC/scripts/offline/healthcheck.sh" "$BUNDLE/tools/" 2>/dev/null || true

# ---------- 4) 数据库客户端 ToolPack 说明（按现场收集）----------
cat > "$BUNDLE/clients/README.md" <<'EOF'
# 数据库客户端 ToolPack（离线推送包）

按「目标 OS × 数据库版本 × 架构」放入自解压绿色包（tar.gz），
命名规范：<db>_<version>_<os>_<arch>.tar.gz（如 mysql_8.0.40_el7_x86_64.tar.gz）。
包内布局：bin/（工具）+ tool_manifest.yaml（工具清单与版本）。

任务执行时平台按四级降级自动处理：
远端 PATH 探测 → ToolPack 推送(解压到 /tmp/bk_tools/<hash>) →
tool_path 指定 → 本机回退。
现场依据数据库服务器版本收集后放入本目录，随离线包交付。
EOF

# ---------- 5) 版本清单 + 打包 ----------
log "生成清单..."
{
  echo "version: ${VER}"
  echo "build_date: $(date +%Y%m%d%H%M%S)"
  cd "$BUNDLE" && sha256sum $(find . -type f ! -name manifest.yaml) | sed 's/\.\///'
} > "$BUNDLE/manifests/version.yaml"

log "打包..."
tar czf "${OUT_DIR}/backup-platform-offline-${VER}.tar.gz" -C "$OUT_DIR" \
    "$(basename "$BUNDLE")"
SZ=$(du -h "${OUT_DIR}/backup-platform-offline-${VER}.tar.gz" | cut -f1)
log "✅ 离线包完成: ${OUT_DIR}/backup-platform-offline-${VER}.tar.gz (${SZ})"
log "交付方式：摆渡盘/单向网闸拷入离线环境，root 执行 ./install.sh --mode auto"
