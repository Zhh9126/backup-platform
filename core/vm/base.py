# -*- coding: utf-8 -*-
"""VMProvider 抽象基类：屏蔽 PVE / libvirt / ESXi / Hyper-V 的差异。

设计参照业界主流产品的能力分层（Veeam VADP、Rubrik SLA、Cohesity Instant
Mass Restore、Zerto Journal），但**只声明平台能真实落地的能力**：
Provider 未实现的动作必须抛 ``VMProviderError``（或直接 NotImplementedError），
由引擎转换为「能力不支持」的明确提示，绝不允许静默造假成功。

被管侧零安装原则
----------------
所有 Provider 只允许使用两类通道，不在虚拟化平台上安装任何 agent：
1) 控制面：Hypervisor 原生 API（PVE REST / vCenter / WMI）或 SSH 执行宿主机
   自带命令（virsh / qemu-img / vim-cmd / PowerShell）；
2) 数据面：SSH/SFTP 流式拉取（tar / 文件），或 Hypervisor 原生导出通道。
"""
from typing import List, Optional

from core.vm.types import (
    BackupArtifact, CloneResult, CloneSpec, HealthCheckResult, ProviderCaps,
    SnapshotRef, VMInfo, VMProviderError,
)


class VMProvider:
    """虚拟化平台适配器基类。"""

    provider_id = "base"
    display_name = "基类"
    # 能力默认值：保守声明；具体 Provider 覆盖 capabilities()
    _CAPS = ProviderCaps()

    def __init__(self, hypervisor: dict, logger=None):
        """
        Args:
            hypervisor: vm_hypervisors 表的一行（**已解密** password/token_secret）
            logger: 可选日志器
        """
        self.hv = hypervisor or {}
        self.logger = logger
        self._extra = self._parse_extra(self.hv.get("extra_config"))

    # ---------------- 基础 ----------------
    @staticmethod
    def _parse_extra(raw) -> dict:
        import json
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                v = json.loads(raw)
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}
        return {}

    @property
    def extra(self) -> dict:
        return self._extra

    def log(self, msg, *args):
        if self.logger:
            try:
                self.logger.info("[vm:%s] " + msg, self.provider_id, *args)
            except Exception:
                pass

    def capabilities(self) -> ProviderCaps:
        """返回该 Provider（针对当前配置）的真实能力。"""
        return self._CAPS

    def connect(self):
        """建立连接并做一次轻量探活。返回 (ok: bool, message: str)。"""
        raise NotImplementedError

    def version(self) -> str:
        return ""

    # ---------------- 资产发现 ----------------
    def list_vms(self) -> List[VMInfo]:
        raise NotImplementedError

    def get_vm(self, ref: str) -> Optional[VMInfo]:
        for vm in self.list_vms():
            if str(vm.ref) == str(ref):
                return vm
        return None

    # ---------------- 备份 ----------------
    def ensure_change_tracking(self, vm_ref: str):
        """开启变更跟踪（CBT / dirty bitmap / RCT）。返回 (ok, message)。"""
        return False, "该平台不支持自动开启变更跟踪"

    def backup_full(self, vm_ref: str, work_dir: str,
                    consistency: str = "crash") -> BackupArtifact:
        raise NotImplementedError

    def backup_incremental(self, vm_ref: str, work_dir: str, parent_token: str,
                           consistency: str = "crash") -> BackupArtifact:
        raise VMProviderError("该平台不支持块级增量备份")

    def download_artifact(self, artifact: BackupArtifact, local_path: str) -> dict:
        """把数据源侧产物拉回备份平台，返回 {"size": int, "sha256": str}。"""
        raise NotImplementedError

    def upload_artifact(self, local_path: str, remote_dir: str) -> str:
        """把平台侧产物推回数据源侧（恢复/克隆用），返回远端路径。"""
        raise NotImplementedError

    def cleanup_artifact(self, artifact: BackupArtifact) -> None:
        """清理数据源侧临时产物（备份拉回后调用）。"""
        return None

    def synthesize(self, artifacts: List[str], out_path: str) -> BackupArtifact:
        """把「全量 + 增量链」合成为一份可直接挂载的全量（增量永久 → 定期合成）。"""
        raise VMProviderError("该平台不支持服务端合成全量")

    # ---------------- 恢复 ----------------
    def restore_in_place(self, vm_ref: str, artifacts: List[str],
                         consistency: str = "crash"):
        """原位还原（覆盖原 VM，灾难恢复场景）。返回 (ok, message)。"""
        raise NotImplementedError

    def clone_to_new_vm(self, vm_ref: str, artifacts: List[str],
                        spec: CloneSpec) -> CloneResult:
        """从恢复点克隆出一台新 VM（演练/取证/开发测试/即时恢复）。"""
        raise NotImplementedError

    def delete_vm(self, ref: str, node: str = ""):
        """销毁一台 VM（含其磁盘），用于克隆到期回收。返回 (ok, message)。"""
        return False, "该平台不支持自动销毁虚拟机"

    # ---------------- 运行时 ----------------
    def power_state(self, vm_ref: str, node: str = "") -> str:
        vm = self.get_vm(vm_ref)
        return vm.power_state if vm else "unknown"

    def start_vm(self, vm_ref: str, node: str = ""):
        return False, "该平台不支持开机操作"

    def stop_vm(self, vm_ref: str, node: str = "", force: bool = False):
        return False, "该平台不支持关机操作"

    def guest_ip(self, vm_ref: str, node: str = "") -> str:
        """尽力获取 VM IP（QEMU GA / VMware Tools / Hyper-V KVP），拿不到返回空串。"""
        return ""

    def health_checks(self, vm_ref: str, node: str, checks: List[str],
                      timeout_sec: int = 300) -> List[HealthCheckResult]:
        """恢复验证的健康检查，默认在**平台侧**执行（无需 guest agent）。

        支持的检查项：
          power_on      —— VM 是否进入运行态
          ping:<ip>     —— 平台侧 ICMP/TCP 探测（优先 TCP 22/3389 回退）
          tcp:<ip>:<p>  —— 平台侧建连探测
          exec:<cmd>    —— 默认禁用（安全策略），开启后仅允许白名单命令的参数列表执行
        """
        from core.vm import verify as vmverify
        out: List[HealthCheckResult] = []
        for item in checks or ["power_on"]:
            if item == "power_on":
                r = HealthCheckResult(name=item)
                ok, msg = False, "power_state 未实现"
                try:
                    st = self.power_state(vm_ref, node)
                    ok, msg = (st == "running"), "电源状态=%s" % st
                except Exception as e:
                    msg = str(e)
                r.ok, r.message = ok, msg
                out.append(r)
            else:
                out.append(vmverify.run_platform_check(item))
        return out

    # ---------------- 快照（可选） ----------------
    def create_snapshot(self, vm_ref: str, name: str,
                        consistency: str = "crash") -> SnapshotRef:
        raise VMProviderError("该平台不支持快照接口")

    def delete_snapshot(self, vm_ref: str, snap: SnapshotRef) -> None:
        raise VMProviderError("该平台不支持快照接口")
