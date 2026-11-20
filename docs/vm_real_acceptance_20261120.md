# 本地 VMware 真实备份与恢复验收（2026-11-20）

## 环境和边界

唯一已运行的本地虚拟机为 VMware Workstation 上的 CentOS 7，IP 192.168.109.128，也是平台开发服务器。
原服务器保持运行；新恢复虚拟机的 ethernet0.startConnected=FALSE，独立 UUID，避免重复 IP。
没有新安装客户机 Agent。VMware Tools 已在原系统中存在，本轮仅用其原生文件读取/执行接口验证隔离副本的数据。
平台现有 Provider 为 PVE、libvirt SSH、ESXi SSH、Hyper-V SSH；Workstation 尚未接入平台纳管接口。
因此本报告是原生 Workstation 实机数据验证，不冒充平台 VM API 在生产 Hypervisor 上的端到端验收。

全部验收脚本的主副本在 /root/backup-platform-private/scripts/qa：
prepare_vm_canary.py、workstation_real_recovery.ps1、workstation_cold_acceptance.ps1。
Windows 的 work 目录只用于执行副本；测试数据在 D:/CodexVMRecovery。
客户机校验口令通过 AIDBM_VM_GUEST_PASSWORD 进程环境传入。

## 第一轮：在线热快照（部分通过）

对运行中源 VM 创建 aidbm-real-backup-20261120 快照，备份只读基础 VMDK 与配置，不拷贝正在变化的 delta。
Workstation 的运行中 full clone 被拒绝。快照基础磁盘复制为独立文件后，归档上传 Linux 仓库、下载返回，SHA256 三方一致。
热备归档大小 6,894,887,794 字节；SHA256：
14eb3d166a1cbcc3340681ed1e95b47989898a08f9e0dae7eaded7e8721e720f。

恢复到 D:/CodexVMRecovery/vm-real-20261120/restored/backup.vmx，断网启动成功。
从恢复客户机读取 SQLite 数据库，SHA256 等于备份前基线，PRAGMA integrity_check=ok，proof 表行数为 10,000。
但首次测试夹具未 fsync 的 payload.bin 和 expected.json 在该磁盘快照中是 0 字节，故首轮整体验收仅 partial。
不能据此宣称全部文件恢复通过，也不能把崩溃一致性宣称为应用一致性。
已新增 prepare_vm_canary.py，显式 fsync 文件和目录并 sync，消除后续夹具未刷盘的问题。
首轮证据：build/vm-real-20261120/warm-result.json、repository-integrity.json、recovered-canary.db。
原服务器保留上述快照，未执行回滚或关机。

## 第二轮：独立测试副本的正常关机冷备

在第一轮恢复出的隔离测试客户机内重新写入三个测试文件，执行 sync，并正常关机。
原服务器始终运行。关机后完整复制无外部父盘依赖的 VMDK、VMX、NVRAM，制作独立冷备归档。
本机 vmrun full clone 调用未完成有效磁盘复制，已终止该测试调用并保留其小型失败目录；最终冷备使用关机磁盘复制。
冷备目录 D:/CodexVMRecovery/vm-cold-copy-20261120。
归档使用 gzip level 1 降低 CPU 开销；-Resume 只允许从已经完成的关机副本继续，且拒绝覆盖已有恢复目录。
后续必须完成：归档 Linux 往返校验 → 新目录恢复 → 断网开机 → 三个文件 SHA256 → SQLite 实际查询 → 正常关机。

当前第二轮状态：进行中。以 build/vm-real-20261120/cold-result.json 的最终结果为准。
