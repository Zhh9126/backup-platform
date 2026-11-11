# AIDBM 在线部署说明（联网拉取镜像）

> 适用版本：**v2.3.7**（2026-11-10）
> 适用场景：部署服务器**可以访问公网镜像仓库**，通过 `docker pull` 获取最新镜像后在本机运行。
> 产品定位说明：**联网只用于拉取镜像**；平台运行时本身不依赖公网，所有驱动、JRE、备份工具链都已烘焙在镜像内。完全无外网的环境请改用离线交付包（见第 8 节）。

---

## 1. 部署前提

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux x86_64（镜像基座 Debian bookworm / Python 3.11） |
| 容器运行时 | Docker 20.10+；Compose 方式需 `docker compose` v2（老环境可用 `docker-compose`） |
| 磁盘 | 镜像约 2.26GB，`/var/lib/docker` 建议预留 ≥10GB；备份产物目录请按业务数据量单独规划 |
| 网络 | 部署机可访问 `ghcr.io:443`（仅拉镜像时需要）；平台到被管数据库 / SSH 主机内网可达 |
| 端口 | 默认 `8080/tcp`（Web），可用端口映射或 `WEB_PORT` 调整；需在防火墙放行 |
| 权限 | `root` 或已加入 `docker` 组的用户 |

设计口径：**被管数据库服务器上零安装**。平台通过 SSH 调用数据库自带工具（`mysqldump` / `pg_dump` / `expdp` 等），物理备份所需二进制由平台镜像临时推送到远端 `/tmp` 执行后清理；平台侧依赖全部随镜像交付。

---

## 2. 从仓库下载最新镜像

镜像仓库地址：`ghcr.io/zhh9126/backup-platform`
代码仓库：`https://github.com/Zhh9126/backup-platform`（Gitee 镜像：`https://gitee.com/zhh_w/backup-platform`）

### 2.1 两个可用 tag

| tag | 含义 | 建议 |
|---|---|---|
| `v2.3.7` | 固定版本，可回滚、便于留痕和审计 | **生产推荐** |
| `latest` | 滚动指向当前最新版本（发布时与 `v2.3.7` 同一镜像 ID） | 测试 / 快速体验 |

```bash
# 推荐：拉取固定版本
docker pull ghcr.io/zhh9126/backup-platform:v2.3.7

# 或拉取滚动最新版
docker pull ghcr.io/zhh9126/backup-platform:latest

# 确认本地已有镜像与 tag
docker images | grep backup-platform
docker inspect --format='{{.Id}}  {{json .RepoTags}}' ghcr.io/zhh9126/backup-platform:v2.3.7
```

本版本发布时的 manifest digest（可用于精确定位、复核是否被篡改）：

```
sha256:d8d04c0fb9c30fa3b0960ae358771e6bf1bbc7cbc10bf6dade460dda1116429c
```

```bash
# 按 digest 精确拉取（可选，不受 tag 后续被覆盖的影响）
docker pull ghcr.io/zhh9126/backup-platform@sha256:d8d04c0fb9c30fa3b0960ae358771e6bf1bbc7cbc10bf6dade460dda1116429c

# 回查远端 manifest（可选）
docker manifest inspect ghcr.io/zhh9126/backup-platform:v2.3.7
```

### 2.2 拉取失败的常见处理

- **公开镜像通常无需登录**。若返回 `denied` / `unauthorized`，用具备 `read:packages` 权限的 GitHub PAT 登录：
  ```bash
  echo '<PAT>' | docker login ghcr.io -u '<GitHub用户名>' --password-stdin
  ```
- **国内拉取慢**：可配置 registry 镜像加速（`/etc/docker/daemon.json` 的 `registry-mirrors`，改后 `systemctl restart docker`）。注意 GHCR 是否走加速取决于加速器本身；也可先在有网机器 `docker pull` + `docker save` 再导入目标机。
- **代理 / 防火墙**：确认出网 `443`、DNS 正常；企业代理需给 Docker daemon 配置 `HTTP_PROXY` / `HTTPS_PROXY`。

---

## 3. 部署到服务器

### 3.1 准备持久化目录（关键）

镜像内默认环境变量（Dockerfile 已固化）：

