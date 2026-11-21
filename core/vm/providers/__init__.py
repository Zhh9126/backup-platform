# -*- coding: utf-8 -*-
"""VM Provider 注册表（惰性导入，缺依赖不会拖垮模块加载）。"""
from core.vm.base import VMProvider  # noqa: F401

PROVIDER_META = [
    {"id": "pve", "name": "Proxmox VE (KVM)", "module": "pve", "cls": "PVEProvider",
     "desc": "PVE REST API + PBS 块级去重增量；原生支持克隆为新 VM 与即时恢复。"},
    {"id": "libvirt_ssh", "name": "KVM / libvirt (SSH)", "module": "libvirt_ssh",
     "cls": "LibvirtSSHProvider",
     "desc": "virsh 外部快照全量 + checkpoint/dirty-bitmap 真块级增量，零 agent。"},
    {"id": "esxi_ssh", "name": "VMware ESXi (SSH)", "module": "esxi_ssh",
     "cls": "ESXiSSHProvider",
     "desc": "vim-cmd + tar 流式整机备份，无需 vCenter 授权与 VDDK。"},
    {"id": "hyperv_ssh", "name": "Microsoft Hyper-V (SSH)", "module": "hyperv_ssh",
     "cls": "HyperVSSHProvider",
     "desc": "PowerShell Export-VM 全量 + 生产检查点（VSS 应用一致）。"},
    {"id": "workstation_ssh", "name": "VMware Workstation (SSH)", "module": "workstation_ssh",
     "cls": "WorkstationSSHProvider",
     "desc": "桌面级 Workstation：vmrun 控制面 + 7za 目录级全量备份，崩溃一致，无块级增量。"},
]

PROVIDER_IDS = [p["id"] for p in PROVIDER_META]


def get_provider_class(provider_id: str):
    """按 id 惰性加载 Provider 类；未知返回 None。"""
    for p in PROVIDER_META:
        if p["id"] == provider_id:
            import importlib
            mod = importlib.import_module("core.vm.providers." + p["module"])
            return getattr(mod, p["cls"])
    return None


__all__ = ["PROVIDER_META", "PROVIDER_IDS", "get_provider_class", "VMProvider"]
