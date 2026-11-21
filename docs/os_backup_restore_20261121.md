# 操作系统备份的恢复方法调研与落地（2026-11-21）

## 一、业界怎么恢复操作系统整机备份

调研（联网检索 ReaR / Clonezilla / Veeam BMR / 通用 BMR runbook）后，整机恢复的骨架高度一致：

| 步骤 | 做什么 | 为什么不能省 |
|---|---|---|
| 1 引导介质 | Clonezilla Live / ReaR ISO / 自制救援 U 盘 / PXE 引导目标机 | 目标机系统已损坏，无法自举 |
| 2 重建分区与文件系统 | 按备份中的分区表在目标盘重建分区、格式化 | 文件级备份只有文件，没有分区结构 |
| 3 挂载目标根与 /boot | 按 fstab 布局挂载，激活 LVM/RAID | 文件必须落到正确的挂载点 |
| 4 还原系统文件 | tar/rsync 解包到目标根 | 这是备份产品的核心动作 |
| 5 修复 fstab | 用新盘 UUID 替换旧 UUID | 换盘后 UUID 必变，否则启动后挂载错盘 |
| 6 重建 initramfs | `dracut --force --no-hostonly` / `update-initramfs` | 跨硬件恢复时新磁盘/网卡驱动不在旧 initramfs 里，开机即停在 dracut |
| 7 安装引导加载程序 | `grub2-install` + `grub2-mkconfig`（UEFI 另处理 ESP） | 引导记录在目标盘上，必须重写 |
| 8 校验与切换 | 校验引导、挂载、关键服务后再切业务流量 | 避免带病上线 |

一致性前提：商业产品（Veeam Agentless）靠卷快照或停机抓取保证一致性；纯文件级镜像对运行中的系统无法保证跨文件一致。

参考来源：
- Relax-and-Recover（ReaR）：可启动恢复系统 → 重建分区/文件系统/挂载点 → 调备份工具还原 → 安装引导加载程序；内置 tar 与 rsync 两种恢复方式。
- Clonezilla：device-image 模式，按向导选择镜像与目标磁盘分区后写入。
- Veeam Linux Agentless + Veeam Recovery Media：备份前需创建带驱动的系统镜像/引导盘，恢复时做分区映射后还原。
- 通用 BMR runbook（8 步）：引导 → 定位镜像 → 重建分区 → 还原 → 修 fstab → 重建 initramfs → 装引导 → 校验。

## 二、AIDBM 的落地方式与边界

平台是**离线交付、零 Agent**，因此不做裸机引导介质与 PXE（无基础设施），但必须把恢复所需的**真实参数**随备份一起留存，并产出可执行作业单：

- 备份侧采集（`core/os_backup.py::_collect_metadata`，本机与远程共用）：
  `uname / lsblk(+UUID,PARTUUID,MODEL,SERIAL) / blkid / fstab / crypttab / boot_mode(UEFI|BIOS) /
  root_source / mounts / mdstat / LVM(pvs,vgs,lvs) / grub_files / grub_entries / efi / initramfs /
  boot_files / bootloader_tools / bootloader_version / root_disk / partition_table(sgdisk→parted→fdisk 三级降级)`
- 恢复侧能力：
  1. **文件级异机还原**（平台自动执行，逐文件 SHA256 校验）
  2. **恢复作业单**（`GET /api/os-backup/restore-plan?archive_path=`）：8 步 + 每步命令模板 + 人工确认标记
  3. **可执行脚本骨架**（把作业单渲染成 bash 脚本，占位符由运维按目标机替换）
- 明确不做（页面作业单里如实展示）：裸机引导介质/PXE；卷级一致性快照；自动改写目标机 fstab UUID 与安装 GRUB（给命令模板，需在救援环境人工确认执行）。

## 三、实测证据（2026-11-21）

| 验证项 | 结果 |
|---|---|
| 本机整机元数据采集 | `root_disk=/dev/sda`，partition_table 取到 fdisk 真实输出；lsblk/lsblk -J 兼容降级（旧 util-linux 无 -J） |
| 远程 133 真实主机采集 | 同样取到 `/dev/sda` 与分区表；grub_entries 命中 CentOS 内核条目，initramfs 命中真实 img 文件名 |
| 本机恢复链路 E2E | `/etc` 3080 文件 → 归档 → 预览校验 → 异步还原 → 逐文件 SHA256 一致（18/18） |
| 远程恢复链路 E2E | 133 `/etc` → 远端 tar → SFTP 拉回 → 清单 1775 文件 → 远端源文件 SHA256 逐个一致 → 还原一致（12/12） |
| 恢复作业单 E2E | 本机 + 远程各 8 步齐全、脚本含 grub2-install/initramfs/fstab、能力边界字段完整（21/21） |

## 四、页面操作

1. `/os-backup` → 选主机（本机 / 已纳管 / 直接填连接）→ 选保护范围 → 「立即备份」（提交即返回，后台执行 + 轮询进度）
2. 记录列表点「恢复」→ 弹窗内可选：
   - **预览校验**：核对归档完整性（逐文件大小与清单）
   - **恢复作业单**：查看 8 步恢复方法、平台能力边界、命令模板与脚本骨架（可一键复制）
   - **确认恢复**：把文件级内容还原到指定目标目录（异机救援/迁移/取证场景）
3. 整机换盘/换机裸机恢复：按作业单在救援介质中执行，平台负责数据与参数，不负责引导。