```
BACKUP_ROOT=/data/backups      # 备份产物
INSTANCE_DIR=/data/instance    # 元数据库 meta.db、SECRET_KEY、JDBC 注册表等
LOG_DIR=/data/logs             # 运行日志与操作日志
WEB_HOST=0.0.0.0  WEB_PORT=8080  TZ=Asia/Shanghai
```

三处都落在 `/data`，因此**必须把 `/data` 挂载到宿主机目录或命名卷**，否则容器重建即丢失全部备份与元数据。

```bash
sudo mkdir -p /opt/aidbm/data/{backups,instance,logs}
```

### 3.2 方式 A：`docker run`（最简）

```bash
docker run -d --name backup-platform \
  -p 8080:8080 \
  -v /opt/aidbm/data:/data \
  -e TZ=Asia/Shanghai \
  -e BACKUP_ROOT=/data/backups \
  -e WEB_PORT=8080 \
  --restart unless-stopped \
  ghcr.io/zhh9126/backup-platform:v2.3.7
```

说明：

- `BACKUP_ROOT` / `WEB_PORT` / `TZ` 在镜像内已有默认值，显式写出便于审计与变更。
- **改宿主访问端口**只需改映射左侧，例如 `-p 8081:8080`（容器内仍是 8080）。
- **SELinux 环境**（CentOS/RHEL 等）挂载宿主机目录可能报权限拒绝，卷参数改为 `-v /opt/aidbm/data:/data:Z`。
- **host 网络模式**（需要直连大量内网数据库 / SSH 目标、不想逐个做端口映射时）：
  ```bash
  docker run -d --name backup-platform --network host \
    -v /opt/aidbm/data:/data \
    -e WEB_PORT=8080 \
    --restart unless-stopped \
    ghcr.io/zhh9126/backup-platform:v2.3.7
  ```
  host 模式没有 `-p` 映射，服务直接监听宿主机的 `WEB_PORT`；改端口必须同时改 `-e WEB_PORT`。
- **首次部署建议直接设置强口令**（二选一，见第 4 节）：
  ```bash
  -e WEB_PASSWORD='<自定义强口令>'
  # 或让平台生成随机口令（仅首次部署生效）
  -e AIDBM_RANDOM_ADMIN_PASSWORD=true
  ```

### 3.3 方式 B：`docker compose`（推荐生产）

在 `/opt/aidbm/docker-compose.yml` 写入：

```yaml
services:
  backup-platform:
    image: ghcr.io/zhh9126/backup-platform:v2.3.7
    container_name: backup-platform
    ports:
      - "8080:8080"
    environment:
      - TZ=Asia/Shanghai
      - WEB_PORT=8080
      - BACKUP_ROOT=/data/backups
      # - WEB_PASSWORD=<自定义强口令>
      # - AIDBM_RANDOM_ADMIN_PASSWORD=true
    volumes:
      - ./data:/data
    restart: unless-stopped
```

```bash
cd /opt/aidbm
docker compose up -d          # 老版 Compose 用 docker-compose up -d
docker compose ps
```

> 镜像已自带 `HEALTHCHECK`（探测容器内 `/login` 返回 200），Compose 中无需重复定义。
> 若用 host 网络，把 `ports:` 换成 `network_mode: host`，并按需改 `WEB_PORT`。

### 3.4 常用环境变量

