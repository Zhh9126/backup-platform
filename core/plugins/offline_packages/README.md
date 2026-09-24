# 插件离线包目录（离线环境市场安装源）

完全离线环境无法在线下载插件安装包。把安装包提前放到本目录即可：

```
core/plugins/offline_packages/<插件id>/<安装包>.tar.gz
```

示例：

```
core/plugins/offline_packages/percona-xtrabackup-24/percona-xtrabackup-2.4.29-Linux-x86_64.glibc2.12.tar.gz
core/plugins/offline_packages/mongodb-database-tools/mongodb-database-tools-ubuntu2204-x86_64-100.9.4.tgz
```

规则：
- 目录名 = 插件 id（见 `core/plugins/manifests/*.json` 的 `id` 字段）
- 支持 .tar.gz / .tgz / .tar.bz2 / .tar.xz / .zip / .tar
- 同目录多个包时取修改时间最新的一个
- 「一键安装」优先使用这里的本地包，找不到才尝试在线下载（离线环境会
  直接给出放置指引）

注意：Percona XtraBackup 2.4/8.0 与 mariabackup 为**平台内置组件**
（镜像自带、物理备份引擎零安装推送的二进制源），无需安装也无法卸载，
manifest 已标记 `builtin: true`。
