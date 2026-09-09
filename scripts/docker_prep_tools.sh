#!/usr/bin/env bash
# =============================================================================
# AIDBM 镜像烘焙工具收集脚本
#
# 目标：把平台机（源码部署机）上已安装的数据库备份/恢复工具收集到
# .docker-bake/ 目录，随后 docker build 通过 Dockerfile 的
#   COPY .docker-bake/ /
# 烘焙进镜像 —— 镜像交付后用户直接 docker run 即可执行备份/恢复，
# 不再依赖宿主机 -v 挂载 xtrabackup / mysqldump 等二进制。
#
# 烘焙内容（仅 MySQL/MariaDB 一族需要平台侧自带二进制；其余数据库类型
# 走 SSH 远端使用 DBMS 自带工具 + 原生驱动，已在镜像内）：
#   1. MySQL 8.0.40 客户端  mysql / mysqldump / mysqladmin / mysqlbinlog /
#      mysqlcheck（自带 private openssl，Debian 容器直接可跑）
#   2. MySQL 8.0.40 服务端  mysqld（物理恢复时启动临时实例做可恢复性校验，
#      体积较大，可用 SKIP_MYSQLD=1 跳过；跳过时物理恢复需走远端校验）
#   3. Percona XtraBackup 8.0.x（MySQL 8.0+ 物理备份/恢复）
#   4. Percona XtraBackup 2.4.x（MySQL 5.5-5.7 物理备份/恢复）
#   5. MariaDB Backup（MariaDB 10.x 物理备份/恢复）
#   6. CentOS7 编译二进制所需的旧版动态库（libssl.so.10 等，Debian 镜像
#      没有），统一放入镜像 /opt/toolpack-libs64/ 并以 LD_LIBRARY_PATH 注入。
#
# 使用方法：在平台机（具备上述工具）执行
#   bash scripts/docker_prep_tools.sh
# 然后 docker build -t backup-platform:local .
#
# 自定义源路径（从其他机器收集时使用环境变量覆盖）：
#   MYSQL_HOME=/opt/mysql840b  XB24_DIR=/opt/xtrabackup24/usr/bin
#   MARIABACKUP_DIR=/opt/mariabackup/usr/bin  XB8=/usr/bin/xtrabackup
#   LIBS64_DIR=/lib64            SKIP_MYSQLD=1
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BAKE="$ROOT/.docker-bake"

MYSQL_HOME="${MYSQL_HOME:-/opt/mysql840b}"
XB24_DIR="${XB24_DIR:-/opt/xtrabackup24/usr/bin}"
MARIABACKUP_DIR="${MARIABACKUP_DIR:-/opt/mariabackup/usr/bin}"
XB8="${XB8:-/usr/bin/xtrabackup}"
LIBS64_DIR="${LIBS64_DIR:-/lib64}"

# Debian slim-bookworm 缺失的旧版动态库（按 soname 逐一确认，勿随意增删；
# 与容器同名的新版库不放入，避免 LD_LIBRARY_PATH 全局覆盖系统库）
OLD_LIBS=(
  libaio.so.1
  libnuma.so.1
  libssl.so.10
  libcrypto.so.10
  libgcrypt.so.11
  libprocps.so.4
  libprotobuf-lite.so.3.19.4
  libncurses.so.5
  libtinfo.so.5
  libfreebl3.so
)
# 说明：libgpg-error.so.0 刻意不打入 —— 系统 Debian 版(libgpg-error0)带版本
# 符号节且向后兼容老 libgcrypt.so.11 调用；混入 CentOS7 无版本符号节的老库
# 会污染依赖系统 libgcrypt.so.20 的程序（no version information available）。