| 变量 | 镜像内默认值 | 说明 |
|---|---|---|
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8080` | 监听地址与端口 |
| `WEB_THREADED` | `1` | 内置 Web 服务多线程；置 `0` 会让批量操作排队超时，保持默认即可（镜像默认 `python run.py` 同时启动 Web 与调度器，不要换成只导入 `app` 的 WSGI 启动方式，否则调度器不会启动） |
| `BACKUP_ROOT` | `/data/backups` | 备份产物根目录；平台「存储管理 → 本地备份存储位置」可在线修改，**界面配置优先级更高且重启后仍生效** |
| `INSTANCE_DIR` / `LOG_DIR` | `/data/instance` / `/data/logs` | 元数据库与日志，均在 `/data` 卷内 |
| `TZ` | `Asia/Shanghai` | 时区，影响调度时间与日志 |
| `WEB_USERNAME` / `WEB_PASSWORD` | `admin` / `admin123` | 首个内置超管；**上线必须改掉默认口令** |
| `SECRET_KEY` | 自动生成 | 会话密钥，自动生成并持久化到 `/data/instance/auth_secret.json`，勿手工删除 |
| `AIDBM_RANDOM_ADMIN_PASSWORD` | `false` | 首次部署生成随机超管口令，明文写入 `/data/instance/initial_admin_password.txt`（权限 0600） |
| `AIDBM_FORCE_PASSWORD_CHANGE` | `false` | 开启后强制 `must_change_password` 账号先改密再使用 |
| `SESSION_TIMEOUT` | `28800` | 会话超时（秒） |
| `SCHEDULER_ENABLED` | `true` | 是否启用定时调度 |
| `META_DB_BACKEND` | `sqlite` | 可切换 `postgresql` / `mysql`（配合 `META_DB_HOST` 等，或在设置页切换） |

配置优先级：代码默认值 < 环境变量 < `config.json`；存储路径另受平台「存储管理」界面配置覆盖。

### 3.5 放行防火墙

```bash
# firewalld（CentOS / RHEL / openEuler）
sudo firewall-cmd --add-port=8080/tcp --permanent && sudo firewall-cmd --reload

# ufw（Ubuntu / Debian）
sudo ufw allow 8080/tcp
```

---

## 4. 启动服务与验证

容器创建成功即已启动服务（镜像 `CMD` 为 `python run.py`，进程内同时启动 Web 服务与后台调度器，无需再进容器手工拉起）。

```bash
# 1. 查看容器状态
docker ps --filter name=backup-platform

# 2. 跟随启动日志（出现启动横幅、"调度器已启动"、"Running on ..." 即就绪）
docker logs -f --tail=100 backup-platform

# 3. 健康检查：/login 返回 200 即 Web 可用
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/login

# 4. 浏览器访问
#    http://<服务器IP>:8080   默认账号 admin / admin123
```

**首次登录后立即在「用户管理」修改默认口令**（启动日志会对出厂口令给出安全告警）。若启用了 `AIDBM_RANDOM_ADMIN_PASSWORD=true`，从数据卷读取随机口令：

```bash
sudo cat /opt/aidbm/data/instance/initial_admin_password.txt
```

补充核验：

```bash
# 容器内版本号（应为 2.3.7）
docker exec backup-platform python -c 'import config; print(config.PLATFORM_VERSION)'

# 服务端依赖自检：原生驱动 / JDBC 驱动包 / JRE 是否齐备
docker exec backup-platform python tools/check_env.py

# 日志位置（宿主机）
ls -l /opt/aidbm/data/logs/           # platform.log / error.log / crash.log / operations/
```

常用运维命令：

```bash
docker restart backup-platform        # 重启（数据在卷中，不丢失）
docker stop backup-platform && docker start backup-platform
docker stats backup-platform          # 资源占用
docker inspect backup-platform | grep -A8 Mounts   # 确认 /data 挂载
```

---

## 5. 升级与回滚（联网环境）

数据全部在 `/data` 卷，升级只需替换镜像、用同一命令重建容器：

```bash
# 0. 建议先备份元数据库
sudo cp -a /opt/aidbm/data/instance /opt/aidbm/instance_backup_$(date +%F)

# 1. 拉取新版本
docker pull ghcr.io/zhh9126/backup-platform:vX.Y.Z      # 换成实际新版本 tag

# 2. 停旧容器（不要删数据卷/目录）
docker stop backup-platform && docker rm backup-platform

# 3. 用新 tag 重新执行第 3.2 节完全相同的 docker run 命令
#    （或改 compose 的 image 后 docker compose up -d）

