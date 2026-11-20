# 平台目录灾备

当 AIDBM 管理服务器损坏时，业务备份文件可能仍在，但备份任务、策略、历史记录和存储配置都依赖元数据库。目录灾备把 SQLite 元数据库的一致性快照与原平台密钥封装为一个加密包。校验和恢复命令可在 AIDBM 服务停机时独立运行，全部操作在 Linux 管理服务器上执行。

该功能适用于 SQLite 元数据库。使用 PostgreSQL 或 MySQL 作为**平台元库**的部署需要使用相应数据库的原生备份流程。被保护的业务数据库类型与此无关。业务备份文件、块存储、实时日志产物、外部证书和部署环境配置仍需按各自的保留策略保护。

## 创建与校验

在已安装项目依赖的部署目录中执行。示例使用默认的 SQLite 路径，归档目录沿用实际挂载的异地存储目录。

```bash
backup=/mnt/dr/catalog/aidbm-$(date -u +%Y%m%dT%H%M%SZ).aidbm
key=/secure/aidbm-catalog.key

# 只在首次启用时生成。异地保管一份密钥副本，与加密包分开存放。
(umask 077; set -o noclobber; openssl rand 32 > "$key")

.venv/bin/python run.py catalog create \
  --source instance/meta.db --key-file "$key" --output "$backup"
.venv/bin/python run.py catalog verify "$backup" --key-file "$key"
```

- `--source` 指向当前服务实际使用的元库。自定义了 `META_DB_PATH` 时填写该路径。
- 命令优先使用 `--auth-secret-file` 指定的 JSON 文件，其次使用当前 shell 的 `SECRET_KEY`，最后读取 SQLite 文件同目录的 `auth_secret.json`。使用环境变量或 `config.json` 覆盖平台密钥的部署，需提供服务生效的密钥。密钥 JSON 的结构为 `{"secret_key": "原平台密钥"}`。
- `create` 在线读取已提交快照并执行 SQLite 完整性检查。AES-256-GCM 同时保护密钥与数据库内容，包的权限为 `0600`。单包容量上限约为 64 GiB。目标父目录需已存在并支持硬链接，以便完整写入后原子发布并避免覆盖旧包。
- `verify` 校验整包认证，再检查 SQLite 完整性与目录表。退出码 `0` 表示成功，失败返回非零。命令输出 JSON，可在 Linux cron 或 systemd timer 中按相同顺序安排创建和校验。每次运行使用唯一文件名，由异地存储设置保留周期。
- 创建和校验会在系统临时目录下使用一个私有明文工作目录。运行前确保有足够空间，并可通过 `TMPDIR` 指定本地受保护卷。正常结束时会清理工作目录。
- 离线可执行版可使用 `./backup_platform catalog …` 执行同样的命令。

## 恢复演练与灾后接管

```bash
mkdir -p /srv/aidbm-recovery
.venv/bin/python run.py catalog recover "$backup" \
  --key-file "$key" --directory /srv/aidbm-recovery
```

1. 读取命令返回的 `directory`。认证和 SQLite 校验通过后，该目录内会出现 `meta.db` 和 `auth_secret.json` 两个文件。目录权限为 `0700`，文件权限为 `0600`。
2. 在隔离环境检查还原的任务、备份记录和存储引用。确认原备份存储仍可访问，再选取已知的恢复点验证业务恢复。测试平台接入业务环境前，先隔离定时任务可能连接的生产目标。
3. 灾后接管时，在停用待接管实例的服务和调度后，用同版本 AIDBM 接管新恢复目录。将 `INSTANCE_DIR` 指向该目录，并确保 `META_DB_PATH` 和 `SECRET_KEY` 的生效值与恢复文件一致。使用原路径的外部备份存储需重新挂载，或在接管环境内更新存储配置。确认实例状态后再恢复调度。