say() { printf '\033[1;36m[prep]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[prep:warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[prep:error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 清空重建 -------------------------------------------------------------
rm -rf "$BAKE"
mkdir -p "$BAKE/opt/mysql840b/bin" "$BAKE/opt/xtrabackup24/usr/bin" \
         "$BAKE/opt/mariabackup/usr/bin" "$BAKE/usr/bin" \
         "$BAKE/opt/toolpack-libs64"
touch "$BAKE/.keep"
cd "$BAKE"

# --- 1) MySQL 8.0.40 客户端工具 -------------------------------------------
MYSQL_BINS=(mysql mysqldump mysqladmin mysqlbinlog mysqlcheck my_print_defaults perror)
for b in "${MYSQL_BINS[@]}"; do
  [ -f "$MYSQL_HOME/bin/$b" ] || { warn "缺 $MYSQL_HOME/bin/$b，跳过"; continue; }
  cp -a "$MYSQL_HOME/bin/$b" opt/mysql840b/bin/
done
if [ "${SKIP_MYSQLD:-0}" != "1" ] && [ -f "$MYSQL_HOME/bin/mysqld" ]; then
  say "烘焙 mysqld（$(du -h "$MYSQL_HOME/bin/mysqld" | cut -f1)）用于物理恢复临时实例校验..."
  cp -a "$MYSQL_HOME/bin/mysqld" opt/mysql840b/bin/
else
  warn "SKIP_MYSQLD 或未找到 mysqld：镜像内物理恢复将无法本地临时实例校验"
fi
# 私有运行库（openssl/icu/krb5 等）必须放在 lib/private（二进制 rpath 指向 ../lib）
mkdir -p opt/mysql840b/lib/private opt/mysql840b/lib/plugin
if [ -d "$MYSQL_HOME/lib/private" ]; then
  cp -a "$MYSQL_HOME"/lib/private/. opt/mysql840b/lib/private/
fi
# mysqlclient 链接库（mysql/mysqldump 运行时经 rpath ../lib 加载）
for f in "$MYSQL_HOME"/lib/libmysqlclient.so.21.2.40; do
  [ -f "$f" ] && cp -a "$f" opt/mysql840b/lib/ || true
done
[ -f opt/mysql840b/lib/libmysqlclient.so.21.2.40 ] && \
  ln -sf libmysqlclient.so.21.2.40 opt/mysql840b/lib/libmysqlclient.so.21 && \
  ln -sf libmysqlclient.so.21 opt/mysql840b/lib/libmysqlclient.so
# 服务端插件（mysqld 临时实例启动可能加载）
if [ -d "$MYSQL_HOME/lib/plugin" ] && [ -n "$(ls "$MYSQL_HOME/lib/plugin" 2>/dev/null)" ]; then
  cp -a "$MYSQL_HOME"/lib/plugin/. opt/mysql840b/lib/plugin/
fi
# 字符集/错误信息（mysqld 临时实例启动需要 share 目录）
if [ -d "$MYSQL_HOME/share" ]; then
  cp -a "$MYSQL_HOME/share" opt/mysql840b/share
fi

# --- 2) XtraBackup 2.4（MySQL 5.5-5.7） -----------------------------------
if [ -f "$XB24_DIR/xtrabackup" ]; then
  cp -a "$XB24_DIR"/xtrabackup "$XB24_DIR"/xbstream "$XB24_DIR"/xbcrypt \
    "$XB24_DIR"/innobackupex opt/xtrabackup24/usr/bin/ 2>/dev/null || \
    cp -a "$XB24_DIR"/xtrabackup opt/xtrabackup24/usr/bin/
else
  warn "未找到 $XB24_DIR/xtrabackup，跳过 XtraBackup 2.4"
fi

# --- 3) MariaDB Backup（MariaDB 10.x） -------------------------------------
if [ -f "$MARIABACKUP_DIR/mariadb-backup" ]; then
  cp -a "$MARIABACKUP_DIR/mariadb-backup" opt/mariabackup/usr/bin/
  ln -sf mariadb-backup opt/mariabackup/usr/bin/mariabackup
else
  warn "未找到 $MARIABACKUP_DIR/mariadb-backup，跳过 MariaDB Backup"
fi

# --- 4) XtraBackup 8.0（MySQL 8.0+） ---------------------------------------
if [ -f "$XB8" ]; then
  cp -a "$XB8" usr/bin/xtrabackup
else
  warn "未找到 $XB8，跳过 XtraBackup 8.0"
fi

# --- 5) 旧版动态库 ---------------------------------------------------------
missing_libs=0
for so in "${OLD_LIBS[@]}"; do
  if [ -e "$LIBS64_DIR/$so" ]; then
    # /lib64 下多为符号链接（libgcrypt.so.11 -> libgcrypt.so.11.8.2），
    # 必须解引用拷贝为真实文件并以 NEEDED 名保存，否则镜像内是悬空链接
    cp -L "$LIBS64_DIR/$so" opt/toolpack-libs64/
  else
    warn "旧库 $LIBS64_DIR/$so 不存在（其它库可能已满足），缺失计数 +1"
    missing_libs=$((missing_libs + 1))
  fi
done
echo ".done" > .done

say "烘焙布局生成完毕: $BAKE"
echo "  容器内最终路径                          → 宿主机来源"
echo "  /usr/bin/xtrabackup(8.0)              → $XB8"
echo "  /opt/xtrabackup24/usr/bin/xtrabackup   → $XB24_DIR/xtrabackup"
echo "  /opt/mariabackup/usr/bin/mariabackup   → $MARIABACKUP_DIR/mariadb-backup"
echo "  /opt/mysql840b/bin/{mysql,mysqldump,mysqld,...} → $MYSQL_HOME/bin/"
echo "  /opt/toolpack-libs64/*.so               → $LIBS64_DIR"
echo "  体积: $(du -sh "$BAKE" | cut -f1)"