# 4. 按第 4 节验证 /login 200、版本号、任务与记录仍在
```

回滚：把镜像 tag 换回旧版本再执行同一启动命令即可。**回滚前务必先备份 `/data/instance`**，因为新版本可能已对元数据库做过列/表结构迁移。

---

## 6. 故障排查

| 现象 | 排查与处置 |
|---|---|
| `docker pull` 超时 / denied | 检查 `ghcr.io:443` 出网、DNS、企业代理；按 2.2 登录 GHCR 或配置加速 |
| 容器启动后立即退出 | `docker logs backup-platform`；常见为 `WEB_PORT` 冲突（`ss -lntp | grep 8080`）、`/data` 目录不可写 |
| `/login` 连不上 | `docker ps` 看状态、`curl` 容器内自测、检查防火墙与端口映射；host 模式确认 `WEB_PORT` |
| 宿主机找不到备份文件 | 多为 `/data` 未挂载；用 `docker inspect ... Mounts` 核实，并在「存储管理」页 / 备份日志弹窗查看容器内路径 → 宿主机路径映射与未挂载告警 |
| 重启后会话失效 / 配置丢失 | `SECRET_KEY`、`meta.db` 在 `/data/instance`，卷被删或未挂载才会丢失；确认卷未用 `docker rm -v` 删除 |
| 磁盘被写满 | `df -h`；把 `BACKUP_ROOT` 指向大盘并在「存储管理」页同步配置，配置定期清理保留策略 |
| 启动日志有出厂口令告警 | 立即改密，或重建时用 `WEB_PASSWORD` / `AIDBM_RANDOM_ADMIN_PASSWORD=true` |
| `Unable to find image ... locally` 并去公网拉取 | 启动用的镜像名与本地 tag 不一致（离线导入场景高发）；`docker images` 查实际 tag，或补 `docker tag` |

---

## 7. 可选：磁带库 / 可移动介质设备透传

镜像已内置 `mt-st`（`mt`）、`mtx`、`lsscsi`、`sg3-utils`。真实设备需**先在宿主机识别，再透传给容器**：

```bash
# 宿主机识别（先起带库再起服务器）
lsscsi -g | grep -E 'tape|mediumx'
mtx -f /dev/sg2 status
mt  -f /dev/nst0 status
```

`docker run` 增加（`--device` 会同时创建设备节点并放行设备 cgroup，单纯 `-v /dev:/dev` 不够）：

```bash
  --device /dev/tape/by-id/scsi-XXXX-nst:/dev/tape/by-id/scsi-XXXX-nst:rwm \
  --device /dev/sg2:/dev/sg2:rwm \
```

Compose 等价写法：

```yaml
    devices:
      - /dev/tape/by-id/scsi-XXXX-nst:/dev/tape/by-id/scsi-XXXX-nst:rwm
      - /dev/sg2:/dev/sg2:rwm
```

之后在平台「存储管理 → 磁带库」配置设备节点、机械手 `/dev/sgN`、流式块大小（LTO 建议 256KB 对齐）与单盘容量上限，并在「磁带库」页查看库存、装载与写后校验。
**诚实边界**：仓库内为目录模拟带库的完整自动化验证，真实磁带库 / 机械手 / LTO WORM 介质尚无真机验证报告；现场首次接入请先用空白介质做小额全链路演练。

---

## 8. 与离线部署的差异

| 环节 | 联网部署（本文） | 完全离线部署 |
|---|---|---|
| 获取镜像 | `docker pull ghcr.io/zhh9126/backup-platform:v2.3.7` | `docker load -i aidbm_2.3.7_offline.tar.gz` |
| 镜像 tag | 固定版本或 `latest` 均可 | **必须用固定版本 tag**；离线环境用 `latest` 易被误认为「去公网拉最新」 |
| 启动命令 | 与本文第 3、4 节完全相同 | 与本文第 3、4 节完全相同，仅换镜像来源 |
| 运行时公网依赖 | 无 | 无 |

离线交付包 `/opt/backup-platform/packages/aidbm_2.3.7_offline.tar.gz`（约 1.3GB）已把 4 个 tag
（`ghcr.io/...:v2.3.7`、`ghcr.io/...:latest`、`aidbm:2.3.7`、`aidbm:latest`）打进 `manifest.json` 的 `RepoTags`，`docker load` 后可任选其一启动，全程零联网。

---

## 附：最小可复制命令

```bash
sudo mkdir -p /opt/aidbm/data/{backups,instance,logs}
docker pull ghcr.io/zhh9126/backup-platform:v2.3.7
docker run -d --name backup-platform -p 8080:8080 \
  -v /opt/aidbm/data:/data -e TZ=Asia/Shanghai \
  --restart unless-stopped \
  ghcr.io/zhh9126/backup-platform:v2.3.7
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/login   # 期望 200
# 浏览器打开 http://<服务器IP>:8080 ，admin / admin123，登录后立即改密
```
