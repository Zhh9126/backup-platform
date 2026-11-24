# -*- coding: utf-8 -*-
"""oVirt / Red Hat Virtualization (RHEV) Provider —— REST 控制面接入。

oVirt 4.x REST API：GET /ovirt-engine/api/vms（Bearer token）。
数据面（快照/磁盘导出）需 ovirt-imageio 或 qcow2 导出，当前无真机环境，
明确不实现，详见 rest_api.RestAPIVMProvider 的诚实边界。
"""
from core.vm.providers.rest_api import RestAPIVMProvider
from core.vm.types import VMInfo


class OVirtProvider(RestAPIVMProvider):
    provider_id = "ovirt"
    display_name = "oVirt / RHEV"
    api_base = "/ovirt-engine/api"
    list_path = "/ovirt-engine/api/vms"

    def _extract_items(self, data):
        if isinstance(data, dict) and isinstance(data.get("vm"), list):
            return data["vm"]
        return super()._extract_items(data)

    def _map_vm(self, row):
        status = (row.get("status") or "unknown")
        if isinstance(status, dict):
            status = status.get("state") or "unknown"
        cpu = 0
        try:
            cpu = int(((row.get("cpu") or {}).get("topology") or {}).get("cores") or 0)
        except Exception:
            pass
        mem = 0
        try:
            mem = int(row.get("memory") or 0) // (1024 * 1024)
        except Exception:
            pass
        return VMInfo(
            ref=str(row.get("id") or ""), name=str(row.get("name") or ""),
            power_state=str(status), cpu=cpu, memory_mb=mem,
            guest_os=str(((row.get("guest_info") or {}).get("os") or {}).get("type") or ""),
            raw=row)
