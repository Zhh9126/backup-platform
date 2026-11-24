# -*- coding: utf-8 -*-
"""OpenStack 系 Provider（华为 FusionSphere / EasyStack / 通用 OpenStack）。

Nova API：GET /v2.1/{project}/servers（Bearer token 或 username/password）。
控制面接入真实可用；数据面（Cinder 卷快照 / glance 镜像导出）无真机环境，
明确不实现，不伪造备份成功。
"""
from core.vm.providers.rest_api import RestAPIVMProvider
from core.vm.types import VMInfo


class OpenStackProvider(RestAPIVMProvider):
    provider_id = "openstack"
    display_name = "OpenStack 系 (FusionSphere/EasyStack)"
    list_path = "/v2.1/servers/detail"

    def _extract_items(self, data):
        if isinstance(data, dict) and isinstance(data.get("servers"), list):
            return data["servers"]
        return super()._extract_items(data)

    def _map_vm(self, row):
        flavor = row.get("flavor") or {}
        try:
            mem = int(flavor.get("ram") or 0)
        except Exception:
            mem = 0
        try:
            cpu = int(flavor.get("vcpus") or 0)
        except Exception:
            cpu = 0
        return VMInfo(
            ref=str(row.get("id") or ""), name=str(row.get("name") or ""),
            power_state=str(row.get("status") or "unknown"),
            cpu=cpu, memory_mb=mem,
            guest_os=str((row.get("metadata") or {}).get("os_type") or ""),
            raw=row)
